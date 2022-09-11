from core.elements.trainer import TrainerBase
from core.log import do_logging
from core.typing import AttrDict, dict2AttrDict
from tools.timer import Timer


class TrainingLoop:
    def __init__(
        self, 
        config: AttrDict, 
        dataset, 
        trainer: TrainerBase, 
        **kwargs
    ):
        self.config = dict2AttrDict(config)
        self.dataset = dataset
        self.trainer = trainer
        self.model = self.trainer.model
        self.rng = self.model.rng

        for k, v in kwargs.items():
            setattr(self, k, v)

        self.post_init()

    def post_init(self):
        pass

    def train(self, step):
        self._before_train(step)
        train_step, stats = self._train()
        self._after_train()

        return train_step, stats

    def _before_train(self, step):
        pass

    def _train(self):
        data = self._sample_data()
        if data is None:
            return 0, None
        
        with Timer('train'):
            stats = self.trainer.train(data)
        n = self.trainer.config.n_epochs * self.trainer.config.n_mbs
        
        return n, stats

    def _after_train(self):
        pass

    def _sample_data(self):
        with Timer('sample'):
            data = self.dataset.sample()
        if data is None:
            return None
        data.setdefault('global_state', data.obs)
        if 'next_obs' in data:
            data.setdefault('next_global_state', data.next_obs)
        return data
