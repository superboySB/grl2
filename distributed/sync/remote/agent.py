import threading
import ray

from .parameter_server import ParameterServer
from ..common.typing import ModelStats, ModelWeights
from core.elements.builder import ElementsBuilder
from core.elements.strategy import Strategy
from core.monitor import Monitor
from core.remote.base import RayBase
from utility.display import pwt


class Agent(RayBase):
    def __init__(
        self, 
        config: dict, 
        env_stats: dict, 
        parameter_server: ParameterServer=None,
        monitor: Monitor=None
    ):
        super().__init__(config['aid'], seed=config.get('seed'))

        self.aid = config['aid']
        self.parameter_server = parameter_server
        self.monitor = monitor

        self.builder: ElementsBuilder = ElementsBuilder(
            config=config, 
            env_stats=env_stats
        )
        self.config = self.builder.config
        elements = self.builder.build_training_strategy_from_scratch(
            build_monitor=False, 
            save_config=False
        )
        self.strategy: Strategy = elements.strategy
        self.buffer = elements.buffer

        self.train_signal = True

    """ Model Management """
    def get_model_path(self):
        return self.strategy.get_model_path()

    def set_model_weights(self, model_weights: ModelWeights):
        self.strategy.reset_model_path(model_weights.model)
        if model_weights.weights:
            self.strategy.set_weights(model_weights.weights)
        pwt('Set model to', model_weights.model, 'with weights', 
            None if model_weights.weights is None else list(model_weights.weights))

    """ Communications with Parameter Server """
    def publish_weights(self, wait=True):
        model_weights = ModelWeights(
            self.get_model_path(), 
            self.strategy.get_weights(aux_stats=False, train_step=True, env_step=False)
        )
        assert set(model_weights.weights) == set(['model', 'opt', 'train_step']), list(model_weights.weights)
        ids = self.parameter_server.update_strategy_weights.remote(
            self.aid, model_weights
        )
        if wait:
            ray.get(ids)

    """ Training """
    def start_training(self):
        self._training_thread = threading.Thread(target=self._training, daemon=True)
        self._training_thread.start()

    def _training(self):
        while self.train_signal:
            stats = self.strategy.train_record()
            if stats is None:
                # print('Training stopped due to no data being received in time')
                continue
            self.publish_weights()
            self._send_train_stats(stats)

        pwt('Training terminated')

    def stop_training(self):
        self.train_signal = False
        self._training_thread.join()

    def _send_train_stats(self, stats):
        stats['train_step'] = self.strategy.get_train_step()
        model_stats = ModelStats(self.get_model_path(), stats)
        self.monitor.store_train_stats.remote(model_stats)

    """ Data Management """
    # def merge_episode(self, train_step, episode, n):
    #     # print('merge', train_step, self.train_step, self.buffer.ready(), n, self.buffer.size(), self.buffer.max_size())
    #     if train_step != self.train_step:
    #         return False
    #     if self.buffer.ready():
    #         return True
    #     self.buffer.merge_episode(episode, n)
    #     return self.buffer.ready()
    
    def merge_data(self, rid, data, n):
        self.buffer.merge_data(rid, data, n)

    def is_buffer_ready(self):
        return self.buffer.ready()

    """ Implementations """
    def _wait(self, ids, wait=False):
        return ray.get(ids) if wait else ids