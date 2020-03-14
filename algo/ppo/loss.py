import tensorflow as tf


def _reduce_mean(x, name, n):
    with tf.name_scope(name):        
        return tf.reduce_mean(x) if n is None else tf.reduce_sum(x) / n

def compute_ppo_loss(logpi, old_logpi, advantages, clip_range, entropy, mask=None, n=None):
    assert (mask is None) == (n is None), f'Both/Neither mask and/nor n should be None, but get \nmask:{mask}\nn:{n}'

    m = 1. if mask is None else mask
    with tf.name_scope('ppo_loss'):
        ratio = tf.exp(logpi - old_logpi, name='ratio')
        loss1 = -advantages * ratio
        loss2 = -advantages * tf.clip_by_value(ratio, 1. - clip_range, 1. + clip_range)
        
        ppo_loss = _reduce_mean(tf.maximum(loss1, loss2) * m, 'ppo_loss', n)
        entropy = tf.reduce_mean(entropy, name='entropy_loss')
        # debug stats: KL between old and current policy and fraction of data being clipped
        approx_kl = .5 * _reduce_mean((old_logpi - logpi)**2 * m, 'approx_kl', n)
        p_clip_frac = _reduce_mean(tf.cast(tf.greater(tf.abs(ratio - 1.), clip_range), tf.float32) * m, 
                                'clip_frac', n)

    return ppo_loss, entropy, approx_kl, p_clip_frac

def compute_value_loss(value, traj_ret, old_value, clip_range, mask=None, n=None):
    assert (mask is None) == (n is None), f'Both/Neither mask and/nor n should be None, but get \nmask:{mask}\nn:{n}'
    
    m = 1. if mask is None else mask
    with tf.name_scope('value_loss'):
        value_clipped = old_value + tf.clip_by_value(value - old_value, -clip_range, clip_range)
        loss1 = (value - traj_ret)**2
        loss2 = (value_clipped - traj_ret)**2
        
        value_loss = _reduce_mean(tf.maximum(loss1, loss2) * m, 'value_loss', n)
        v_clip_frac = _reduce_mean(
            tf.cast(tf.greater(tf.abs(value-old_value), clip_range), tf.float32) * m,
            'clip_frac', n)

    return value_loss, v_clip_frac
