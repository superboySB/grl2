import collections
import numpy as np
import tensorflow as tf
from tensorflow_probability import distributions as tfd
from tensorflow.keras import layers
from tensorflow.keras.mixed_precision.experimental import global_policy

from core.tf_config import build
from core.module import Module
from core.decorator import config
from utility.tf_utils import static_scan
from utility.tf_distributions import Categorical, OneHotDist, TanhBijector, SampleDist
from nn.func import mlp


RSSMState = collections.namedtuple('RSSMState', ('mean', 'std', 'stoch', 'deter'))

# Ignore seed to allow parallel execution
def _mnd_sample(self, sample_shape=(), seed=None, name='sample'):
    return tf.random.normal(
        tuple(sample_shape) + tuple(self.event_shape),
        self.mean(), self.stddev(), self.dtype, seed, name)

tfd.MultivariateNormalDiag.sample = _mnd_sample

def _cat_sample(self, sample_shape=(), seed=None, name='sample'):
    assert len(sample_shape) in (0, 1), sample_shape
    indices = tf.random.categorical(
        self.logits_parameter(), sample_shape[0] if sample_shape else 1,
        self.dtype, seed, name)
    if not sample_shape:
        indices = indices[..., 0]
    return indices

tfd.Categorical.sample = _cat_sample


class RSSM(Module):
    @config
    def __init__(self, name='rssm'):
        super().__init__(name)

        self.embed_layer = layers.Dense(
            self._hidden_size, 
            activation=self._activation,
            name='embed')
        self.cell = layers.GRUCell(self._deter_size)
        self.img_layers = mlp(
            [self._hidden_size], 
            out_dim=2*self._stoch_size, 
            activation=self._activation,
            name='img')
        self.obs_layers = mlp(
            [self._hidden_size], 
            out_dim=2*self._stoch_size,
            activation=self._activation,
            name='obs')

    @tf.function
    def observe(self, embed, action, state=None):
        if state is None:
            state = self.get_initial_state(batch_size=tf.shape(action)[0])
        # make tensors time-major
        embed = tf.transpose(embed, [1, 0, 2])
        action = tf.transpose(action, [1, 0, 2])
        post, prior = static_scan(
            lambda prev, inputs: self.obs_step(prev[0], *inputs),
            (state, state), (action, embed))
        post = RSSMState(*[tf.transpose(v, [1, 0, 2]) for v in post])
        prior = RSSMState(*[tf.transpose(v, [1, 0, 2]) for v in prior])
        return post, prior

    @tf.function
    def imagine(self, action, state=None):
        if state is None:
            state = self.get_initial_state(batch_size=tf.shape(action)[0])
        action = tf.transpose(action, [1, 0, 2])
        prior = static_scan(self.img_step, state, action)
        prior = RSSMState(*[tf.transpose(v, [1, 0, 2]) for v in prior])
        return prior

    @tf.function
    def obs_step(self, prev_state, prev_action, embed):
        prior = self.img_step(prev_state, prev_action)
        x = tf.concat([prior.deter, embed], -1)
        x = self.obs_layers(x)
        post = self._compute_rssm_state(x, prior.deter)
        return post, prior

    @tf.function
    def img_step(self, prev_state, prev_action):
        x, deter = self._compute_deter_state(prev_state, prev_action)
        x = self.img_layers(x)
        prior = self._compute_rssm_state(x, deter)
        return prior

    def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
        if inputs is not None:
            assert batch_size is None or batch_size == tf.shape(inputs)[0]
            batch_size = tf.shape(inputs)[0]
        assert batch_size is not None
        if dtype is None:
            dtype = global_policy().compute_dtype
        return RSSMState(
            mean=tf.zeros([batch_size, self._stoch_size], dtype=dtype),
            std=tf.zeros([batch_size, self._stoch_size], dtype=dtype),
            stoch=tf.zeros([batch_size, self._stoch_size], dtype=dtype),
            deter=self.cell.get_initial_state(inputs, batch_size, dtype))
        
    def get_feat(self, state):
        return tf.concat([state.stoch, state.deter], -1)

    def get_dist(self, mean, std):
        return tfd.MultivariateNormalDiag(mean, std)

    def _compute_deter_state(self, prev_state, prev_action):
        x = tf.concat([prev_state.stoch, prev_action], -1)
        x = self.embed_layer(x)
        x, deter = self.cell(x, tf.nest.flatten(prev_state.deter))
        deter = deter[-1]
        return x, deter

    def _compute_rssm_state(self, x, deter):
        mean, std = tf.split(x, 2, -1)
        std = tf.nn.softplus(std) + .1
        stoch = self.get_dist(mean, std).sample()
        state = RSSMState(mean=mean, std=std, stoch=stoch, deter=deter)
        return state


