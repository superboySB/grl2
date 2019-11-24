from abc import ABC
import threading
import numpy as np

from utility.display import assert_colorize
from utility.display import pwc
from utility.utils import to_int
from utility.run_avg import RunningMeanStd
from buffer.replay.utils import add_buffer, copy_buffer


class Replay(ABC):
    """ Interface """
    def __init__(self, config, state_shape, action_dim, gamma):
        self.memory = {}

        # params for general replay buffer
        self.type = config['type']
        self.capacity = to_int(config['capacity'])
        self.min_size = to_int(config['min_size'])
        self.batch_size = config['batch_size']
        self.n_steps = config['n_steps']

        # reward hacking
        self.reward_scale = config.get('reward_scale', 1)
        self.reward_clip = config.get('reward_clip')
        self.normalize_reward = config.get('normalize_reward')
        if self.normalize_reward:
            self.running_reward_stats = RunningMeanStd()
        self.gamma = gamma
        
        self.is_full = False
        self.mem_idx = 0
        
        # locker used to avoid conflict introduced by tf.data.Dataset and multiple workers
        self.locker = threading.Lock()

    @property
    def good_to_learn(self):
        return len(self) >= self.min_size

    def __len__(self):
        return self.capacity if self.is_full else self.mem_idx

    def __call__(self):
        while True:
            yield self.sample()

    def sample(self):
        assert_colorize(self.good_to_learn, 'There are not sufficient transitions to start learning --- '
                                            f'transitions in buffer: {len(self)}\t'
                                            f'minimum required size: {self.min_size}')
        with self.locker:
            samples = self._sample()

        return samples

    def merge(self, local_buffer, length):
        """ Merge a local buffer to the replay buffer, useful for distributed algorithms """
        assert_colorize(length < self.capacity, 
                    f'Local buffer cannot be largeer than the replay: {length} vs. {self.capacity}')
        with self.locker:
            self._merge(local_buffer, length)

    def add(self):
        """ Add a single transition to the replay buffer """
        raise NotImplementedError

    """ Implementation """
    def _add(self, state, action, reward, done):
        """ add is only used for single agent, no multiple adds are expected to run at the same time
            but it may fight for resource with self.sample if background learning is enabled """
        if self.n_steps > 1:
            add_buffer(self.tb, self.tb_idx, state, action, reward, 
                        done, self.n_steps, self.gamma)
            
            if not self.tb_full and self.tb_idx == self.tb_capacity - 1:
                self.tb_full = True
            self.tb_idx = (self.tb_idx + 1) % self.tb_capacity

            if done:
                # flush all elements in temporary buffer to memory if an episode is done
                self.merge(self.tb, self.tb_idx or self.tb_capacity)
                assert (self.tb_capacity if self.tb_full else self.tb_idx) == (self.tb_idx or self.tb_capacity)
                self.tb_full = False
                self.tb_idx = 0
            elif self.tb_full:
                # add ready experiences in temporary buffer to memory
                n_not_ready = self.n_steps - 1
                n_ready = self.tb_capacity - n_not_ready
                assert self.tb_idx == 0
                self.merge(self.tb, n_ready)
                copy_buffer(self.tb, 0, n_not_ready, self.tb, self.tb_capacity - n_not_ready, self.tb_capacity)
                self.tb_idx = n_not_ready
                self.tb_full = False
        else:
            with self.locker:
                add_buffer(self.memory, self.mem_idx, state, action, reward,
                            done, self.n_steps, self.gamma)
                self.mem_idx = (self.mem_idx + 1) % self.capacity

    def _sample(self):
        raise NotImplementedError

    def _merge(self, local_buffer, length):
        end_idx = self.mem_idx + length

        if end_idx > self.capacity:
            first_part = self.capacity - self.mem_idx
            second_part = length - first_part
            
            copy_buffer(self.memory, self.mem_idx, self.capacity, local_buffer, 0, first_part)
            copy_buffer(self.memory, 0, second_part, local_buffer, first_part, length)
        else:
            copy_buffer(self.memory, self.mem_idx, end_idx, local_buffer, 0, length)
            
        if self.normalize_reward:
            # compute running reward statistics
            self.running_reward_stats.update(local_buffer['reward'][:length])

        # memory is full, recycle buffer via FIFO
        if not self.is_full and end_idx >= self.capacity:
            pwc('Memory is full', color='blue')
            self.is_full = True
        
        self.mem_idx = end_idx % self.capacity

    def _get_samples(self, indexes):
        indexes = np.asarray(indexes) # convert tuple to array
        
        state = self.memory['state'][indexes] 
        action = self.memory['action'][indexes]
        reward = np.copy(self.memory['reward'][indexes])
        done = self.memory['done'][indexes]
        steps = self.memory['steps'][indexes]
        
        # squeeze steps since it is of shape [None, 1]
        next_indexes = (indexes + np.squeeze(steps)) % self.capacity
        assert indexes.shape == next_indexes.shape
        # using zero state as the terminal state
        next_state = np.where(done, np.zeros_like(state), self.memory['state'][next_indexes])

        # process rewards
        if self.normalize_reward:
            reward = self.running_reward_stats.normalize(reward)
        reward *= np.where(done, 1, self.reward_scale)
        if self.reward_clip:
            reward = np.clip(reward, -self.reward_clip, self.reward_clip)
        
        return (
            state,
            action,
            reward,
            next_state,
            done,
            steps,
        )
