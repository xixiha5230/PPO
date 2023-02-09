import torch
import torch.nn as nn
import numpy as np

from torch.distributions import MultivariateNormal
from torch.distributions import Categorical, Normal
from layers.StateNet import StateNetIR, weights_init_
from gym import spaces


class ActorCritic(nn.Module):
    def __init__(self, obs_space, action_space, config: dict):
        super(ActorCritic, self).__init__()
        self.config = config
        self.has_continuous_action = config['has_continuous_action_space']
        self.device = config['device']
        self.use_lstm = config['recurrence']['use_lstm']
        self.layer_type = config['recurrence']['layer_type']
        if self.use_lstm:
            self.hidden_state_size = config['recurrence']['hidden_state_size']
        self.hidden_layer_size = config['hidden_layer_size']
        if self.has_continuous_action:
            self.action_dim = action_space.shape[0]
            self.action_max = max(action_space.high)
        self.obs_space = obs_space
        if isinstance(obs_space, spaces.Tuple):
            self.state = StateNetIR(obs_space, 512)
            in_features_size = self.state.out_size
        elif len(obs_space.shape) == 1:
            in_features_size = obs_space.shape[0]
        else:
            raise NotImplementedError(obs_space.shape)

        # gru
        if self.use_lstm:
            if self.layer_type == 'gru':
                self.rnn = nn.GRU(in_features_size,
                                  self.hidden_state_size, batch_first=True)
            elif self.layer_type == 'lsym':
                self.recurrent_layer = nn.LSTM(
                    in_features_size, self.hidden_state_size, batch_first=True)
            else:
                raise NotImplementedError(self.layer_type)
            self.rnn.apply(weights_init_)

            after_rnn_size = self.hidden_state_size
        else:
            after_rnn_size = in_features_size

        # hidden layer
        self.lin_hidden = nn.Sequential(
            nn.Linear(after_rnn_size, self.hidden_layer_size),
            nn.ReLU()
        )
        self.lin_hidden.apply(weights_init_)

        # actor
        if self.has_continuous_action:
            self.mu = nn.Sequential(
                nn.Linear(self.hidden_layer_size, self.hidden_layer_size),
                nn.ReLU(),
                nn.Linear(self.hidden_layer_size, self.action_dim),
                nn.Tanh(),
            )
            self.mu.apply(weights_init_)

            self.std = nn.Sequential(
                nn.Linear(self.hidden_layer_size, self.hidden_layer_size),
                nn.ReLU(),
                nn.Linear(self.hidden_layer_size, self.action_dim),
                nn.Softplus(),
            )
            self.std.apply(weights_init_)
        else:
            ac_h = nn.Linear(self.hidden_layer_size, self.hidden_layer_size)
            nn.init.orthogonal_(ac_h.weight, np.sqrt(2))
            pi = nn.Linear(self.hidden_layer_size, self.action_dim)
            nn.init.orthogonal_(pi.weight, np.sqrt(0.01))
            self.mu = nn.Sequential()
            self.mu.append(ac_h)
            self.mu.append(nn.ReLU())
            self.mu.append(pi)
        # critic
        self.critic = nn.Sequential(
            nn.Linear(self.hidden_layer_size, self.hidden_layer_size),
            nn.ReLU(),
            nn.Linear(self.hidden_layer_size, 1)
        )
        self.critic.apply(weights_init_)

    def forward(self):
        raise NotImplementedError

    def act(self, state, hidden_in=None, sequence_length=1):
        # complex input
        if isinstance(self.obs_space, spaces.Tuple):
            feature = self.state(state)
        else:
            feature = state

        # lstm
        if self.use_lstm:
            if sequence_length == 1:
                # Case: sampling training data or model optimization using sequence length == 1
                feature, hidden_out = self.rnn(
                    feature.unsqueeze(1), hidden_in)
                # Remove sequence length dimension
                feature = feature.squeeze(1)
            else:
                feature_shape = tuple(feature.size())
                feature = feature.reshape(
                    (feature_shape[0] // sequence_length), sequence_length, feature_shape[1])
                feature, hidden_out = self.rnn(
                    feature, hidden_in)
                out_shape = tuple(feature.size())
                feature = feature.reshape(
                    out_shape[0] * out_shape[1], out_shape[2])
        else:
            hidden_out = None

        # hiddden
        feature = self.lin_hidden(feature)

        # actor
        if self.has_continuous_action:
            action_mean = self.action_max * self.mu(feature)
            action_std = self.std(feature)
            dist = Normal(action_mean, action_std)
        else:
            action_probs = self.mu(feature)
            dist = Categorical(logits=action_probs)

        # critic
        value = self.critic(feature)

        action = dist.sample()
        action_logprob = dist.log_prob(action)
        return action.detach(), action_logprob.detach(), value.squeeze(-1).detach(), hidden_out.detach() if hidden_out != None else None

    def evaluate(self, state, action, hidden_in=None, sequence_length=None):
        # complex input
        if isinstance(self.obs_space, spaces.Tuple):
            feature = self.state(state)
        else:
            feature = state

        # lstm
        if self.use_lstm:
            if sequence_length == 1:
                # Case: sampling training data or model optimization using sequence length == 1
                feature, hidden_out = self.rnn(
                    feature.unsqueeze(1), hidden_in)
                # Remove sequence length dimension
                feature = feature.squeeze(1)
            else:
                feature_shape = tuple(feature.size())
                feature = feature.reshape(
                    (feature_shape[0] // sequence_length), sequence_length, feature_shape[1])
                feature, hidden_out = self.rnn(
                    feature, hidden_in)
                out_shape = tuple(feature.size())
                feature = feature.reshape(
                    out_shape[0] * out_shape[1], out_shape[2])
        else:
            hidden_out = None

        # hiddden
        feature = self.lin_hidden(feature)

        # actor
        if self.has_continuous_action:
            action_mean = self.action_max * self.mu(feature)
            action_std = self.std(feature)
            dist = Normal(action_mean, action_std)
        else:
            action_probs = self.mu(feature)
            dist = Categorical(logits=action_probs)

        # critic
        value = self.critic(feature)

        action_logprob = dist.log_prob(action)
        dist_entropy = dist.entropy()

        return action_logprob, value, dist_entropy
