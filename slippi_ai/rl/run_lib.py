import dataclasses
import typing as tp

import numpy as np
import tensorflow as tf

from slippi_ai import (
    data,
    dolphin,
    eval_lib,
    evaluators,
    flag_utils,
    reward,
    saving,
    tf_utils,
    train_lib,
    utils,
)

from slippi_ai import value_function as vf_lib
from slippi_ai.rl import learner as learner_lib

field = lambda f: dataclasses.field(default_factory=f)

@dataclasses.dataclass
class RuntimeConfig:
  max_step: int = 10  # maximum training step
  max_runtime: tp.Optional[int] = None  # maximum runtime in seconds
  log_interval: int = 10  # seconds between logging
  save_interval: int = 300  # seconds between saving to disk

  # Periodically reset the environments to deal with memory leaks in dolphin.
  reset_every_n_steps: tp.Optional[int] = None
  # Without burnin, we see a spike in teacher_kl after every reset. My guess is
  # that this is because the trajectories and therefore gradients become highly
  # correlated, which is bad. Empirically 10 is a good value to set this to.
  burnin_steps_after_reset: int = 0

@dataclasses.dataclass
class ActorConfig:
  rollout_length: int = 64
  num_envs: int = 1
  async_envs: bool = False
  num_env_steps: int = 0
  inner_batch_size: int = 1
  async_inference: bool = False
  gpu_inference: bool = False
  num_agent_steps: int = 0

@dataclasses.dataclass
class AgentConfig:
  # TODO: merge with ActorConfig?
  path: tp.Optional[str] = None
  tag: tp.Optional[str] = None
  compile: bool = True
  name: str = 'Master Player'

  def get_kwargs(self) -> dict:
    state = eval_lib.load_state(path=self.path, tag=self.tag)
    return dict(
        state=state,
        compile=self.compile,
        name=self.name,
    )

@dataclasses.dataclass
class Config:
  runtime: RuntimeConfig = field(RuntimeConfig)

  # num_actors: int = 1
  dolphin: eval_lib.DolphinConfig = field(eval_lib.DolphinConfig)
  learner: learner_lib.LearnerConfig = field(learner_lib.LearnerConfig)
  actor: ActorConfig = field(ActorConfig)
  agent: AgentConfig = field(AgentConfig)

  # Take learner steps without changing the parameters to burn-in the
  # optimizer state for RL.
  optimizer_burnin_steps: int = 0
  # Take some steps to update just the value function before doing RL.
  # Useful if we're training against the level 9 cpu.
  value_burnin_steps: int = 0


class LearnerManager:

  def __init__(
      self,
      learner: learner_lib.Learner,
      batch_size: int,
      unroll_length: int,
      build_actor: tp.Callable[[], evaluators.RolloutWorker],
      port: int,
      burnin_steps_after_reset: int = 0,
  ):
    self._learner = learner
    self._hidden_state = learner.initial_state(batch_size)
    self._build_actor = build_actor
    self._unroll_length = unroll_length
    self._port = port
    self._burnin_steps_after_reset = burnin_steps_after_reset

    self.learner_profilers = {True: utils.Profiler(), False: utils.Profiler()}
    self.rollout_profiler = utils.Profiler()
    self.reset_profiler = utils.Profiler(burnin=0)

    with self.reset_profiler:
      self.initialize_actor()

  def initialize_actor(self):
    self.actor = self._build_actor()
    self.actor.start()
    for _ in range(self._burnin_steps_after_reset):
      self.step(train=False)

  def reset_actor(self):
    with self.reset_profiler:
      self.actor.stop()
      self.initialize_actor()

  def step(self, train: bool = True) -> tuple[evaluators.Trajectory, dict]:
    with self.rollout_profiler:
      trajectories, timings = self.actor.rollout(self._unroll_length)
      trajectory = trajectories[self._port]

    with self.learner_profilers[train]:
      self._hidden_state, metrics = self._learner.compiled_step(
          trajectory, self._hidden_state, train=train)
    return trajectory, dict(learner=metrics, actor_timing=timings)

class Logger:

  def __init__(self):
    self.buffer = []

  def record(self, to_log):
    to_log = utils.map_single_structure(train_lib.mean, to_log)
    self.buffer.append(to_log)

  def flush(self, step: int) -> dict:
    to_log = tf.nest.map_structure(lambda *xs: np.mean(xs), *self.buffer)
    train_lib.log_stats(to_log, step, take_mean=False)
    self.buffer = []
    return to_log

