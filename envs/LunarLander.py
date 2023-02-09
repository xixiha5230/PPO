import gym
import numpy as np
import time


class LunarLander:
    def __init__(self, continuous=False):
        self._env = gym.make("LunarLander-v2", continuous=continuous)

    @property
    def observation_space(self):
        return self._env.observation_space

    @property
    def action_space(self):
        return self._env.action_space

    def reset(self):
        self._rewards = []
        obs = self._env.reset()
        return obs

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        self._rewards.append(reward)
        if done:
            info = {"reward": sum(self._rewards),
                    "length": len(self._rewards)}
        else:
            info = None
        return obs, reward / 300.0, done, info

    def render(self, mode='human'):
        return self._env.render(mode=mode)

    def close(self):
        self._env.close()
