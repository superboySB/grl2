import os, atexit
import logging
from collections import defaultdict
from typing import Union
import numpy as np
import jax.numpy as jnp
import tensorflow as tf

from core.log import do_logging
from core.typing import ModelPath
from tools import graph
from tools.utils import isscalar

logger = logging.getLogger(__name__)


""" Recorder """
class Recorder:
    def __init__(self, model_path: ModelPath=None, record_file='record.txt'):
        record_file = record_file if record_file.endswith('record.txt') \
            else record_file + '/record.txt'
        if model_path is not None:
            recorder_dir = f'{model_path.root_dir}/{model_path.model_name}'
            path = os.path.join(recorder_dir, record_file)
            # if os.path.exists(path) and os.stat(path).st_size != 0:
            #     i = 1
            #     name, suffix = path.rsplit('.', 1)
            #     while os.path.exists(name + f'{i}.' + suffix):
            #         i += 1
            #     pwc(f'Warning: Log file "{path}" already exists!', 
            #         f'Data will be logged to "{name + f"{i}." + suffix}" instead.',
            #         color='magenta')
            #     path = name + f"{i}." + suffix
            if not os.path.isdir(recorder_dir):
                os.makedirs(recorder_dir)
            self.record_path = path
            self._out_file = open(path, 'a')
            atexit.register(self._out_file.close)
            do_logging(f'Record data to "{self._out_file.name}"', logger=logger)
        else:
            self._out_file = None
            do_logging(f'Record directory is not specified; no data will be recorded to the disk',
                logger=logger)

        self._first_row = True
        self._headers = []
        self._current_row = {}
        self._store_dict = defaultdict(list)

    def __contains__(self, item):
        return item in self._store_dict and self._store_dict[item] != []

    def contains_stats(self, item):
        return item in self._store_dict and self._store_dict[item] != []

    def store(self, **kwargs):
        for k, v in kwargs.items():
            if isinstance(v, (jnp.DeviceArray)):
                v = np.array(v)
            if v is None:
                continue
            elif isinstance(v, (list, tuple)):
                self._store_dict[k] += list(v)
            else:
                self._store_dict[k].append(v)

    def peep_stats_names(self):
        return list(self._store_dict)

    """ All get functions below will remove the corresponding items from the store """
    def get_raw_item(self, key):
        if key in self._store_dict:
            v = self._store_dict[key]
            del self._store_dict[key]
            return v
        return None
        
    def get_item(self, key, mean=True, std=False, min=False, max=False):
        stats = {}
        if key not in self._store_dict:
            return stats
        v = self._store_dict[key]
        if isscalar(v):
            stats[key] = v
            return
        if mean:
            stats[f'{key}'] = np.mean(v).astype(np.float32)
        if std:
            stats[f'{key}_std'] = np.std(v).astype(np.float32)
        if min:
            stats[f'{key}_min'] = np.min(v).astype(np.float32)
        if max:
            stats[f'{key}_max'] = np.max(v).astype(np.float32)
        del self._store_dict[key]
        return stats

    def get_raw_stats(self):
        stats = self._store_dict.copy()
        self._store_dict.clear()
        return stats

    def get_stats(self, mean=True, std=False, min=False, max=False, adaptive=True):
        stats = {}
        for k in sorted(self._store_dict):
            v = self._store_dict[k]
            k_std, k_min, k_max = std, min, max
            if (k.endswith('score') or k.endswith('epslen')) and not k.startswith('metrics/'):
                k = f'metrics/{k}'
            if (
                adaptive 
                and not k.startswith('aux/') 
                and not k.startswith('misc/') 
                and not k.startswith('time/')
                and not k.endswith('std')
                and not k.endswith('min')
                and not k.endswith('max')
            ):
                k_std = k_min = k_max = True
            if isscalar(v):
                stats[k] = v
                continue
            if mean:
                try:
                    if np.any(np.isnan(v)):
                        print(k, v)
                    stats[k] = np.mean(v).astype(np.float32)
                except:
                    print(k)
                    assert False
            if k_std:
                stats[f'{k}_std'] = np.std(v).astype(np.float32)
            if k_min:
                try:
                    stats[f'{k}_min'] = np.min(v).astype(np.float32)
                except:
                    print(k)
                    assert False
            if k_max:
                stats[f'{k}_max'] = np.max(v).astype(np.float32)
        self._store_dict.clear()
        return stats

    def get_count(self, name):
        return len(self._store_dict[name])

    def record_stats(self, stats, print_terminal_info=True):
        if not self._first_row and not set(stats).issubset(set(self._headers)):
            if self._first_row:
                do_logging(f'All previous records are erased because stats does not match the first row\n'
                    f'stats = {set(stats)}\nfirst row = {set(self._headers)}', 
                    logger=logger, level='pwt')
            self._out_file.seek(0)
            self._out_file.truncate()
            self._first_row = True
        [self.record_tabular(k, v) for k, v in stats.items()]
        self.dump_tabular(print_terminal_info=print_terminal_info)

    def _record_tabular(self, key, val):
        """
        Record a value of some diagnostic.

        Call this only once for each diagnostic quantity, each iteration.
        After using ``record_tabular`` to store values for each diagnostic,
        make sure to call ``dump_tabular`` to write them out to file and
        stdout (otherwise they will not get saved anywhere).
        """
        if self._first_row:
            if key not in self._headers:
                self._headers.append(key)
        else:
            assert key in self._headers, \
                f"Trying to introduce a new key {key} " \
                "that you didn't include in the first iteration"
        assert key not in self._current_row, \
            f"You already set {key} this iteration. " \
            "Maybe you forgot to call dump_tabular()"
        self._current_row[key] = val

    def record_tabular(self, key, val=None, mean=True, std=False, min=False, max=False):
        """
        Record a value or possibly the mean/std/min/max values of a diagnostic.
        """
        if val is not None:
            self._record_tabular(key, val)
        else:
            v = np.asarray(self._store_dict[key])
            if mean:
                self._record_tabular(f'{key}_mean', np.mean(v))
            if std:
                self._record_tabular(f'{key}_std', np.std(v))
            if min:
                self._record_tabular(f'{key}_min', np.min(v))
            if max:
                self._record_tabular(f'{key}_max', np.max(v))
        self._store_dict[key] = []

    def dump_tabular(self, print_terminal_info=True):
        """
        Write to disk all the diagnostics from the current iteration.
        """
        def is_print_keys(key):
            return (not key.endswith('std')
                and not key.endswith('max')
                and not key.endswith('min')) and (
                    key.startswith('metrics/') 
                    or key.startswith('run/') 
                    or '/' not in key)
        
        vals = []
        key_lens = [len(key) for key in self._headers]
        max_key_len = max(15,max(key_lens))
        n_slashes = 22 + max_key_len
        if print_terminal_info:
            print("-"*n_slashes)
        for key in self._headers:
            val = self._current_row.get(key, "")
            # print(key, np.array(val).dtype)
            valstr = f"{val:8.3g}" if hasattr(val, "__float__") else val
            if is_print_keys(key) and print_terminal_info:
                print(f'| {key:>{max_key_len}s} | {valstr:>15s} |')
            vals.append(val)
        if print_terminal_info:
            print("-"*n_slashes)
        if self._out_file is not None:
            if self._first_row and os.stat(self.record_path).st_size == 0:
                self._out_file.write("\t".join(self._headers)+"\n")
            self._out_file.write("\t".join(map(str,vals))+"\n")
            self._out_file.flush()
        self._current_row.clear()
        self._store_dict.clear()
        self._first_row = False


