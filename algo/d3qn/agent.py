import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision.experimental import global_policy

from utility.display import pwc
from utility.rl_utils import n_step_target, transformed_n_step_target
from utility.losses import huber_loss
from utility.schedule import TFPiecewiseSchedule, PiecewiseSchedule
from utility.timer import Timer
from core.tf_config import build
from core.base import BaseAgent
from core.decorator import agent_config
from core.optimizer import Optimizer


class Agent(BaseAgent):
    @agent_config
    def __init__(self, *, dataset, env):
        self._dtype = global_policy().compute_dtype
        self._is_per = not dataset.buffer_type().endswith('uniform')

        self.dataset = dataset

        if self._schedule_lr:
            self._lr = TFPiecewiseSchedule(
                [(5e5, self._lr), (2e6, 5e-5)], outside_value=5e-5)

        # optimizer
        self._optimizer = Optimizer(self._optimizer, self.q, self._lr, clip_norm=self._clip_norm)
        self._ckpt_models['optimizer'] = self._optimizer

        self._action_dim = env.action_dim

        # Explicitly instantiate tf.function to initialize variables
        obs_dtype = env.obs_dtype if len(env.obs_shape) == 3 else self._dtype
        TensorSpecs = dict(
            obs=(env.obs_shape, env.obs_dtype, 'obs'),
            action=((env.action_dim,), self._dtype, 'action'),
            reward=((), self._dtype, 'reward'),
            nth_obs=(env.obs_shape, env.obs_dtype, 'nth_obs'),
            done=((), self._dtype, 'done'),
        )
        if self._is_per:
            TensorSpecs['IS_ratio'] = ((), self._dtype, 'IS_ratio')
        if 'steps' in self.dataset.data_format:
            TensorSpecs['steps'] = ((), self._dtype, 'steps')
        self.learn = build(self._learn, TensorSpecs)

        self._sync_target_nets()

    def __call__(self, obs, deterministic=False):
        return self.q(obs, deterministic, self._act_eps)

    def learn_log(self, step):
        data = self.dataset.sample()
        if self._is_per:
            saved_idxes = data['saved_idxes'].numpy()
            del data['saved_idxes']
        with Timer('learn', 10000):
            terms = self.learn(**data)
        if step % self._target_update_freq == 0:
            self._sync_target_nets()

        if self._schedule_lr:
            step = tf.convert_to_tensor(step, tf.float32)
            terms['lr'] = self._lr(step)
        terms = {k: v.numpy() for k, v in terms.items()}

        if self._is_per:
            self.dataset.update_priorities(terms['priority'], saved_idxes)
        self.store(**terms)

    @tf.function
    def _learn(self, obs, action, reward, nth_obs, done, steps=1, IS_ratio=1):
        target_fn = (transformed_n_step_target if self._tbo 
                    else n_step_target)
        loss_fn = huber_loss if self._loss_type == 'huber' else tf.square
        terms = {}
        with tf.GradientTape() as tape:
            q = self.q.value(obs, action)
            nth_action = self.q.action(nth_obs, noisy=False)
            nth_action = tf.one_hot(nth_action, self._action_dim, dtype=self._dtype)
            nth_q = self.target_q.value(nth_obs, nth_action)
            target_q = target_fn(reward, done, nth_q, self._gamma, steps)
            error = target_q - q
            loss = tf.reduce_mean(IS_ratio * loss_fn(error))
        tf.debugging.assert_shapes([
            [q, (None,)],
            [nth_q, (None,)],
            [target_q, (None,)],
            [error, (None,)],
            [loss, ()],
        ])
        if self._is_per:
            priority = self._compute_priority(tf.abs(error))
            terms['priority'] = priority
        
        terms['norm'] = self._optimizer(tape, loss)
        
        terms.update(dict(
            q=q,
            loss=loss,
        ))

        return terms

    def _compute_priority(self, priority):
        """ p = (p + 𝝐)**𝛼 """
        priority += self._per_epsilon
        priority **= self._per_alpha
        return priority

    @tf.function
    def _sync_target_nets(self):
        [tv.assign(mv) for mv, tv in zip(
            self.q.trainable_variables, self.target_q.trainable_variables)]
