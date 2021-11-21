import tensorflow as tf
from tensorflow.keras import layers, activations, initializers, regularizers, constraints
from tensorflow.keras.mixed_precision import global_policy

from core.module import Module
from nn.registry import rnn_registry
from nn.typing import GRUState
from utility.tf_utils import assert_rank


rnn_registry.register('gru')(layers.GRU)


class MGRUCell(layers.Layer):
    def __init__(self,
                 units,
                 activation='tanh',
                 recurrent_activation='sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 unit_update_bias=True,
                 use_ln=False,
                 kernel_regularizer=None,
                 recurrent_regularizer=None,
                 bias_regularizer=None,
                 kernel_constraint=None,
                 recurrent_constraint=None,
                 bias_constraint=None,
                 dropout=0.,
                 recurrent_dropout=0.,
                 implementation=1,
                 **kwargs):
        super().__init__(**kwargs)
        self.units = units
        self.activation = activations.get(activation)
        self.recurrent_activation = activations.get(recurrent_activation)
        self.use_bias = use_bias
        self.use_ln = use_ln

        self.kernel_initializer = initializers.get(kernel_initializer)
        self.recurrent_initializer = initializers.get(recurrent_initializer)
        self.bias_initializer = initializers.get(bias_initializer)
        self.unit_update_bias = unit_update_bias

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.state_size = GRUState(h=self.units)
        self.output_size = self.units

    def build(self, input_shapes):
        input_dim = input_shapes[0][-1]
        self.kernel = self.add_weight(
            shape=(input_dim + self.state_size[0], self.units * 3),
            name='kernel',
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            constraint=self.kernel_constraint)
        # self.recurrent_kernel = self.add_weight(
        #     shape=(self.units, self.units * 3),
        #     name='recurrent_kernel',
        #     initializer=self.recurrent_initializer,
        #     regularizer=self.recurrent_regularizer,
        #     constraint=self.recurrent_constraint)

        if self.use_bias:
            if self.unit_update_bias:
                def bias_initializer(_, *args, **kwargs):
                    return tf.concat([
                      self.bias_initializer((self.units * 2,), *args, **kwargs),
                      -initializers.Ones()((self.units,), *args, **kwargs),
                    ], -1)
            else:
                bias_initializer = self.bias_initializer
            self.bias = self.add_weight(
                shape=(self.units * 3,),
                name='bias',
                initializer=bias_initializer,
                regularizer=self.bias_regularizer,
                constraint=self.bias_constraint)
        else:
            self.bias = None

        if self.use_ln:
            self.x_ln = layers.LayerNormalization(name='x_ln')
            # self.h_ln = layers.LayerNormalization(name='h_ln')
        else:
            self.x_ln = lambda x: x
            # self.h_ln = lambda x: x

    def call(self, x, states):
        x, mask, filter = tf.nest.flatten(x)
        h_0 = states[0]
        assert_rank([x, mask, filter, h_0], 2)
        if mask is not None:
            h_0 = h_0 * mask
        
        # it sigfinicantly increases the running time when separate normalizations are applied to x and h
        x = self.x_ln(tf.matmul(tf.concat([x, h_0], -1), self.kernel))
        # x = self.x_ln(tf.matmul(x, self.kernel)) + self.h_ln(tf.matmul(h, self.recurrent_kernel))
        if self.use_bias:
            x = tf.nn.bias_add(x, self.bias)
        r, c, z = tf.split(x, 3, 1)
        r, z = self.recurrent_activation(r), self.recurrent_activation(z)
        c = self.activation(c)
        h = z * c + (1-z) * h_0
        x = h

        h = tf.where(filter, h, h_0)

        return x, GRUState(h)
    
    def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
        state_size = self.state_size
        if inputs is not None:
            assert batch_size is None or batch_size == tf.shape(inputs)[0]
            batch_size = tf.shape(inputs)[0]
        if dtype is None:
            dtype = global_policy().compute_dtype
        return GRUState(h=tf.zeros([batch_size, state_size[0]], dtype))


