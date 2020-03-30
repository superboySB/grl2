import numpy as np
import tensorflow as tf
from tensorflow.keras import layers
from tensorflow_probability import distributions as tfd

from utility.display import pwc
from core.module import Module
from core.tf_config import build
from core.decorator import config
from utility.rl_utils import clip_but_pass_gradient, logpi_correction
from utility.tf_distributions import DiagGaussian, Categorical, TanhBijector
from nn.func import cnn, mlp, dnc_rnn
from nn.utils import get_initializer


class PPOAC(Module):
    @config
    def __init__(self, action_dim, is_action_discrete, name):
        super().__init__(name=name)

        self._is_action_discrete = is_action_discrete
        
        self._cnn_name = None if isinstance(self._cnn_name, str) and self._cnn_name.lower() == 'none' else self._cnn_name

        """ Network definition """
        if self._cnn_name:
            self._cnn = cnn(self._cnn_name, time_distributed=False)
        # actor/critic head
        self.actor = mlp(self._actor_units, 
                        out_dim=action_dim, 
                        norm=self._norm, 
                        name='actor', 
                        activation=self._activation, 
                        kernel_initializer=get_initializer('orthogonal'))
        if not self._is_action_discrete:
            self.logstd = tf.Variable(
                initial_value=np.log(self._init_std)*np.ones(action_dim),
                dtype=tf.float32, 
                trainable=True, 
                name=f'actor/logstd')
        self.critic = mlp(self._critic_units, 
                            out_dim=1,
                            norm=self._norm, 
                            name='critic', 
                            activation=self._activation, 
                            kernel_initializer=get_initializer('orthogonal'))

    def __call__(self, x, return_value=False):
        pwc(f'{self.name} is retracing: x={x.shape}', color='cyan')
        if hasattr(self, '_cnn'):
            x = self._cnn(x)
        actor_output = self.actor(x)

        if self._is_action_discrete:
            act_dis = tfd.Categorical(actor_output)
        else:
            act_dis = tfd.MultivariateNormalDiag(actor_output, tf.exp(self.logstd))

        if return_value:
            value = tf.squeeze(self.critic(x))
            return act_dis, value
        else:
            return act_dis

    def reset_states(self, **kwargs):
        return


def create_model(model_config, action_dim, is_action_discrete, n_envs):
    ac = PPOAC(model_config, action_dim, is_action_discrete, 'ac')

    return dict(ac=ac)

if __name__ == '__main__':
    config = dict(
        cnn_name='none',
        shared_mlp_units=[4],
        use_dnc=False,
        lstm_units=3,
        actor_units=[2],
        critic_units=[2],
        norm='none',
        activation='relu',
        kernel_initializer='he_uniform'
    )

    batch_size = np.random.randint(1, 10)
    seq_len = np.random.randint(1, 10)
    obs_shape = [5]
    action_dim = np.random.randint(1, 10)
    for is_action_discrete in [True, False]:
        action_dtype = np.int32 if is_action_discrete else np.float32
        
        ac = PPOAC(config, action_dim, is_action_discrete, 'ac')

        from utility.display import display_var_info

        display_var_info(ac.trainable_variables)

        # test rnn state
        x = np.random.rand(batch_size, seq_len, *obs_shape).astype(np.float32)
        ac.step(tf.convert_to_tensor(x[:, 0]))
        states = ac.get_initial_state(batch_size=batch_size)
        ac.reset_states(states)
        
        states = [s.numpy() for s in ac._rnn.states]
        np.testing.assert_allclose(states, 0.)
        for i in range(seq_len):
            y = ac.step(tf.convert_to_tensor(x[:, i], tf.float32))
        step_states = [s.numpy() for s in ac._rnn.states]
        ac.reset_states()
        states = [s.numpy() for s in ac._rnn.states]
        np.testing.assert_allclose(states, 0.)
        if is_action_discrete:
            a = np.random.randint(low=0, high=action_dim, size=(batch_size, seq_len))
        else:
            a = np.random.rand(batch_size, seq_len, action_dim)
        ac.train_step(tf.convert_to_tensor(x, tf.float32))
        train_step_states = [s.numpy() for s in ac._rnn.states]
        np.testing.assert_allclose(step_states, train_step_states)

