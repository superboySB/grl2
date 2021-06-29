import logging
import numpy as np

from algo.ppo.buffer import init_buffer, print_buffer, \
    compute_indices, standardize
from algo2.mappo.buffer import Buffer as BufferBase


logger = logging.getLogger(__name__)

def reshape_to_store(memory, n_envs, n_steps, n_agents, sample_size=None, keys=None):
    start_dim = 3 if sample_size else 2
    
    keys = keys or list(memory.keys())
    memory = {k: memory[k].reshape(n_envs, n_steps, n_agents, *memory[k].shape[start_dim:])
        for k in keys}

    return memory


def reshape_to_sample(memory, n_envs, n_steps, n_agents, sample_size=None, keys=None):
    leading_dims = (-1, sample_size, n_agents) if sample_size else (-1, n_agents)

    keys = keys or list(memory.keys())
    memory = {k: memory[k].reshape(*leading_dims, *memory[k].shape[3:])
            for k in keys}
    if sample_size:
        for k in keys:
            v = memory[k]
            assert v.shape[:3] == (n_envs * n_steps / sample_size, sample_size, n_agents), v.shape
    else:
        for k in keys:
            v = memory[k]
            assert v.shape[:2] == (n_envs * n_steps, n_agents), (v.shape, n_envs, n_steps)

    return memory


class Buffer(BufferBase):
    def sample(self, sample_keys=None):
        assert self._ready
        if self._mb_idx == 0:
            np.random.shuffle(self._shuffled_idxes)
        sample_keys = sample_keys or self._sample_keys
        self._mb_idx, self._curr_idxes = compute_indices(
            self._shuffled_idxes, self._mb_idx, 
            self._mb_size, self.N_MBS)
        
        sample = {k: self._memory[k][self._curr_idxes, 0]
            if k in self._state_keys 
            else self._memory[k][self._curr_idxes] 
            for k in sample_keys}
        
        if 'advantage' in sample and self._norm_adv == 'minibatch':
            sample['advantage'] = standardize(
                sample['advantage'], mask=sample['life_mask'], epsilon=self._epsilon)
        
        return sample

    def reshape_to_store(self):
        if not self._is_store_shape:
            self._memory = reshape_to_store(
                self._memory, self._n_envs, self.N_STEPS, 
                self._n_agents, self._sample_size, self._non_state_keys)
            for k in self._state_keys:
                self._memory[k] = self._states[k].reshape(
                    -1, self.N_STEPS, self._states[k].shape[-1])
            self._is_store_shape = True

    def reshape_to_sample(self):
        if self._is_store_shape:
            self._memory = reshape_to_sample(
                self._memory, self._n_envs, self.N_STEPS, 
                self._n_agents, self._sample_size, self._non_state_keys)
            for k in self._state_keys:
                self._memory[k] = self._states[k].reshape(
                    -1, self._n_agents, self._sample_size, self._states[k].shape[-1])
            self._is_store_shape = False

    def _init_buffer(self, data):
        self._n_agents = data['reward'].shape[1]

        init_buffer(self._memory, pre_dims=(self._n_envs, self.N_STEPS, self._n_agents), **data)
        self._memory['traj_ret'] = np.zeros((self._n_envs, self.N_STEPS, self._n_agents), dtype=np.float32)
        self._memory['advantage'] = np.zeros((self._n_envs, self.N_STEPS, self._n_agents), dtype=np.float32)
        self._states = {}
        for k in self._state_keys:
            if k in data:
                self._memory[k] = self._states[k] = np.zeros(
                    (self._n_envs * self._n_agents, self.N_STEPS, data[k].shape[-1]), 
                    dtype=np.float32)

        print_buffer(self._memory)
        if self._inferred_sample_keys or getattr(self, '_sample_keys', None) is None:
            self._sample_keys = set(self._memory.keys()) - set(('discount', 'reward'))
            self._inferred_sample_keys = True
        self._non_state_keys = set(self._memory.keys()) - set(self._state_keys)
