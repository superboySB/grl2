import haiku as hk

from nn.registry import layer_registry
from nn.utils import get_initializer, get_activation, call_norm, calculate_scale


@layer_registry.register('layer')
class Layer:
    def __init__(
        self, 
        *args, 
        layer_type='linear', 
        norm=None, 
        activation=None, 
        w_init='glorot_uniform', 
        b_init='zeros', 
        name=None, 
        norm_after_activation=False, 
        norm_kwargs={
            'axis': -1, 
            'create_scale': True, 
            'create_offset': True, 
        }, 
        **kwargs):
        self.name = name or layer_type
        self.layer_cls = layer_registry.get(layer_type)
        self.layer_args = args
        scale = kwargs.pop('scale', calculate_scale(activation))
        self.w_init = get_initializer(w_init, scale=scale)
        self.b_init = get_initializer(b_init)
        self.layer_kwargs = kwargs

        self.norm = norm
        self.norm_kwargs = norm_kwargs
        self._norm_after_activation = norm_after_activation
        self.activation = get_activation(activation)

    def __call__(self, x, is_training=True, **kwargs):
        if self.layer_args:
            x = self.layer_cls(
                *self.layer_args, 
                w_init=self.w_init, 
                b_init=self.b_init, 
                name=self.name, 
                **self.layer_kwargs
            )(x)
        
        if not self._norm_after_activation:
            x = call_norm(self.norm, self.norm_kwargs, x, is_training=is_training)
        if self.activation is not None:
            x = self.activation(x)
        if self._norm_after_activation:
            x = call_norm(self.norm, self.norm_kwargs, x, is_training=is_training)

        return x

layer_registry.register('linear')(hk.Linear)

if __name__ == '__main__':
    import jax
    def f(x):
        def l(x):
            layer = Layer(3, w_init='orthogonal', scale=.01, 
                activation='relu', norm='layer', name='layer')
            return layer(x)
        mlp = hk.transform(l)
        rng = jax.random.PRNGKey(42)
        params = mlp.init(rng, x)
        return params, mlp
    rng = jax.random.PRNGKey(42)
    x = jax.random.normal(rng, (2, 3)) 
    params, mlp = f(x)
    print(params)
    print(mlp.apply(params, None, x))
    print(hk.experimental.tabulate(mlp)(x))
    def g(params, x):
        rng = jax.random.PRNGKey(42)
        y = mlp.apply(params, rng, x)
        return y
    import graphviz
    dot = hk.experimental.to_dot(g)(params, x)
    graphviz.Source(dot)