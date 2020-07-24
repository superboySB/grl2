import numpy as np
import tensorflow as tf

from utility.display import pwc
from utility.schedule import TFPiecewiseSchedule
from core.tf_config import build
from core.decorator import agent_config, step_track
from core.optimizer import Optimizer
from nn.rnn import LSTMState
from algo.ppo.base import PPOBase
from algo.ppo.loss import compute_ppo_loss, compute_value_loss


class Agent(PPOBase):
    @agent_config
    def __init__(self, dataset, env):
        super().__init__(dataset=dataset, env=env)

        # optimizer
        if getattr(self, 'schedule_lr', False):
            self._lr = TFPiecewiseSchedule(
                [(300, self._lr), (1000, 5e-5)])
        self._optimizer = Optimizer(
            self._optimizer, self.ac, self._lr, 
            clip_norm=self._clip_norm, epsilon=self._opt_eps)

        # previous and current state of LSTM
        self.state = self.ac.get_initial_state(batch_size=env.n_envs)
        # Explicitly instantiate tf.function to avoid unintended retracing
        TensorSpecs = dict(
            obs=((self._sample_size, *env.obs_shape), env.obs_dtype, 'obs'),
            action=((self._sample_size, *env.action_shape), env.action_dtype, 'action'),
            traj_ret=((self._sample_size,), tf.float32, 'traj_ret'),
            value=((self._sample_size,), tf.float32, 'value'),
            advantage=((self._sample_size,), tf.float32, 'advantage'),
            logpi=((self._sample_size,), tf.float32, 'logpi'),
            mask=((self._sample_size,), tf.float32, 'mask'),
        )
        if self._store_state:
            state_type = type(self.ac.state_size)
            TensorSpecs['state'] = state_type(*[((sz, ), self._dtype, name) 
                for name, sz in self.ac.state_size._asdict().items()])
        self.learn = build(self._learn, TensorSpecs, print_terminal_info=True)

    def reset_states(self, states=None):
        self.state = states

    def get_states(self):
        return self.state

    def __call__(self, obs, reset=None, deterministic=False, 
                update_curr_state=True, update_rms=False, **kwargs):
        if len(obs.shape) % 2 != 0:
            obs = np.expand_dims(obs, 0)    # add batch dimension
        assert len(obs.shape) in (2, 4), obs.shape  
        if self.state is None:
            self.state = self.ac.get_initial_state(batch_size=tf.shape(obs)[0])
        if reset is None:
            mask = tf.ones(tf.shape(obs)[0], dtype=tf.float32)
        else:
            mask = tf.cast(1. - reset, tf.float32)
        if update_rms:
            self.update_obs_rms(obs)
        obs = self.normalize_obs(obs)
        prev_state = self.state
        out, state = self.model.action(obs, self.state, mask, deterministic)
        if update_curr_state:
            self.state = state
        if deterministic:
            return out.numpy()
        else:
            action, terms = out
            terms['mask'] = mask
            if self._store_state:
                terms.update(prev_state._asdict())
            terms = tf.nest.map_structure(lambda x: x.numpy(), terms)
            terms['obs'] = obs  # return normalized obs
            return action.numpy(), terms

    @step_track
    def learn_log(self, step):
        for i in range(self.N_UPDATES):
            for j in range(self.N_MBS):
                data = self.dataset.sample()
                value = data['value']
                data = {k: tf.convert_to_tensor(v) for k, v in data.items()}
                state, terms = self.learn(**data)

                terms = {k: v.numpy() for k, v in terms.items()}

                terms['value'] = np.mean(value)
                kl, p_clip_frac, v_clip_frac = \
                    terms['kl'], terms['p_clip_frac'], terms['v_clip_frac']
                for k in ['kl', 'p_clip_frac', 'v_clip_frac']:
                    del terms[k]

                self.store(**terms)
                if getattr(self, '_max_kl', None) and kl > self._max_kl:
                        break
            if getattr(self, '_max_kl', None) and kl > self._max_kl:
                pwc(f'{self._model_name}: Eearly stopping after '
                    f'{i*self.N_MBS+j+1} update(s) due to reaching max kl.',
                    f'Current kl={kl:.3g}', color='blue')
                break
        self.store(kl=kl, p_clip_frac=p_clip_frac, v_clip_frac=v_clip_frac)
        if not isinstance(self._lr, float):
            step = tf.cast(self._env_step, self._dtype)
            self.store(learning_rate=self._lr(step))

        return i * self.N_MBS + j + 1

    @tf.function
    def _learn(self, obs, action, traj_ret, value, advantage, logpi, mask, state=None):
        old_value = value
        with tf.GradientTape() as tape:
            act_dist, value, state = self.ac(obs, state, mask=mask, return_value=True)
            new_logpi = act_dist.log_prob(action)
            entropy = act_dist.entropy()
            # policy loss
            log_ratio = new_logpi - logpi
            ppo_loss, entropy, kl, p_clip_frac = compute_ppo_loss(
                log_ratio, advantage, self._clip_range, entropy)
            # value loss
            value_loss, v_clip_frac = compute_value_loss(
                value, traj_ret, old_value, self._clip_range)

            with tf.name_scope('total_loss'):
                policy_loss = (ppo_loss - self._entropy_coef * entropy)
                value_loss = self._value_coef * value_loss
                total_loss = policy_loss + value_loss

        terms = dict(
            advantage=advantage, 
            ratio=tf.exp(log_ratio), 
            entropy=entropy, 
            kl=kl, 
            p_clip_frac=p_clip_frac,
            v_clip_frac=v_clip_frac,
            ppo_loss=ppo_loss,
            value_loss=value_loss
        )
        terms['grads_norm'] = self._optimizer(tape, total_loss)

        return state, terms