class Actor(Module):
    @config
    def __init__(self, obs_shape, action_dim, is_action_discrete, name='actor'):
        super().__init__(name=name)

        """ Network definition """
        out_dim = action_dim if is_action_discrete else 2*action_dim
        self._layers = mlp(self._units_list, 
                            out_dim=out_dim,
                            activation=self._activation)

        self._is_action_discrete = is_action_discrete

    @tf.Module.with_name_scope
    def __call__(self, x):
        x = self._layers(x)

        if self._is_action_discrete:
            dist = tfd.RelaxedOneHotCategorical(1, logits=x)
        else:
            raw_init_std = np.log(np.exp(self._init_std) - 1)
            mean, std = tf.split(x, 2, -1)
            # https://www.desmos.com/calculator/rcmcf5jwe7
            # we bound the mean to [-5, +5] to avoid numerical instabilities 
            # as atanh becomes difficult in highly saturated regions
            mean = self._mean_scale * tf.tanh(mean / self._mean_scale)
            std = tf.nn.softplus(std + raw_init_std) + self._min_std
            dist = tfd.Normal(mean, std)
            dist = tfd.TransformedDistribution(dist, TanhBijector())
            dist = tfd.Independent(dist, 1)
            dist = SampleDist(dist)

        return dist


class Encoder(Module):
    @config
    def __init__(self, name='encoder'):
        super().__init__(name=name)

        if getattr(self, '_has_cnn', None):
            self._layers = ConvEncoder(time_distributed=True)
        else:
            self._layers = mlp(self._units_list, activation=self._activation)
    
    @tf.Module.with_name_scope
    def __call__(self, x):
        x = self._layers(x)
        
        return x

        
class Decoder(Module):
    @config
    def __init__(self, out_dim=1, dist='normal', name='decoder'):
        super().__init__(name=name)

        self._dist = dist
        if getattr(self, '_has_cnn', None):
            self._layers = ConvDecoder(time_distributed=True)
        else:
            self._layers = mlp(self._units_list,
                            out_dim=out_dim,
                            activation=self._activation)
    
    @tf.Module.with_name_scope
    def __call__(self, x):
        x = self._layers(x)
        if not getattr(self, '_has_cnn', None):
            rbd = 0 if x.shape[-1] == 1 else 1  # #reinterpreted batch dimensions
            x = tf.squeeze(x)
            if self._dist == 'normal':
                return tfd.Independent(tfd.Normal(x, 1), rbd)
            if self._dist == 'binary':
                return tfd.Independent(tfd.Bernoulli(x), rbd)
            raise NotImplementedError(self._dist)

        return x


class ConvEncoder(Module):
    def __init__(self, *, time_distributed=False, name='dreamer_cnn', **kwargs):
        """ Hardcode CNN: Assume image of shape (64 ⨉ 64 ⨉ 3) by default """
        super().__init__(name=name)
        conv2d = lambda *args, **kwargs: (
            layers.TimeDistributed(layers.Conv2D(*args, **kwargs))
            if time_distributed else
            layers.Conv2D(*args, **kwargs)
        )
        depth = 32
        kwargs = dict(kernel_size=4, strides=2, activation='relu')
        self.conv1 = conv2d(1 * depth, **kwargs)
        self.conv2 = conv2d(2 * depth, **kwargs)
        self.conv3 = conv2d(4 * depth, **kwargs)
        self.conv4 = conv2d(8 * depth, **kwargs)

    def __call__(self, x):
        assert x.shape[-3:] == (64, 64, 3), x.shape
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.conv4(x)
        shape = tf.concat([tf.shape(x)[:-3], [tf.reduce_prod(tf.shape(x)[-3:])]], 0)
        x = tf.reshape(x, shape)

        return x


class ConvDecoder(Module):
    def __init__(self, *, time_distributed=False, name='dreamer_cnntrans', **kwargs):
        """ Hardcode CNN: Assume images of shape (64 ⨉ 64 ⨉ 3) by default """
        super().__init__(name=name)

        deconv2d = lambda *args, **kwargs: (
            layers.TimeDistributed(layers.Conv2DTranspose(*args, **kwargs))
            if time_distributed else
            layers.Conv2DTranspose(*args, **kwargs)
        )
        depth = 32
        kwargs = dict(strides=2, activation='relu')
        self._dense = layers.Dense(32 * depth)
        self.deconv1 = deconv2d(4 * depth, 5, **kwargs)
        self.deconv2 = deconv2d(2 * depth, 5, **kwargs)
        self.deconv3 = deconv2d(1 * depth, 6, **kwargs)
        self.deconv4 = deconv2d(3, 6, strides=2)

    def __call__(self, x):
        x = self._dense(x)
        shape = tf.concat([tf.shape(x)[:-1], [1, 1, x.shape[-1]]], 0)
        x = tf.reshape(x, shape)
        x = self.deconv1(x)
        x = self.deconv2(x)
        x = self.deconv3(x)
        x = self.deconv4(x)

        return tfd.Independent(tfd.Normal(x, 1), 3)


def create_model(config, obs_shape, action_dim, is_action_discrete):
    encoder_config = config['encoder']
    rssm_config = config['rssm']
    decoder_config = config['decoder']
    reward_config = config['reward']
    value_config = config['value']
    actor_config = config['actor']
    disc_config = config.get('discount')  # pcont in the original implementation
    models = dict(
        encoder=Encoder(encoder_config),
        rssm=RSSM(rssm_config),
        decoder=Decoder(decoder_config, out_dim=obs_shape[0]),
        reward=Decoder(reward_config, name='reward'),
        value=Decoder(value_config, name='value'),
        actor=Actor(actor_config, obs_shape, action_dim, is_action_discrete)
    )

    if disc_config:
        models['discount'] = Decoder(disc_config, dist='binary', name='discount')
    return models
