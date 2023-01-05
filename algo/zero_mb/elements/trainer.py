from functools import partial
import jax

from core.log import do_logging
from core.elements.trainer import create_trainer
from core import optimizer
from algo.zero.elements.trainer import Trainer as TrainerBase


class Trainer(TrainerBase):
    def compile_train(self):
        _jit_train = jax.jit(self.theta_train)
        def jit_train(*args, **kwargs):
            self.rng, rng = jax.random.split(self.rng)
            return _jit_train(*args, rng=rng, **kwargs)
        self.jit_train = jit_train
        
        _jit_img_train = jax.jit(self.img_train)
        def jit_img_train(*args, **kwargs):
            self.rng, rng = jax.random.split(self.rng)
            return _jit_img_train(*args, rng=rng, **kwargs)
        self.jit_img_train = jit_img_train
        
        self.haiku_tabulate()

    def img_train(
        self, 
        theta, 
        rng, 
        opt_state, 
        data, 
    ):
        do_logging('train is traced', backtrack=4)
        if self.config.get('theta_opt'):
            theta, opt_state, stats = optimizer.optimize(
                self.loss.img_loss, 
                theta, 
                opt_state, 
                kwargs={
                    'rng': rng, 
                    'data': data, 
                }, 
                opt=self.opts.theta, 
                name='train/theta'
            )
        else:
            theta.value, opt_state.value, stats = optimizer.optimize(
                self.loss.img_value_loss, 
                theta.value, 
                opt_state.value, 
                kwargs={
                    'rng': rng, 
                    'data': data, 
                }, 
                opt=self.opts.value, 
                name='train/value'
            )
            theta.policy, opt_state.policy, stats = optimizer.optimize(
                self.loss.img_policy_loss, 
                theta.policy, 
                opt_state.policy, 
                kwargs={
                    'rng': rng, 
                    'data': data, 
                    'stats': stats
                }, 
                opt=self.opts.policy, 
                name='train/policy'
            )
        return theta, opt_state, stats

create_trainer = partial(create_trainer,
    name='zero', trainer_cls=Trainer
)
