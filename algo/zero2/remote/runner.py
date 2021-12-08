import functools
import random
import numpy as np
import ray
from ray._raylet import ObjectRef

from .parameter_server import ParameterServer
from core.elements.builder import ElementsBuilder
from core.log import do_logging
from core.remote.base import RayBase
from env.func import create_env
from env.utils import batch_env_output
from run.utils import search_for_config
from utility import pkg
from utility.timer import Timer
from utility.utils import AttrDict2dict, batch_dicts, dict2AttrDict


class RunnerManager(RayBase):
    def __init__(self, config, store_data=True, remote_agent=None, parameter_server=None):
        self.config = dict2AttrDict(config['runner'])
        self.n_envs = config['env']['n_workers'] * config['env']['n_envs']
        self.n_eval_envs = max(self.n_envs * (1 - self.config.self_play_frac) * .5, 100)
        config = AttrDict2dict(config)
        RemoteRunner = TwoAgentRunner.as_remote()
        self.runners = [RemoteRunner.remote(
            config, 
            store_data=store_data, 
            remote_agent=remote_agent,
            parameter_server=parameter_server) 
                for _ in range(config['env']['n_workers'])]

    def max_steps(self):
        return self.config.MAX_STEPS

    def initialize_rms(self):
        obs_rms_list, rew_rms_list = list(
            zip(*ray.get([r.initialize_rms.remote() for r in self.runners])))
        return obs_rms_list, rew_rms_list

    def reset(self, wait=False):
        oids = [r.reset.remote() for r in self.runners]
        if wait:
            ray.get(oids)
        else:
            return oids

    def run(self, weights):
        if not isinstance(weights, ObjectRef):
            weights = ray.put(weights)
        import time
        start = time.time()
        with Timer('manager_run', 10):
            steps, stats = list(zip(*ray.get([r.run.remote(weights) for r in self.runners])))
        print('runner manager run', time.time() - start)
        stats = batch_dicts(stats, lambda x: sum(x, []))
        return sum(steps), stats
    
    def evaluate(self, total_episodes, weights=None, 
                other_root_dir=None, other_model_name=None):
        """ Evaluation is problematic if self.runner.run does not end in a pass """
        if other_root_dir is not None and other_model_name is not None:
            self.set_other_agent_from_path(other_root_dir, other_model_name)
        n_eps = 0
        stats_list = []
        i = 0
        while n_eps < total_episodes:
            _, _, stats = self.run(weights)
            n_eps += len(next(iter(stats.values())))
            stats_list.append(stats)
            i += 1
        print('Total number of runs:', i)
        stats = batch_dicts(stats_list, lambda x: sum(x, []))
        return stats, n_eps

    def set_model_path(self, root_dir, model_name):
        rid = ray.put(root_dir)
        mid = ray.put(model_name)
        [r.set_model_path.remote(rid, mid) for r in self.runners]

    """ Other Agent's Operations """
    def set_other_agent_from_path(self, root_dir, model_name, wait=False):
        oids = [r.set_other_agent_from_path.remote(root_dir, model_name) for r in self.runners]
        if wait:
            ray.get(oids)
        else:
            return oids


