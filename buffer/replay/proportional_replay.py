import numpy as np
import ray

from utility.decorators import override
from buffer.replay.ds.sum_tree import SumTree
from buffer.replay.prioritized_replay import PrioritizedReplay


class ProportionalPrioritizedReplay(PrioritizedReplay):
    """ Interface """
    def __init__(self, config, state_shape, action_dim, gamma):
        super().__init__(config, state_shape, action_dim, gamma)
        self.data_structure = SumTree(self.capacity)        # mem_idx    -->     priority

    """ Implementation """
    @override(PrioritizedReplay)
    def _sample(self):
        total_priorities = self.data_structure.total_priorities
        
        segment = total_priorities / self.batch_size

        priorities, indexes = list(zip(*[self.data_structure.find(np.random.uniform(i * segment, (i+1) * segment))
                                        for i in range(self.batch_size)]))

        priorities = np.array(priorities)
        probabilities = priorities / total_priorities

        # compute importance sampling ratios
        IS_ratios = self._compute_IS_ratios(probabilities)
        samples = self._get_samples(indexes)
        
        return IS_ratios, indexes, samples
