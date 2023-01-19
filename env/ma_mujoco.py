import gym
from gym.spaces import Box
from gym.wrappers import TimeLimit
import numpy as np

from env.ma_mujoco_env.multiagent_mujoco.mujoco_multi import MujocoMulti

class MAMujoco(gym.Wrapper):
    def __init__(self, config):
        scenario, agent_conf = config.env_name.split('_')
        config.env_args.scenario = f'{scenario}-v2'
        config.env_args.agent_conf = agent_conf
        config.env_args.episode_limit = config.max_episode_steps

        self.env = MujocoMulti(**config)

        self.action_space = self.env.action_space

        self.single_agent = config.single_agent
        if self.single_agent:
            self.n_agents = 1
            self.n_units = self.env.n_agents
            self.uid2aid = [0] * self.n_units
        else:
            self.n_agents = self.env.n_agents
            self.uid2aid = list(range(self.n_agents))
            self.n_units = self.n_agents

        self.observation_space = [Box(low=np.array([-10]*self.n_agents), high=np.array([10]*self.n_agents)) for _ in range(self.n_agents)]

        self.obs_shape = [{
            'obs': (self.env.obs_size, ), 
            'global_state': (self.env.obs_size, )
        } for _ in range(self.n_agents)]
        self.obs_dtype = [{
            'obs': np.float32, 
            'global_state': np.float32
        } for _ in range(self.n_agents)]

        self.action_space = self.env.action_space[:1] \
            if self.single_agent else self.env.action_space

        self.reward_range = None
        self.metadata = None
        self.max_episode_steps = self.env.episode_limit
        self._score = np.zeros(self.n_agents)
        self._dense_score = np.zeros(self.n_agents)
        self._epslen = 0

    def random_action(self):
        action = [a.sample() for a in self.action_space]
        return action

    def step(self, actions):
        obs, state, reward, done, _, _ = self.env.step(actions)
        reward = np.reshape(reward, -1)
        done = done[0]
        obs = get_obs(obs, state, self.single_agent)

        self._score += reward[0]
        self._dense_score += reward[0]
        self._epslen += 1

        info = {
            'score': self._score, 
            'dense_score': self._dense_score, 
            'epslen': self._epslen, 
            'game_over': self._epslen == self.max_episode_steps
        }

        reward = np.split(reward, self.n_agents)
        if done and self._epslen == self.max_episode_steps:
            done = [np.zeros(self.n_units)] if self.single_agent else \
                [np.zeros(1) for _ in range(self.n_agents)]
        else:
            done = [np.ones(self.n_units) * done] if self.single_agent else \
                [np.ones(1) * done for _ in range(self.n_agents)]
        assert len(obs) == self.n_agents, (obs, self.n_agents)
        assert len(reward) == self.n_agents, (reward, self.n_agents)
        assert len(done) == self.n_agents, (done, self.n_agents)
        return obs, reward, done, info

    def reset(self):
        obs, state, _ = self.env.reset()
        obs = get_obs(obs, state, self.single_agent)
        assert len(obs) == self.n_agents, (obs, self.n_agents)

        self._score = np.zeros(self.n_agents)
        self._dense_score = np.zeros(self.n_agents)
        self._epslen = 0

        return obs

def get_obs(obs, state, single_agent):
    agent_obs = []
    if single_agent:
        agent_obs.append({'obs': np.stack(obs, -2), 'global_state': np.stack(state, -2)})
    else:
        for o, s in zip(obs, state):
            o = np.expand_dims(o, 0)
            s = np.expand_dims(s, 0)
            agent_obs.append({'obs': o, 'global_state': s})
    return agent_obs