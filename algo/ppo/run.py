import numpy as np
import tensorflow as tf


def run_trajectories(env, model, buffer):
    buffer.reset()
    model.reset_states()
    state = env.reset()

    for _ in range(env.max_episode_steps):
        action, logpi, value = model.step(tf.convert_to_tensor(state, tf.float32))
        next_state, reward, done, _ = env.step(action.numpy())
        buffer.add(state=state, 
                    action=action.numpy(), 
                    reward=np.expand_dims(reward, 1), 
                    value=value.numpy(), 
                    old_logpi=logpi.numpy(), 
                    nonterminal=np.expand_dims(1-done, 1), 
                    mask=np.expand_dims(env.get_mask(), 1))
        
        state = next_state
        if np.all(done):
            break
        
    _, _, last_value = model.step(tf.convert_to_tensor(state, tf.float32))
    buffer.finish(last_value.numpy())

    score, epslen = env.get_score(), env.get_epslen()

    return score, epslen
