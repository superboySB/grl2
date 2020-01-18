import numpy as np
import tensorflow as tf
import ray

from core import tf_config
from core.ensemble import Ensemble
from utility.display import pwc
from utility.timer import TBTimer
from env.gym_env import create_gym_env
from algo.apex.buffer import create_local_buffer
from algo.apex.base_worker import BaseWorker


class Worker(BaseWorker):
    """ Interface """
    def __init__(self, 
                name,
                worker_id, 
                model_fn,
                buffer_fn,
                config,
                model_config, 
                env_config, 
                buffer_config):
        tf_config.configure_threads(1, 1)
        tf_config.configure_gpu()

        env = create_gym_env(env_config)
        
        models = Ensemble(model_fn, model_config, env.state_shape, env.action_dim, env.is_action_discrete)
        
        buffer_config['seqlen'] = env.max_episode_steps
        buffer_keys = ['state', 'action', 'reward', 'done', 'steps']
        buffer = buffer_fn(buffer_config, *buffer_keys)

        super().__init__(
            name=name,
            worker_id=worker_id,
            models=models,
            env=env,
            buffer=buffer,
            actor=models['actor'],
            value=models['q1'],
            config=config)

        self.best_score = -float('inf')
        
    def run(self, learner, replay):
        step = 0
        while step < self.MAX_STEPS:
            with TBTimer(f'{self.name} pull weights', self.TIME_INTERVAL, to_log=self.timer):
                weights = self.pull_weights(learner)

            with TBTimer(f'{self.name} eval model', self.TIME_INTERVAL, to_log=self.timer):
                step, scores, _ = self.eval_model(weights, step, replay)

            with TBTimer(f'{self.name} send data', self.TIME_INTERVAL, to_log=self.timer):
                self._send_data(replay)

            score = np.mean(scores)
            self.best_score = max(self.best_score, score)

            if score == self.best_score:
                self.save(print_terminal_info=False)

    def _log_condition(self):
        return True

    def _logging(self, step):
        self.store(**self.get_value('score', mean=True, std=True, min=True, max=True))
        self.store(**self.get_value('epslen', mean=True, std=True, min=True, max=True))
        self.log(step=step, print_terminal_info=False)

def create_worker(name, worker_id, model_fn, config, model_config, 
                env_config, buffer_config):
    config = config.copy()
    model_config = model_config.copy()
    env_config = env_config.copy()
    buffer_config = buffer_config.copy()

    buffer_config['n_envs'] = env_config.get('n_envs', 1)
    buffer_fn = create_local_buffer

    env_config['seed'] += worker_id * 100
    
    config['model_name'] = f'worker_{worker_id}'
    config['replay_type'] = buffer_config['type']

    if env_config.get('is_deepmind_env'):
        RayWorker = ray.remote(num_cpus=2)(Worker)
    else:
        RayWorker = ray.remote(num_cpus=1)(Worker)
    worker = RayWorker.remote(name, worker_id, model_fn, buffer_fn, config, 
                        model_config, env_config, buffer_config)

    ray.get(worker.save_config.remote(dict(
        env=env_config,
        model=model_config,
        agent=config,
        replay=buffer_config
    )))

    return worker
