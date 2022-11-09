import functools
import numpy as np
import ray

from core.elements.builder import ElementsBuilder
from core.log import do_logging
from core.mixin.actor import rms2dict
from core.utils import configure_gpu, set_seed
from tools.utils import TempStore, batch_dicts, modify_config
from tools.run import Runner
from tools.timer import Every, Timer
from tools import pkg
from env.func import create_env


def train(config, agent, env, eval_env_config, buffer):
    life_long = env.stats().life_long
    routine_config = config.routine
    config.env = eval_env_config
    collect_fn = pkg.import_module(
        'elements.utils', algo=routine_config.algorithm).collect
    collect = functools.partial(collect_fn, buffer)

    suite_name = env.name.split("-")[0] \
        if '-' in env.name else 'gym'
    em = pkg.import_module(suite_name, pkg='env')
    info_func = em.info_func if hasattr(em, 'info_func') else None

    step = agent.get_env_step()
    runner = Runner(
        env, agent, step=step, nsteps=routine_config.n_steps, info_func=info_func)
    if life_long:
        info = batch_dicts(env.info(), list)
        agent.store(**info)

    def initialize_rms():
        print('Start to initialize running stats...')
        for _ in range(10):
            runner.run(action_selector=env.random_action, step_fn=collect)
            agent.actor.update_obs_rms(
                {name: buffer[name] for name in agent.actor.obs_names})
            agent.actor.update_reward_rms(
                np.array(buffer['reward']), np.array(buffer['discount']))
            buffer.reset()
        buffer.clear()
        agent.set_env_step(runner.step)
        agent.save()

    if step == 0 and agent.actor.is_obs_normalized:
        initialize_rms()

    runner.step = step
    # print("Initial running stats:", 
    #     *[f'{k:.4g}' for k in agent.get_rms_stats() if k])
    to_record = Every(
        routine_config.LOG_PERIOD, 
        start=1000, 
        final=routine_config.MAX_STEPS)
    to_eval = Every(
        routine_config.EVAL_PERIOD, 
        start=1000, 
        final=routine_config.MAX_STEPS)
    rt = Timer('run')
    tt = Timer('train')
    et = Timer('eval')
    lt = Timer('log')

    eval_process = None
    def evaluate_agent(step, agent):
        with TempStore(agent.model.get_states, agent.model.reset_states):
            with et:
                eval_main = pkg.import_main('eval', config=config)
                eval_main = ray.remote(eval_main)
                p = eval_main.remote(
                    [config.asdict()], 
                    routine_config.N_EVAL_EPISODES, 
                    record=routine_config.RECORD_VIDEO, 
                    fps=1, 
                    info=step // routine_config.EVAL_PERIOD * routine_config.EVAL_PERIOD
                )
                return p

    def record_stats(step, start_env_step, train_step, start_train_step):
        aux_stats = agent.actor.get_rms_stats()
        aux_stats = rms2dict(aux_stats)
        with lt:
            agent.store(**{
                'stats/train_step': agent.get_train_step(),
                'time/fps': (step-start_env_step)/rt.last(), 
                'time/tps': (train_step-start_train_step)/tt.last(),
            }, **Timer.all_stats(), **aux_stats)
            agent.record(step=step)
            agent.save()

    do_logging('Training starts...')
    train_step = agent.get_train_step()
    while step < routine_config.MAX_STEPS:
        start_env_step = agent.get_env_step()
        assert buffer.size() == 0, buffer.size()
        with rt:
            step = runner.run(step_fn=collect)

        # reward normalization
        reward = np.array(buffer['reward'])
        discount = np.array(buffer['discount'])
        agent.actor.update_reward_rms(reward, discount)
        buffer.update(
            'reward', agent.actor.normalize_reward(reward))
        
        # observation normalization
        def normalize_obs(name, obs_name):
            raw_obs = buffer[name]
            obs = agent.actor.normalize_obs(raw_obs, name=obs_name)
            buffer.update(name, obs)
            return raw_obs
        for name in agent.actor.obs_names:
            raw_obs = normalize_obs(name, name)
            if f'next_{name}' in buffer:
                normalize_obs(f'next_{name}', name)
            agent.actor.update_obs_rms(raw_obs, name)

        start_train_step = agent.get_train_step()
        with tt:
            agent.train_record()
        
        train_step = agent.get_train_step()
        agent.set_env_step(step)
        # no need to reset buffer
        if to_eval(step):
            if eval_process is not None:
                _, _, video = ray.get(eval_process)
                agent.video_summary(video, step=step, fps=1)
            eval_process = evaluate_agent(step, agent)
        if to_record(step):
            if life_long:
                info = batch_dicts(env.info(), list)
                agent.store(**info)
            record_stats(step, start_env_step, train_step, start_train_step)

def main(configs, train=train, gpu=-1):
    assert len(configs) == 1, configs
    config = configs[0]
    seed = config.get('seed')
    do_logging(f'seed={seed}', level='print')
    set_seed(seed)
    configure_gpu()
    use_ray = config.routine.get('EVAL_PERIOD', False)
    if use_ray:
        from tools.ray_setup import sigint_shutdown_ray
        ray.init(num_cpus=config.env.n_runners)
        sigint_shutdown_ray()

    if config.model.K == 0:
        config = modify_config(config, overwrite_existed_only=True, meta_type=None)
    elif config.model.L == 0:
        config = modify_config(config, overwrite_existed_only=True, meta_type='plain')
    else:
        config = modify_config(config, overwrite_existed_only=True, meta_type='bmg')
    
    def build_envs():
        env = create_env(config.env, force_envvec=True)
        eval_env_config = config.env.copy()
        if config.routine.get('EVAL_PERIOD', False):
            if config.env.env_name.startswith('procgen'):
                if 'num_levels' in eval_env_config:
                    eval_env_config['num_levels'] = 0
                if 'seed' in eval_env_config \
                    and eval_env_config['seed'] is not None:
                    eval_env_config['seed'] += 1000
                for k in list(eval_env_config.keys()):
                    # pop reward hacks
                    if 'reward' in k:
                        eval_env_config.pop(k)
            else:
                eval_env_config['n_envs'] = 1
            eval_env_config['n_runners'] = 1
        
        return env, eval_env_config
    
    env, eval_env_config = build_envs()

    env_stats = env.stats()
    builder = ElementsBuilder(config, env_stats, to_save_code=True)
    elements = builder.build_agent_from_scratch()

    train(config, elements.agent, env, eval_env_config, elements.buffer)
