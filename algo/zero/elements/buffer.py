import collections
import random
import time
import logging
import numpy as np
from pathlib import Path


from core.log import do_logging
from utility.utils import config_attr, dict2AttrDict, moments, standardize
from replay.utils import load_data


logger = logging.getLogger(__name__)


def compute_nae(reward, discount, value, last_value, 
                gamma, mask=None, epsilon=1e-8):
    next_return = last_value
    traj_ret = np.zeros_like(reward)
    for i in reversed(range(reward.shape[1])):
        traj_ret[:, i] = next_return = (reward[:, i]
            + discount[:, i] * gamma * next_return)

    # Standardize traj_ret and advantages
    traj_ret_mean, traj_ret_var = moments(traj_ret)
    traj_ret_std = np.maximum(np.sqrt(traj_ret_var), 1e-8)
    value = standardize(value, mask=mask, epsilon=epsilon)
    # To have the same mean and std as trajectory return
    value = (value + traj_ret_mean) / traj_ret_std     
    advantage = standardize(traj_ret - value, mask=mask, epsilon=epsilon)
    traj_ret = standardize(traj_ret, mask=mask, epsilon=epsilon)

    return advantage, traj_ret

def compute_gae(reward, discount, value, last_value, gamma, 
                gae_discount, norm_adv=False, mask=None, epsilon=1e-8):
    if last_value is not None:
        last_value = np.expand_dims(last_value, 1)
        next_value = np.concatenate([value[:, 1:], last_value], axis=1)
    else:
        next_value = value[:, 1:]
        value = value[:, :-1]
    assert value.shape == next_value.shape, (value.shape, next_value.shape)
    advs = delta = (reward + discount * gamma * next_value - value)
    next_adv = 0
    for i in reversed(range(advs.shape[1])):
        advs[:, i] = next_adv = (delta[:, i] 
            + discount[:, i] * gae_discount * next_adv)
    traj_ret = advs + value
    if norm_adv:
        advs = standardize(advs, mask=mask, epsilon=epsilon)
    return advs, traj_ret

def compute_indices(idxes, mb_idx, mb_size, N_MBS):
    start = mb_idx * mb_size
    end = (mb_idx + 1) * mb_size
    mb_idx = (mb_idx + 1) % N_MBS
    curr_idxes = idxes[start: end]
    return mb_idx, curr_idxes

def reshape_to_sample(memory, n_envs, n_steps, sample_size):
    batch_size = n_envs * n_steps
    if sample_size is not None:
        batch_size //= sample_size
        leading_dims = (batch_size, -1)
    else:
        leading_dims = (batch_size,)
    memory = {k: v.reshape(*leading_dims, *v.shape[2:])
        for k, v in memory.items()}

    return memory


