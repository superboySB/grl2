import time
import numpy as np
import ray 

from core.tf_config import configure_gpu, configure_precision, silence_tf_logs
from utility.utils import Every, TempStore
from utility.ray_setup import sigint_shutdown_ray
from utility.graph import video_summary
from utility.run import Runner, evaluate
from utility.timer import Timer
from utility import pkg
from env.gym_env import create_env



def train(agent, env, eval_env, buffer):
    def collect(env, step, reward, next_obs, **kwargs):
        kwargs['reward'] = agent.normalize_reward(reward)
        buffer.add(**kwargs)
    step = agent.env_step
    action_selector = lambda *args, **kwargs: agent(*args, **kwargs, update_rms=True)
    runner = Runner(env, agent, step=step, nsteps=agent.N_STEPS)
    
    to_log = Every(agent.LOG_PERIOD, agent.LOG_PERIOD)
    while step < agent.MAX_STEPS:
        start_env_step = agent.env_step
        start_train_step = agent.train_step
        start_time = time.time()
        step = runner.run(action_selector=action_selector, step_fn=collect)
        
        _, terms = agent(runner.obs, update_curr_state=False, reset=env.already_done())
        buffer.finish(terms['value'])
        agent.learn_log(step)
        buffer.reset()

        if to_log(agent.train_step):
            duration = time.time()-start_time
            agent.store(
                fps=(agent.env_step-start_env_step)/duration,
                tps=(agent.train_step-start_train_step)/duration,
            )

            with TempStore(agent.get_states, agent.reset_states):
                scores, epslens, video = evaluate(
                    eval_env, agent, record=False)
                # video_summary(f'{agent.name}/sim', video, step=step)
                agent.store(eval_score=scores, eval_epslen=np.mean(epslens))

            agent.log(step)
            agent.save()

def main(env_config, model_config, agent_config, buffer_config):
    algo = agent_config['algorithm']
    env = env_config['name']
    if 'atari' not in env:
        print('Any changes to config is dropped as we switch to a non-atari environment')
        from utility import yaml_op
        root_dir = agent_config['root_dir']
        model_name = agent_config['model_name']
        directory = pkg.get_package(algo, 0, '/')
        config = yaml_op.load_config(f'{directory}/config2.yaml')
        env_config = config['env']
        model_config = config['model']
        agent_config = config['agent']
        buffer_config = config['buffer']
        agent_config['root_dir'] = root_dir
        agent_config['model_name'] = model_name
        env_config['name'] = env

    create_model, Agent = pkg.import_agent(config=agent_config)
    PPOBuffer = pkg.import_module('buffer', algo=algo).PPOBuffer

    silence_tf_logs()
    configure_gpu()
    configure_precision(agent_config['precision'])

    use_ray = env_config.get('n_workers', 1) > 1
    if use_ray:
        ray.init()
        sigint_shutdown_ray()

    env = create_env(env_config, force_envvec=True)
    eval_env_config = env_config.copy()
    eval_env_config['seed'] += 1000
    eval_env = create_env(eval_env_config, force_envvec=True)

    buffer_config['n_envs'] = env.n_envs
    buffer = PPOBuffer(buffer_config)

    models = create_model(model_config, env)
    
    agent = Agent(name='ppo', 
                config=agent_config, 
                models=models, 
                buffer=buffer,
                env=env)

    agent.save_config(dict(
        env=env_config,
        model=model_config,
        agent=agent_config,
        buffer=buffer_config
    ))

    train(agent, env, eval_env, buffer)

    if use_ray:
        ray.shutdown()
