import numpy as np

from utility.decorators import override
from buffer.replay.basic_replay import Replay
from buffer.replay.utils import init_buffer


class UniformReplay(Replay):
    """ Interface """
    def __init__(self, args, state_shape, action_dim, gamma):
        super().__init__(args, state_shape, action_dim, gamma)

        init_buffer(self.memory, self.capacity, state_shape, action_dim, False)

        # Code for single agent
        if self.n_steps > 1:
            self.tb_capacity = args['tb_capacity']
            self.tb_idx = 0
            self.tb_full = False
            self.tb = {}
            init_buffer(self.tb, self.tb_capacity, state_shape, action_dim, False)

    @override(Replay)
    def add(self, state, action, reward, done):
        super()._add(state, action, reward, done)

    """ Implementation """
    @override(Replay)
    def _sample(self):
        size = self.capacity if self.is_full else self.mem_idx
        indexes = np.random.randint(0, size, self.batch_size)
        
        samples = self._get_samples(indexes)

        return samples