class PPOBuffer:
    def __init__(self, config):
        self.config = config_attr(self, config)
        self._add_attributes()

    def _add_attributes(self):
        self._use_dataset = getattr(self, '_use_dataset', False)
        if self._use_dataset:
            do_logging(f'Dataset is used for data pipline', logger=logger)

        self._sample_size = getattr(self, '_sample_size', None)
        self._state_keys = ['h', 'c']
        if self._sample_size:
            assert self._n_envs * self.N_STEPS % self._sample_size == 0, \
                f'{self._n_envs} * {self.N_STEPS} % {self._sample_size} != 0'
            size = self._n_envs * self.N_STEPS // self._sample_size
            do_logging(f'Sample size: {self._sample_size}', logger=logger)
        else:
            size = self._n_envs * self.N_STEPS
        self._size = size
        self._mb_size = size // self.N_MBS
        self._idxes = np.arange(size)
        self._shuffled_idxes = np.arange(size)
        self._gae_discount = self._gamma * self._lam
        self._memory = collections.defaultdict(list)
        self._is_store_shape = True
        self._inferred_sample_keys = False
        self._norm_adv = getattr(self, '_norm_adv', 'minibatch')
        self._epsilon = 1e-5
        if hasattr(self, 'N_VALUE_EPOCHS'):
            self.N_EPOCHS += self.N_VALUE_EPOCHS
        self.reset()
        do_logging(f'Batch size: {size}', logger=logger)
        do_logging(f'Mini-batch size: {self._mb_size}', logger=logger)

        self._sleep_time = 0.025
        self._sample_wait_time = 0
        self._epoch_idx = 0

    @property
    def batch_size(self):
        return self._mb_size

    def __getitem__(self, k):
        return self._memory[k]

    def __contains__(self, k):
        return k in self._memory
    
    def ready(self):
        return self._ready

    def reset(self):
        self._memory = collections.defaultdict(list)
        self._is_store_shape = True
        self._idx = 0
        self._mb_idx = 0
        self._epoch_idx = 0
        self._ready = False

    def add(self, **data):
        def add_data(data):
            for k, v in data.items():
                if isinstance(v, dict):
                    add_data(v)
                else:
                    self._memory[k].append(v)

        add_data(data)

        self._idx += 1

    def update(self, key, value, field='mb', mb_idxes=None):
        if field == 'mb':
            mb_idxes = self._curr_idxes if mb_idxes is None else mb_idxes
            self._memory[key][mb_idxes] = value
        elif field == 'all':
            assert self._memory[key].shape == value.shape, (self._memory[key].shape, value.shape)
            self._memory[key] = value
        else:
            raise ValueError(f'Unknown field: {field}. Valid fields: ("all", "mb")')

    def update_value_with_func(self, fn):
        assert self._mb_idx == 0, f'Unfinished sample: self._mb_idx({self._mb_idx}) != 0'
        mb_idx = 0

        for start in range(0, self._size, self._mb_size):
            end = start + self._mb_size
            curr_idxes = self._idxes[start:end]
            obs = self._memory['obs'][curr_idxes]
            if self._sample_size:
                state = tuple([self._memory[k][curr_idxes, 0] 
                    for k in self._state_keys])
                mask = self._memory['mask'][curr_idxes]
                value, state = fn(obs, state=state, mask=mask, return_state=True)
                self.update('value', value, mb_idxes=curr_idxes)
                next_idxes = curr_idxes + self._mb_size
                self.update('state', state, mb_idxes=next_idxes)
            else:
                value = fn(obs)
                self.update('value', value, mb_idxes=curr_idxes)
        
        assert mb_idx == 0, mb_idx

    def sample(self, sample_keys=None):
        if not self._ready:
            self._wait_to_sample()

        self._shuffle_indices()
        sample = self._sample(sample_keys)
        self._post_process_for_dataset()

        return sample

    """ For distributed training """
    def retrieve_all_data(self):
        assert self._idx == self.N_STEPS, (self._idx, self.N_STEPS)
        data = self._memory.copy()
        self.reset()
        return data

    def append_data(self, data):
        for k, v in data.items():
            self._memory[k].append(v)

    def stack_sequantial_memory(self):
        for k, v in self._memory.items():
            if len(v) == 1:
                self._memory[k] = v[0]
            else:
                self._memory[k] = np.stack(v, 1)
            if k == 'last_value':
                assert self._memory[k].shape[:2] == (self.config.n_envs,), (k, self._memory[k].shape)
            else:
                assert self._memory[k].shape[:2] == (self.config.n_envs, self.N_STEPS), (k, (self.config.n_envs, self.N_STEPS), self._memory[k].shape)

    def concat_batch_memory(self):
        for k, v in self._memory.items():
            self._memory[k] = np.concatenate(v)
            if k == 'last_value':
                assert self._memory[k].shape[:2] == (self.config.n_envs,), (k, self._memory[k].shape)
            else:
                assert self._memory[k].shape[:2] == (self.config.n_envs, self.N_STEPS), (k, self._memory[k].shape)

    """ Implementations """
    def _wait_to_sample(self):
        while not self._ready:
            time.sleep(self._sleep_time)
            self._sample_wait_time += self._sleep_time

    def _shuffle_indices(self):
        if self.N_MBS > 1 and self._mb_idx == 0:
            np.random.shuffle(self._shuffled_idxes)
        
    def _sample(self, sample_keys=None):
        sample_keys = sample_keys or self._sample_keys
        self._mb_idx, self._curr_idxes = compute_indices(
            self._shuffled_idxes, self._mb_idx, 
            self._mb_size, self.N_MBS)

        sample = self._get_sample(sample_keys, self._curr_idxes)
        sample = self._process_sample(sample)

        return sample

    def _get_sample(self, sample_keys, idxes):
        sample = {k: self._memory[k][idxes, 0]
            if k in self._state_keys else self._memory[k][idxes] 
            for k in sample_keys}
        action_rnn_dim = sample['action_h'].shape[-1]
        sample['action_h'] = sample['action_h'].reshape(-1, action_rnn_dim)
        sample['action_c'] = sample['action_c'].reshape(-1, action_rnn_dim)
        return sample

    def _process_sample(self, sample):
        if 'advantage' in sample and self._norm_adv == 'minibatch':
            sample['advantage'] = standardize(
                sample['advantage'], mask=sample.get('life_mask'), 
                epsilon=self._epsilon)
        return sample
    
    def _post_process_for_dataset(self):
        if self._mb_idx == 0:
            self._epoch_idx += 1
            if self._epoch_idx == self.N_EPOCHS:
                # resetting here is especially important 
                # if we use tf.data as sampling is done 
                # in a background thread
                self.reset()

    def finish(self, last_value=None):
        self.compute_advantage_return_in_memory(last_value)
        self.reshape_to_sample()

    def clear(self):
        self._memory = collections.defaultdict(list)
        self.reset()

    def reshape_to_sample(self):
        self._memory = reshape_to_sample(
            self._memory, self._n_envs, self.N_STEPS, self._sample_size)
        self._ready = True

    def compute_advantage_return_in_memory(self, last_value=None):
        if self._adv_type != 'vtrace' and last_value is None:
            last_value = self._memory.pop('last_value')
        if self._adv_type == 'nae':
            assert self._norm_adv == 'batch', self._norm_adv
            self._memory['advantage'], self._memory['traj_ret'] = \
                compute_nae(
                reward=self._memory['reward'], 
                discount=self._memory['discount'],
                value=self._memory['value'],
                last_value=last_value,
                gamma=self._gamma,
                mask=self._memory.get('life_mask'),
                epsilon=self._epsilon)
        elif self._adv_type == 'gae':
            self._memory['advantage'], self._memory['traj_ret'] = \
                compute_gae(
                reward=self._memory['reward'], 
                discount=self._memory['discount'],
                value=self._memory['value'],
                last_value=last_value,
                gamma=self._gamma,
                gae_discount=self._gae_discount,
                norm_adv=self._norm_adv == 'batch',
                mask=self._memory.get('life_mask'),
                epsilon=self._epsilon)
        elif self._adv_type == 'vtrace':
            pass
        else:
            raise NotImplementedError


