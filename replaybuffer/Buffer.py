from gym import spaces as gym_spaces
from gymnasium import spaces as gymnasium_spaces
import torch
import numpy as np


class Buffer():
    '''The buffer stores and prepares the training data. It supports recurrent policies. '''

    def __init__(self, config: dict, observation_space: gymnasium_spaces, action_space: gymnasium_spaces) -> None:
        '''
        Args:
            config {dict} -- Configuration and hyperparameters of the environment, trainer and model.
            observation_space {gymnasium.spaces} -- The observation space of the agent
            action_space {gymnasium.spaces} -- The action space of the agent
        '''
        conf_train = config['train']
        self.device = conf_train['device']
        self.n_mini_batches = conf_train['num_mini_batch']
        self.action_type = conf_train['action_type']
        self.multi_task = conf_train['multi_task']
        self.use_rnd = conf_train['use_rnd']

        conf_recurrence = config['recurrence']
        hidden_state_size = conf_recurrence['hidden_state_size']
        self.layer_type = conf_recurrence['layer_type']
        self.sequence_length = conf_recurrence['sequence_length']

        worker = config['worker']
        if self.multi_task:
            self.task_num = len(config.get('task', []))
            self.n_workers = worker['num_workers'] // self.task_num
        else:
            self.n_workers = worker['num_workers']
        self.worker_steps = worker['worker_steps']

        conf_ppo = config['ppo']
        self.gamma = conf_ppo['gamma']
        self.lamda = conf_ppo['lamda']

        self.batch_size = self.n_workers * self.worker_steps
        self.mini_batch_size = self.batch_size // self.n_mini_batches
        self.actual_sequence_length = 0
        # Reward
        self.rewards = np.zeros((self.n_workers, self.worker_steps), dtype=np.float32)
        # Done
        self.dones = np.zeros((self.n_workers, self.worker_steps), dtype=np.bool)
        # Action & Log_probs
        if self.action_type == 'continuous':
            self.actions = torch.zeros((self.n_workers, self.worker_steps) + (action_space.shape[0], )).to(self.device)
            self.log_probs = torch.zeros((self.n_workers, self.worker_steps) + (action_space.shape[0], )).to(self.device)
        elif self.action_type == 'discrete':
            self.actions = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
            self.log_probs = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
        else:
            raise NotImplementedError(self.action_type)
        # Observation
        if isinstance(observation_space,  gym_spaces.Tuple) or isinstance(observation_space, gymnasium_spaces.Tuple):
            self.obs = [[torch.zeros((self.n_workers,) + t.shape).to(self.device)
                         for t in observation_space] for _ in range(self.worker_steps)]
        else:
            self.obs = torch.zeros((self.n_workers, self.worker_steps) + observation_space.shape).to(self.device)
        # hxs & cxs
        self.hxs = torch.zeros((self.n_workers, self.worker_steps, hidden_state_size)).to(self.device)
        if self.layer_type == 'lstm':
            self.cxs = torch.zeros((self.n_workers, self.worker_steps, hidden_state_size)).to(self.device)
        # Value
        self.values = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
        # Advantage
        self.advantages = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
        # RDN rnd_next_obs && rnd_values && rnd_rewards && rnd_advantages
        if self.use_rnd:
            # only rnd the obs on first dim!!!
            self.rnd_next_obs = torch.zeros((self.n_workers, self.worker_steps) + observation_space[0].shape).to(self.device)
            self.rnd_values = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
            self.rnd_rewards = np.zeros((self.n_workers, self.worker_steps), dtype=np.float32)
            self.rnd_advantages = torch.zeros((self.n_workers, self.worker_steps)).to(self.device)
            # TODO SAVE? reuse? Multitask done
            self.rnd_reward_rms = RunningMeanStd(shape=(1,))

    def prepare_batch_dict(self) -> None:
        '''Flattens the training samples and stores them inside a dictionary. Due to using a recurrent policy,
        the data is split into episodes or sequences beforehand.
        '''
        # Supply training samples
        samples = {
            'actions': self.actions,
            'obs': self.obs,
            # The loss mask is used for masking the padding while computing the loss function.
            # This is only of significance while using recurrence.
            'loss_mask': torch.ones((self.n_workers, self.worker_steps), dtype=torch.bool).to(self.device)
        }

        max_sequence_length = 1

        # Add collected recurrent cell states to the dictionary
        samples['hxs'] = self.hxs
        if self.layer_type == 'lstm':
            samples['cxs'] = self.cxs

        # Split data into sequences and apply zero-padding
        # Retrieve the indices of dones as these are the last step of a whole episode
        episode_done_indices = []
        for w in range(self.n_workers):
            episode_done_indices.append(list(self.dones[w].nonzero()[0]))
            # Append the index of the last element of a trajectory as well, as it 'artifically' marks the end of an episode
            if len(episode_done_indices[w]) == 0 or episode_done_indices[w][-1] != self.worker_steps - 1:
                episode_done_indices[w].append(self.worker_steps - 1)

        # Retrieve unpadded sequence indices
        self.flat_sequence_indices = np.asarray(self._arange_sequences(np.arange(
            0, self.n_workers * self.worker_steps).reshape((self.n_workers, self.worker_steps)), episode_done_indices)[0], dtype=object)

        # Split obs, values, advantages, recurrent cell states, actions and log_probs into episodes and then into sequences
        for key, value in samples.items():
            # Splits the povided data into episodes by done indices and then into sequences
            sequences, max_sequence_length = self._arange_sequences(value, episode_done_indices)

            # Apply zero-padding to ensure that each sequence has the same length
            # Therfore we can train batches of sequences in parallel instead of one sequence at a time
            for i, sequence in enumerate(sequences):
                sequences[i] = self._pad_sequence(sequence, max_sequence_length)

            # Stack sequences (target shape: (Sequence, Step, Data ...) and apply data to the samples dictionary
            if isinstance(sequences[0], list):
                samples[key] = [torch.stack([sequences[j][i] for j in range(len(sequences))], axis=0)
                                for i in range(len(sequences[0]))]
            else:
                samples[key] = torch.stack(sequences, axis=0)

            if (key == 'hxs' or key == 'cxs'):
                # Select only the very first recurrent cell state of a sequence and add it to the samples.
                samples[key] = samples[key][:, 0]

        # If the sequence length is based on entire episodes, it will be as long as the longest episode.
        # Hence, this information has to be stored for the mini batch generation.
        self.actual_sequence_length = max_sequence_length
        self.num_sequences = len(sequences[0]) if isinstance(sequences[0], list) else len(sequences)

        # Add remaining data samples
        samples['values'] = self.values
        samples['log_probs'] = self.log_probs
        samples['advantages'] = self.advantages
        if self.use_rnd:
            samples['rnd_values'] = self.rnd_values
            samples['rnd_advantages'] = self.rnd_advantages
            samples['rnd_next_obs'] = self.rnd_next_obs

        # Flatten all samples and convert them to a tensor
        self.samples_flat = {}
        for key, value in samples.items():
            if not key == 'hxs' and not key == 'cxs':
                if isinstance(value, list):
                    for i, v in enumerate(value):
                        value[i] = v.reshape(v.shape[0] * v.shape[1], *v.shape[2:])
                else:
                    value = value.reshape(value.shape[0] * value.shape[1], *value.shape[2:])
            self.samples_flat[key] = value

    def _arange_sequences(self, data, episode_done_indices):
        '''Splits the povided data into episodes and then into sequences.
        The split points are indicated by the envrinoments' done signals.

        Arguments:
            data {torch.tensor} -- The to be split data arrange into num_worker, worker_steps
            episode_done_indices {list} -- Nested list indicating the indices of done signals. Trajectory ends are treated as done

        Returns:
            {list} -- Data arranged into sequences of variable length as list
        '''
        sequences = []
        max_length = 1
        if isinstance(data, list):
            data = [torch.stack([data[i][j] for i in range(len(data))], axis=1) for j in range(len(data[0]))]
        for w in range(self.n_workers):
            start_index = 0
            for done_index in episode_done_indices[w]:
                # Split trajectory into episodes
                if isinstance(data, list):
                    episode = [d[w, start_index:done_index + 1] for d in data]
                else:
                    episode = data[w, start_index:done_index + 1]
                # Split episodes into sequences
                episode_len = len(episode[0]) if isinstance(episode, list) else len(episode)
                if self.sequence_length > 0:
                    for start in range(0, episode_len, self.sequence_length):
                        end = start + self.sequence_length
                        sequences.append([e[start:end] for e in episode]
                                         if isinstance(episode, list) else episode[start:end])
                    max_length = self.sequence_length
                else:
                    # If the sequence length is not set to a proper value, sequences will be based on episodes
                    sequences.append(episode)
                    max_length = episode_len if episode_len > max_length else max_length
                start_index = done_index + 1
        return sequences, max_length

    def _pad_sequence(self, sequence: np.ndarray, target_length: int) -> np.ndarray:
        '''Pads a sequence to the target length using zeros.

        Args:
            sequence {np.ndarray} -- The to be padded array (i.e. sequence)
            target_length {int} -- The desired length of the sequence

        Returns:
            {torch.tensor} -- Returns the padded sequence
        '''
        # Determine the number of zeros that have to be added to the sequence
        delta_length = target_length - len(sequence[0] if isinstance(sequence, list) else sequence)
        # If the sequence is already as long as the target length, don't pad
        if delta_length <= 0:
            return sequence
        # Construct array of zeros
        if isinstance(sequence, list):
            res = []
            for s in sequence:
                if len(s.shape) > 1:
                    # Case: pad multi-dimensional array (e.g. visual observation)
                    padding = torch.zeros(((delta_length,) + s.shape[1:]), dtype=s.dtype).to(self.device)
                else:
                    padding = torch.zeros(delta_length, dtype=s.dtype).to(self.device)
                # Concatenate the zeros to the sequence
                res.append(torch.cat((s, padding), axis=0))
            return res
        else:
            if len(sequence.shape) > 1:
                # Case: pad multi-dimensional array (e.g. visual observation)
                padding = torch.zeros(((delta_length,) + sequence.shape[1:]), dtype=sequence.dtype).to(self.device)
            else:
                padding = torch.zeros(delta_length, dtype=sequence.dtype).to(self.device)
            # Concatenate the zeros to the sequence
            return torch.cat((sequence, padding), axis=0)

    def recurrent_mini_batch_generator(self) -> dict:
        '''A recurrent generator that returns a dictionary providing training data arranged in mini batches.
        This generator shuffles the data by sequences.

        Yields:
            {dict} -- Mini batch data for training
        '''
        # optimization 1: normalized advantages in batch data
        self.samples_flat['normalized_advantages'] = (self.samples_flat['advantages'] - self.samples_flat['advantages'].mean()
                                                      ) / (self.samples_flat['advantages'].std() + 1e-8)
        if self.use_rnd:
            self.samples_flat['normalized_rnd_advantages'] = (self.samples_flat['rnd_advantages'] - self.samples_flat['rnd_advantages'].mean()
                                                              ) / (self.samples_flat['rnd_advantages'].std() + 1e-8)
        # Determine the number of sequences per mini batch
        num_sequences_per_batch = self.num_sequences // self.n_mini_batches
        # Arrange a list that determines the sequence count for each mini batch
        num_sequences_per_batch = [num_sequences_per_batch] * self.n_mini_batches
        remainder = self.num_sequences % self.n_mini_batches
        for i in range(remainder):
            # Add the remainder if the sequence count and the number of mini batches do not share a common divider
            num_sequences_per_batch[i] += 1
        # Prepare indices, but only shuffle the sequence indices and not the entire batch.
        indices = torch.arange(0, self.num_sequences *
                               self.actual_sequence_length).reshape(self.num_sequences, self.actual_sequence_length)
        sequence_indices = torch.randperm(self.num_sequences)
        # Compose mini batches
        start = 0
        for num_sequences in num_sequences_per_batch:
            end = start + num_sequences
            mini_batch_padded_indices = indices[sequence_indices[start:end]].reshape(-1)
            mini_batch_unpadded_indices = self.flat_sequence_indices[sequence_indices[start:end].tolist()]
            mini_batch_unpadded_indices = [item for sublist in mini_batch_unpadded_indices for item in sublist]
            mini_batch = {}
            for key, value in self.samples_flat.items():
                if key == 'hxs' or key == 'cxs':
                    # Select recurrent cell states of sequence starts
                    mini_batch[key] = value[sequence_indices[start:end]].to(self.device)
                elif key == 'log_probs' or 'advantages' in key or key == 'values' or 'rnd' in key:
                    # Select unpadded data
                    mini_batch[key] = value[mini_batch_unpadded_indices].to(self.device)
                else:
                    # Select padded data
                    if isinstance(value, list):
                        mini_batch[key] = [v[mini_batch_padded_indices].to(self.device) for v in value]
                    else:
                        mini_batch[key] = value[mini_batch_padded_indices].to(self.device)
            start = end
            yield mini_batch

    def free_memory(self):
        '''free cuda memory'''
        del(self.samples_flat)
        if self.device == 'cuda':
            torch.cuda.empty_cache()

    def calc_advantages(self, last_value: torch.Tensor, rnd_last_value: torch.Tensor = None) -> None:
        '''Generalized advantage estimation (GAE)

        Arguments:
            last_value {torch.tensor} -- Value of the last agent's state
        '''
        with torch.no_grad():
            self._calc_advantages(self.dones, self.rewards, self.values, last_value, self.advantages)
            if self.use_rnd:
                self._calc_advantages(np.zeros_like(self.dones), self.rnd_rewards,
                                      self.rnd_values, rnd_last_value, self.rnd_advantages)

    def _calc_advantages(self, dones: np.ndarray, rewards: np.ndarray, values: torch.Tensor, last_value: torch.Tensor, advantages: torch.Tensor):
        with torch.no_grad():
            gae = 0
            # mask values on terminal states
            mask = torch.tensor(dones).logical_not().to(self.device)
            rewards = torch.tensor(rewards).to(self.device)
            for t in reversed(range(self.worker_steps)):
                # delta = r + gamma * V' * (1-done) - V
                delta = rewards[:, t] + self.gamma * last_value * mask[:, t] - values[:, t]
                # gae = delta + gamma * lamda * gae' * (1-done)
                gae = delta + self.gamma * self.lamda * gae * mask[:, t]
                advantages[:, t] = gae
                last_value = values[:, t]

    def normalize_rnd_rewards(self):
        # OpenAI's usage of Forward filter is definitely wrong;
        # Because: https://github.com/openai/random-network-distillation/issues/16#issuecomment-488387659
        gamma = self.gamma  # Make code faster.
        intrinsic_returns = [[] for _ in range(self.n_workers)]
        for worker in range(self.n_workers):
            rewems = 0
            for step in reversed(range(self.worker_steps)):
                rewems = rewems * gamma + self.rnd_rewards[worker][step]
                intrinsic_returns[worker].insert(0, rewems)
        self.rnd_reward_rms.update(np.ravel(intrinsic_returns).reshape(-1, 1))

        self.rnd_rewards = self.rnd_rewards / (self.rnd_reward_rms.var ** 0.5)


