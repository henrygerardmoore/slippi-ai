"""Test a trained model."""

from absl import app
from absl import flags
import fancyflags as ff

from slippi_ai import eval_lib, flag_utils
from slippi_ai import dolphin as dolphin_lib

PORTS = (1, 2)

PLAYERS = {p: ff.DEFINE_dict(f"p{p}", **eval_lib.PLAYER_FLAGS) for p in PORTS}

dolphin_config = dolphin_lib.DolphinConfig(
    headless=False,
    infinite_time=False,
)
DOLPHIN = ff.DEFINE_dict(
    'dolphin', **flag_utils.get_flags_from_default(dolphin_config))

flags.DEFINE_integer('runtime', 300, 'Running time, in seconds.')

FLAGS = flags.FLAGS

def main(_):
  eval_lib.disable_gpus()

  players = {
      port: eval_lib.get_player(**player.value)
      for port, player in PLAYERS.items()
  }

  dolphin = dolphin_lib.Dolphin(
      players=players,
      **dolphin_lib.DolphinConfig.kwargs_from_flags(DOLPHIN.value),
  )

  agents: list[eval_lib.Agent] = []

  for port, opponent_port in zip(PORTS, reversed(PORTS)):
    player = players[port]
    if isinstance(player, dolphin_lib.AI):
      agent = eval_lib.build_agent(
          controller=dolphin.controllers[port],
          opponent_port=opponent_port,
          console_delay=DOLPHIN.value['online_delay'],
          **PLAYERS[port].value['ai'],
      )
      agents.append(agent)

      eval_lib.update_character(player, agent.config)

  total_frames = 60 * FLAGS.runtime

  # Main loop
  for _ in range(total_frames):
    # "step" to the next frame
    gamestate = dolphin.step()

    # if gamestate.frame == -123: # initial frame
    #   controller.release_all()

    for agent in agents:
      agent.step(gamestate)

  dolphin.stop()

if __name__ == '__main__':
  # https://github.com/python/cpython/issues/87115
  __spec__ = None
  app.run(main)