@rnn_registry.register('mgru')
class MGRU(Module):
    def __init__(self, name='mgru', **config):
        super().__init__(name=name)
        config = config.copy()
        self._state_mask = config.pop('state_mask', True)
        cell = MGRUCell(**config)
        self._rnn = layers.RNN(cell, return_sequences=True, return_state=True)
        self.state_type = GRUState

    def call(self, x, state, mask=None, filter=None, additional_input=[]):
        xs = [x] + additional_input
        mask = tf.ones((*x.shape[:2], 1), dtype=tf.float32) \
            if mask is None else tf.expand_dims(mask, axis=-1)
        filter = tf.ones_like(mask, dtype=tf.bool) \
            if filter is None else tf.expand_dims(filter, axis=-1)
        assert_rank(xs + [mask], 3)
        if not self._state_mask:
            # mask out inputs
            for i, v in enumerate(xs):
                xs[i] *= tf.cast(mask, v.dtype)
        x = tf.concat(xs, axis=-1) if len(xs) > 1 else xs[0]
        if not mask.dtype.is_compatible_with(global_policy().compute_dtype):
            mask = tf.cast(mask, global_policy().compute_dtype)
        x = self._rnn((x, mask), initial_state=state)
        x, state = x[0], GRUState(x[1])
        return x, state

    def reset_states(self, states=None):
        self._rnn.reset_states(states)

    def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
        if inputs is None:
            assert batch_size is not None
            inputs = tf.zeros([batch_size, 1, 1])
        return GRUState(*self._rnn.cell.get_initial_state(inputs, dtype=dtype))

    @property
    def state_size(self):
        return self._rnn.cell.state_size

    @property
    def state_keys(self):
        return GRUState(*GRUState._fields)


if __name__ == '__main__':
    from utility.timer import timeit
    # inputs
    shape = (32, 16, 256)
    x0 = tf.random.normal(shape)
    m = tf.random.uniform(shape[:2], 0, 2, dtype=tf.int32)
    em = tf.cast(m[..., None], tf.float32)
    run_times = 1000
    assert x0.shape.ndims == em.shape.ndims
    # keras lstm
    c = tf.keras.layers.GRUCell(256)
    l = tf.keras.layers.RNN(c, return_sequences=True, return_state=True)
    opt = tf.keras.optimizers.Adam(5e-5)
    lv = l.variables

    # def keras_gru_call():
    #     for _ in range(run_times):
    #         with tf.GradientTape() as tape:
    #             x = l(x0, initial_state=None)
    #             x, s = x[0], x[1:]
    #             y = tf.ones_like(x)
    #             loss = tf.reduce_mean((y-x)**2)
    #         gs = tape.gradient(loss, lv)
    #         opt.apply_gradients(zip(gs, lv))

    # timeit(keras_gru_call, to_print=True)

    # # custom lstm
    # c = MGRUCell(256)
    # l = tf.keras.layers.RNN(c, return_sequences=True, return_state=True)
    # opt = tf.keras.optimizers.Adam(5e-5)

    # def custom_gru_cell_call():
    #     for _ in range(run_times):
    #         with tf.GradientTape() as tape:
    #             x = l((x0, em), initial_state=None)
    #             x, s = x[0], x[1:]
    #             y = tf.ones_like(x)
    #             loss = tf.reduce_mean((y-x)**2)
    #         gs = tape.gradient(loss, lv)
    #         opt.apply_gradients(zip(gs, lv))
    
    # timeit(custom_gru_cell_call, to_print=True)

    # l = MGRU({'units': 256})
    # opt = tf.keras.optimizers.Adam(5e-5)

    # def custom_gru_call():
    #     for _ in range(run_times):
    #         with tf.GradientTape() as tape:
    #             x, s = l(x0, None, m)
    #             y = tf.ones_like(x)
    #             loss = tf.reduce_mean((y-x)**2)
    #         gs = tape.gradient(loss, lv)
    #         opt.apply_gradients(zip(gs, lv))
    
    # timeit(custom_gru_call, to_print=True)

    c = MGRUCell(256, use_ln=True)
    l = tf.keras.layers.RNN(c, return_sequences=True, return_state=True)
    opt = tf.keras.optimizers.Adam(5e-5)

    def custom_gruln_call():
        for _ in range(run_times):
            with tf.GradientTape() as tape:
                x = l((x0, em), initial_state=None)
                x, s = x[0], x[1:]
                y = tf.ones_like(x)
                loss = tf.reduce_mean((y-x)**2)
            gs = tape.gradient(loss, lv)
            opt.apply_gradients(zip(gs, lv))
    
    timeit(custom_gruln_call, to_print=True)

