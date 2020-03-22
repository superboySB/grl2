""" Implementation of single process environment """
import itertools
import numpy as np
import gym
import ray

from utility import tf_distributions
from utility.utils import isscalar, convert_dtype
from env.wrappers import *
from env.deepmind_wrappers import make_deepmind_env


def action_dist_type(env):
    if isinstance(env.action_space, gym.spaces.Discrete):
        return tf_distributions.Categorical
    elif isinstance(env.action_space, gym.spaces.Box):
        return tf_distributions.DiagGaussian
    else:
        raise NotImplementedError

def _make_env(config):
    if config.get('is_deepmind_env', False):
        env = make_deepmind_env(config)
    else:
        env = gym.make(config['name'])
        max_episode_steps = config.get('max_episode_steps', env.spec.max_episode_steps)
        if max_episode_steps < env.spec.max_episode_steps:
            env = TimeLimit(env, max_episode_steps)
        if config.get('log_video', False):
            print(f'video will be logged at {config["video_path"]}')
            env = gym.wrappers.Monitor(env, config['video_path'], force=True)
        if config.get('action_repetition'):
            env = ActionRepeat(env, config['n_ar'])
        env = EnvStats(env, config.get('precision', 32))
        if config.get('log_episode'):
            env = LogEpisode(env)
        if config.get('auto_reset'):
            env = AutoReset(env)
    env.seed(config.get('seed', 42))

    return env

def create_env(config, env_fn=_make_env):
    EnvType = Env if config.get('n_envs', 1) == 1 else EnvVec
    if config.get('n_workers', 1) == 1:
        if EnvType == EnvVec and config.get('efficient_envvec', False):
            EnvType = EfficientEnvVec
        return EnvType(config, env_fn)
    else:
        return RayEnvVec(EnvType, config, env_fn)


class Env:
    def __init__(self, config, env_fn=_make_env):
        self.name = config['name']
        self.env = env_fn(config)
        self.max_episode_steps = self.env.spec.max_episode_steps

    def __getattr__(self, name):
        return getattr(self.env, name)

    @property
    def n_envs(self):
        return 1

    def random_action(self, **kwargs):
        action = self.env.action_space.sample()
        return action
        
    def step(self, action, **kwargs):
        state, reward, done, info = self.env.step(action, **kwargs)

        return state.astype(self.obs_dtype), np.float32(reward), done, info

    def close(self):
        del self


class EnvVec:
    def __init__(self, config, env_fn=_make_env):
        self.n_envs = n_envs = config['n_envs']
        self.name = config['name']
        self.envs = [env_fn(config) for i in range(n_envs)]
        if 'seed' in config:
            [env.seed(config['seed'] + i) for i, env in enumerate(self.envs)]
        self.env = self.envs[0]
        self.max_episode_steps = self.env.spec.max_episode_steps

    def __getattr__(self, name):
        return getattr(self.env, name)

    def random_action(self, **kwargs):
        return convert_dtype([env.action_space.sample() for env in self.envs], dtype=self.action_dtype, copy=False)

    def reset(self):
        return np.asarray([env.reset() for env in self.envs], dtype=self.obs_dtype)
    
    def step(self, actions, **kwargs):
        state, reward, done, info = _envvec_step(self.envs, actions, **kwargs)

        return (convert_dtype(state, dtype=self.obs_dtype, copy=False), 
                convert_dtype(reward, dtype=np.float32), 
                convert_dtype(done, dtype=np.bool), 
                info)

    def get_mask(self):
        """ Get mask at the current step. Should only be called after self.step """
        return np.asarray([env.get_mask() for env in self.envs], dtype=np.bool)

    def get_score(self):
        return np.asarray([env.get_score() for env in self.envs])

    def get_epslen(self):
        return np.asarray([env.get_epslen() for env in self.envs])

    def get_already_done(self):
        return np.asarray([env.already_done for env in self.envs], dtype=np.bool)

    def close(self):
        del self


class EfficientEnvVec(EnvVec):
    def random_action(self):
        valid_envs = [env for env in self.envs if not env.already_done]
        return [env.action_space.sample() for env in valid_envs]
        
    def step(self, actions, **kwargs):
        valid_env_ids, valid_envs = zip(*[(i, env) for i, env in enumerate(self.envs) if not env.already_done])
        assert len(valid_envs) == len(actions), f'valid_env({len(valid_envs)}) vs actions({len(actions)})'
        for k, v in kwargs.items():
            assert len(actions) == len(v), f'valid_env({len(actions)}) vs {k}({len(v)})'
        
        state, reward, done, info = _envvec_step(valid_envs, actions, **kwargs)
        for i in range(len(info)):
            info[i]['env_id'] = valid_env_ids[i]
        
        return (convert_dtype(state, self._precision, copy=False), 
                convert_dtype(reward, dtype=np.float32), 
                convert_dtype(done, dtype=np.bool), 
                info)


