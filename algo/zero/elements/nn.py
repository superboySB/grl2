from jax import lax, nn
import jax.numpy as jnp
import haiku as hk

from core.typing import dict2AttrDict
from nn.func import mlp, nn_registry
from nn.index import IndexModule
from nn.utils import get_activation
from jax_tools import jax_assert, jax_dist
""" Source this file to register Networks """


@nn_registry.register('policy')
class Policy(IndexModule):
    def __init__(
        self, 
        is_action_discrete, 
        action_dim, 
        out_act=None, 
        init_std=1, 
        out_scale=.01, 
        name='policy', 
        **config
    ):
        self.action_dim = action_dim
        self.is_action_discrete = is_action_discrete
        self.out_act = out_act
        self.init_std = init_std

        config['out_scale'] = out_scale
        super().__init__(config=config, out_size=self.action_dim , name=name)

    def __call__(self, x, hx=None, action_mask=None):
        x = super().__call__(x, hx)

        if self.is_action_discrete:
            if action_mask is not None:
                jax_assert.assert_shape_compatibility([x, action_mask])
                x = jnp.where(action_mask, x, -1e10)
            return x
        else:
            if self.out_act == 'tanh':
                x = jnp.tanh(x)
            logstd_init = hk.initializers.Constant(lax.log(self.init_std))
            logstd = hk.get_parameter(
                'logstd', 
                shape=(self.action_dim,), 
                init=logstd_init
            )
            return x, logstd


@nn_registry.register('value')
class Value(IndexModule):
    def __init__(
        self, 
        out_act=None, 
        out_size=1, 
        name='value', 
        **config
    ):
        self.out_act = get_activation(out_act)
        super().__init__(config=config, out_size=out_size, name=name)

    def __call__(self, x, hx=None):
        value = super().__call__(x, hx)

        if value.shape[-1] == 1:
            value = jnp.squeeze(value, -1)
        value = self.out_act(value)
        return value


@nn_registry.register('model')
class Model(hk.Module):
    def __init__(
        self, 
        out_size, 
        max_logvar, 
        min_logvar, 
        name='model', 
        **config, 
    ):
        super().__init__(name=name)
        self.config = dict2AttrDict(config, to_copy=True)
        self.out_size = out_size
        self.max_logvar = max_logvar
        self.min_logvar = min_logvar

    def __call__(self, x, action):
        net = self.build_net()
        x = jnp.concatenate([x, action], -1)

        x = net(x)
        mean, logvar = compute_mean_logvar(
            x, self.max_logvar, self.min_logvar)
        logstd = logvar / 2
        dist = jax_dist.MultivariateNormalDiag(mean, logstd)

        return dist

    @hk.transparent
    def build_net(self):
        net = mlp(
            **self.config,
            out_size=2 * self.out_size
        )
        return net


@nn_registry.register('emodels')
class EnsembleModels(hk.Module):
    def __init__(
        self, 
        n, 
        out_size, 
        max_logvar, 
        min_logvar, 
        name='emodels', 
        **config
    ):
        super().__init__(name=name)
        self.config = dict2AttrDict(config, to_copy=True)
        self.n = n
        self.out_size = out_size
        self.max_logvar = max_logvar
        self.min_logvar = min_logvar

    def __call__(self, x, action):
        nets = self.build_net()
        x = jnp.concatenate([x, action], -1)

        x = jnp.stack([net(x) for net in nets], -2)
        mean, logvar = compute_mean_logvar(
            x, self.max_logvar, self.min_logvar)
        logstd = logvar / 2
        dist = jax_dist.MultivariateNormalDiag(mean, logstd)

        return dist

    @hk.transparent
    def build_net(self):
        nets = [mlp(
            **self.config,
            out_size=2 * self.out_size, 
            name=f'model{i}'
        ) for i in range(self.n)]
        return nets


