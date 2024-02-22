def get_config():
    config = {
        # general options
        'checkpoints_dir': 'checkpoints',
        'tokenizer_basename': 'tokenizer_{}.json',
        'data_loaders_basename': 'data_loaders.pt',
        'model_dir': 'weights',
        'model_basename': 'transformer',
        'experiment_name': 'runs/model',
        'src_lang': 'en',
        'target_lang': 'vi',

        # model
        'd_model': 512,
        'num_layers': 6,
        'num_heads': 8,
        'd_ffn': 2048,
        'dropout_rate': 0.1,

        # optimization
        'learning_rate': 1e-4,
        'label_smoothing': 0.1,
        'max_grad_norm': 1.0,
        'beam_size': 5,

        # dataset (hugging face)
        'dataset_path': 'mt_eng_vietnamese',
        'dataset_name': 'iwslt2015-en-vi',

        # training
        'preload': None,
        'batch_size': 32,
        'num_epochs': 10,
        'num_validation_samples': 5,
        'num_test_samples': 5,
        'seq_length': 120,
    }
    return config

