import tensorflow as tf

from core.elements.loss import Loss, LossEnsemble
from utility import rl_loss
from utility.tf_utils import reduce_mean, explained_variance


def prefix_name(terms, name):
    if name is not None:
        new_terms = {}
        for k, v in terms.items():
            new_terms[f'{name}/{k}'] = v
        return new_terms
    return terms


class PGLossImpl(Loss):
    def _pg_loss(
        self, 
        tape, 
        act_dist,
        action, 
        advantage, 
        logprob, 
        action_mask=None, 
        mask=None, 
        n=None, 
        name=None,
    ):
        new_logprob = act_dist.log_prob(action)
        tf.debugging.assert_all_finite(new_logprob, 'Bad new_logprob')
        entropy = act_dist.entropy()
        tf.debugging.assert_all_finite(entropy, 'Bad entropy')
        log_ratio = new_logprob - logprob
        raw_pg_loss, raw_entropy, kl, clip_frac = rl_loss.ppo_loss(
            log_ratio, 
            advantage, 
            self.config.clip_range, 
            entropy, 
            mask=mask, 
            n=n, 
            reduce=False
        )
        tf.debugging.assert_all_finite(raw_pg_loss, 'Bad raw_pg_loss')
        raw_pg_loss = reduce_mean(raw_pg_loss, mask, n)
        pg_loss = self.config.pg_coef * raw_pg_loss
        entropy = reduce_mean(raw_entropy, mask, n)
        entropy_loss = - self.config.entropy_coef * entropy

        loss = pg_loss + entropy_loss

        if self.config.get('debug', True):
            with tape.stop_recording():
                terms = dict(
                    ratio=tf.exp(log_ratio),
                    raw_entropy=raw_entropy,
                    entropy=entropy,
                    kl=kl,
                    new_logprob=new_logprob, 
                    p_clip_frac=clip_frac,
                    raw_pg_loss=raw_pg_loss,
                    pg_loss=pg_loss,
                    entropy_loss=entropy_loss, 
                    actor_loss=loss,
                    adv_std=tf.math.reduce_std(advantage, axis=-1), 
                )
                if action_mask is not None:
                    terms['n_avail_actions'] = tf.reduce_sum(
                        tf.cast(action_mask, tf.float32), -1)
                terms = prefix_name(terms, name)
        else:
            terms = {}

        return terms, loss


class ValueLossImpl(Loss):
    def _value_loss(
        self, 
        tape, 
        value, 
        traj_ret, 
        old_value, 
        mask=None,
        n=None, 
        name=None, 
    ):
        value_loss_type = getattr(self.config, 'value_loss', 'mse')
        v_clip_frac = 0
        if value_loss_type == 'huber':
            raw_value_loss = rl_loss.huber_loss(
                value, 
                traj_ret, 
                threshold=self.config.huber_threshold
            )
        elif value_loss_type == 'mse':
            raw_value_loss = .5 * (value - traj_ret)**2
        elif value_loss_type == 'clip':
            raw_value_loss, v_clip_frac = rl_loss.clipped_value_loss(
                value, 
                traj_ret, 
                old_value, 
                self.config.clip_range, 
                mask=mask, 
                n=n,
                reduce=False
            )
        elif value_loss_type == 'clip_huber':
            raw_value_loss, v_clip_frac = rl_loss.clipped_value_loss(
                value, 
                traj_ret, 
                old_value, 
                self.config.clip_range, 
                mask=mask, 
                n=n, 
                huber_threshold=self.config.huber_threshold,
                reduce=False
            )
        else:
            raise ValueError(f'Unknown value loss type: {value_loss_type}')

        value_loss = reduce_mean(raw_value_loss, mask)
        loss = reduce_mean(value_loss, mask, n)
        loss = self.config.value_coef * loss

        if self.config.get('debug', True):
            with tape.stop_recording():
                ev = explained_variance(traj_ret, value)
                terms = dict(
                    value=value,
                    raw_v_loss=raw_value_loss,
                    v_loss=loss,
                    explained_variance=ev,
                    traj_ret_std=tf.math.reduce_std(traj_ret, axis=-1), 
                    v_clip_frac=v_clip_frac,
                )
                terms = prefix_name(terms, name)
        else:
            terms = {}

        return terms, loss


class PPOPolicyLoss(PGLossImpl):
    def loss(
        self, 
        obs, 
        action, 
        advantage, 
        logprob, 
        prev_reward=None, 
        prev_action=None, 
        state=None, 
        action_mask=None, 
        life_mask=None, 
        mask=None
    ):
        loss_mask = life_mask if self.config.policy_life_mask else None
        n = None if loss_mask is None else tf.reduce_sum(loss_mask)
        with tf.GradientTape() as tape:
            x, _ = self.model.encode(
                x=obs, 
                prev_reward=prev_reward, 
                prev_action=prev_action, 
                state=state, 
                mask=mask
            )
            act_dist = self.policy(x, action_mask)
            terms, loss = self._pg_loss(
                tape=tape, 
                act_dist=act_dist, 
                action=action, 
                advantage=advantage, 
                logprob=logprob, 
                action_mask=action_mask, 
                mask=loss_mask, 
                n=n
            )

        if life_mask is not None:
            terms['n_alive_units'] = tf.reduce_sum(
                life_mask, -1)

        return tape, loss, terms


class PPOValueLoss(ValueLossImpl):
    def loss(
        self, 
        global_state, 
        value, 
        traj_ret, 
        prev_reward=None, 
        prev_action=None, 
        state=None, 
        life_mask=None, 
        mask=None
    ):
        old_value = value
        loss_mask = life_mask if self.config.value_life_mask else None
        n = None if loss_mask is None else tf.reduce_sum(loss_mask)
        with tf.GradientTape() as tape:
            value, _ = self.model.compute_value(
                global_state=global_state,
                prev_reward=prev_reward,
                prev_action=prev_action,
                state=state,
                mask=mask
            )

            terms, loss = self._value_loss(
                tape=tape, 
                value=value, 
                traj_ret=traj_ret, 
                old_value=old_value, 
                mask=loss_mask,
                n=n,
            )

        return tape, loss, terms


def create_loss(config, model, name='ppo'):
    def constructor(config, cls, name):
        return cls(
            config=config, 
            model=model[name], 
            name=name)

    return LossEnsemble(
        config=config,
        model=model,
        constructor=constructor,
        name=name,
        policy=PPOPolicyLoss,
        value=PPOValueLoss,
    )
