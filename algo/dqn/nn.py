import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.mixed_precision.experimental import global_policy
from tensorflow_probability import distributions as tfd

from utility.display import pwc
from core.module import Module
from core.decorator import config
from nn.func import mlp, cnn
from nn.layers import Noisy
        

class Q(Module):
    @config
    def __init__(self, action_dim, name='q'):
        super().__init__(name=name)
        self._dtype = global_policy().compute_dtype

        self._action_dim = action_dim

        """ Network definition """
        self._cnn = cnn(self._cnn)

        layer_type = dict(noisy=Noisy, dense=layers.Dense)[self._layer_type]
        if self._duel:
            self._v_head = mlp(
                self._head_units, 
                out_size=1, 
                layer_type=layer_type, 
                activation=self._activation, 
                out_dtype='float32',
                name='v')
        self._a_head = mlp(
            self._head_units, 
            out_size=action_dim, 
            layer_type=layer_type, 
            activation=self._activation, 
            out_dtype='float32',
            name='a' if self._duel else 'q')

    def __call__(self, x, deterministic=False, epsilon=0):
        x = np.array(x)
        if not deterministic and np.random.uniform() < epsilon:
            size = x.shape[0] if len(x.shape) % 2 == 0 else None
            return np.random.randint(self._action_dim, size=size)
        if len(x.shape) % 2 != 0:
            x = tf.expand_dims(x, 0)
        
        noisy = not deterministic
        action = self.action(x, noisy=noisy, reset=False)
        action = np.squeeze(action.numpy())

        return action

    @tf.function
    def action(self, x, noisy=True, reset=True):
        q = self.value(x, noisy=noisy, reset=reset)
        return tf.argmax(q, axis=-1)
    
    @tf.function
    def value(self, x, action=None, noisy=True, reset=True):
        x = self.cnn(x)
        q = self.mlp(x, action=action, noisy=noisy, reset=reset)
        return q

    def cnn(self, x):
        if self._cnn:
            x = self._cnn(x)
        return x

    def mlp(self, x, action=None, noisy=True, reset=True):
        if self._duel:
            v = self._v_head(x, noisy=noisy, reset=reset)
            a = self._a_head(x, noisy=noisy, reset=reset)
            q = v + a - tf.reduce_mean(a, axis=1, keepdims=True)
        else:
            q = self._a_head(x, noisy=noisy, reset=reset)

        if action is not None:
            if len(action.shape) < len(q.shape):
                action = tf.one_hot(action, self._action_dim, dtype=q.dtype)
            assert q.shape[-1] == action.shape[-1], f'{q.shape} vs {action.shape}'
            q = tf.reduce_sum(q * action, -1)
        return q

    def reset_noisy(self):
        if self._layer_type == 'noisy':
            if self._duel:
                self._v_head.reset()
            self._a_head.reset()


def create_model(config, env, **kwargs):
    action_dim = env.action_dim
    q = Q(config, action_dim, 'q')
    target_q = Q(config, action_dim, 'target_q')
    return dict(
        q=q,
        target_q=target_q,
    )
