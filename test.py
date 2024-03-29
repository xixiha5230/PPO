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
from utils.recurrent_cell_init import recurrent_cell_init

parser = argparse.ArgumentParser()
parser.add_argument(
    "--config_file",
    type=str,
    default="PPO_logs/xx.yaml",
    help="The config file",
)
parser.add_argument(
    "--ckpt",
    type=int,
    default=0,
    help="The checkpoint index",
)
parser.add_argument(
    "--unity",
    action="store_true",
)
parser.add_argument(
    "--seed",
    type=int,
    default=0,
    help="The seed",
)

parser.add_argument("--save_gif", type=bool, default=False)


def test(args):
    # TODO task 增加后模型读取错误

    config = ConfigHelper(args.config_file)
    logger = Logger(config, True)

    total_test_episodes = 1 if args.save_gif else 20
    save_gif = args.save_gif
    custom_ckpt = args.ckpt
    seed = args.seed
    if args.unity:
        config.env_name = None
    env = create_env(
        config,
        render_mode="rgb_array" if save_gif else "human",
        id=51,
        time_scale=1,
        seed=seed,
    )
    observation_space = env.observation_space
    if config.env_action_type == "continuous":
        action_space = env.action_space
    elif config.env_action_type == "discrete":
        action_space = env.action_space.n
    state_normalizer = logger.load_pickle("state_normalizer.pkl")
    state_normalizer.config = config

    # initialize a PPO agent
    ppo_agent = PPO(observation_space, action_space, config)

    # load latest checkpoint
    latest_checkpoint = max(
        glob.glob(os.path.join(logger.checkpoint_path, "*")), key=os.path.getctime
    )
    if custom_ckpt != 0:
        latest_checkpoint = f"{logger.checkpoint_path}/{custom_ckpt}.pth"
    print(f"resume from {latest_checkpoint}")
    ppo_agent.load(latest_checkpoint)
    ppo_agent.policy.eval()

    # start testing
    test_running_reward = 0
    images = []
    with torch.no_grad():
        for ep in range(1, total_test_episodes + 1):
            state = env.reset()
            state = state_normalizer(state, update=False)
            h_out = recurrent_cell_init(
                1, config.hidden_state_size, config.layer_type, config.device
            )
            step = 0
            while True:
                h_in = h_out
                state = _obs_2_tensor(state, config.device)
                if len(state[-1].shape) < 2:
                    state = [s.unsqueeze(0) for s in state]
                action, h_out, task_predict = ppo_agent.eval_select_action(state, h_in)
                # if step % 30 == 0:
                #     imageio.imwrite(
                #         f"./assert/{ep}_image_{step}.png",
                #         state[0][0].cpu().numpy(),
                #     )
                step += 1
                state, _, _, info = env.step(action[0].cpu().numpy())
                state = state_normalizer(state, update=False)
                env.render()
                if save_gif:
                    images.append(env.render())
                time.sleep(0.01)
                if info:
                    print(f'Episode: {ep} \t\t Reward: {info["reward"]}')
                    test_running_reward += info["reward"]
                    break

    if save_gif:
        imageio.mimsave(f"{logger.run_log_dir}/test.gif", images)
    env.close()

    print(
        "============================================================================================"
    )
    avg_test_reward = test_running_reward / total_test_episodes
    print("average test reward : " + str(avg_test_reward))
    print(
        "============================================================================================"
    )


if __name__ == "__main__":
    test(parser.parse_args())
