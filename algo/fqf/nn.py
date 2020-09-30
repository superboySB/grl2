import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow_probability import distributions as tfd

from core.module import Module, Ensemble
from core.decorator import config
from utility.rl_utils import epsilon_greedy
from nn.func import mlp, cnn
        

class Encoder(Module):
    def __init__(self, config, name='encoder'):
        super().__init__(name=name)
        self._layers = cnn(**config)

    def call(self, x):
        x = self._layers(x)
        return x


class FractionProposalNetwork(Module):
    @config
    def __init__(self, name='fqn'):
        super().__init__(name=name)
        kernel_initializer = tf.keras.initializers.VarianceScaling(
            1./np.sqrt(3.), distribution='uniform')
        self._layers = mlp(
            out_size=self.N,
            name='fpn',
            kernel_initializer=kernel_initializer)
    
    def call(self, x):
        x = self._layers(x)

        probs = tf.nn.softmax(x, axis=-1)

        tau_0 = tf.zeros([*probs.shape[:-1], 1], dtype=probs.dtype)
        tau_rest = tf.math.cumsum(probs, axis=-1)

        tau = tf.concat([tau_0, tau_rest], axis=-1)          # [B, N+1]
        tau_hat = (tau[..., :-1] + tau[..., 1:]) / 2.   # [B, N]

        tf.debugging.assert_shapes([
            [tau_0, (None, 1)],
            [probs, (None, self.N)],
            [tau, (None, self.N+1)],
            [tau_hat, (None, self.N)]
        ])

        return tau, tau_hat


class Q(Module):
    @config
    def __init__(self, action_dim, name='q'):
        super().__init__(name=name)

        self._action_dim = action_dim

        """ Network definition """
        kwargs = {}
        if hasattr(self, '_kernel_initializer'):
            kwargs['kernel_initializer'] = self._kernel_initializer
        self._kwargs = kwargs

        if self._duel:
            self._v_head = mlp(
                self._units_list, 
                out_size=1, 
                activation=self._activation, 
                out_dtype='float32',
                name='v',
                **kwargs)
        self._a_head = mlp(
            self._units_list, 
            out_size=action_dim, 
            activation=self._activation, 
            out_dtype='float32',
            name='a' if self._duel else 'q',
            **kwargs)

    @property
    def action_dim(self):
        return self._action_dim

    def action(self, x, tau_hat, tau_range=None):
        _, q = self(x, tau_hat, tau_range)
        return tf.argmax(q, axis=-1, output_type=tf.int32)

    def call(self, x, tau_hat, tau_range=None, action=None):
        assert tau_range is None or tau_hat.shape[-1] + 1 == tau_range.shape[-1], \
            (tau_hat.shape, tau_range.shape)
        x = tf.expand_dims(x, 1)    # [B, 1, cnn.out_size]
        cnn_out_size = x.shape[-1]
        qt_embed = self.qt_embed(tau_hat, cnn_out_size)   # [B, N, cnn.out_size]
        x = x * qt_embed            # [B, N, cnn.out_size]
        qtv = self.qtv(x, action=action)
        if tau_range is None:
            return qtv
        else:
            q = self.q(qtv, tau_range)
            return qtv, q
    
    def qt_embed(self, tau_hat, cnn_out_size):
        # phi network
        tau_hat = tf.expand_dims(tau_hat, axis=-1)      # [B, N, 1]
        assert tau_hat.shape.ndims == 3, tau_hat.shape
        pi = tf.convert_to_tensor(np.pi, dtype=tau_hat.dtype)
        # start from 1 since degree of 0 is meaningless
        degree = tf.cast(tf.range(1, self._tau_embed_size+1), tau_hat.dtype) * pi * tau_hat
        qt_embed = tf.math.cos(degree)                  # [B, N, E]
        
        qt_embed = self.mlp(qt_embed, [cnn_out_size], 
                activation=self._phi_activation,
                name='phi',
                **self._kwargs)                  # [B, N, cnn.out_size]
        
        return qt_embed

    def qtv(self, x, action=None):
        if self._duel:
            v_qtv = self._v_head(x) # [B, N, 1]
            a_qtv = self._a_head(x) # [B, N, A]
            qtv = v_qtv + a_qtv - tf.reduce_mean(a_qtv, axis=-1, keepdims=True)
        else:
            qtv = self._a_head(x)   # [B, N, A]
        
        if action is not None:
            action = tf.expand_dims(action, axis=1)
            if len(action.shape) < len(qtv.shape):
                action = tf.one_hot(action, self._action_dim, dtype=qtv.dtype)
            qtv = tf.reduce_sum(qtv * action, axis=-1)        # [B, N]
            
        return qtv

    def q(self, qtv, tau_range):
        diff = tau_range[..., 1:] - tau_range[..., :-1]
        if len(qtv.shape) > len(diff.shape):
            diff = tf.expand_dims(diff, axis=-1)        # expand diff if qtv includes the action dimension
        q = tf.reduce_sum(diff * qtv, axis=1)           # [B, A] / [B]
        
        return q


class FQF(Ensemble):
    def __init__(self, config, env, **kwargs):
        super().__init__(
            model_fn=create_components, 
            config=config,
            env=env,
            **kwargs)

    @tf.function
    def action(self, x, deterministic=False, epsilon=0):
        if x.shape.ndims % 2 != 0:
            x = tf.expand_dims(x, axis=0)
        assert x.shape.ndims == 4, x.shape

        x = self.encoder(x)
        tau, tau_hat = self.fpn(x)
        qtv, q = self.q(x, tau_hat, tau_range=tau)
        action = tf.argmax(q, axis=-1, output_type=tf.int32)
        qtv = tf.math.reduce_max(qtv, -1)
        action = epsilon_greedy(action, epsilon, 
            is_action_discrete=True, action_dim=self.q.action_dim)
        action = tf.squeeze(action)
        qtv = tf.squeeze(qtv)

        return action, {'qtv': qtv}

    @tf.function
    def value(self, x):
        if x.shape.ndims % 2 != 0:
            x = tf.expand_dims(x, axis=0)
        assert x.shape.ndims == 4, x.shape

        x = self.encoder(x)
        tau, tau_hat, _ = self.fpn(x)
        qtv, q = self.q(x, tau_hat, tau_range=tau)
        qtv = tf.squeeze(qtv)
        q = tf.squeeze(q)

        return qtv, q


def create_components(config, env, **kwargs):
    action_dim = env.action_dim
    return dict(
        encoder=Encoder(config['encoder'], name='cnn'),
        target_encoder=Encoder(config['encoder'], name='target_cnn'),
        fpn=FractionProposalNetwork(config['fpn'], name='fpn'),
        target_fpn=FractionProposalNetwork(config['fpn'], name='target_fpn'),
        q=Q(config['iqn'], action_dim, name='iqn'),
        target_q=Q(config['iqn'], action_dim, name='target_iqn'),
    )

def create_model(config, env, **kwargs):
    return FQF(config, env, **kwargs)
