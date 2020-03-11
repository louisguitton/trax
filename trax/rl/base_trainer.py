# coding=utf-8
# Copyright 2020 The Trax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base class for RL trainers."""

import os

from absl import logging
import tensorflow as tf
from trax import utils


class BaseTrainer(object):
  """Base class for RL trainers."""

  def __init__(
      self,
      train_env,
      eval_env,
      output_dir=None,
      trajectory_dump_dir=None,
      trajectory_dump_min_count_per_shard=16,
      async_mode=False,
  ):
    """Base class constructor.

    Args:
      train_env: EnvProblem to use for training. Settable.
      eval_env: EnvProblem to use for evaluation. Settable.
      output_dir: Directory to save checkpoints and metrics to.
      trajectory_dump_dir: Directory to dump trajectories to. Trajectories are
        saved in shards of name <epoch>.pkl under this directory. Settable.
      trajectory_dump_min_count_per_shard: Minimum number of trajectories to
        collect before dumping in a new shard. Sharding is for efficient
        shuffling for model training in SimPLe.
      async_mode: (bool) If True, this means we are in async mode and we read
        trajectories from a location rather than interact with the environment.
    """
    self._train_env = None
    self._action_space = None
    self._observation_space = None

    # A setter that sets the above three fields.
    self.train_env = train_env
    # Should we reset the train_env? Ex: After we set the env.
    self._should_reset_train_env = True

    self._eval_env = eval_env
    self.trajectory_dump_dir = trajectory_dump_dir
    self._trajectory_dump_min_count_per_shard = (
        trajectory_dump_min_count_per_shard)
    self._trajectory_buffer = []
    self._async_mode = async_mode
    self._output_dir = output_dir
    self._trainer_reset_called = False

  def reset(self, output_dir=None):
    self._trainer_reset_called = True
    if output_dir is not None:
      self._output_dir = output_dir
    assert self._output_dir is not None
    tf.io.gfile.makedirs(self._output_dir)

  @property
  def async_mode(self):
    return self._async_mode

  @async_mode.setter
  def async_mode(self, async_mode):
    logging.vlog(1, 'Changing async mode from %s to: %s', self._async_mode,
                 async_mode)
    self._async_mode = async_mode

  def _assert_env_compatible(self, new_env):

    def assert_same_space(space1, space2):
      assert space1.shape == space2.shape
      assert space1.dtype == space2.dtype

    assert_same_space(new_env.observation_space, self._observation_space)
    assert_same_space(new_env.action_space, self._action_space)

  @property
  def eval_env(self):
    return self._eval_env

  @property
  def train_env(self):
    return self._train_env

  @train_env.setter
  def train_env(self, new_train_env):
    if self._train_env is None:
      self._action_space = new_train_env.action_space
      self._observation_space = new_train_env.observation_space
    else:
      self._assert_env_compatible(new_train_env)
    self._train_env = new_train_env
    self._should_reset_train_env = True

  @property
  def epoch(self):
    raise NotImplementedError

  def train_epoch(self, evaluate=True):
    raise NotImplementedError

  def evaluate(self):
    raise NotImplementedError

  def save(self):
    raise NotImplementedError

  def maybe_save(self):
    raise NotImplementedError

  def flush_summaries(self):
    raise NotImplementedError

  def dump_trajectories(self, force=False):
    """Dumps trajectories in a new shard.

    Should be called at most once per epoch.

    Args:
      force: (bool) Whether to complete unfinished trajectories and create a new
        shard even if we have not reached the minimum size.
    """
    pkl_module = utils.get_pickle_module()
    if self.trajectory_dump_dir is None:
      return
    tf.io.gfile.makedirs(self.trajectory_dump_dir)

    trajectories = self.train_env.trajectories
    if force:
      trajectories.complete_all_trajectories()

    # complete_all_trajectories() also adds trajectories that were just reset.
    # We don't want them since they have just the initial observation and no
    # actions, so we filter them out.
    def has_any_action(trajectory):
      return (trajectory.time_steps and
              trajectory.time_steps[0].action is not None)

    self._trajectory_buffer.extend(
        filter(has_any_action, trajectories.completed_trajectories))

    trajectories.clear_completed_trajectories()
    ready = (
        len(self._trajectory_buffer) >=
        self._trajectory_dump_min_count_per_shard)
    if ready or force:
      shard_path = os.path.join(self.trajectory_dump_dir,
                                '{}.pkl'.format(self.epoch))
      if tf.io.gfile.exists(shard_path):
        # Since we do an extra dump at the end of the training loop, we
        # sometimes dump 2 times in the same epoch. When this happens, merge the
        # two sets of trajectories.
        with tf.io.gfile.GFile(shard_path, 'rb') as f:
          self._trajectory_buffer = pkl_module.load(f) + self._trajectory_buffer
      with tf.io.gfile.GFile(shard_path, 'wb') as f:
        pkl_module.dump(self._trajectory_buffer, f)
      self._trajectory_buffer = []

  def training_loop(self, n_epochs, evaluate=True):
    """RL training loop."""
    if not self._trainer_reset_called:
      logging.info('Calling trainer reset.')
      self.reset(output_dir=self._output_dir)

    logging.info('Starting the RL training loop.')
    for _ in range(self.epoch, n_epochs):
      self.train_epoch(evaluate=evaluate)
      self.maybe_save()
      self.dump_trajectories()
    self.save()
    self.dump_trajectories(force=True)
    if evaluate:
      self.evaluate()
    self.flush_summaries()
    self.indicate_done()

  def indicate_done(self):
    """If in async mode, workers need to know we are done."""
    if not self.async_mode:
      return
    with tf.io.gfile.GFile(os.path.join(self._output_dir, '__done__'),
                           'wb') as f:
      f.write('')
