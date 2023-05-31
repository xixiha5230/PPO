import argparse
import glob
import os
import time

import imageio
import torch

from algorithm.PPO import PPO
from utils.ConfigHelper import ConfigHelper
from utils.env_helper import create_env
from utils.Logger import Logger
from utils.obs_2_tensor import _obs_2_tensor

parser = argparse.ArgumentParser()
parser.add_argument(
    '--config_file',
    type=str,
    default='PPO_logs/CarRace/new_env/run_0/config.yaml',
    help='The config file',
)
parser.add_argument(
    '--ckpt',
    type=int,
    default=0,
    help='The checkpoint index',
)
parser.add_argument(
    '--rm_rmm',
    type=int,
    default=0,
    help='The checkpoint index',
)
parser.add_argument(
    '--save_gif',
    type=bool,
    default=False
)


def test(args):
    # TODO task 增加后模型读取错误

    config = ConfigHelper(args.config_file)
    logger = Logger(config.env_name, config.exp_name, config.run_num, True, True)

    total_test_episodes = 1 if args.save_gif else 10
    save_gif = args.save_gif
    custom_ckpt = args.ckpt

    env = create_env(config, render_mode='rgb_array' if save_gif else 'human', id=51, time_scale=1)
    observation_space = env.observation_space
    if config.action_type == 'continuous':
        action_space = env.action_space
    elif config.action_type == 'discrete':
        action_space = env.action_space.n
    if config.use_state_normailzation:
        state_normalizer = logger.load_pickle('state_normalizer.pkl')

    # initialize a PPO agent
    ppo_agent = PPO(observation_space, action_space, config)
    ppo_agent.policy.eval()

    # load latest checkpoint
    latest_checkpoint = max(glob.glob(os.path.join(logger.checkpoint_path, '*')), key=os.path.getctime)
    if custom_ckpt != 0:
        latest_checkpoint = f'{logger.checkpoint_path}/{custom_ckpt}.pth'
    print(f'resume from {latest_checkpoint}')
    ppo_agent.load(latest_checkpoint)
    if args.act != 0:
        _p = PPO(observation_space, action_space, config)
        latest_checkpoint = f'{logger.checkpoint_path}/{args.act}.pth'
        _p.load(latest_checkpoint)
        ppo_agent.policy.task_predict_net = _p.policy.task_predict_net
    # start testing
    test_running_reward = 0
    images = []
    with torch.no_grad():
        for ep in range(1, total_test_episodes+1):
            state = env.reset()
            if config.use_state_normailzation:
                if config.multi_task:
                    state_normalizer(state[:-1]).append(state[-1])
                else:
                    state = state_normalizer(state)
            h_out = ppo_agent.init_recurrent_cell_states(1)
            step = 0
            while True:
                h_in = h_out
                action, h_out = ppo_agent.eval_select_action(_obs_2_tensor(state, config.device), h_in)
                step += 1
                if(args.rm_rnn != 0 and step % args.rm_rnn == 0):
                    h_out = ppo_agent.init_recurrent_cell_states(1)
                    print("***del***")
                state, _, _, info = env.step(action[0].cpu().numpy())
                if config.use_state_normailzation:
                    if config.multi_task:
                        state_normalizer(state[:-1]).append(state[-1])
                    else:
                        state = state_normalizer(state)

                env.render()
                # pygame.event.get()
                if save_gif:
                    images.append(env.render())
                time.sleep(0.01)
                if info:
                    print(f'Episode: {ep} \t\t Reward: {info["reward"]}')
                    test_running_reward += info['reward']
                    break

    if save_gif:
        imageio.mimsave(f'{logger.run_log_dir}/test.gif', images)
    env.close()

    print('============================================================================================')
    avg_test_reward = test_running_reward / total_test_episodes
    print('average test reward : ' + str(avg_test_reward))
    print('============================================================================================')


if __name__ == '__main__':
    test(parser.parse_args())
