import jax
import ray

from core.log import do_logging
from core.utils import configure_gpu, set_seed, save_code_for_seed
from core.typing import AttrDict, modelpath2outdir
from tools.display import print_dict
from tools.timer import Every
from algo.lka_common.train import *
from algo.happo.train import init_running_stats
from algo.happo_mb.run import Runner
from algo.happo_mb.train import update_config, env_run, ego_train, dynamics_run, get_aids, get_lka_aids


def train(
    agent, 
    dynamics, 
    runner: Runner, 
    routine_config, 
    dynamics_routine_config, 
    # dynamics_optimize=dynamics_optimize, 
    # dynamics_run=dynamics_run, 
    # lka_optimize=lka_optimize, 
    # lka_train=lka_train, 
    # env_run=env_run, 
    # ego_optimize=ego_optimize, 
    # ego_train=ego_train, 
):
    do_logging('Training starts...')
    env_step = agent.get_env_step()
    to_record = Every(
        routine_config.LOG_PERIOD, 
        start=env_step, 
        init_next=env_step != 0, 
        final=routine_config.MAX_STEPS
    )
    init_running_stats(agent, runner, dynamics)
    env_name = runner.env_config().env_name
    eval_data = load_eval_data(filename=env_name)
    rng = dynamics.model.rng

    rollout_type = routine_config.get('rollout_type', 'mix')
    assert rollout_type in ('lka', 'mix', 'one'), rollout_type
    n_agents = runner.env_stats().n_agents
    while env_step < routine_config.MAX_STEPS:
        rng, lka_rng = jax.random.split(rng, 2)
        lka_aids = get_lka_aids(rollout_type, n_agents)
        aids = get_aids(routine_config, n_agents)

        errors = AttrDict()
        time2record = to_record(env_step)

        if dynamics_routine_config.model_warm_up and \
            env_step < dynamics_routine_config.model_warm_up_steps:
            dynamics_optimize(dynamics, warm_up_stage=True)
        else:
            dynamics_optimize(dynamics)

            lka_train(
                agent, 
                dynamics, 
                routine_config, 
                dynamics_routine_config, 
                n_runs=routine_config.n_lka_steps, 
                rng=lka_rng, 
                train_aids=aids, 
                lka_aids=[], 
                run_fn=dynamics_run, 
                opt_fn=ego_optimize, 
            )
        # if routine_config.quantify_dynamics_errors and time2record:
        #     errors.lka = quantify_dynamics_errors(
        #         agent, dynamics, runner.env_config(), MODEL_EVAL_STEPS, None)

        env_step, _ = ego_train(
            agent, 
            runner, 
            dynamics, 
            routine_config, 
            train_aids=aids, 
            lka_aids=lka_aids, 
            run_fn=env_run, 
            opt_fn=ego_optimize
        )
        # if routine_config.quantify_dynamics_errors and time2record:
        #     errors.ego = quantify_dynamics_errors(
        #         agent, dynamics, runner.env_config(), MODEL_EVAL_STEPS, [])

        if time2record:
            # if routine_config.quantify_dynamics_errors:
            #     outdir = modelpath2outdir(agent.get_model_path())
            #     log_dynamics_errors(errors, outdir, env_step)
            if dynamics.get_train_step() > 0:
                stats = dynamics.valid_stats()
                dynamics.store(**stats)
            eval_and_log(agent, dynamics, None, routine_config, 
                         agent.training_data, eval_data, errors)


def main(configs, train=train):
    assert len(configs) > 1, len(configs)
    config, dynamics_config = configs[0], configs[-1]
    update_config(config, dynamics_config)
    seed = config.get('seed')
    set_seed(seed)

    configure_gpu()
    use_ray = config.env.get('n_runners', 1) > 1
    if use_ray:
        from tools.ray_setup import sigint_shutdown_ray
        ray.init(num_cpus=config.env.n_runners)
        sigint_shutdown_ray()

    runner = Runner(config.env)

    env_stats = runner.env_stats()
    env_stats.n_envs = config.env.n_runners * config.env.n_envs
    print_dict(env_stats)

    # build agent
    agent = build_agent(config, env_stats)
    # build dynamics
    dynamics = build_dynamics(config, dynamics_config, env_stats)
    save_code_for_seed(config)

    routine_config = config.routine.copy()
    dynamics_routine_config = dynamics_config.routine.copy()
    train(
        agent, 
        dynamics, 
        runner, 
        routine_config, 
        dynamics_routine_config
    )

    do_logging('Training completed')
