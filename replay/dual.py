import numpy as np

from utility.utils import to_int
from replay.base import Replay
from replay.uniform import UniformReplay
from replay.per import ProportionalPER


class DualReplay(Replay):
    def __init__(self, config):
        self._type = config['type']
        self.capacity = to_int(config['capacity'])
        self.min_size = to_int(config['min_size'])
        self.batch_size = config['batch_size']

        BufferType = ProportionalPER if self._type.endswith('proportional') else UniformReplay
        config['type'] = 'proportional' if self._type.endswith('proportional') else 'Uniform'
        config['capacity'] = int(self.capacity * config['cap_frac'])
        config['min_size'] = self.min_size
        config['batch_size'] = int(self.batch_size * config['bs_frac'])
        print(f'Fast replay capacity({config["capacity"]})')
        print(f'Fast replay batch size({config["batch_size"]})')
        self.fast_replay = BufferType(config)
        
        config['capacity'] = self.capacity - config['capacity']
        config['min_size'] = self.min_size - config['min_size']
        config['batch_size'] = self.batch_size - config['batch_size']
        print(f'Slow replay capacity({config["capacity"]})')
        print(f'Slow replay batch size({config["batch_size"]})')
        self.slow_replay = BufferType(config)

    def buffer_type(self):
        return self._type
        
    def good_to_learn(self):
        return self.fast_replay.good_to_learn()

    def __len__(self):
        return self.capacity if self.is_full else len(self.fast_replay) + len(self.fast_replay)

    def sample(self, batch_size=None):
        assert self.good_to_learn()
        batch_size = batch_size or self.batch_size
        if self.slow_replay.good_to_learn():
            regular_samples = self.fast_replay.sample()
            additional_samples = self.slow_replay.sample()
            return self.combine_samples(regular_samples, additional_samples)
        else:
            regular_samples = self.fast_replay.sample(batch_size)
            return regular_samples

    def combine_samples(self, samples1, samples2):
        samples = {}
        assert len(samples1) == len(samples2)
        for k in samples1.keys():
            samples[k] = np.concatenate([samples1[k], samples2[k]])
            assert samples[k].shape[0] == self.batch_size

        return samples

    def merge(self, local_buffer, length, dest_replay):
        if dest_replay == 'fast_replay':
            self.fast_replay.merge(local_buffer, length)
        elif dest_replay == 'slow_replay':
            self.slow_replay.merge(local_buffer, length)
        else:
            raise NotImplementedError
