from core.elements.model import Model, Ensemble
from core.typing import AttrDict, dict2AttrDict


class Loss:
    def __init__(
        self,
        *,
        config: AttrDict,
        model: Model,
        name: str
    ):
        self.config = config
        self.name = name

        self.model = model
        self.params = self.model.params
        self.modules = model.modules
        self.rng = self.model.rng
        self.post_init()

    def loss(self):
        raise NotImplementedError

    def post_init(self):
        """ Add some additional attributes and do some post processing here """
        pass

    def log_for_debug(self, tape, stats, debug=True, **data):
        if debug and self.config.get('debug', True):
            with tape.stop_recording():
                stats.update(data)


class LossEnsemble(Ensemble):
    def __init__(
        self, 
        *, 
        config: AttrDict, 
        components=None, 
        name, 
    ):
        super().__init__(
            config=config,
            components=components, 
            name=name,
        )
        self.model = dict2AttrDict({
            k: v.model for k, v in components.items()
        }, shallow=True)