def run(config: Config):
  pretraining_state = eval_lib.load_state(
      tag=config.agent.tag, path=config.agent.path)

  saving.upgrade_config(pretraining_state['config'])
  pretraining_config = flag_utils.dataclass_from_dict(
      train_lib.Config, pretraining_state['config'])

  # Make sure we don't train the teacher
  with tf_utils.non_trainable_scope():
    teacher = saving.load_policy_from_state(pretraining_state)
  policy = saving.load_policy_from_state(pretraining_state)

  rl_state = pretraining_state
  rl_state['step'] = 0

  # TODO: put this code into saving.py or train_lib.py
  vf_config = pretraining_config.value_function
  value_function = None
  if vf_config.train_separate_network:
    value_net_config = pretraining_config.network
    if vf_config.separate_network_config:
      value_net_config = vf_config.network
    value_function = vf_lib.ValueFunction(
        network_config=value_net_config,
        embed_state_action=policy.embed_state_action,
    )

  # This allows us to only update the optimizer state.
  learning_rate = tf.Variable(
      config.learner.learning_rate,
      name='learning_rate',
      trainable=False)
  learner_config = dataclasses.replace(
      config.learner, learning_rate=learning_rate)

  batch_size = config.actor.num_envs
  learner = learner_lib.Learner(
      config=learner_config,
      teacher=teacher,
      policy=policy,
      value_function=value_function,
  )

  # Initialize and restore variables
  embedders = dict(policy.embed_state_action.embedding)
  embed_controller = policy.controller_embedding
  dummy_trajectory = evaluators.Trajectory(
      states=embedders['state'].dummy([2, 1]),
      name=embedders['name'].dummy([2, 1]),
      actions=eval_lib.dummy_sample_outputs(embed_controller, [2, 1]),
      rewards=np.full([1, 1], 0, dtype=np.float32),
      is_resetting=np.full([2, 1], False),
      initial_state=policy.initial_state(1),
      delayed_actions=[
          eval_lib.dummy_sample_outputs(embed_controller, [1])
      ] * policy.delay,
  )

  learner.initialize(dummy_trajectory, pretraining_state['state'])

  PORT = 1
  ENEMY_PORT = 2
  dolphin_kwargs = dict(
      players={
          PORT: dolphin.AI(),
          ENEMY_PORT: dolphin.CPU(),
      },
      **dataclasses.asdict(config.dolphin),
  )

  agent_kwargs = dict(
      state=rl_state,
      compile=config.agent.compile,
      name=config.agent.name,
  )

  env_kwargs = {}
  if config.actor.async_envs:
    env_kwargs.update(
        num_steps=config.actor.num_env_steps,
        inner_batch_size=config.actor.inner_batch_size,
    )

  build_actor = lambda: evaluators.RolloutWorker(
      agent_kwargs={PORT: agent_kwargs},
      dolphin_kwargs=dolphin_kwargs,
      env_kwargs=env_kwargs,
      num_envs=config.actor.num_envs,
      async_envs=config.actor.async_envs,
      async_inference=config.actor.async_inference,
      use_gpu=config.actor.gpu_inference,
  )

  learner_manager = LearnerManager(
      learner=learner,
      batch_size=batch_size,
      unroll_length=config.actor.rollout_length,
      port=PORT,
      build_actor=build_actor,
      burnin_steps_after_reset=config.runtime.burnin_steps_after_reset,
  )

  step_profiler = utils.Profiler()

  def get_log_data(
      trajectory: evaluators.Trajectory,
      metrics: dict,
  ):
    timings = {}

    # TODO: we shouldn't take the mean over these timings
    step_time = step_profiler.mean_time()
    steps_per_rollout = config.actor.num_envs * config.actor.rollout_length
    fps = steps_per_rollout / step_time
    mps = fps / (60 * 60)  # in-game minutes per second

    timings.update(
        rollout=learner_manager.rollout_profiler.mean_time(),
        learner=learner_manager.learner_profilers[True].mean_time(),
        reset=learner_manager.reset_profiler.mean_time(),
        total=step_time,
        fps=fps,
        mps=mps,
    )
    actor_timing = metrics['actor_timing']
    for key in ['env_pop', 'env_push']:
      timings[key] = actor_timing[key]
    for key in ['agent_pop', 'agent_step']:
      timings[key] = actor_timing[key][PORT]

    learner_metrics = metrics['learner']
    kos = reward.compute_rewards(trajectory.states, damage_ratio=0)
    kos_per_minute = kos.mean() * (60 * 60)

    return dict(
        ko_diff=kos_per_minute,
        timings=timings,
        learner=learner_metrics,
    )

  logger = Logger()

  @utils.periodically(config.runtime.log_interval)
  def maybe_flush(step):
    print('\nStep:', step)

    metrics = logger.flush(step)

    timings = metrics['timings']
    timing_str = ', '.join(
        ['{k}: {v:.3f}'.format(k=k, v=v) for k, v in timings.items()])
    print(timing_str)

    ko_diff = metrics['ko_diff']
    print(f'KO_diff_per_minute: {ko_diff:.3f}')

    learner_metrics = metrics['learner']
    for key in ['teacher_kl', 'actor_kl']:
      print(f'{key}: {learner_metrics[key]:.3f}')
    print(f'uev: {learner_metrics["value"]["uev"]:.3f}')

  try:
    for step in range(config.runtime.max_step):
      with step_profiler:
        log_interval = config.runtime.reset_every_n_steps
        if step > 0 and log_interval and step % log_interval == 0:
          learner_manager.reset_actor()

        policy_vars = {PORT: learner.policy_variables()}
        learner_manager.actor.update_variables(policy_vars)

        if step < config.learner_burnin_steps:
          learning_rate.assign(0)
        else:
          learning_rate.assign(config.learner.learning_rate)

        trajectory, metrics = learner_manager.step()

      if step > 0:
        logger.record(get_log_data(trajectory, metrics))
        maybe_flush(step)
  finally:
    learner_manager.actor.stop()
