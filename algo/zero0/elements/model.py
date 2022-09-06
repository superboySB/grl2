import os
from typing import Tuple
import tensorflow as tf

from core.elements.model import Model as ModelBase, ModelEnsemble as ModelEnsembleBase
from core.mixin.model import NetworkSyncOps
from core.tf_config import build
from tools.file import source_file
from core.typing import AttrDict
from .utils import compute_inner_steps

# register ppo-related networks 
source_file(os.path.realpath(__file__).replace('model.py', 'nn.py'))


class Model(ModelBase):
    def _pre_init(self):
        self.config = compute_inner_steps(self.config)
        self.has_rnn = bool(self.config.get('rnn_type'))
        if self.config.encoder.nn_id is not None \
            and self.config.encoder.nn_id.startswith('cnn'):
            self.config.encoder.time_distributed = 'rnn' in self.config

    def _build(
        self, 
        env_stats: AttrDict, 
        evaluation: bool=False
    ):
        basic_shape = (None,)
        dtype = tf.keras.mixed_precision.experimental.global_policy().compute_dtype
        shapes = env_stats['obs_shape']
        dtypes = env_stats['obs_dtype']
        TensorSpecs = {k: ((*basic_shape, *v), dtypes[k], k) 
            for k, v in shapes.items()}

        if self.state_size:
            TensorSpecs.update(dict(
                state=self.state_type(*[((None, sz), dtype, name) 
                    for name, sz in self.state_size._asdict().items()]),
                mask=(basic_shape, tf.float32, 'mask'),
                evaluation=evaluation,
                return_eval_stats=evaluation,
            ))
        self.action = build(self.action, TensorSpecs)

    @tf.function
    def action(
        self, 
        obs, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None,
        evaluation=False, 
        return_eval_stats=False
    ):
        x, state = self.encode(obs, state=state, mask=mask)
        act_dist = self.policy(x, evaluation=evaluation)
        action = self.policy.action(act_dist, evaluation)

        if self.policy.is_action_discrete:
            pi = tf.nn.softmax(act_dist.logits)
            terms = {
                'mu': pi
            }
        else:
            mean = act_dist.mean()
            std = tf.exp(self.policy.logstd)
            terms = {
                'mu_mean': mean,
                'mu_std': std * tf.ones_like(mean), 
            }

        if evaluation:
            value = self.meta_value(x)
            return action, {'value': value}, state
        else:
            logprob = act_dist.log_prob(action)
            tf.debugging.assert_all_finite(logprob, 'Bad logprob')
            value = self.meta_value(x)
            terms.update({'mu_logprob': logprob, 'value': value})

            return action, terms, state    # keep the batch dimension for later use

    def forward(
        self, 
        obs, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None,
    ):
        x, state = self.encode(obs, state=state, mask=mask)
        act_dist = self.policy(x)
        value = self.meta_value(x)
        return x, act_dist, value

    @tf.function
    def compute_value(
        self, 
        obs, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None
    ):
        shape = obs.shape
        x = tf.reshape(obs, [-1, *shape[2:]])
        x, state = self.encode(x, state, mask)
        value = self.meta_value(x)
        value = tf.reshape(value, (-1, shape[1]))
        return value, state

    def encode(
        self, 
        x, 
        state: Tuple[tf.Tensor]=None,
        mask: tf.Tensor=None
    ):
        x = self.encoder(x)
        use_meta = self.config.inner_steps is not None
        if use_meta and hasattr(self, 'embed'):
            gamma = self.meta('gamma', inner=use_meta)
            lam = self.meta('lam', inner=use_meta)
            x = self.embed(x, gamma, lam)
        if hasattr(self, 'rnn'):
            x, state = self.rnn(x, state, mask)
            return x, state
        else:
            return x, None


class ModelEnsemble(ModelEnsembleBase):
    def _pre_init(self):
        self.config = compute_inner_steps(self.config)

    def _post_init(self):
        self.sync_ops = NetworkSyncOps()
        
        self.state_size = self.rl.state_size
        self.state_keys = self.rl.state_keys
        self.state_type = self.rl.state_type
        self.get_states = self.rl.get_states
        self.reset_states = self.rl.reset_states
        self.action = self.rl.action

    def sync_nets(self):
        if self.config.inner_steps is not None:
            self.sync_meta_nets()
            self.sync_rl_nets()

    @tf.function
    def sync_meta_nets(self):
        keys = sorted([k for k in self.meta.keys() if k.startswith('meta')])
        source = [self.meta[k] for k in keys]
        target = [self.rl[k] for k in keys]
        self.sync_ops.sync_nets(source, target)

    @tf.function
    def sync_rl_nets(self):
        keys = sorted([k for k in self.meta.keys() if not k.startswith('meta')])
        source = [self.rl[k] for k in keys]
        target = [self.meta[k] for k in keys]
        self.sync_ops.sync_nets(source, target)


def create_model(
    config, 
    env_stats, 
    name='zero', 
    to_build=False,
    to_build_for_eval=False,
    **kwargs
):
    config.policy.action_dim = env_stats.action_dim
    config.policy.is_action_discrete = env_stats.is_action_discrete

    if config['rnn_type'] is None:
        config.pop('rnn', None)
    else:
        config['rnn']['nn_id'] = config['actor_rnn_type']

    rl = Model(
        config=config, 
        env_stats=env_stats, 
        name='rl',
        to_build=to_build, 
        to_build_for_eval=to_build_for_eval,
        **kwargs
    )
    meta = Model(
        config=config, 
        env_stats=env_stats, 
        name='meta',
        to_build=False, 
        to_build_for_eval=False,
        **kwargs
    )
    return ModelEnsemble(
        config=config, 
        env_stats=env_stats, 
        components=dict(
            rl=rl, 
            meta=meta, 
        ), 
        name=name, 
        to_build=to_build, 
        to_build_for_eval=to_build_for_eval,
        **kwargs
    )
