import logging
import numpy as np

from utility.utils import infer_dtype


logger = logging.getLogger(__name__)

def init_buffer(buffer, pre_dims, has_steps=False, precision=None, **kwargs):
    buffer.clear()
    if isinstance(pre_dims, int):
        pre_dims = [pre_dims]
    assert isinstance(pre_dims, (list, tuple))
    # v in buffer should have the same shape as v in kwargs except those specified by pre_dims
    info = infer_info(precision=precision, **kwargs)
    buffer.update(
        {k: np.zeros([*pre_dims, *v_shape], v_dtype) 
            if v_dtype else [None for _ in range(pre_dims[0])]
            for k, (v_shape, v_dtype) in info.items()})
    # we define an additional item, steps, that specifies steps in multi-step learning
    if has_steps:
        buffer['steps'] = np.ones(pre_dims, np.uint8)

def add_buffer(buffer, idx, n_steps, gamma, cycle=False, **kwargs):
    for k in buffer.keys():
        if k == 'steps':
            buffer[k][idx] = 1
        else:
            buffer[k][idx] = kwargs[k]

    # Update previous experience if multi-step is required
    for i in range(1, n_steps):
        k = idx - i
        if (k < 0 and not cycle) or buffer['discount'][k] == 0:
            break
        buffer['reward'][k] += gamma**i * kwargs['reward']
        buffer['discount'][k] = kwargs['discount']
        if 'steps' in buffer:
            buffer['steps'][k] += 1
        if 'next_obs' in buffer:
            buffer['next_obs'][k] = kwargs['next_obs']

def copy_buffer(dest_buffer, dest_start, dest_end, orig_buffer, orig_start, orig_end, dest_keys=True):
    assert dest_end - dest_start == orig_end - orig_start, (
            f'Inconsistent lengths of dest_buffer(dest_end - dest_start)'
            f'and orig_buffer({orig_end - orig_start}).')
    if dest_end - dest_start == 0:
        return
    
    for key in (dest_buffer if dest_keys else orig_buffer).keys():
        dest_buffer[key][dest_start: dest_end] = orig_buffer[key][orig_start: orig_end]

def infer_info(precision, **kwargs):
    """ infer shape/type from kwargs so that we can use them for buffer initialization """
    info = {}
    pre_dims_len = 0 if isinstance(kwargs['reward'], (int, float)) \
        else len(kwargs['reward'].shape)
    for k, v in kwargs.items():
        logger.debug(f'{k}, {v}, {type(v)}')
        if isinstance(v, (int, float, np.floating, np.signedinteger, np.ndarray)):
            np_v = np.array(v, copy=False)
            dtype = infer_dtype(np_v.dtype, precision)
            info[k] = (np_v.shape[pre_dims_len:], dtype)
        else:
            info[k] = ((), None)

    return info

def print_buffer(buffer, prefix=''):
    logger.info(f'{prefix} Buffer Info:')
    for k, v in buffer.items():
        shape = v.shape if isinstance(v, np.ndarray) else (len(v), np.array(v[0]).shape)
        dtype = v.dtype if isinstance(v, np.ndarray) else list
        logger.info(f'\t{k}: shape({shape}), type({dtype})')


def adjust_n_steps(data, seqlen, n_steps, max_steps, gamma):
    results = {}
    for k, v in data.items():
        if k == 'q' or k == 'v':
            vs = v
        else:
            results[k] = v.copy()[:seqlen]
    for i in range(seqlen):
        if n_steps < max_steps:
            for j in range(1, max_steps):
                if results['discount'][i] == 1:
                    cum_rew = results['reward'][i] + gamma**j * data['reward'][i+j]
                    if j >= n_steps and cum_rew + gamma**(j+1) * vs[i+j+1] * data['discount'][i+j+1] \
                        <= results['reward'][i] + gamma**j * vs[i+j] * data['discount'][i+j]:
                        print('break', i, j, cum_rew + gamma**(j+1) * vs[i+j+1] * data['discount'][i+j+1], \
                            results['reward'][i] + gamma**j * vs[i+j] * data['discount'][i+j])
                        break
                    results['reward'][i] = cum_rew
                    results['next_obs'][i] = data['next_obs'][i+j]
                    results['discount'][i] = data['discount'][i+j]
                    results['steps'][i] += 1
                else:
                    break
        else:
            for j in range(1, n_steps):
                if results['discount'][i]:
                    results['reward'][i] = results['reward'][i] * gamma**j * data['reward'][i+j]
                    results['next_obs'][i] = data['next_obs'][i+j]
                    results['discount'][i] = data['discount'][i+j]
                    results['steps'][i] += 1
    return results


def adjust_n_steps_envvec(data, seqlen, n_steps, max_steps, gamma):
    # we do forward update since updating discount in a backward pass is problematic when max_steps > n_steps
    results = {}
    logp = np.zeros_like(data['reward'])
    for k, v in data.items():
        if k == 'q' or k == 'v':
            vs = v
        elif k == 'logp':
            logp = v
        else:
            results[k] = v.copy()[:, :seqlen]
    obs_exp_dims = tuple(range(1, data['obs'].ndim-1))
    for i in range(seqlen):
        cond = np.ones_like(results['reward'][:, 0], dtype=bool)
        if n_steps < max_steps:
            for j in range(1, max_steps):
                disc = results['discount'][:, i]
                jth_rew = data['reward'][:, i+j] - logp[:, i+j]
                cum_rew = results['reward'][:, i] + gamma**j * jth_rew * disc
                cur_cond = disc == 1 if j < n_steps else np.logical_and(
                    disc == 1, cum_rew + gamma**(j+1) * vs[:, i+j+1] * data['discount'][:, i+j+1] \
                        > results['reward'][:, i] + gamma**j * vs[:, i+j] * data['discount'][:, i+j]
                )
                cond = np.logical_and(cond, cur_cond)
                results['reward'][:, i] = np.where(
                    cond, cum_rew, results['reward'][:, i])
                results['next_obs'][:, i] = np.where(
                    np.expand_dims(cond, obs_exp_dims), data['next_obs'][:, i+j], results['next_obs'][:, i])
                results['discount'][:, i] = np.where(
                    cond, data['discount'][:, i+j], results['discount'][:, i])
                results['steps'][:, i] += np.where(
                    cond, np.ones_like(cond, dtype=np.uint8), np.zeros_like(cond, dtype=np.uint8))
        else:
            for j in range(1, n_steps):
                disc = data['discount'][:, i]
                jth_rew = data['reward'][:, i+j] - logp[:, i+j]
                cond = disc == 1
                results['reward'][:, i] = np.where(
                    cond, results['reward'][:, i] + gamma**j * jth_rew * disc, results['reward'][:, i])
                results['next_obs'][:, i] = np.where(
                    np.expand_dims(cond, obs_exp_dims), data['next_obs'][:, i+j], results['next_obs'][:, i])
                results['discount'][:, i] = np.where(
                    cond, data['discount'][:, i+j], results['discount'][:, i])
                results['steps'][:, i] += np.where(
                    cond, np.ones_like(cond, dtype=np.uint8), np.zeros_like(cond, dtype=np.uint8))
    return results