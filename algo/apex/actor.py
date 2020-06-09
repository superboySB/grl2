import time
import threading
import functools
import collections
import numpy as np
import tensorflow as tf
from tensorflow_probability import distributions as tfd
import ray

from core.module import Ensemble
from core.tf_config import *
from core.base import BaseAgent
from core.decorator import config
from utility.display import pwc
from utility.utils import Every
from utility.timer import Timer
from utility.rl_utils import n_step_target
from utility.ray_setup import cpu_affinity
from utility.run import Runner, evaluate
from utility import pkg
from env.gym_env import create_env
from core.dataset import process_with_env, DataFormat, RayDataset


def get_learner_class(BaseAgent):
    class Learner(BaseAgent):
        def __init__(self,
                    config, 
                    model_config,
                    env_config,
                    model_fn,
                    replay):
            cpu_affinity('Learner')
            silence_tf_logs()
            configure_threads(config['n_cpus'], config['n_cpus'])
            configure_gpu()
            configure_precision(config.get('precision', 32))

            env = create_env(env_config)
            
            obs_dtype = env.obs_dtype
            action_dtype =  env.action_dtype
            algo = config['algorithm'].split('-', 1)[-1]
            is_per = ray.get(replay.name.remote()).endswith('per')
            n_steps = config['n_steps']
            data_format = pkg.import_module('agent', algo).get_data_format(
                env, is_per, n_steps)
            process = functools.partial(process_with_env, env=env)
            dataset = RayDataset(replay, data_format, process)

            self.models = Ensemble(
                model_fn=model_fn, 
                config=model_config, 
                env=env)

            super().__init__(
                name='ddpg' if 'actor' in model_config else 'dq',
                config=config, 
                models=self.models,
                dataset=dataset,
                env=env,
            )

            self._log_locker = threading.Lock()
            
        def start_learning(self):
            self._learning_thread = threading.Thread(target=self._learning, daemon=True)
            self._learning_thread.start()
            
        def _learning(self):
            start_time = time.time()
            while not self.dataset.good_to_learn():
                time.sleep(1)
            pwc(f'{self.name} starts learning...', color='blue')

            to_log = Every(self.LOG_PERIOD)
            while self.train_step < self.MAX_STEPS:
                start_train_step = self.train_step
                start_env_step = self.env_step
                start_time = time.time()
                self.learn_log(start_env_step)
                if to_log(self.train_step) and 'score' in self._logger and 'eval_score' in self._logger:
                    duration = time.time() - start_time
                    self.store(
                        train_step=self.train_step,
                        fps=(self.env_step - start_env_step) / duration,
                        tps=(self.train_step - start_train_step)/duration)
                    with self._log_locker:
                        self.log(self.env_step)
                    self.save(print_terminal_info=False)

        def get_weights(self, name=None):
            return self.models.get_weights(name=name)

        def record_episode_info(self, **kwargs):
            with self._log_locker:
                self.store(**kwargs)
            if 'epslen' in kwargs:
                self.env_step += np.sum(kwargs['epslen'])

    return Learner