class BCBuffer:
    def __init__(self, config):
        self.config = config_attr(self, config)
        self._dir = Path(config.dir)
        self._memory = {}
        self._filenames = []
        self._idx = 0
        self._random_indices = None
        self._memlen = None
        self._batch_trajs = self.config.batch_size // 4
        self._data_keys = {}

    def load_data(self):
        start = time.time()
        for filename in self._dir.glob('*.npz'):
            self._filenames.append(filename)

        self._memlen = len(self._filenames)
        while self._memlen % self._batch_trajs != 0:
            self._filenames.pop()
            self._memlen -= 1
        do_logging(f'{len(self._filenames)} filenames are loaded. Total loading time: {time.time() - start}', logger=logger)
        self._random_indices = np.arange(self._memlen)
        self.construct_data_template()

    def construct_data_template(self):
        filename = self._filenames[0]
        traj = load_data(filename)
        self._data_keys = [
            k for k in traj.keys() 
            if k != 'pid' and k != 'discount' and k != 'reward'
        ]

    def sample(self):
        if self._idx == 0:
            np.random.shuffle(self._random_indices)
        indices = self._random_indices[self._idx: self._idx + self._batch_trajs]
        filenames = [self._filenames[i] for i in indices]
    
        data = {k: [] for k in self._data_keys}

        for filename in filenames:
            # if filename in self._memory:
            #     traj = self._memory[filename]
            # else:
            #     traj = load_data(filename)
            #     self._memory[filename] = traj
            # To save some memory we do not store trajectories in memory
            traj = load_data(filename)
            while traj is None:
                filename = random.choice(self._filenames)
                traj = load_data(filename)
            for k, v in traj.items():
                if k in self._data_keys:
                    data[k].append(v)

        data = {k: np.concatenate(v, 0) for k, v in data.items()}

        self._idx = (self._idx + self._batch_trajs) % self._memlen
        
        return data


def create_buffer(config, central_buffer=False):
    config = dict2AttrDict(config)
    if central_buffer:
        assert config.training == 'ppo', config.training
        import ray
        RemoteBuffer = ray.remote(PPOBuffer)
        return RemoteBuffer.remote(config)
    elif config['training'] == 'ppo':
        return PPOBuffer(config)
    elif config['training'] == 'bc':
        return BCBuffer(config)
    else:
        raise ValueError(config['training'])
