import tensorflow as tf

from utility.rl_utils import n_step_target, quantile_regression_loss
from utility.schedule import TFPiecewiseSchedule
from core.optimizer import Optimizer
from algo.dqn.base import DQNBase, get_data_format


class Agent(DQNBase):
    def _construct_optimizers(self):
        if self._schedule_lr:
            self._lr = TFPiecewiseSchedule(
                [(5e5, self._lr), (2e6, 5e-5)], outside_value=5e-5)
        self._optimizer = Optimizer(
            self._optimizer, self.q, self._lr, 
            clip_norm=self._clip_norm, epsilon=1e-2/self._batch_size)

    @tf.function
    def _learn(self, obs, action, reward, next_obs, discount, steps=1, IS_ratio=1):
        terms = {}
        # compute target returns
        next_action = self.q.action(next_obs, self.K)
        _, next_qtv, _ = self.target_q.value(next_obs, self.N_PRIME, next_action)
        reward = reward[:, None]
        discount = discount[:, None]
        if not isinstance(steps, int):
            steps = steps[:, None]
        returns = n_step_target(reward, next_qtv, discount, self._gamma, steps, self._tbo)
        returns = tf.expand_dims(returns, axis=1)      # [B, 1, N']
        tf.debugging.assert_shapes([
            [next_qtv, (None, self.N_PRIME)],
            [returns, (None, 1, self.N_PRIME)],
        ])

        with tf.GradientTape() as tape:
            tau_hat, qtv, q = self.q.value(obs, self.N, action)
            qtv = tf.expand_dims(qtv, axis=-1)  # [B, N, 1]
            qr_loss = quantile_regression_loss(qtv, returns, tau_hat, kappa=self.KAPPA)
            loss = tf.reduce_mean(IS_ratio * qr_loss)

        if self._is_per:
            priority = self._compute_priority(qr_loss)
            terms['priority'] = priority
        
        terms['norm'] = self._optimizer(tape, loss)
        
        terms.update(dict(
            q=q,
            returns=returns,
            qr_loss=qr_loss,
            loss=loss,
        ))

        return terms
