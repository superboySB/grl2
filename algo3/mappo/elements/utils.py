import tensorflow as tf


def get_data_format(config, env_stats, model, expand_state=True):
    basic_shape = (None, config['sample_size'])
    shapes = env_stats['obs_shape']
    dtypes = env_stats['obs_dtype']
    data_format = {k: ((*basic_shape, *v), dtypes[k], k) 
        for k, v in shapes.items()}

    data_format.update(dict(
        action=((*basic_shape, *env_stats.action_shape), env_stats.action_dtype, 'action'),
        value=(basic_shape, tf.float32, 'value'),
        traj_ret=(basic_shape, tf.float32, 'traj_ret'),
        advantage=(basic_shape, tf.float32, 'advantage'),
        logpi=(basic_shape, tf.float32, 'logpi'),
        mask=(basic_shape, tf.float32, 'mask'),
    ))
    if config['store_state']:
        dtype = tf.keras.mixed_precision.experimental.global_policy().compute_dtype
        if expand_state:
            data_format.update({
                name: ((None, sz), dtype)
                    for name, sz in model.state_size._asdict().items()
            })
        else:
            state_type = type(model.state_size)
            data_format['state'] = state_type(*[((None, sz), dtype, name) 
                for name, sz in model.state_size._asdict().items()])
    
    return data_format