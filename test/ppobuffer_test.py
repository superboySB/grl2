import random
import numpy as np

from algo.ppo.buffer import PPOBuffer
from utility.utils import standardize


gamma = .99
lam = .95
gae_discount = gamma * lam
config = dict(
    gamma=gamma,
    lam=lam,
    advantage_type='gae'
)
kwargs = dict(
    config=config,
    n_envs=8, 
    epslen=1000, 
    n_minibatches=2, 
    obs_shape=[3], 
    obs_dtype=np.float32, 
    action_shape=[2], 
    action_dtype=np.float32,
)

buffer = PPOBuffer(**kwargs)

class TestClass:
    def test_gae(self):
        d = np.zeros((kwargs['n_envs'], 1))
        m = np.ones((kwargs['n_envs'], 1))
        diff = kwargs['epslen'] - kwargs['n_envs']
        for i in range(kwargs['epslen']):
            r = np.random.rand(kwargs['n_envs'], 1)
            v = np.random.rand(kwargs['n_envs'], 1)
            if np.random.randint(2):
                d[np.random.randint(kwargs['n_envs'])] = 1
            buffer.add(reward=r,
                    value=v,
                    nonterminal=1-d,
                    mask=m)
            mask = 1
            if np.all(d == 1):
                break
        last_value = np.random.rand(kwargs['n_envs'], 1)
        buffer.finish(last_value)

        # implementation originally from openai's baselines
        # modified to add mask
        mb_returns = np.zeros_like(buffer.memory['reward'])
        mb_advs = np.zeros_like(buffer.memory['reward'])
        lastgaelam = 0
        for t in reversed(range(buffer.idx)):
            if t == buffer.idx - 1:
                nextnonterminal = buffer.memory['nonterminal'][:, t]
                nextvalues = last_value
            else:
                nextnonterminal = buffer.memory['nonterminal'][:, t]
                nextvalues = buffer.memory['value'][:, t+1]
            delta = buffer.memory['reward'][:, t] + gamma * nextvalues * nextnonterminal - buffer.memory['value'][:, t]
            mb_advs[:, t] = lastgaelam = delta + gae_discount * nextnonterminal * lastgaelam
        mb_advs = standardize(mb_advs, mask=buffer.memory['mask'])

        np.testing.assert_allclose(mb_advs, buffer.memory['advantage'], atol=1e-6)
        