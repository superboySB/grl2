import numpy as np
from copy import deepcopy

from core.decorator import config
from utility.display import pwc
from utility.utils import moments, standardize
from replay.utils import init_buffer, print_buffer


class PPOBuffer:
    @config
    def __init__(self):
        self._mb_len = self._n_envs // self._n_mbs
        self._idxes = np.arange(self._n_envs)

        self.gae_discount = self._gamma * self._lam

        self._memory = {}
        self._idx = 0
        self._batch_idx = 0
        self._ready = False      # Whether the buffer is _ready to be read

    def add(self, **data):
        if 'reward' not in self._memory:
            init_buffer(self._memory, pre_dims=(self._n_envs, self._n_steps), **data)
            self._memory['value'] = np.zeros((self._n_envs, self._n_steps+1), dtype=np.float32)
            self._memory['traj_ret'] = np.zeros((self._n_envs, self._n_steps), dtype=np.float32)
            self._memory['advantage'] = np.zeros((self._n_envs, self._n_steps), dtype=np.float32)
            print_buffer(self._memory)
            
        for k, v in data.items():
            self._memory[k][:, self._idx] = v

        self._idx += 1

    def sample(self):
        assert self._ready
        if self._batch_idx == 0:
            np.random.shuffle(self._idxes)

        start = self._batch_idx * self._mb_len
        end = (self._batch_idx + 1) * self._mb_len

        keys = ['obs', 'action', 'traj_ret', 'value', 
                'advantage', 'old_logpi', 'mask']

        return {k: self._memory[k][self._idxes[start: end], :self._idx] for k in keys}

    def finish(self, last_value):
        self._memory['value'][:, self._idx] = last_value
        valid_slice = np.s_[:, :self._idx]
        self._memory['mask'][:, self._idx:] = 0
        mask = self._memory['mask'][valid_slice]

        # Environment hack
        if hasattr(self, '_reward_scale'):
            self._memory['reward'] *= self._reward_scale
        if hasattr(self, '_reward_clip'):
            self._memory['reward'] = np.clip(self._memory['reward'], -self.reward_clip, self.reward_clip)

        if self._adv_type == 'nae':
            traj_ret = self._memory['traj_ret'][valid_slice]
            next_return = last_value
            for i in reversed(range(self._idx)):
                traj_ret[:, i] = next_return = (self._memory['reward'][:, i]
                    + self._memory['nonterminal'][:, i] * self._gamma * next_return)

            # Standardize traj_ret and advantages
            traj_ret_mean, traj_ret_std = moments(traj_ret, mask=mask)
            value = standardize(self._memory['value'][valid_slice], mask=mask)
            # To have the same mean and std as trajectory return
            value = (value + traj_ret_mean) / (traj_ret_std + 1e-8)     
            self._memory['advantage'][valid_slice] = standardize(traj_ret - value, mask=mask)
            self._memory['traj_ret'][valid_slice] = standardize(traj_ret, mask=mask)
        elif self._adv_type == 'gae':
            advs = delta = (self._memory['reward'][valid_slice] 
                + self._memory['nonterminal'][valid_slice] 
                * self._gamma * self._memory['value'][:, 1:self._idx+1]
                - self._memory['value'][valid_slice])
            next_adv = 0
            for i in reversed(range(self._idx)):
                advs[:, i] = next_adv = (delta[:, i] 
                + self._memory['nonterminal'][:, i] * self.gae_discount * next_adv)
            self._memory['traj_ret'][valid_slice] = advs + self._memory['value'][valid_slice]
            self._memory['advantage'][valid_slice] = standardize(advs, mask=mask)
        else:
            raise NotImplementedError

        for k, v in self._memory.items():
            shape = v[valid_slice].shape
            v[valid_slice] = np.reshape((v[valid_slice].T * mask.T).T, shape)
        
        self._ready = True

    def reset(self):
        self._idx = 0
        self._batch_idx = 0
        self._ready = False
        self._memory['mask'] = np.zeros((self._n_envs, self._n_steps), dtype=bool)

    def good_to_learn(self):
        return np.sum(self._memory['mask']) > self.min_transitions


if __name__ == '__main__':
    _gamma = .99
    lam = .95
    gae_discount = _gamma * lam
    config = dict(
        _gamma=_gamma,
        lam=lam,
        advantage_type='gae',
        _n_mbs=1,
        min_transitions=500
    )
    kwargs = dict(
        config=config,
        _n_envs=8, 
        _n_steps=1000, 
    )
    buffer = PPOBuffer(**kwargs)
    d = np.zeros((kwargs['_n_envs']))
    m = np.ones((kwargs['_n_envs']))
    for i in range(kwargs['_n_steps']):
        r = np.random.rand(kwargs['_n_envs'])
        v = np.random.rand(kwargs['_n_envs'])
        if np.random.randint(2):
            d[np.random.randint(kwargs['_n_envs'])] = 1
        buffer.add(reward=r,
                value=v,
                nonterminal=1-d,
                mask=m)
        m = 1-d
        if np.all(d == 1):
            break
    last_value = np.random.rand(kwargs['_n_envs'])
    buffer.finish(last_value)
    