class Worker:
    @config
    def __init__(self, 
                *,
                worker_id,
                model_config,
                env_config, 
                buffer_config,
                model_fn,
                buffer_fn):
        silence_tf_logs()
        configure_threads(1, 1)
        self._id = worker_id

        self.env = env = create_env(env_config)
        self.n_envs = self.env.n_envs

        if buffer_config['seqlen'] == 0:
            buffer_config['seqlen'] = env.max_episode_steps // getattr(env, 'frame_skip', 1)
        self._seqlen = buffer_config['seqlen']
        self.buffer = buffer_fn(buffer_config)
        self._is_per = buffer_config['type'].endswith('per')

        self.runner = Runner(self.env, self, nsteps=self.SYNC_PERIOD)

        self.models = Ensemble(
            model_fn=model_fn, 
            config=model_config, 
            env=env)

        self._is_dpg = 'actor' in self.models
        self._is_iqn = self._algorithm.endswith('iqn')
        assert self._is_dpg != self.env.is_action_discrete
        if self._is_dpg:
            self.actor = self.models['actor']
            self.q = self.models['q1']
            self._pull_names = ['actor', 'q1'] if self._is_per else ['actor']
        else:
            self.q = self.models['q']
            self._pull_names = ['q']
        
        self._info = collections.defaultdict(list)

        if self._is_per:
            TensorSpecs = dict(
                obs=(env.obs_shape, env.obs_dtype, 'obs'),
                action=(env.action_shape, env.action_dtype, 'action'),
                reward=((), tf.float32, 'reward'),
                next_obs=(env.obs_shape, env.obs_dtype, 'next_obs'),
                discount=((), tf.float32, 'discount'),
                steps=((), tf.float32, 'steps')
            )
            if not self._is_dpg:
                if self._is_iqn:
                    TensorSpecs['qtv'] = ((self.K,), tf.float32, 'qtv')
                else:
                    TensorSpecs['q']= ((), tf.float32, 'q')
            if self._is_iqn:
                self.compute_priorities = build(
                    self._compute_iqn_priorities, TensorSpecs, batch_size=self._seqlen)
            else:
                self.compute_priorities = build(
                    self._compute_dqn_priorities, TensorSpecs)

    def __call__(self, x, deterministic=False, **kwargs):
        if self._is_dpg:
            return self.actor(x, deterministic, self._act_eps)
        else:
            x = np.array(x)
            if len(x.shape) % 2 != 0:
                x = tf.expand_dims(x, 0)
            if self._is_iqn:
                out = self.q.value(x, self.K)
                qtv, q = tf.nest.map_structure(lambda x: np.squeeze(x.numpy()), out[1:])
            else:
                q = np.squeeze(self.q.value(x).numpy())
            if np.random.uniform() < self._act_eps:
                action = self.env.random_action()
            else:
                action = np.argmax(q, axis=-1)
            action = np.int32(action)
            if self._is_per:
                return action, {'qtv': qtv[:, action]} if self._is_iqn else {'q': q[action]}
            else:
                return action

    def run(self, learner, replay):
        step = 0
        while True:
            weights = self._pull_weights(learner)
            step += self._seqlen
            self._run(weights, replay)
            self._send_episode_info(learner)

    def _run(self, weights, replay):
        def collect(env, step, info, **kwargs):
            self.buffer.add_data(**kwargs)
            if self.buffer.is_full():
                self._send_data(replay)

        self.models.set_weights(weights)
        if self._seqlen == 0:
            self.runner.run_traj(step_fn=collect)
        else:
            self.runner.run(step_fn=collect)

    def store(self, score, epslen):
        self._info['score'].append(score)
        self._info['epslen'].append(epslen)

    @tf.function
    def _compute_dqn_priorities(self, obs, action, reward, next_obs, discount, steps, q=None):
        if self._is_dpg:
            q = self.q(obs, action)
            nth_action = self.actor.action(next_obs, deterministic=False)
            nth_q = self.q(next_obs, nth_action)
        else:
            nth_action = self.q.action(next_obs, False)
            nth_q = self.q.value(next_obs, nth_action)
            
        returns = n_step_target(reward, nth_q, self._gamma, discount, steps, self._tbo)
        
        priority = tf.abs(returns - q)
        priority += self._per_epsilon
        priority **= self._per_alpha

        tf.debugging.assert_shapes([(priority, (None,))])

        return tf.squeeze(priority)
    
    @tf.function
    def _compute_iqn_priorities(self, obs, action, reward, next_obs, discount, steps, qtv=None):
        nth_action = self.q.action(next_obs, self.N_PRIME)
        _, nth_qtv, _ = self.q.value(next_obs, self.N_PRIME, nth_action)
        reward = reward[None, :, None]
        discount = discount[None, :, None]
        steps = steps[None, :, None]
        returns = n_step_target(reward, nth_qtv, self._gamma, discount, steps, self._tbo)
        returns = tf.transpose(returns, (1, 2, 0))      # [B, 1, N']
        qtv = qtv[..., None]
        tf.debugging.assert_shapes([[qtv, (self._seqlen, self.K, 1)]])
        tf.debugging.assert_shapes([[returns, (self._seqlen, 1, self.N_PRIME)]])
        
        priority = tf.reduce_mean(tf.abs(returns - qtv), axis=[1, 2])
        priority += self._per_epsilon
        priority **= self._per_alpha

        tf.debugging.assert_shapes([(priority, (None,))])

        return tf.squeeze(priority)
        
    def _pull_weights(self, learner):
        return ray.get(learner.get_weights.remote(name=self._pull_names))

    def _send_data(self, replay, buffer=None, target_replay='fast_replay'):
        buffer = buffer or self.buffer
        mask, data = buffer.sample()

        if self._is_per:
            data_tensor = {k: tf.convert_to_tensor(v) for k, v in data.items()}
            if self._is_iqn:
                del data['qtv']
            else:
                del data['q']
            data['priority'] = self.compute_priorities(**data_tensor).numpy()
        replay.merge.remote(data, data['action'].shape[0], target_replay=target_replay)
        buffer.reset()

    def _send_episode_info(self, learner):
        if self._info:
            learner.record_episode_info.remote(**self._info)
            self._info.clear()

def get_worker_class():
    return Worker

class Evaluator:
    @config
    def __init__(self, 
                *,
                model_config,
                env_config,
                model_fn):
        silence_tf_logs()
        configure_threads(1, 1)

        self.env = env = create_env(env_config)
        self.n_envs = self.env.n_envs

        self.models = Ensemble(
                model_fn=model_fn, 
                config=model_config, 
                env=env)

        self._is_dpg = 'actor' in self.models
        self._is_iqn = self._algorithm.endswith('iqn')
        assert self._is_dpg != self.env.is_action_discrete
        if self._is_dpg:
            self.actor = self.models['actor']
            self._pull_names = ['actor']
        else:
            self.q = self.models['q']
            self._pull_names = ['q']
        
        self._info = collections.defaultdict(list)

    def __call__(self, x, deterministic=True, **kwargs):
        if self._is_dpg:
            return self.actor(x, deterministic=True)
        else:
            if self._is_iqn:
                return self.q(x, self.K, deterministic=True)
            return self.q(x, deterministic=True)

    def run(self, learner):
        while True:
            weights = self._pull_weights(learner)
            self._run(weights)
            self._send_episode_info(learner)

    def _run(self, weights):
        self.models.set_weights(weights)
        score, epslen, _ = evaluate(self.env, self)
        self.store(score, epslen)

    def store(self, score, epslen):
        self._info['eval_score'] += score
        self._info['eval_epslen'] += epslen

    def _pull_weights(self, learner):
        return ray.get(learner.get_weights.remote(name=self._pull_names))

    def _send_episode_info(self, learner):
        if self._info:
            learner.record_episode_info.remote(**self._info)
            self._info.clear()

def get_evaluator_class():
    return Evaluator