# TODO 暂存


class RunningMeanStd:
    # https://en.wikipedia.org/wiki/Algorithms_for_calculating_variance#Parallel_algorithm
    # -> It's indeed batch normalization. :D
    def __init__(self, epsilon=1e-4, shape=()):
        self.mean = np.zeros(shape, 'float64')
        self.var = np.ones(shape, 'float64')
        self.count = epsilon

    def update(self, x):
        batch_mean = np.mean(x, axis=0)
        batch_var = np.var(x, axis=0)
        batch_count = x.shape[0]
        self.update_from_moments(batch_mean, batch_var, batch_count)

    def update_from_moments(self, batch_mean, batch_var, batch_count):
        self.mean, self.var, self.count = RunningMeanStd.update_mean_var_count_from_moments(
            self.mean, self.var, self.count, batch_mean, batch_var, batch_count)

    @staticmethod
    def update_mean_var_count_from_moments(mean, var, count, batch_mean, batch_var, batch_count):
        delta = batch_mean - mean
        tot_count = count + batch_count

        new_mean = mean + delta * batch_count / tot_count
        m_a = var * count
        m_b = batch_var * batch_count
        M2 = m_a + m_b + np.square(delta) * count * batch_count / tot_count
        new_var = M2 / tot_count
        new_count = tot_count

        return new_mean, new_var, new_count
