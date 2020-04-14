import numpy as np
import tensorflow as tf
from tensorflow.keras.mixed_precision.experimental import global_policy

from utility.display import pwc
from utility.rl_utils import n_step_target, transformed_n_step_target
from utility.schedule import TFPiecewiseSchedule
from utility.timer import TBTimer
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
            self._actor_lr = TFPiecewiseSchedule(
                [(2e5, self._actor_lr), (1e6, 1e-5)])
            self._q_lr = TFPiecewiseSchedule(
                [(2e5, self._q_lr), (1e6, 1e-5)])

        self._actor_opt = Optimizer(self._optimizer, self.actor, self._actor_lr)
        self._q_opt = Optimizer(self._optimizer, [self.q1, self.q2], self._q_lr)
        self._ckpt_models['actor_opt'] = self._actor_opt
        self._ckpt_models['q_opt'] = self._q_opt

        if isinstance(self.temperature, float):
            self.temperature = tf.Variable(self.temperature, trainable=False)
        else:
            if getattr(self, '_schedule_lr', False):
                self._temp_lr = TFPiecewiseSchedule(
                    [(5e5, self._temp_lr), (1e6, 1e-5)])
            self._temp_opt = Optimizer(self._optimizer, self.temperature, self._temp_lr)
            self._ckpt_models['temp_opt'] = self._temp_opt

        self._action_dim = env.action_dim
        self._is_action_discrete = env.is_action_discrete

        TensorSpecs = dict(
            obs=(env.obs_shape, self._dtype, 'obs'),
            action=((env.action_dim,), self._dtype, 'action'),
            reward=((), self._dtype, 'reward'),
            nth_obs=(env.obs_shape, self._dtype, 'nth_obs'),
            done=((), self._dtype, 'done'),
        )
        if self._is_per:
            TensorSpecs['IS_ratio'] = ((), self._dtype, 'IS_ratio')
        if 'steps'  in self.dataset.data_format:
            TensorSpecs['steps'] = ((), self._dtype, 'steps')
        self.learn = build(self._learn, TensorSpecs)

        self._sync_target_nets()

    def __call__(self, obs, deterministic=False, epsilon=0):
        return self.actor(obs, deterministic=deterministic, epsilon=epsilon)

    def learn_log(self, step):
        with TBTimer('sample', 1000):
            data = self.dataset.sample()
        if self._is_per:
            saved_idxes = data['saved_idxes'].numpy()
            del data['saved_idxes']

        with TBTimer('learn', 1000):
            terms = self.learn(**data)
        self._update_target_nets()

        if self._schedule_lr:
            step = tf.convert_to_tensor(step, tf.float32)
            terms['actor_lr'] = self._actor_lr(step)
            terms['q_lr'] = self._q_lr(step)
            if not isinstance(self.temperature, (float, tf.Variable)):
                terms['temp_lr'] = self._temp_lr(step)
        terms = {k: v.numpy() for k, v in terms.items()}

        if self._is_per:
            self.dataset.update_priorities(terms['priority'], saved_idxes)
        self.store(**terms)

    @tf.function
    def _learn(self, obs, action, reward, nth_obs, done, steps=1, IS_ratio=1):
        target_entropy = getattr(self, 'target_entropy', -self._action_dim)
        old_action = action
        target_fn = (transformed_n_step_target if getattr(self, 'tbo', False) 
                    else n_step_target)
        with tf.GradientTape(persistent=True) as tape:
            action, logpi, terms = self.actor.train_step(obs)
            q1_with_actor = self.q1.value(obs, action)
            q2_with_actor = self.q2.value(obs, action)
            q_with_actor = tf.minimum(q1_with_actor, q2_with_actor)

            next_action, next_logpi, _ = self.actor.train_step(nth_obs)
            next_q1_with_actor = self.target_q1.value(nth_obs, next_action)
            next_q2_with_actor = self.target_q2.value(nth_obs, next_action)
            next_q_with_actor = tf.minimum(next_q1_with_actor, next_q2_with_actor)
            
            if isinstance(self.temperature, (float, tf.Variable)):
                temp = next_temp = self.temperature
            else:
                log_temp, temp = self.temperature.value(obs, action)
                _, next_temp = self.temperature.value(nth_obs, next_action)
                temp_loss = -tf.reduce_mean(IS_ratio * log_temp 
                    * tf.stop_gradient(logpi + target_entropy))
                terms['temp'] = temp

            q1 = self.q1.value(obs, old_action)
            q2 = self.q2.value(obs, old_action)

            tf.debugging.assert_shapes(
                [(IS_ratio, (None,)),
                (q1, (None,)), 
                (q2, (None,)), 
                (logpi, (None,)), 
                (q_with_actor, (None,)), 
                (next_q_with_actor, (None,))])
            
            actor_loss = tf.reduce_mean(IS_ratio * tf.stop_gradient(temp) * logpi - q_with_actor)

            nth_value = next_q_with_actor - next_temp * next_logpi
            target_q = target_fn(reward, done, nth_value, self._gamma, steps)
            q1_error = target_q - q1
            q2_error = target_q - q2

            tf.debugging.assert_shapes([(q1_error, (None,)), (q2_error, (None,))])

            q1_loss = .5 * tf.reduce_mean(IS_ratio * q1_error**2)
            q2_loss = .5 * tf.reduce_mean(IS_ratio * q2_error**2)
            q_loss = q1_loss + q2_loss

        if self._is_per:
            priority = self._compute_priority((tf.abs(q1_error) + tf.abs(q2_error)) / 2.)
            terms['priority'] = priority
            
        terms['actor_norm'] = self._actor_opt(tape, actor_loss)
        terms['q_norm'] = self._q_opt(tape, q_loss)
        if not isinstance(self.temperature, (float, tf.Variable)):
            terms['temp_norm'] = self._temp_opt(tape, temp_loss)
            
        terms.update(dict(
            actor_loss=actor_loss,
            q1=q1, 
            q2=q2,
            target_q=target_q,
            q1_loss=q1_loss, 
            q2_loss=q2_loss,
            q_loss=q_loss, 
        ))

        return terms

    def _compute_priority(self, priority):
        """ p = (p + 𝝐)**𝛼 """
        priority += self._per_epsilon
        priority **= self._per_alpha
        tf.debugging.assert_greater(priority, 0.)
        return priority

    @tf.function
    def _sync_target_nets(self):
        tvars = self.target_q1.variables + self.target_q2.variables
        mvars = self.q1.variables + self.q2.variables
        [tvar.assign(mvar) for tvar, mvar in zip(tvars, mvars)]

    @tf.function
    def _update_target_nets(self):
        tvars = self.target_q1.trainable_variables + self.target_q2.trainable_variables
        mvars = self.q1.trainable_variables + self.q2.trainable_variables
        [tvar.assign(self._polyak * tvar + (1. - self._polyak) * mvar) 
            for tvar, mvar in zip(tvars, mvars)]
