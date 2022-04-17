import tensorflow as tf

from core.elements.loss import Loss
from utility.rl_loss import huber_loss, reduce_mean, ppo_loss, clipped_value_loss
from utility.tf_utils import explained_variance


class PPOLossImpl(Loss):
    def _compute_value_loss(
        self, 
        value, 
        traj_ret, 
        old_value, 
        mask=None
    ):
        value_loss_type = getattr(self.config, 'value_loss', 'mse')
        v_clip_frac = 0
        if value_loss_type == 'huber':
            value_loss = reduce_mean(
                huber_loss(value, traj_ret, threshold=self.config.huber_threshold), mask)
        elif value_loss_type == 'mse':
            value_loss = .5 * reduce_mean((value - traj_ret)**2, mask)
        elif value_loss_type == 'clip':
            value_loss, v_clip_frac = clipped_value_loss(
                value, traj_ret, old_value, self.config.clip_range, 
                mask=mask)
        elif value_loss_type == 'clip_huber':
            value_loss, v_clip_frac = clipped_value_loss(
                value, traj_ret, old_value, self.config.clip_range, 
                mask=mask, threshold=self.config.huber_threshold)
        else:
            raise ValueError(f'Unknown value loss type: {value_loss_type}')
        
        return value_loss, v_clip_frac


class PPOLoss(PPOLossImpl):
    def loss(
        self, 
        obs, 
        action, 
        value, 
        traj_ret, 
        advantage, 
        logpi, 
        state=None, 
        mask=None
    ):
        old_value = value
        terms = {}
        with tf.GradientTape() as tape:
            x, state = self.model.encode(obs, state, mask)
            act_dist = self.policy(x)
            new_logpi = act_dist.log_prob(action)
            entropy = act_dist.entropy()
            # policy loss
            log_ratio = new_logpi - logpi
            policy_loss, entropy, kl, p_clip_frac = ppo_loss(
                log_ratio, advantage, self.config.clip_range, entropy)
            # value loss
            value = self.value(x)
            value_loss, v_clip_frac = self._compute_value_loss(
                value, traj_ret, old_value)

            actor_loss = (policy_loss - self.config.entropy_coef * entropy)
            value_loss = self.config.value_coef * value_loss
            loss = actor_loss + value_loss

        pi = tf.exp(logpi)
        new_pi = tf.exp(new_logpi)
        ratio = tf.exp(log_ratio)
        terms.update(dict(
            value=value,
            ratio=ratio, 
            entropy=entropy, 
            kl=kl, 
            logpi=logpi,
            new_logpi=new_logpi, 
            pi=pi,
            new_pi=new_pi, 
            diff_pi=new_pi - pi, 
            p_clip_frac=p_clip_frac,
            policy_loss=policy_loss,
            actor_loss=actor_loss,
            v_loss=value_loss,
            explained_variance=explained_variance(traj_ret, value),
            v_clip_frac=v_clip_frac
        ))

        return tape, loss, terms

def create_loss(config, model, name='ppo'):
    return PPOLoss(config=config, model=model, name=name)
