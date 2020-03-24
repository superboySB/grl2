import time
from queue import Queue
import threading
import numpy as np
import ray

from utility.display import pwc
from utility.timer import Timer
from env.gym_env import create_env
from core import log


TIME_PERIOD = 10000

@ray.remote(num_cpus=1)
class Worker:
    def __init__(self, worker_id, env_config):
        self.id = worker_id
        self.n_envs = env_config['n_envs']
        env_config['n_workers'] = env_config['n_envs'] = 1
        self.envs = [create_env(env_config) for _ in range(self.n_envs)]
        self.states = [()] * self.n_envs
        self.action_queue = Queue(self.n_envs)

    def start_step_loop(self, learner):
        self._step_thread = threading.Thread(
            target=self._step_loop, args=[learner], daemon=True)
        self._step_thread.start()

    def start_env(self, learner):
        self.states = [env.reset() for env in self.envs]
        ray.get([learner.enqueue_state.remote(self.id, env_id, state)
            for env_id, state in enumerate(self.states)])
        pwc(f'Worker {self.id} started', color='blue')
        
    def enqueue_action(self, env_id, action):
        with Timer(f'Worker {self.id}: enqueue action', TIME_PERIOD):
            self.action_queue.put((env_id, action))

    def _step_loop(self, learner):
        episode = 0
        scores = []
        epslens = []
        while True:
            with Timer(f'Worker {self.id}: dequeue action', TIME_PERIOD):
                env_id, action = self.action_queue.get()

            next_state, reward, done, info = self.envs[env_id].step(action)

            learner.enqueue_state.remote(self.id, env_id, next_state)
            learner.add_transition.remote(self.states[env_id], action, reward, done, next_state)

            if done:
                scores.append(self.envs[env_id].get_score())
                epslens.append(self.envs[env_id].get_epslen())
                episode += 1
                next_state = self.envs[env_id].reset()
                if self.id == 1 and len(scores) > 10:
                    stats = {
                        f'score': np.mean(scores), 
                        f'score_std': np.std(scores),
                        f'score_max': np.max(scores), 
                        f'epslen': np.mean(epslens), 
                        f'epslen_std': np.std(epslens), 
                    }
                    learner.scalar_summary.remote(stats, episode)
                    scores = []
                    epslens = []

            self.states[env_id] = next_state

def create_worker(worker_id, env_config):
    env_config = env_config.copy()

    return Worker.remote(worker_id, env_config)
