# Logos

Logos is a personal ROS robot project built as a shared catkin workspace for the robot itself and the framework that lets an LLM-driven cognition loop talk to it.

This repository is the common codebase: ROS packages, launch files, Python nodes, UI assets, face and audio helpers, messages, and bundled robot assets. The per-agent runtime state, prompts, and workspace-specific Python code live elsewhere under `~/robot_workspaces/<name>/`.

## What's in here

- `src/logos_msgs/` - custom ROS messages and actions
- `src/logos_face/` - face rendering nodes and configs
- `src/logos_framework/` - cognition loop, Python worker, and web UI bridge
- `src/logos_hardware/` - hardware-facing ROS nodes and TTS/audio glue
- `src/logos_ui/` - terminal UI and speech-to-text helpers
- `src/logos_bringup/` - launch files and runtime coordination
- `animations/`, `sound_files/`, `wakewords/`, `models/` - robot assets and bundled runtime data
- `bin/` - convenience launch wrappers
- `docs/` - architecture and startup docs
- `tools/` - generation, validation, and one-off utilities

This is a ROS Noetic catkin workspace. The generated build products live in `build/`, `devel/`, `logs/`, and related catkin output directories and should not be treated as source.

## Workspace Model

The repo is the shared framework. Each running Logos instance uses its own workspace under `~/robot_workspaces/<name>/` with its own:

- `.system/` prompt and framework config
- `config/` hooks and runtime config
- `src/logos/` workspace-specific Python API code
- `src/skills/` and other agent-local behavior
- `state/`, `ipc/`, and other runtime artifacts

That separation matters: this repo supplies the robot harness, while the workspace supplies the individual agent identity, prompt assembly inputs, and persistent state.

## Basic Setup

```bash
source /opt/ros/noetic/setup.bash
catkin_make
source devel/setup.bash
```

If you are working against a named workspace, the usual start path is:

```bash
roslaunch logos_framework start_framework.launch workspace:=Logos
```

For the full robot stack, the helper scripts in `bin/` are the quickest entry points:

```bash
bin/logos_cog.sh <workspace_name>
bin/logos_core.sh
bin/logos_face.sh
bin/logos_stt.sh
bin/logos_idle.sh
```

The core hardware stack is started separately from the cognition stack. The more detailed startup guide lives in [docs/LOGOS_STARTUP_AND_BASIC_USAGE.md](docs/LOGOS_STARTUP_AND_BASIC_USAGE.md).

## Architecture Docs

- [Framework internals](docs/LOGOS_FRAMEWORK.md)
- [ROS and launch architecture](docs/LOGOS_ROS_ARCHITECTURE.md)
- [Startup and basic usage](docs/LOGOS_STARTUP_AND_BASIC_USAGE.md)

If you are orienting yourself from scratch, the docs index in [docs/README.md](docs/README.md) is the quickest next stop.

## Hardware And Runtime Assumptions

This project currently assumes a physical Logos setup roughly like the one used during development:

- a ThinkPad/robot computer running the ROS workspace
- external monitors for the desktop and the face display
- a Kobuki mobile base when the body is powered on
- microphone input for speech-to-text
- an exported Gemini API key in the environment, not in source files

The repository also includes bundled voice, wake-word, and animation assets that the runtime expects to find locally.

## Current Status

This is an evolving personal robot project, built openly and a little experimentally. The architecture is real, but it is not polished in the way a mature commercial product would be. Expect rough edges, iterative docs, and the occasional "I learned this by getting it wrong first" energy.

That honesty is intentional. The goal here is to make the system understandable and usable for technically curious readers without pretending it is finished.

## License

This repository is licensed under the MIT License. See [LICENSE](LICENSE).

Several bundled third-party assets in `wakewords/` and `festival/` should still be reviewed for their redistribution terms before publishing broadly.
