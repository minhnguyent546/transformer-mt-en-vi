from pathlib import Path
from tqdm.autonotebook import tqdm
from typing import Any

import torch
from torch import optim
import torch.nn.functional as Fun
from torch import Tensor
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from tokenizers import Tokenizer

from transformer import Transformer, make_transformer
import transformer.utils.functional as fun
from nmt.constants import SpecialToken
from nmt.utils import stats

def make_model(
    src_tokenizer: Tokenizer,
    target_tokenizer: Tokenizer,
    config: dict
) -> Transformer:
    src_vocab_size = src_tokenizer.get_vocab_size()
    target_vocab_size = target_tokenizer.get_vocab_size()
    src_pad_token_id = src_tokenizer.token_to_id(SpecialToken.PAD)
    target_pad_token_id = target_tokenizer.token_to_id(SpecialToken.PAD)
    model = make_transformer(
        src_vocab_size,
        target_vocab_size,
        config['seq_length'],
        config['seq_length'],
        src_pad_token_id,
        target_pad_token_id,
        device='auto',
        d_model=config['d_model'],
        num_heads=config['num_heads'],
        num_layers=config['num_layers'],
        d_ffn=config['d_ffn'],
        dropout_rate=config['dropout_rate'],
        attention_dropout_rate=config['attention_dropout_rate'],
    )
    return model

def make_optimizer(model: Transformer, config: dict) -> optim.Optimizer:
    optim_type = config['optim']
    if optim_type == 'adam':
        optimizer = optim.Adam(
            model.parameters(),
            lr=config['learning_rate'],
            betas=config['betas'],
            eps=config['eps'],
            weight_decay=config['weight_decay']
        )
    elif optim_type == 'adamw':
        optimizer = optim.AdamW(
            model.parameters(),
            lr=config['learning_rate'],
            betas=config['betas'],
            eps=config['eps'],
            weight_decay=config['weight_decay']
        )
    else:
        raise ValueError('Invalid optimizer option: ' + optim_type)

    return optimizer

def get_weights_file_path(epoch: str, config: dict) -> str:
    model_dir = Path(config['checkpoints_dir']) / config['model_dir']
    model_basename = config['model_basename']
    model_file = f'{model_basename}_{epoch}.pt'
    return str(model_dir / model_file)

def get_latest_weights_file_path(config: dict) -> str | None:
    model_dir = Path(config['checkpoints_dir']) / config['model_dir']
    model_basename = config['model_basename']
    saved_files = list(model_dir.glob(f'{model_basename}_*.pt'))
    if len(saved_files) > 0:
        latest_file = sorted(saved_files)[-1]
        return str(latest_file)

    return None

def noam_decay(step_num: int, d_model: int = 512, warmup_steps: int = 4000):
    """
    As described in https://arxiv.org/pdf/1706.03762.pdf
    """
    step_num = max(step_num, 1)
    return d_model ** (-0.5) * min(step_num ** (-0.5), step_num * warmup_steps ** (-1.5))

def compute_accuracy(pred: Tensor, labels: Tensor, pad_token_id: int | None = None) -> float:
    num_el = labels.numel()
    matches = (pred == labels)
    if pad_token_id is not None:
        num_el = (labels != pad_token_id).sum().item()
        matches = matches & (labels != pad_token_id)

    acc = matches.sum().item() / num_el
    return acc

def decode_with_teacher_forcing(
    model: Transformer,
    device: torch.device,
    encoder_input: Tensor,
    decoder_input: Tensor,
    has_batch_dim: bool = False,
) -> Tensor:
    """
    Args:
        model (Transformer): model to be used for decoding
        device (torch.device): device
        encoder_input (Tensor): encoder input
        decoder_input (Tensor): decoder input
        has_batch_dim (bool): whether input tensors have batch dimension (default: False)

    Returns:
        Tensor: tensor of predicted token ids
    """

    encoder_input = encoder_input.to(device)
    decoder_input = decoder_input.to(device)

    if not has_batch_dim:
        encoder_input.unsqueeze_(0)
        decoder_input.unsqueeze_(0)

    decoder_output = model(encoder_input, decoder_input)  # (batch_size, seq_length, d_model)
    logits = model.linear(decoder_output)  # (batch_size, seq_length, target_vocab_size)

    pred_token_ids = logits.argmax(dim=-1)  # (batch_size, seq_length)

    return pred_token_ids

def greedy_search_decode(
    model: Transformer,
    device: torch.device,
    encoder_input: Tensor,
    target_tokenizer: Tokenizer,
    seq_length: int,
) -> Tensor:
    """
    Args:
        model (Transformer): model to be used for decoding
        device (torch.device): device
        encoder_input (Tensor): encoder input
        target_tokenizer (Tokenizer): target tokenizer
        seq_length (int): maximum sequence length

    Returns:
        Tensor: tensor of predicted token ids
    """

    sos_token_id = target_tokenizer.token_to_id(SpecialToken.SOS)
    eos_token_id = target_tokenizer.token_to_id(SpecialToken.EOS)

    encoder_input = encoder_input.unsqueeze(0).to(device)
    encoder_mask = fun.create_encoder_mask(encoder_input, model.src_pad_token_id, has_batch_dim=True)
    encoder_output = model.encode(encoder_input, src_mask=encoder_mask)

    # initialize decoder input which contains only <SOS> token
    decoder_input = torch.empty((1, 1)).fill_(sos_token_id).type_as(encoder_input).to(device)
    for _ in range(seq_length):
        # create mask for decoder input
        decoder_mask = fun.create_causal_mask(decoder_input.size(1)).to(device)

        # decode
        decoder_output = model.decode(encoder_output, decoder_input,
                                      src_mask=encoder_mask, target_mask=decoder_mask)

        # get token with highest probability
        logits = model.linear(decoder_output[:, -1, :])  # (1, target_vocab_size)
        next_token = logits.argmax(dim=-1)

        # concatenate the next token to the decoder input for the next prediction
        decoder_input = torch.cat([
            decoder_input,
            torch.empty((1, 1)).fill_(next_token.item()).type_as(encoder_input).to(device)
        ], dim=1)

        # if we reach the <EOS> token, then stop
        if next_token == eos_token_id:
            break

    return decoder_input.squeeze(0)

