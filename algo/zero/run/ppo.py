import ray

from core.elements.builder import ElementsBuilder
from env.func import get_env_stats
from .runner import RunnerManager
from utility.ray_setup import sigint_shutdown_ray
from utility.timer import Every, Timer


def train(agent, buffer, runner_manager, parameter_server=None):
    # assert agent.get_env_step() == 0, (agent.get_env_step(), 'Comment out this line when you want to restore from a trained model')
    if agent.get_env_step() == 0 and agent.actor.is_obs_normalized:
        obs_rms_list, rew_rms_list = runner_manager.initialize_rms()
        agent.update_rms_from_stats_list(obs_rms_list, rew_rms_list)

    to_record = Every(agent.LOG_PERIOD, agent.LOG_PERIOD)
    rt = Timer('run')
    tt = Timer('train')
    lt = Timer('log')

    def record_stats(step):
        with lt:
            agent.store(**{
                'misc/train_step': agent.get_train_step(),
                'time/run': rt.total(), 
                'time/train': tt.total(),
                'time/log': lt.total(),
                'time/run_mean': rt.average(), 
                'time/train_mean': tt.average(),
                'time/log_mean': lt.average(),
            })
            agent.record(step=step)
            agent.save()

    step = agent.get_env_step()
    print('Training starts...')
    while step < runner_manager.MAX_STEPS:
        start_env_step = agent.get_env_step()
        with rt:
            weights = agent.get_weights(opt_weights=False)
            steps, data, stats = runner_manager.run(weights)
        step = sum(steps)

        for d in data:
            buffer.append_data(d)
        buffer.finish()

        # for o in agent.actor.obs_names:
        #     agent.actor.update_obs_rms(buffer[o], o)
        # agent.actor.update_reward_rms(buffer['reward'], buffer['discount'])

        start_train_step = agent.get_train_step()
        with tt:
            agent.train_record()
        train_step = agent.get_train_step()

        agent.store(
            **stats,
            fps=(step-start_env_step)/rt.last(),
            tps=(train_step-start_train_step)/tt.last())
        agent.set_env_step(step)
        buffer.reset()

        if to_record(train_step) and agent.contains_stats('score'):
            record_stats(step)

def main(config):
    # from core.utils import save_config
    # save_config(config.root_dir, config.model_name, config)
    ray.init()
    sigint_shutdown_ray()

    env_stats = get_env_stats(config.env)
    name = config.algorithm
    builder = ElementsBuilder(
        config, 
        env_stats, 
        name=name)
    elements = builder.build_agent_from_scratch()
    agent = elements.agent
    runner_manager = RunnerManager(config, name=agent.name)
    runner_manager.set_other_player('logs/card_gd/zero/baseline', 'zero_0')

    builder.save_config()

    train(elements.agent, elements.buffer, runner_manager)

    ray.shutdown()