""" Tensorboard Writer """
class TensorboardWriter:
    def __init__(self, model_path: ModelPath, name):
        self._writer = create_tb_writer(model_path)
        self.name = name
        tf.summary.experimental.set_step(0)
    
    def set_summary_step(self, step):
        """ Sets tensorboard step """
        set_summary_step(step)

    def scalar_summary(self, stats, prefix=None, step=None):
        """ Adds scalar summary to tensorboard """
        scalar_summary(self._writer, stats, prefix=prefix, step=step)

    def histogram_summary(self, stats, prefix=None, step=None):
        """ Adds histogram summary to tensorboard """
        histogram_summary(self._writer, stats, prefix=prefix, step=step)

    def image_summary(self, images, name, prefix=None, step=None):
        image_summary(self._writer, images, name, prefix=prefix, step=step)

    def graph_summary(self, sum_type, *args, step=None):
        """ Adds graph summary to tensorboard
        This should only be called inside @tf.function
        Args:
            sum_type str: either "video" or "image"
            args: Args passed to summary function defined in utility.graph,
                of which the first must be a str to specify the tag in Tensorboard
        """
        assert isinstance(args[0], str), f'args[0] is expected to be a name string, but got "{args[0]}"'
        args = list(args)
        args[0] = f'{self.name}/{sum_type}/{args[0]}'
        graph_summary(self._writer, sum_type, args, step=step)

    def video_summary(self, video, step=None, fps=30):
        graph.video_summary(f'{self.name}/sim', video, fps=fps, step=step)

    def matrix_summary(
        self, 
        *, 
        model, 
        matrix, 
        label_top=True, 
        label_bottom=False, 
        xlabel, 
        ylabel, 
        xticklabels, 
        yticklabels,
        name, 
        step=None, 
    ):
        matrix_summary(
            model=model, 
            matrix=matrix, 
            label_top=label_top, 
            label_bottom=label_bottom, 
            xlabel=xlabel, 
            ylabel=ylabel, 
            xticklabels=xticklabels, 
            yticklabels=yticklabels,
            name=name, 
            writer=self._writer, 
            step=step, 
        )

    def flush(self):
        self._writer.flush()