def compute_mean_logvar(x, max_logvar, min_logvar):
    mean, logvar = jnp.split(x, -1)
    logvar = max_logvar - nn.softplus(max_logvar - logvar)
    logvar = min_logvar + nn.softplus(logvar - min_logvar)

    return mean, logvar


if __name__ == '__main__':
    import jax
    # config = dict( 
    #     w_init='orthogonal', 
    #     scale=1, 
    #     activation='relu', 
    #     norm='layer', 
    #     out_scale=.01, 
    #     out_size=2
    # )
    # def layer_fn(x, *args):
    #     layer = HyperParamEmbed(**config)
    #     return layer(x, *args)
    # import jax
    # rng = jax.random.PRNGKey(42)
    # x = jax.random.normal(rng, (2, 3))
    # net = hk.transform(layer_fn)
    # params = net.init(rng, x, 1, 2, 3.)
    # print(params)
    # print(net.apply(params, None, x, 1., 2, 3))
    # print(hk.experimental.tabulate(net)(x, 1, 2, 3.))
    import os, sys
    os.environ["XLA_FLAGS"] = '--xla_dump_to=/tmp/foo'
    os.environ['XLA_FLAGS'] = "--xla_gpu_force_compilation_parallelism=1"

    config = {
        'units_list': [3], 
        'w_init': 'orthogonal', 
        'activation': 'relu', 
        'norm': None, 
        'out_scale': .01,
    }
    def layer_fn(x, *args):
        layer = EnsembleModels(5, 3, **config)
        return layer(x, *args)
    import jax
    rng = jax.random.PRNGKey(42)
    x = jax.random.normal(rng, (2, 3, 3))
    a = jax.random.normal(rng, (2, 3, 2))
    net = hk.transform(layer_fn)
    params = net.init(rng, x, a)
    print(params)
    print(net.apply(params, rng, x, a))
    print(hk.experimental.tabulate(net)(x, a))

    # config = {
    #     'units_list': [64,64], 
    #     'w_init': 'orthogonal', 
    #     'activation': 'relu', 
    #     'norm': None, 
    #     'index': 'all', 
    #     'index_config': {
    #         'use_shared_bias': False, 
    #         'use_bias': True, 
    #         'w_init': 'orthogonal', 
    #     }
    # }
    # def net_fn(x, *args):
    #     net = Value(**config)
    #     return net(x, *args)

    # rng = jax.random.PRNGKey(42)
    # x = jax.random.normal(rng, (2, 3, 4))
    # hx = jnp.eye(3)
    # hx = jnp.tile(hx, [2, 1, 1])
    # net = hk.transform(net_fn)
    # params = net.init(rng, x, hx)
    # print(params)
    # print(net.apply(params, rng, x, hx))
    # print(hk.experimental.tabulate(net)(x, hx))

    # config = {
    #     'units_list': [2, 3], 
    #     'w_init': 'orthogonal', 
    #     'activation': 'relu', 
    #     'norm': None, 
    #     'out_scale': .01,
    #     'rescale': .1, 
    #     'out_act': 'atan', 
    #     'combine_xa': True, 
    #     'out_size': 3, 
    #     'index': 'all', 
    #     'index_config': {
    #         'use_shared_bias': False, 
    #         'use_bias': True, 
    #         'w_init': 'orthogonal', 
    #     }
    # }
    # def net_fn(x, *args):
    #     net = Reward(**config)
    #     return net(x, *args)

    # rng = jax.random.PRNGKey(42)
    # x = jax.random.normal(rng, (2, 3, 4))
    # action = jax.random.randint(rng, (2, 3), minval=0, maxval=3)
    # hx = jnp.eye(3)
    # hx = jnp.tile(hx, [2, 1, 1])
    # net = hk.transform(net_fn)
    # params = net.init(rng, x, action, hx)
    # print(params)
    # print(net.apply(params, rng, x, action, hx))
    # print(hk.experimental.tabulate(net)(x, action, hx))
