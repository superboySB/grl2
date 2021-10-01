import logging
import tensorflow as tf

from core.agent import AgentBase
from core.decorator import override
from env.typing import EnvOutput


logger = logging.getLogger(__name__)


def collect(buffer, env, env_step, reset, next_obs, **kwargs):
    buffer.add(**kwargs)


def get_data_format(*, env_stats, **kwargs):
    obs_dtype = tf.uint8 if len(env_stats.obs_shape) == 3 else tf.float32
    action_dtype = tf.int32 if env_stats.is_action_discrete else tf.float32
    data_format = dict(
        obs=((None, *env_stats.obs_shape), obs_dtype),
        action=((None, *env_stats.action_shape), action_dtype),
        value=((None, ), tf.float32), 
        traj_ret=((None, ), tf.float32),
        advantage=((None, ), tf.float32),
        logpi=((None, ), tf.float32),
    )

    return data_format


class Agent(AgentBase):
    """ Initialization """
    @override(AgentBase)
    def _post_init(self, env_stats, dataset):
        super()._post_init(env_stats, dataset)

        self._value_input = None   # we record last obs before training to compute the last value

    """ Standard PPO methods """
    def before_run(self, env):
        pass

    def record_inputs_to_vf(self, env_output):
        self._value_input = self.actor.normalize_obs(env_output.obs)

    def compute_value(self, obs=None):
        # be sure you normalize obs first if obs normalization is required
        obs = self._value_input if obs is None else obs
        obs = obs.get('global_state', obs['obs'])
        value, _ = self.model.compute_value(obs).numpy()
        return value.numpy()

    def _sample_train(self):
        def ppo_training():
            for i in range(self.N_EPOCHS):
                for j in range(1, self.N_MBS+1):
                    with self._sample_timer:
                        data = self._sample_data()

                    data = {k: tf.convert_to_tensor(v) for k, v in data.items()}

                    with self._learn_timer:
                        terms = self.trainer.train(**data)

                    kl = terms.pop('kl').numpy()
                    value = terms.pop('value').numpy()
                    terms = {f'train/{k}': v.numpy() for k, v in terms.items()}

                    self.store(**terms)
                    if getattr(self, '_max_kl', None) and kl > self._max_kl:
                        break
                    if self._value_update == 'reuse':
                        self.dataset.update('value', value)

                    self._after_train_step()

                if getattr(self, '_max_kl', None) and kl > self._max_kl:
                    logger.info(f'{self._model_name}: Eearly stopping after '
                        f'{i*self.N_MBS+j} update(s) due to reaching max kl.'
                        f'Current kl={kl:.3g}')
                    break
                
                if self._value_update == 'once':
                    self.dataset.update_value_with_func(self.compute_value)
                if self._value_update is not None:
                    last_value = self.compute_value()
                    self.dataset.finish(last_value)
                
                self._after_train_epoch()
            n = i * self.N_MBS + j

            self.store(**{
                'misc/policy_updates': n,
                'train/kl': kl,
                'train/value': value,
                'time/sample_mean': self._sample_timer.average(),
                'time/learn_mean': self._learn_timer.average(),
            })

            if self._to_summary(self.train_step + n):
                self._summary(data, terms)
            
            return n

        def extra_value_training():
            for _ in range(self.N_VALUE_EPOCHS):
                for _ in range(self.N_MBS):
                    data = self.dataset.sample(self._value_sample_keys)

                    data = {k: tf.convert_to_tensor(data[k]) 
                        for k in self._value_sample_keys}

                    terms = self.trainer.learn_value(**data)
                    terms = {f'train/{k}': v.numpy() for k, v in terms.items()}
                    self.store(**terms)

        n = ppo_training()
        extra_value_training()
        self._after_learn()

        return n
    
    def _sample_data(self):
        return self.dataset.sample()

    def _after_train_step(self):
        """ Does something after each training step """
        pass

    def _after_train_epoch(self):
        """ Does something after each training epoch """
        pass
    
    def _after_learn(self):
        self._store_additional_stats()

    def _store_additional_stats(self):
        self.store(**self.actor.get_rms_stats())
