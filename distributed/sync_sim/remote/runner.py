import collections
from typing import List, Set, Union
import numpy as np
from ray.util.queue import Queue

from .agent import Agent
from .parameter_server import ParameterServer
from .typing import ModelStats, ModelWeights
from core.elements.builder import ElementsBuilder
from core.log import do_logging
from core.mixin.actor import RMS
from core.monitor import Monitor
from core.remote.base import RayBase
from core.typing import ModelPath
from env.func import create_env
from env.typing import EnvOutput
from utility.timer import Timer
from utility.utils import dict2AttrDict


class MultiAgentSimRunner(RayBase):
    def __init__(
        self, 
        configs: List[dict], 
        store_data: bool, 
        evaluation: bool, 
        param_queues: List[Queue]=None, 
        parameter_server: ParameterServer=None,
        monitor: Monitor=None
    ):
        super().__init__()

        self._store_data = store_data
        self._evaluation = evaluation
        self.remote_agents: List[Agent] = []
        self.param_queues = param_queues
        self.parameter_server = parameter_server
        self.monitor = monitor
        
        self.env = create_env(configs[0]['env'], no_remote=True)
        self.env_stats = self.env.stats()
        self.n_envs = self.env_stats.n_envs
        self.n_agents = self.env_stats.n_agents
        self.n_players = self.env_stats.n_players
        self.pid2aid = self.env_stats.pid2aid
        self.aid2pids = self.env_stats.aid2pids
        self.n_players_per_agent = [len(pids) for pids in self.aid2pids]

        self.env_output = self.env.output()

        if param_queues is not None:
            assert self.n_agents == len(param_queues), (self.n_agents, len(param_queues))

        self.builder = ElementsBuilder(
            configs[0], 
            self.env_stats
        )

        self._push_every_episode = configs[0]['runner']['push_every_episode']
        
        self.build_from_configs(configs)

    def build_from_configs(self, configs: List[dict]):
        assert len(configs) == self.n_agents, (len(configs), self.n_agents)
        configs = [dict2AttrDict(config) for config in configs]
        config = configs[0]
        self.config = config.runner

        self.agents: List[Agent] = []
        self.is_agent_active: List[bool] = [True for _ in configs]
        self.active_models: Set[ModelPath] = set()
        self.current_models: List[ModelPath] = []
        self.buffers = []
        self.rms: List[RMS] = []

        for aid, config in enumerate(configs):
            config.buffer.type = 'local'
            elements = self.builder.build_acting_agent_from_scratch(
                config, 
                build_monitor=True, 
                to_build_for_eval=self._evaluation, 
                to_restore=False
            )
            self.agents.append(elements.agent)
            
            model_path = ModelPath(config.root_dir, config.model_name)
            self.active_models.add(model_path)
            self.current_models.append(model_path)

            self.rms.append(RMS(config.actor.rms))

            if self._store_data:
                buffer = self.builder.build_buffer(
                    elements.model, 
                    config=config, 
                    n_players=self.n_players_per_agent[aid])
                self.buffers.append(buffer)
        assert len(self.agents) == len(self.active_models) == len(self.rms) == self.n_agents, (
            len(self.agents), len(self.active_models), len(self.rms), self.n_agents)

    """ Running Routines """
    def random_run(self):
        """ Random run the environment to collect running stats """
        step = 0
        agent_env_outs = self.env_output if self.n_players == 1 else self._divide_outs(self.env_output)
        self._update_rms(agent_env_outs)
        while step < self.config.N_STEPS:
            self.env_output = self.env.step(self.env.random_action())
            agent_env_outs = self.env_output if self.n_players == 1 else self._divide_outs(self.env_output)
            self._update_rms(agent_env_outs)
            self._log_for_done(self.env_output)
            step += 1

        for aid in range(self.n_agents):
            self.agents[aid].actor.update_rms_from_stats(
                self.rms[aid].get_rms_stats())
            self._send_aux_stats(aid)

        # stats = [a.get_stats() for a in self.agents]
        # print(f'Random running stats:')
        # for i, s in enumerate(stats):
        #     print(f'Random Agent {i}')
        #     print_dict(s)

    def run(self):
        def set_weights():
            for aid, pq in enumerate(self.param_queues):
                w = pq.get()
                self.is_agent_active[aid] = w.model in self.active_models
                self.current_models[aid] = w.model
                assert set(w.weights) == set(['model', 'aux', 'train_step']), set(w.weights)
                self.agents[aid].set_weights(w.weights)

        with Timer('runner_set_weights') as wt:
            set_weights()
        self._reset_local_buffers()
        with Timer('runner_run') as rt:
            step, n_episodes = self._run_impl()

        for aid, is_active in enumerate(self.is_agent_active):
            if is_active:
                self.agents[aid].store(**{
                    'time/set_weights': wt.total(),
                    'time/set_weights_mean': wt.average(),
                    'time/run': rt.total(),
                    'time/run_mean': rt.average(),
                    'time/fps': step / rt.last(),
                    'time/eps': n_episodes / rt.last()
                })
                self._send_aux_stats(aid)
                self._send_run_stats(aid, step, n_episodes)

        return step

    def evaluate(self):
        video, rewards = [], []
        step, n_episodes = self._run_impl(video, rewards)
        stats = {
            'score': np.stack([
                np.concatenate(a.get_raw_item('score')) 
                for a in self.agents], 1),
            'dense_score': np.stack([
                np.concatenate(a.get_raw_item('dense_score')) 
                for a in self.agents], 1),
        }
        return step, n_episodes, video, rewards, stats

    """ Running Setups """
    def set_active_model_paths(self, model_paths: List[ModelPath]):
        self.active_models = set(model_paths)

    def set_current_model_paths(self, model_paths: List[ModelPath]):
        self.current_models = model_paths

    """ Implementations """
    def _reset_local_buffers(self):
        if self._store_data:
            [b.reset() for b in self.buffers]

    def _reset(self):
        self.env_output = self.env.reset()
        self._reset_local_buffers()
        return self.env_output

    def _run_impl(self, video: list=None, reward: list=None):
        if self.n_players == 1:
            step, n_episodes = self._run_impl_sa(video, reward)
        else:
            step, n_episodes = self._run_impl_ma(video, reward)
        return step, n_episodes

    def _run_impl_ma(self, video: list=None, rewards: list=None):
        def check_end(step):
            return step >= self.config.N_STEPS

        def agents_infer(agents, agent_env_outs):
            assert len(agent_env_outs)  == len(agents), (len(agent_env_outs), len(agents))
            # action, terms = zip(*[
            #     a(o, evaluation=self._evaluation) 
            #     for a, o in zip(agents, agent_env_outs)])
            action, terms = [], []
            for aid, (agent, o) in enumerate(zip(agents, agent_env_outs)):
                with Timer(f'{aid}/infer') as it:
                    a, t = agent(o, evaluation=self._evaluation)
                action.append(a)
                terms.append(t)
                agent.store(**{
                    'time/ind_infer': it.total(),
                    'time/ind_infer_mean': it.average(),
                })
            return action, terms

        def step_env(agent_actions):
            action = np.concatenate(agent_actions, 1)
            assert action.shape == (self.n_envs, self.n_players), (action.shape, (self.n_envs, self.n_players))
            self.env_output = self.env.step(action)
            if video is not None:
                video.append(self.env.get_screen(convert_batch=False)[0])
            if rewards is not None:
                rewards.append(self.env_output.reward[0])
            agent_env_outs = self._divide_outs(self.env_output)
            return agent_env_outs
        
        def store_data(agent_env_outs, agent_actions, agent_terms, next_agent_env_outs):
            if self._store_data:
                assert len(agent_env_outs) == len(agent_actions) \
                    == len(agent_terms) == len(next_agent_env_outs) \
                    == len(self.buffers), (
                        len(agent_env_outs), len(agent_actions), 
                        len(agent_terms), len(next_agent_env_outs),
                        len(self.buffers)
                    )
                if self.config.get('partner_action'):
                    agent_logits = [t.pop('logits') for t in agent_terms]
                    agent_pactions = []
                    agent_plogits = []
                    for aid in range(self.n_agents):
                        pactions = [a for i, a in enumerate(agent_actions) if i != aid]
                        agent_pactions.append(np.concatenate(pactions, 1))
                        plogits = [a for i, a in enumerate(agent_logits) if i != aid]
                        agent_plogits.append(np.concatenate(plogits, 1))
                for aid, (agent, env_out, next_env_out, buffer) in enumerate(
                        zip(self.agents, agent_env_outs, next_agent_env_outs, self.buffers)):
                    if self.is_agent_active[aid]:
                        if self._push_every_episode:
                            for eid, reset in enumerate(env_out.reset):
                                if reset:
                                    eps, epslen = buffer.retrieve_episode(eid)
                                    self.remote_agents[aid].merge_episode.remote(eps, epslen)
                        stats = {
                            **env_out.obs,
                            'action': agent_actions[aid],
                            'reward': agent.actor.normalize_reward(next_env_out.reward),
                            'discount': next_env_out.discount,
                        }
                        if self.config.get('partner_action'):
                            stats['plogits'] = agent_plogits[aid]
                            stats['paction'] = agent_pactions[aid]
                        stats.update(agent_terms[aid])
                        buffer.add(stats)

        step = 0
        n_episodes = 0
        if self._push_every_episode:
            self._reset()
        agent_env_outs = self._divide_outs(self.env_output)
        self._update_rms(agent_env_outs)
        while not check_end(step):
            with Timer('infer') as it:
                action, terms = agents_infer(self.agents, agent_env_outs)
            with Timer('step_env') as et:
                next_agent_env_outs = step_env(action)
            with Timer('update_rms') as st:
                self._update_rms(next_agent_env_outs)
            with Timer('store_data') as dt:
                store_data(agent_env_outs, action, terms, next_agent_env_outs)
            agent_env_outs = next_agent_env_outs
            with Timer('log') as lt:
                n_episodes += self._log_for_done(self.env_output)
            step += 1
        _, terms = agents_infer(self.agents, agent_env_outs)

        for aid in range(self.n_agents):
            self.agents[aid].store(
                **{f'time/{t.name}': t.total() for t in [it, et, st, dt, lt]}, 
                **{f'time/{t.name}_mean': t.average() for t in [it, et, st, dt, lt]}
            )

        if self._store_data and not self._push_every_episode:
            for aid, (term, buffer) in enumerate(zip(terms, self.buffers)):
                data, n = buffer.retrieve_all_data(term['value'])
                self.remote_agents[aid].merge_data.remote(data, n)

        return step * self.n_envs, n_episodes

    def _run_impl_sa(self, video: list=None, rewards: list=None):
        def check_end(step):
            return step >= self.config.N_STEPS

        def step_env(action):
            assert action.shape == (self.n_envs,), (action.shape, (self.n_envs,))
            self.env_output = self.env.step(action)
            if video is not None:
                video.append(self.env.get_screen(convert_batch=False)[0])
            if rewards is not None:
                rewards.append(self.env_output.reward[0])
            return self.env_output
        
        def store_data(env_output, action, terms, next_env_output):
            if self._store_data:
                if self._push_every_episode:
                    for eid, reset in enumerate(env_output.reset):
                        if reset:
                            eps, epslen = self.buffers[0].retrieve_episode(eid)
                            self.remote_agents[0].merge_episode.remote(eps, epslen)
                stats = {
                    **env_output.obs,
                    'action': action,
                    'reward': self.agents[0].actor.normalize_reward(next_env_output.reward),
                    'discount': next_env_output.discount,
                }
                stats.update(terms)
                self.buffers[0].add(stats)

        step = 0
        n_episodes = 0
        env_output = self._reset() if self._push_every_episode else self.env_output
        self._update_rms(env_output)
        while not check_end(step):
            with Timer('infer') as it:
                action, terms = self.agents[0](env_output, evaluation=self._evaluation)
            with Timer('step_env') as et:
                self.env_output = step_env(action)
            with Timer('update_rms') as st:
                self._update_rms(env_output)
            with Timer('store_data') as dt:
                store_data(env_output, action, terms, self.env_output)
            env_output = self.env_output
            with Timer('log') as lt:
                n_episodes += self._log_for_done(env_output)
            step += 1
        _, terms = self.agents[0](self.env_output, evaluation=self._evaluation)

        for aid in range(self.n_agents):
            self.agents[aid].store(
                **{f'time/{t.name}': t.total() for t in [it, et, st, dt, lt]}, 
                **{f'time/{t.name}_mean': t.average() for t in [it, et, st, dt, lt]}
            )
        if self._store_data and not self._push_every_episode:
            for aid, buffer in enumerate(self.buffers):
                data, n = buffer.retrieve_all_data(terms['value'])
                self.remote_agents[aid].merge_data.remote(data, n)

        return step * self.n_envs, n_episodes

    def _divide_outs(self, out):
        agent_obs = [collections.defaultdict(list) for _ in range(self.n_agents)]
        agent_reward = [[] for _ in range(self.n_agents)]
        agent_discount = [[] for _ in range(self.n_agents)]
        agent_reset = [[] for _ in range(self.n_agents)]
        for pid, aid in enumerate(self.pid2aid):
            for k, v in out.obs.items():
                agent_obs[aid][k].append(v[:, pid])
            agent_reward[aid].append(out.reward[:, pid])
            agent_discount[aid].append(out.discount[:, pid])
            agent_reset[aid].append(out.reset[:, pid])
        assert len(agent_obs) == len(agent_reward) == len(agent_discount) == len(agent_reset) == self.n_agents, \
            (len(agent_obs), len(agent_reward), len(agent_discount), len(agent_reset), self.n_agents)
        outs = []
        for o, r, d, re in zip(agent_obs, agent_reward, agent_discount, agent_reset):
            for k, v in o.items():
                o[k] = np.stack(v, 1)
            r = np.stack(r, 1)
            d = np.stack(d, 1)
            re = np.stack(re, 1)
            outs.append(EnvOutput(o, r, d, re))
        # # TODO: remove this test code
        # for i in range(self.n_agents):
        #     for k, v in outs[i].obs.items():
        #         assert v.shape[:2] == (self.n_envs, self.n_players_per_agent[i]), \
        #             (v.shape, (self.n_envs, self.n_players_per_agent))
        #     assert outs[i].reward.shape == (self.n_envs, self.n_players_per_agent[i]), (outs[i].reward.shape, (self.n_envs, self.n_players_per_agent[i]))
        #     assert outs[i].discount.shape == (self.n_envs, self.n_players_per_agent[i])
        #     assert outs[i].reset.shape == (self.n_envs, self.n_players_per_agent[i])

        return outs

    def _update_rms(self, agent_env_outs: Union[EnvOutput, List[EnvOutput]]):
        if self.n_players == 1:
            self.rms[0].update_obs_rms(agent_env_outs.obs)
            self.rms[0].update_reward_rms(agent_env_outs.reward, agent_env_outs.discount)
        else:
            assert len(self.rms) == len(agent_env_outs), (len(self.rms), len(agent_env_outs))
            for rms, out in zip(self.rms, agent_env_outs):
                rms.update_obs_rms(out.obs)
                rms.update_reward_rms(out.reward, out.discount)

    def _log_for_done(self, output: EnvOutput):
        # logging when any env is reset 
        done_env_ids = [i for i, r in enumerate(output.reset) if np.all(r)]
        if done_env_ids:
            info = self.env.info(done_env_ids)
            score, epslen, dense_score = [], [], []
            for i in info:
                score.append(i['score'])
                dense_score.append(i['dense_score'])
                epslen.append(i['epslen'])
            if self.n_players == 1:
                self.agents[0].store(
                    score=score, 
                    dense_score=dense_score, 
                    epslen=epslen
                )
            else:
                score = np.stack(score, 1)
                assert score.shape == (self.n_players, len(info)), (score.shape, (self.n_players, len(info)))
                dense_score = np.stack(dense_score, 1)
                assert dense_score.shape == (self.n_players, len(info)), (dense_score.shape, (self.n_players, len(info)))
                epslen = np.stack(epslen, 0)
                assert epslen.shape[0] == len(info), (score.shape, len(info))
                for pid, aid in enumerate(self.pid2aid):
                    if self.is_agent_active[aid]:
                        self.agents[aid].store(
                            score=score[pid], 
                            dense_score=dense_score[pid], 
                            epslen=epslen
                        )

        return len(done_env_ids)

    def _send_aux_stats(self, aid):
        aux_stats = self.rms[aid].get_rms_stats()
        self.rms[aid].reset_rms_stats()
        model_weights = ModelWeights(
            self.current_models[aid], {'aux': aux_stats})
        self.parameter_server.update_strategy_aux_stats.remote(aid, model_weights)

    def _send_run_stats(self, aid, env_step, n_episodes):
        stats = self.agents[aid].get_raw_stats()
        stats['env_step'] = env_step
        stats['n_episodes'] = n_episodes
        model = self.current_models[aid]
        assert model in self.active_models, (model, self.active_models)
        model_stats = ModelStats(model, stats)
        self.monitor.store_run_stats.remote(model_stats)
