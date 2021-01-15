import tensorflow as tf

from utility.rl_loss import n_step_target, quantile_regression_loss
from utility.schedule import TFPiecewiseSchedule
from core.optimizer import Optimizer
from algo.dqn.base import DQNBase, get_data_format, collect


class Agent(DQNBase):
    def _construct_optimizers(self):
        if self._schedule_lr:
            self._value_lr = TFPiecewiseSchedule(self._value_lr)
        self._q_opt = Optimizer(
            self._q_opt, [self.encoder, self.qe, self.q], self._value_lr, 
            clip_norm=self._clip_norm, epsilon=1e-2/self._batch_size)
        self._fpn_opt = Optimizer(self._fpn_opt, self.fpn, self._fpn_lr, 
            rho=.95, epsilon=1e-5, centered=True)

    @tf.function
    def summary(self, data, terms):
        tf.summary.histogram('tau', terms['tau'], step=self._env_step)
        tf.summary.histogram('fpn_entropy', terms['fpn_entropy'], step=self._env_step)

    @tf.function
    def _learn(self, obs, action, reward, next_obs, discount, steps=1, IS_ratio=1):
        terms = {}
        # compute target returns
        next_x = self.encoder(next_obs)
        
        next_tau, next_tau_hat = self.fpn(next_x)
        next_qt_embed = self.qe(next_x, next_tau_hat)
        next_action = self.q.action(next_x, next_qt_embed, tau_range=next_tau)
        next_x = self.target_encoder(next_obs)
        next_qt_embed = self.target_qe(next_x, next_tau_hat)
        next_qtv = self.target_q(next_x, next_qt_embed, action=next_action)
        
        reward = reward[:, None]
        discount = discount[:, None]
        if not isinstance(steps, int):
            steps = steps[:, None]
        returns = n_step_target(reward, next_qtv, discount, self._gamma, steps, self._tbo)
        tf.debugging.assert_shapes([
            [next_qtv, (None, self.N)],
            [returns, (None, self.N)],
        ])
        returns = tf.expand_dims(returns, axis=1)      # [B, 1, N]

        with tf.GradientTape(persistent=True) as tape:
            x = self.encoder(obs)
            x_no_grad = tf.stop_gradient(x) # forbid gradients to cnn when computing fpn loss
            
            tau, tau_hat = self.fpn(x_no_grad)
            qt_embed = self.qe(x_no_grad, tau_hat)
            terms['tau'] = tau
            tau_hat = tf.stop_gradient(tau_hat) # forbid gradients to fpn when computing qr loss
            qtv, q = self.q(
                x, qt_embed, tau_range=tau, action=action)
            qtv_ext = tf.expand_dims(qtv, axis=-1)
            tau_hat = tf.expand_dims(tau_hat, axis=-1) # [B, N, 1]
            error, qr_loss = quantile_regression_loss(
                qtv_ext, returns, tau_hat, kappa=self.KAPPA, return_error=True)
            qr_loss = tf.reduce_mean(IS_ratio * qr_loss)

            # compute out gradients for fpn
            tau_1_N = tau[..., 1:-1]
            qt_embed = self.qe(x, tau_1_N)
            tau_qtv = self.q(x_no_grad, qt_embed, action=action)     # [B, N-1]
            tf.debugging.assert_shapes([
                [qtv, (None, self.N)],
                [tau_qtv, (None, self.N-1)],
            ])
            fpn_out_grads = tf.stop_gradient(
                2 * tau_qtv - qtv[..., :-1] - qtv[..., 1:])
            tf.debugging.assert_shapes([
                [fpn_out_grads, (None, self.N-1)],
                [tau, (None, self.N+1)],
            ])
            fpn_raw_loss = tf.reduce_mean(fpn_out_grads * tau_1_N, axis=-1)
            fpn_entropy = - tf.reduce_sum(tau_1_N * tf.math.log(tau_1_N), axis=-1)
            tf.debugging.assert_shapes([
                [fpn_raw_loss, (None,)],
                [fpn_entropy, (None,)],
            ])
            fpn_entropy_loss = - self._ent_coef * fpn_entropy
            fpn_loss = tf.reduce_mean(IS_ratio * (fpn_raw_loss + fpn_entropy_loss))

        if self._is_per:
            error = tf.reduce_max(tf.reduce_mean(tf.abs(error), axis=2), axis=1)
            priority = self._compute_priority(error)
            terms['priority'] = priority
        
        terms['iqn_norm'] = self._q_opt(tape, qr_loss)
        terms['fpn_norm'] = self._fpn_opt(tape, fpn_loss)
        
        terms.update(dict(
            q=q,
            returns=returns,
            qr_loss=qr_loss,
            fpn_entropy=fpn_entropy,
            fpn_out_grads=fpn_out_grads,
            fpn_raw_loss=fpn_raw_loss,
            fpn_entropy_loss=fpn_entropy_loss,
            fpn_loss=fpn_loss,
        ))

        return terms