class RayEnvVec:
    def __init__(self, EnvType, config, env_fn=_make_env):
        self.name = config['name']
        self.n_workers= config['n_workers']
        self.envsperworker = config['n_envs']
        self.n_envs = self.envsperworker * self.n_workers

        RayEnvType = ray.remote(EnvType)
        # leave the name "envs" for consistency, albeit workers seems more appropriate
        if 'seed' in config:
            self.envs = [config.update({'seed': 100*i}) or RayEnvType.remote(config.copy(), env_fn) 
                    for i in range(self.n_workers)]
        else:
            self.envs = [RayEnvType.remote(config.copy(), env_fn) 
                    for i in range(self.n_workers)]

        self.env = EnvType(config, env_fn)
        self.max_episode_steps = self.env.max_episode_steps

    def __getattr__(self, name):
        return getattr(self.env, name)

    def reset(self):
        return np.reshape(ray.get([env.reset.remote() for env in self.envs]), 
                          (self.n_envs, *self.obs_shape))

    def random_action(self, **kwargs):
        return np.reshape(ray.get([env.random_action.remote() for env in self.envs]), 
                          (self.n_envs, *self.action_shape))

    def step(self, actions, **kwargs):
        actions = np.squeeze(actions.reshape(self.n_workers, self.envsperworker, *self.action_shape))
        if kwargs:
            kwargs = dict([(k, np.squeeze(v.reshape(self.n_workers, self.envsperworker, -1))) for k, v in kwargs.items()])
            kwargs = [dict(v) for v in zip(*[itertools.product([k], v) for k, v in kwargs.items()])]
            state, reward, done, info = zip(*ray.get([env.step.remote(a, **kw) for env, a, kw in zip(self.envs, actions, kwargs)]))
        else:
            state, reward, done, info = zip(*ray.get([env.step.remote(a) for env, a in zip(self.envs, actions)]))
        if not isinstance(self.env, Env):
            info_lists = info
            info = []
            for i in info_lists:
                info += i

        return (np.reshape(state, (self.n_envs, *self.obs_shape)).astype(self.obs_dtype), 
                np.reshape(reward, self.n_envs).astype(np.float32), 
                np.reshape(done, self.n_envs).astype(bool),
                info)

    def get_mask(self):
        """ Get mask at the current step. Should only be called after self.step """
        return np.reshape(ray.get([env.get_mask.remote() for env in self.envs]), self.n_envs)

    def get_score(self):
        return np.reshape(ray.get([env.get_score.remote() for env in self.envs]), self.n_envs)

    def get_epslen(self):
        return np.reshape(ray.get([env.get_epslen.remote() for env in self.envs]), self.n_envs)

    def get_already_done(self):
        return np.reshape(ray.get([env.get_already_done.remote() for env in self.envs]), self.n_envs)

    def close(self):
        del self
    
def _envvec_step(envvec, actions, **kwargs):
    if kwargs:
        for k, v in kwargs.items():
            if isscalar(v):
                kwargs[k] = np.tile(v, actions.shape[0])
        kwargs = [dict(v) for v in zip(*[itertools.product([k], v) for k, v in kwargs.items()])]
        return zip(*[env.step(a, **kw) for env, a, kw in zip(envvec, actions, kwargs)])
    else:
        return zip(*[env.step(a) for env, a in zip(envvec, actions)])

if __name__ == '__main__':
    # performance test
    default_config = dict(
        name='LunarLander-v2', # Pendulum-v0, CartPole-v0
        video_path='video',
        log_video=False,
        n_workers=1,
        n_envs=1,
        log_episode=True,
        auto_reset=True,
        seed=0
    )

    env = create_env(default_config)
    o = env.reset()
    eps = [dict(
        obs=o,
        action=np.zeros(env.action_shape), 
        reward=0.,
        done=False
    )]
    for _ in range(3000):
        a = env.random_action()
        o, r, d, i = env.step(a)
        eps.append(dict(
                obs=o,
                action=a if r != 0 else 0,
                reward=r,
                done=d
            ))
        if d or len(eps) == env.max_episode_steps:
            print('check episodes')
            eps2 = i['episode']
            eps = {k: np.array([t[k] for t in eps]) for k in eps2.keys()}
            print(eps.keys())
            for k in eps.keys():
                print(k)
                np.testing.assert_allclose(eps[k], eps2[k])
            eps = []