def length_penalty(length: int, alpha: float = 0.6) -> float:
    """
    As formula described in We at al. (2016)
    """
    return (5 + length) ** alpha / (5 + 1) ** alpha

def beam_search_decode(
    model: Transformer,
    device: torch.device,
    beam_size: int,
    encoder_input: Tensor,
    target_tokenizer: Tokenizer,
    seq_length: int,
    return_topk: int = 1,
) -> list[Tensor]:
    """
    Args:
        model (Transformer): model to be used for decoding
        device (torch.device): device
        beam_size (int): beam size
        encoder_input (Tensor): encoder input
        target_tokenizer (Tokenizer): target tokenizer
        seq_length (int): maximum sequence length
        return_topk (int): return top k best candidates (default: 1)

    Returns:
        list[Tensor]: list of candidate tensors of predicted token ids
    """

    sos_token_id = target_tokenizer.token_to_id(SpecialToken.SOS)
    eos_token_id = target_tokenizer.token_to_id(SpecialToken.EOS)

    encoder_input = encoder_input.unsqueeze(0).to(device)
    encoder_mask = fun.create_encoder_mask(encoder_input, model.src_pad_token_id, has_batch_dim=True)
    encoder_output = model.encode(encoder_input, src_mask=encoder_mask)

    # initialize decoder input which contains only <SOS> token
    decoder_input = torch.empty((1, 1)).fill_(sos_token_id).type_as(encoder_input).to(device)

    # candidate list of tuples (decoder_input, log_score)
    cands = [(decoder_input, 0.0)]
    for _ in range(seq_length):
        new_cands = []

        for cand, log_score in cands:
            # do not expand the candidate that have reached <EOS> token
            if cand[0, -1].item() == eos_token_id:
                new_cands.append((cand, log_score))
                continue

            # create mask for decoder input
            cand_mask = fun.create_causal_mask(cand.size(1)).to(device)

            # decode
            decoder_output = model.decode(encoder_output, cand,
                                          src_mask=encoder_mask, target_mask=cand_mask)

            # get next token probabilities
            # logits: shape ``(1, target_vocab_size)``
            # topk_prob       : shape ``(1, beam_size)``
            # topk_token      : shape ``(1, beam_size)``
            logits = model.linear(decoder_output[:, -1, :])

            output = Fun.log_softmax(logits, dim=-1) / length_penalty(cand.size(1) + 1)
            # get the top k largest tokens
            topk_token_prob, topk_token = torch.topk(output, beam_size, dim=1)
            for j in range(beam_size):
                # token: shape ``(1, 1)``
                # token_prob: scalar
                token = topk_token[0][j].unsqueeze(0).unsqueeze(0)
                token_prob = topk_token_prob[0][j].item()

                new_cand = torch.cat([
                    cand,
                    token
                ], dim=1)

                new_cands.append((new_cand, log_score + token_prob))

        cands = sorted(new_cands, key=lambda x: x[1], reverse=True)
        cands = cands[:beam_size]

        if all([cand[0][-1].item() == eos_token_id for cand, _ in cands]):
            break

    assert len(cands) == beam_size
    cands = cands[:return_topk]
    result_cands = [cand[0].squeeze(0) for cand in cands]
    return result_cands

def evaluate(
    model: Transformer,
    criterion,
    eval_data_loader: DataLoader,
) -> stats.Stats:
    """
    Args:
        model (Transformer): model to be evaluated
        criterion: loss function
        eval_data_loader (DataLoader): data loader for evaluation

    Returns:
        Stats: evaluation stats
    """

    device = model.device
    batch_iterator = tqdm(eval_data_loader, desc='Evaluating')

    eval_stats = stats.Stats()

    # set model in validation mode
    model.eval()

    with torch.no_grad():
        for batch in batch_iterator:
            encoder_input = batch['encoder_input'].to(device)  # (batch_size, seq_length)
            decoder_input = batch['decoder_input'].to(device)  # (batch_size, seq_length)
            labels = batch['labels'].to(device)  # (batch_size, seq_length)

            decoder_output = model(encoder_input, decoder_input)  # (batch_size, seq_length, d_model)
            logits = model.linear(decoder_output)  # (batch_size, seq_length, target_vocab_size)
            pred = logits.argmax(dim=-1)  # (batch_size, seq_length)

            # calculating the loss
            # logits: (batch_size * seq_length, target_vocab_size)
            # label: (batch_size * seq_length)
            target_vocab_size = logits.size(-1)
            loss = criterion(logits.view(-1, target_vocab_size), labels.view(-1))
            eval_stats.update_step(loss.item(), pred.view(-1), labels.view(-1))

            batch_iterator.set_postfix({'loss': f'{loss.item():0.3f}'})

    # set model back to training mode
    model.train()

    return eval_stats
