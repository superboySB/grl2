from collections import namedtuple
import tensorflow as tf
from tensorflow.keras import layers, activations, initializers, regularizers, constraints
from tensorflow.keras.mixed_precision.experimental import global_policy

from core.module import Module
from utility.tf_utils import assert_rank

LSTMState = namedtuple('LSTMState', ['h', 'c'])

class LSTMCell(layers.Layer):
    def __init__(self,
                 units,
                 activation='tanh',
                 recurrent_activation='sigmoid',
                 use_bias=True,
                 kernel_initializer='glorot_uniform',
                 recurrent_initializer='orthogonal',
                 bias_initializer='zeros',
                 unit_forget_bias=True,
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
        self.unit_forget_bias = unit_forget_bias

        self.kernel_regularizer = regularizers.get(kernel_regularizer)
        self.recurrent_regularizer = regularizers.get(recurrent_regularizer)
        self.bias_regularizer = regularizers.get(bias_regularizer)

        self.kernel_constraint = constraints.get(kernel_constraint)
        self.recurrent_constraint = constraints.get(recurrent_constraint)
        self.bias_constraint = constraints.get(bias_constraint)

        self.state_size = LSTMState(h=self.units, c=self.units)
        self.output_size = self.units

    def build(self, input_shapes):
        input_dim = input_shapes[0][-1]
        self.kernel = self.add_weight(
            shape=(input_dim, self.units * 4),
            name='kernel',
            initializer=self.kernel_initializer,
            regularizer=self.kernel_regularizer,
            constraint=self.kernel_constraint)
        self.recurrent_kernel = self.add_weight(
            shape=(self.units, self.units * 4),
            name='recurrent_kernel',
            initializer=self.recurrent_initializer,
            regularizer=self.recurrent_regularizer,
            constraint=self.recurrent_constraint)

        if self.use_bias:
            if self.unit_forget_bias:
                def bias_initializer(_, *args, **kwargs):
                    return tf.concat([
                      self.bias_initializer((self.units,), *args, **kwargs),
                      initializers.Ones()((self.units,), *args, **kwargs),
                      self.bias_initializer((self.units * 2,), *args, **kwargs),
                    ], -1)
            else:
                bias_initializer = self.bias_initializer
            self.bias = self.add_weight(
              shape=(self.units * 4,),
              name='bias',
              initializer=bias_initializer,
              regularizer=self.bias_regularizer,
              constraint=self.bias_constraint)
        else:
            self.bias = None

        if self.use_ln:
            self.x_ln = layers.LayerNormalization(name='x_ln')
            self.h_ln = layers.LayerNormalization(name='h_ln')
            self.c_ln = layers.LayerNormalization(name='c_ln')
        else:
            self.x_ln = lambda x: x
            self.h_ln = lambda x: x
            self.c_ln = lambda x: x

    def call(self, x, states):
        x, mask = tf.nest.flatten(x)
        h, c = states
        if mask is not None:
            h = h * mask
            c = c * mask
        
        x = self.x_ln(tf.matmul(x, self.kernel)) + self.h_ln(tf.matmul(h, self.recurrent_kernel))
        if self.use_bias:
            x = tf.nn.bias_add(x, self.bias)
        i, f, c_, o = tf.split(x, 4, 1)
        i, f, o = self.recurrent_activation(i), self.recurrent_activation(f), self.recurrent_activation(o)
        c_ = self.activation(c_)
        c = f * c + i * c_
        h = o * self.activation(self.c_ln(c))
            
        return h, LSTMState(h, c)
    
    def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
        state_size = self.state_size
        if inputs is not None:
            assert batch_size is None or batch_size == tf.shape(inputs)[0]
            batch_size = tf.shape(inputs)[0]
        if dtype is None:
            dtype = global_policy().compute_dtype
        return LSTMState(
            h=tf.zeros([batch_size, state_size[0]], dtype),
            c=tf.zeros([batch_size, state_size[1]], dtype))


class LSTM(Module):
    def __init__(self, config, name='rnn'):
        super().__init__(name=name)
        cell = LSTMCell(**config)
        self._rnn = layers.RNN(cell, return_sequences=True, return_state=True)
    
    def call(self, x, state, mask, additional_input=[]):
        xs = [x]
        mask = tf.expand_dims(mask, axis=-1)
        assert_rank(xs + additional_input + [mask], 3)
        for k in additional_input:
            k *= mask
            xs.append(k)
        x = tf.concat(xs, axis=-1) if len(xs) > 1 else x
        if not mask.dtype.is_compatible_with(global_policy().compute_dtype):
            mask = tf.cast(mask, global_policy().compute_dtype)
        x = self._rnn((x, mask), initial_state=state)
        x, state = x[0], LSTMState(*x[1:])
        return x, state

    def reset_states(self, states=None):
        self._rnn.reset_states(states)

    def get_initial_state(self, inputs=None, batch_size=None, dtype=None):
        if inputs is None:
            assert batch_size is not None
            inputs = tf.zeros([batch_size, 1, 1])
        return LSTMState(*self._rnn.cell.get_initial_state(inputs, dtype=dtype))

    @property
    def state_size(self):
        return self._rnn.cell.state_size

    @property
    def state_keys(self):
        return ['h', 'c']


if __name__ == '__main__':
    from utility.timer import timeit
    # inputs
    shape = (32, 16, 512)
    x0 = tf.random.normal(shape)
    m = tf.random.uniform(shape[:2], 0, 2, dtype=tf.int32)
    m = tf.cast(m[..., None], tf.float32)
    run_times = 1000
    assert x0.shape.ndims == m.shape.ndims
    # keras lstm
    c = tf.keras.layers.LSTMCell(512)
    l = tf.keras.layers.RNN(c, return_sequences=True, return_state=True)
    opt = tf.keras.optimizers.Adam(5e-5)
    lv = l.variables

    def keras_lstm_call():
        for _ in range(run_times):
            with tf.GradientTape() as tape:
                x = l(x0, initial_state=None)
                x, s = x[0], x[1:]
                y = tf.ones_like(x)
                loss = tf.reduce_mean((y-x)**2)
            gs = tape.gradient(loss, lv)
            opt.apply_gradients(zip(gs, lv))

    timeit(keras_lstm_call, to_print=True)

    # custom lstm
    mc = LSTMCell(512)
    ml = tf.keras.layers.RNN(mc, return_sequences=True, return_state=True)

    def custom_lstm_call():
        for _ in range(run_times):
            with tf.GradientTape() as tape:
                x = ml((x0, m), initial_state=None)
                x, s = x[0], x[1:]
                y = tf.ones_like(x)
                loss = tf.reduce_mean((y-x)**2)
            gs = tape.gradient(loss, lv)
            opt.apply_gradients(zip(gs, lv))
    
    timeit(custom_lstm_call, to_print=True)

    mlc = LSTMCell(512, use_ln=True)
    mll = tf.keras.layers.RNN(mlc, return_sequences=True, return_state=True)

    def custom_lstm_call():
        for _ in range(run_times):
            with tf.GradientTape() as tape:
                x = mll((x0, m), initial_state=None)
                x, s = x[0], x[1:]
                y = tf.ones_like(x)
                loss = tf.reduce_mean((y-x)**2)
            gs = tape.gradient(loss, lv)
            opt.apply_gradients(zip(gs, lv))
    
    timeit(custom_lstm_call, to_print=True)