""" Recorder Ops """
def record_stats(recorder, stats, print_terminal_info=True):
    [recorder.record_tabular(k, v) for k, v in stats.items()]
    recorder.dump_tabular(print_terminal_info=print_terminal_info)

def store(recorder, **kwargs):
    recorder.store(**kwargs)

def get_raw_item(recorder, key):
    return recorder.get_raw_item(key)

def get_item(recorder, key, mean=True, std=False, min=False, max=False):
    return recorder.get_item(key, mean=mean, std=std, min=min, max=max)

def get_raw_stats(recorder):
    return recorder.get_raw_stats()

def get_stats(recorder, mean=True, std=False, min=False, max=False):
    return recorder.get_stats(mean=mean, std=std, min=min, max=max)

def contains_stats(recorder, key):
    return key in recorder

def create_recorder(model_path: ModelPath):
    # recorder save stats in f'{root_dir}/{model_name}/logs/record.txt'
    recorder = Recorder(model_path)
    return recorder


""" Tensorboard Ops """
def set_summary_step(step):
    tf.summary.experimental.set_step(step)

def scalar_summary(writer, stats, prefix=None, step=None):
    if step is not None:
        tf.summary.experimental.set_step(step)
    prefix = prefix or 'stats'
    with writer.as_default():
        for k, v in stats.items():
            if isinstance(v, str):
                continue
            if '/' not in k:
                k = f'{prefix}/{k}'
            # print(k, np.array(v).dtype)
            tf.summary.scalar(k, tf.reduce_mean(v), step=step)

def histogram_summary(writer, stats, prefix=None, step=None):
    if step is not None:
        tf.summary.experimental.set_step(step)
    prefix = prefix or 'stats'
    with writer.as_default():
        for k, v in stats.items():
            if isinstance(v, (str, int, float)):
                continue
            if '/' not in k:
                k = f'{prefix}/{k}'
            tf.summary.histogram(k, v, step=step)

def graph_summary(writer, sum_type, args, step=None):
    """ This function should only be called inside a tf.function """
    fn = {'image': graph.image_summary, 'video': graph.video_summary}[sum_type]
    if step is None:
        step = tf.summary.experimental.get_step()
    def inner(*args):
        tf.summary.experimental.set_step(step)
        with writer.as_default():
            fn(*args)
    return tf.numpy_function(inner, args, [])

def image_summary(writer, images, name, prefix=None, step=None):
    if step is not None:
        tf.summary.experimental.set_step(step)
    if len(images.shape) == 3:
        images = images[None]
    if prefix:
        name = f'{prefix}/{name}'
    with writer.as_default():
        tf.summary.image(name, images, step=step)

def matrix_summary(
    *, 
    model: ModelPath, 
    matrix: np.ndarray, 
    label_top=True, 
    label_bottom=False, 
    xlabel: str, 
    ylabel: str, 
    xticklabels: Union[str, int, np.ndarray], 
    yticklabels: Union[str, int, np.ndarray],
    name, 
    writer, 
    step=None, 
):
    save_path = None if model is None else '/'.join([*model, name])
    image = graph.matrix_plot(
        matrix, 
        label_top=label_top, 
        label_bottom=label_bottom, 
        save_path=save_path, 
        xlabel=xlabel, 
        ylabel=ylabel, 
        xticklabels=xticklabels, 
        yticklabels=yticklabels
    )
    image_summary(writer, image, name, step=step)

def create_tb_writer(model_path: ModelPath):
    # writer for tensorboard summary
    # stats are saved in directory f'{root_dir}/{model_name}'
    writer = tf.summary.create_file_writer('/'.join(model_path))
    writer.set_as_default()
    return writer

def create_tensorboard_writer(model_path: ModelPath, name):
    return TensorboardWriter(model_path, name)