class TwoAgentRunner(RayBase):
    def __init__(self, config, store_data=True, remote_agent=None, parameter_server: ParameterServer=None):
        super().__init__()
        self.store_data = store_data
        self.n_agents = 4
        self.other_agent = None
        self.remote_agent = remote_agent
        self.parameter_server = parameter_server
        self._other_root_dir = None
        self._other_model_name = None
        self.build_from_config(config)
        self.ready_ids = []

    def build_from_config(self, config):
        config = dict2AttrDict(config)
        self.config = config.runner
        self.name = config.name
        self._root_dir = config.root_dir
        self._model_name = config.model_name
        self._self_play_frac = self.config.get('self_play_frac', 0)
        config.buffer.type = 'local'
        n_workers = config.env.n_workers
        config.env.n_workers = 1

        self.env = create_env(config.env)
        self.env_stats = self.env.stats()
        self.env_output = self.env.output(convert_batch=False)

        self.builder = ElementsBuilder(config, self.env_stats)
        elements = self.builder.build_actor_agent_from_scratch()
        self.agent = elements.agent
        if self.config.initialize_other:
            # Initialize a homogeneous agent, otherwise, call <set_other_agent_from_path>
            elements = self.builder.build_actor_agent_from_scratch()
            self.other_agent = elements.agent
        self.step = self.agent.get_env_step() // n_workers
        self.n_episodes = 0

        if self.store_data:
            self.buffer = self.builder.build_buffer(
                elements.model, remote_agent=self.remote_agent)
            collect_fn = pkg.import_module('elements.utils', algo=config.algorithm).collect
            self.collect = functools.partial(collect_fn, self.buffer)
        else:
            self.buffer = None
            self.collect = None
        self._set_pids(self.config.agent_pids)
        for pid in self.agent_pids:
            assert pid not in config.env.skip_players, (self.agent_pids, config.env.skip_players)

    """ Environment Interactions """
    def initialize_rms(self):
        for _ in range(10):
            self.runner.run(action_selector=self.env.random_action, step_fn=self.collect)
            self.agent.actor.update_obs_rms(np.concatenate(self.buffer['obs']))
            self.agent.actor.update_reward_rms(self.buffer['reward'], self.buffer['discount'])
            self.buffer.reset()
        self.buffer.clear()
        return self.agent.actor.get_rms_stats()

    def reset(self):
        self.env_output = self.env.reset(convert_batch=False)
        self.buffer.reset()
        self.ready_ids = []

    def run(self, weights):
        train_step, weights = weights
        self.agent.set_weights(weights)
        self.retrieve_other_agent_from_parameter_server()
        self.reset()
        import time
        start = time.time()
        with Timer('runner_run', 10):
            step = self._run_impl(train_step)
        print('runner run', time.time() - start)
        stats = self.agent.get_raw_stats()
        if self.parameter_server is not None and not self._self_play:
            self.push_payoff(stats['win_rate'])
        return step, stats

    def _run_impl(self, train_step):
        def check_end(i):
            if self.store_data:
                return self.buffer.ready_to_retrieve()
            else:
                return i >= self.config.N_STEPS

        def divide_outs(outs):
            agent_eids = []
            agent_outs = []
            other_eids = []
            other_outs = []
            for i, o in enumerate(outs):
                if o.obs['pid'] in self.agent_pids:
                    agent_eids.append(i)
                    agent_outs.append(o)
                else:
                    assert o.obs['pid'] in self.other_pids, (o.obs['pid'], self.other_pids)
                    other_eids.append(i)
                    other_outs.append(o)
            if agent_outs:
                agent_outs = batch_env_output(agent_outs)
            if other_outs:
                other_outs = batch_env_output(other_outs)
            assert len(agent_eids) + len(other_eids) == len(outs), (agent_eids, other_eids)
            return agent_eids, agent_outs, other_eids, other_outs
        
        def merge(agent_eids, agent_action, other_eids, other_action):
            agent_action = list(zip(*agent_action))
            other_action = list(zip(*other_action))
            assert len(agent_eids) == len(agent_action), (agent_eids, agent_action)
            assert len(other_eids) == len(other_action), (other_eids, other_action)
            i = 0
            j = 0
            action = []
            while i != len(agent_eids) and j != len(other_eids):
                if agent_eids[i] < other_eids[j]:
                    action.append(agent_action[i])
                    i += 1
                else:
                    action.append(other_action[j])
                    j += 1
            while i != len(agent_eids):
                action.append(agent_action[i])
                i += 1
            while j != len(other_eids):
                action.append(other_action[j])
                j += 1
            return tuple(map(np.stack, zip(*action)))

        def step_env(action, agent_eids, agent_outs, agent_action, agent_terms):
            if agent_outs:
                for pid in agent_outs.obs['pid']:
                    assert pid in self.agent_pids, agent_outs.obs['pid']
            else:
                assert agent_action == [], agent_action
                assert agent_terms == [], agent_terms
            self.env_output = self.env.step(action, convert_batch=False)
            self.step += self.env.n_envs

            if self.store_data:
                if agent_outs:
                    self.buffer.add({
                        **agent_outs.obs,
                        'action_type': agent_action[0],
                        'card_rank': agent_action[1],
                        'reward': [self.env_output[i].reward for i in agent_eids],
                        'discount': [self.env_output[i].discount for i in agent_eids],
                        **agent_terms
                    })
                done_eids, done_rewards = [], []
                assert len(self.env_output) == self.env.n_envs, (len(self.env_output), self.env.n_envs)
                for i, o in enumerate(self.env_output):
                    if o.discount == 0:
                        done_eids.append(i)
                        done_rewards.append(o.reward)
                if done_eids:
                    self.ready_ids += self.buffer.finish(train_step, done_eids, done_rewards)
            
            # return if the agent is ready to train
            ready, self.ready_ids = ray.wait(self.ready_ids)

            return True if np.any(ray.get(ready)) else False

        def log_for_done(outs):
            # logging when any env is reset 
            done_env_ids = [i for i, o in enumerate(outs) if o.reset]
            if done_env_ids:
                info = self.env.info(done_env_ids)
                score, epslen, won = [], [], []
                for i in info:
                    score.append(i['score'])
                    epslen.append(i['epslen'])
                    won.append(i['won'])
                self.agent.store(score=score, epslen=epslen, win_rate=won)
                self.n_episodes += len(info)

        i = 0
        import time
        start = time.time()
        while not check_end(i):
            agent_eids, agent_outs, other_eids, other_outs = \
                divide_outs(self.env_output)
            agent_action, agent_terms = self.agent(agent_outs, evaluation=False) \
                if agent_outs else ([], [])
            other_action, other_terms = self.other_agent(other_outs, evaluation=False) \
                if other_outs else ([], [])
            action = merge(
                agent_eids, agent_action, other_eids, other_action)
            ready = step_env(action, agent_eids, agent_outs, agent_action, agent_terms)
            if not self._self_play:
                log_for_done(self.env_output)
            if ready:
                break
            i += 1
        print('run finished:', time.time() - start, i)
        return self.step

    """ Player Setups """
    def self_play(self):
        self._set_pids(list(range(self.n_agents)))
        assert self.other_pids == [], self.other_agent

    def set_other_agent_from_path(self, root_dir, model_name):
        if self.other_agent is not None:
            do_logging(f'Make sure your network is defined on CPUs. TF does not release GPU memory automatically and constructing multiple networks incurs large GPU memory overhead.', level='WARNING')
        self._other_root_dir = root_dir
        self._other_model_name = model_name
        path_tuple = (root_dir, model_name)
        path = '/'.join(path_tuple)
        config = search_for_config(path)
        name = config.get('name', self.name)
        builder = ElementsBuilder(config, self.env_stats, name=name)
        elements = builder.build_actor_agent_from_scratch()
        self.other_agent = elements.agent
        self._set_pids(self.config.agent_pids)

    def set_other_agent_weights(self, root_dir, model_name, weights):
        self._other_root_dir = root_dir
        self._other_model_name = model_name
        self.other_agent.set_weights(weights)
        self._set_pids(self.config.agent_pids)

    def compute_other_pids(self):
        return [i for i in range(self.n_agents) if i not in self.agent_pids]

    """ Interactions with other process """
    def retrieve_other_agent_from_parameter_server(self):
        if self.other_agent is None:
            elements = self.builder.build_actor_agent_from_scratch()
            self.other_agent = elements.agent
        if self.parameter_server is not None:
            if random.random() < self._self_play_frac:
                self.self_play()
            else:
                path, weights = ray.get(self.parameter_server.sample_strategy.remote(
                    self._root_dir, self._model_name))
                self.set_other_agent_weights(*path, weights)

    def push_payoff(self, payoff):
        self.parameter_server.add_payoff.remote(
            self._root_dir, self._model_name, 
            self._other_root_dir, self._other_model_name, payoff)

    def _set_pids(self, agent_pids):
        self.agent_pids = agent_pids
        self.other_pids = self.compute_other_pids()
        self._self_play = len(self.agent_pids) == self.n_agents
        if self.buffer is not None:
            self.buffer.set_pids(self.agent_pids, self.other_pids)

    def set_model_path(self, root_dir, model_name):
        self._root_dir = root_dir
        self._model_name = model_name
