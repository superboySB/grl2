import time
import queue
import threading
import numpy as np
import ray

from utility.utils import config_attr
from utility.display import pwc
from core.tf_config import *
from core.mixin import RMS
from algo.seed.actor import \
    get_actor_class as get_actor_base_class, \
    get_learner_class as get_learner_base_class, \
    get_worker_class as get_worker_base_class, \
    get_evaluator_class
from .buffer import Buffer, LocalBuffer


def get_actor_class(AgentBase):
    ActorBase = get_actor_base_class(AgentBase)
    class Actor(ActorBase):
        def __init__(self, actor_id, model_fn, config, model_config, env_config):
            super().__init__(actor_id, model_fn, config, model_config, env_config)

        def _process_output(self, obs, kwargs, out, evaluation):
            out = super()._process_output(obs, kwargs, out, evaluation)
            out[1]['train_step'] = np.ones(obs.shape[0]) * self.train_step
            return out

        def start(self, workers, learner, monitor):
            super().start(workers, learner, monitor)
            self._workers = workers

    return Actor


def get_learner_class(AgentBase):
    LearnerBase = get_learner_base_class(AgentBase)
    class Learner(LearnerBase):
        def _add_attributes(self, env, dataset):
            super()._add_attributes(env, dataset)

            if not hasattr(self, '_push_names'):
                self._push_names = [
                    k for k in self.model.keys() if 'target' not in k]

        def _create_dataset(self, replay, model, env, config, replay_config):
            self.replay = Buffer(replay_config)
            return self.replay

        def push_weights(self):
            obs_rms, _ = self.get_rms_stats()
            obs_rms_id = ray.put(obs_rms)
            train_step, weights = self.get_weights(name=self._push_names)
            train_step_id = ray.put(train_step)
            weights_id = ray.put(weights)
            for a in self._actors:
                ray.get(a.set_weights.remote(train_step_id, weights_id))
                ray.get(a.set_rms_stats.remote(obs_rms_id))
                ray.get(a.resume.remote(train_step_id))

        def _learning(self):
            while True:
                self.dataset.wait_to_sample(self.train_step)

                self.update_obs_rms(np.concatenate(self.dataset['obs']))
                self.update_reward_rms(
                    self.dataset['reward'], self.dataset['discount'])
                self.dataset.update('reward', 
                    self.normalize_reward(self.dataset['reward']), field='all')
                self.dataset.compute_advantage_return()
                self.dataset.reshape_to_sample()

                self.learn_log()

                self.push_weights()

        def _store_buffer_stats(self):
            super()._store_buffer_stats()
            self.store(**self.dataset.get_async_stats())
            # reset dataset for the next training iteration
            self.dataset.reset()

    return Learner


def get_worker_class():
    """ A Worker is only responsible for resetting&stepping environment """
    WorkerBase = get_worker_base_class()
    class Worker(WorkerBase, RMS):
        def __init__(self, worker_id, config, env_config, buffer_config):
            super().__init__(worker_id, config, env_config, buffer_config)

            self._setup_rms_stats()
            self._counters = {f'{i}': 0 for i in range(self._n_envvecs)}

        def env_step(self, eid, action, terms):
            self._counters[f'{eid}'] += 1
            # TODO: consider using a queue here
            env_output = self._envvecs[eid].step(action)
            kwargs = dict(
                obs=self._obs[eid], 
                action=action, 
                reward=env_output.reward,
                discount=env_output.discount, 
                next_obs=env_output.obs)
            kwargs.update(terms)
            self._obs[eid] = env_output.obs

            if self._buffs[eid].is_full():
                # Adds the last value to buffer for gae computation. 
                self._buffs[eid].finish(terms['value'])
                self._send_data(self._replay, self._buffs[eid])

            self._collect(
                self._buffs[eid], self._envvecs[eid], env_step=None,
                reset=env_output.reset, **kwargs)

            done_env_ids = [i for i, r in enumerate(env_output.reset) if r]
            if done_env_ids:
                self._info['score'] += self._envvecs[eid].score(done_env_ids)
                self._info['epslen'] += self._envvecs[eid].epslen(done_env_ids)
                if len(self._info['score']) > 10:
                    self._send_episodic_info(self._monitor)

            return env_output

        def random_warmup(self, steps):
            rewards = []
            discounts = []

            for e in self._envvecs:
                for _ in range(steps // e.n_envs):
                    o, r, d, _ = e.step(e.random_action())
                    self._process_obs(o)
                    rewards.append(r)
                    discounts.append(d)

            rewards = np.swapaxes(rewards, 0, 1)
            discounts = np.swapaxes(discounts, 0, 1)
            self.update_reward_rms(rewards, discounts)

            return self.get_rms_stats()

        def _create_buffer(self, buffer_config, n_envvecs):
            buffer_config['force_envvec'] = True
            return {eid: LocalBuffer(buffer_config) 
                for eid in range(n_envvecs)}

        def _send_episodic_info(self, monitor):
            """ Sends episodic info to monitor for bookkeeping """
            if self._info:
                monitor.record_episodic_info.remote(
                    self._id, **self._info, **self._counters)
                self._info.clear()

    return Worker
