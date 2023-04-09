from core.typing import AttrDict
from core.elements.trainloop import TrainingLoop as TrainingLoopBase
from tools.display import print_dict_info
from tools.utils import prefix_name
from tools.timer import timeit


class TrainingLoop(TrainingLoopBase):
    def _train(self):
        obs_rms = self.buffer.get_obs_rms()
        self.model.update_obs_rms(obs_rms)

        n = 0
        stats = {}
        if self.config.ergodic:
            for data in self.buffer.ergodic_sample(n=self.config.training_data_size):
                stats = self._train_with_data(data)
                n += 1
        else:
            data = self.sample_data()
            if data is None:
                return 0, AttrDict()
            stats = self._train_with_data(data)
            n += 1
        return n, stats

    @timeit
    def valid_stats(self):
        data = self.buffer.range_sample(0, self.config.valid_data_size)
        data = self.trainer.process_data(data)

        _, stats = self.trainer.loss.loss(
            self.model.theta, 
            self.rng, 
            data
        )

        stats = prefix_name(stats, 'dynamics/valid')
        return stats
