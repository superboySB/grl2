from collections import namedtuple
import tensorflow as tf
from tensorflow.keras import layers

from core.module import Module
from nn.registry import block_registry, subsample_registry
from utility.tf_utils import get_stoch_state

State = namedtuple("State", ["deter", 'mean', 'std', 'stoch'])


@block_registry.register('ds')
class DeterStoch(Module):
    def __init__(self, 
                 *,
                 block='resv1',
                 block_kwargs=dict(
                    filter_coefs=[],
                    kernel_sizes=[3, 3],
                    norm=None,
                    norm_kwargs={},
                    activation='relu',
                    am='se',
                    am_kwargs={},
                    dropout_rate=0.,
                    rezero=False,
                 ),
                 min_std=.1,
                 name='ds'):
        super().__init__(name=name)

        self._block = block
        self._block_kwargs = block_kwargs
        self._min_std = .1

    def build(self, input_shape):
        block_cls = block_registry.get(self._block)
        
        prefix = f'{self.scope_name}/'

        self._deter_layer = block_cls(name=f'{prefix}deter/{self._block}', **self._block_kwargs)
        self._stoch_layer = block_cls(name=f'{prefix}stoch/{self._block}', **self._block_kwargs)
        self._concat = layers.Concatenate(axis=-1, name=prefix+'concat')

    def call(self, x, training=False):
        deter = self._deter_layer(x, training=training)
        stoch = self._stoch_layer(x, training=training)
        mean, std, stoch = get_stoch_state(stoch, min_std=self._min_std)

        self.state = State(deter, mean, std, stoch)
        x = self._concat([deter, stoch])
        return x

@block_registry.register('dsl')
class DeterStochLarge(Module):
    def __init__(self,
                 *, 
                 n_blocks,
                 subsample='strided_resv1',
                 subsample_kwargs={},
                 block='resv1',
                 block_kwargs=dict(
                    filter_coefs=[],
                    kernel_sizes=[3, 3],
                    norm=None,
                    norm_kwargs={},
                    activation='relu',
                    am='se',
                    am_kwargs={},
                    dropout_rate=0.,
                    rezero=False,
                 ),
                 min_std=.1,
                 name='ds'):
        super().__init__(name=name)

        self._n_blocks = n_blocks
        self._subsample = subsample
        self._subsample_kwargs = subsample_kwargs.copy()
        self._block = block
        self._block_kwargs = block_kwargs.copy()
        self._min_std = min_std

    def build(self, input_shape):
        block_cls = block_registry.get(self._block)
        subsample_cls = subsample_registry.get(self._subsample)
        
        prefix = f'{self.scope_name}/'
        subsample_kwargs = self._subsample_kwargs.copy()

        subsample_kwargs['filters'] //= 2
        self._deter_layers = [
            subsample_cls(name=f'{prefix}deter/{self._subsample}', **subsample_kwargs)] \
            + [block_cls(name=f'{prefix}deter/{self._block}_{i}', **self._block_kwargs)
                for i in range(self._n_blocks)]
        subsample_kwargs['filters'] *= 2
        self._stoch_layers = [
            subsample_cls(name=f'{prefix}stoch/{self._subsample}', **subsample_kwargs)] \
            + [block_cls(name=f'{prefix}stoch/{self._block}_{i}', **self._block_kwargs)
                for i in range(self._n_blocks)]
        self._concat = layers.Concatenate(axis=-1, name=prefix+'concat')

    def call(self, x, training=False):
        deter = x
        for l in self._deter_layers:
            deter = l(deter, training=training)
        stoch = x
        for l in self._stoch_layers:
            stoch = l(stoch, training=training)
        mean, std, stoch = get_stoch_state(stoch, self._min_std)

        self.state = State(deter, mean, std, stoch)
        x = self._concat([deter, stoch])
        return x
