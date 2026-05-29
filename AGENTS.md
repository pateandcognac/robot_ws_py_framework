# Repository Guidelines

## Project Structure & Module Organization

This repository is a ROS catkin workspace for the Logos robot and, importantly, the shared framework/harness that gives a Vision LLM in the Gemini family control of the robot. Source packages live in `src/`: `logos_msgs` defines custom messages/actions, `logos_face` contains C++ face rendering nodes, `logos_framework` contains the cognition loop and Python worker nodes, `logos_hardware` and `logos_ui` contain ROS nodes and launch/UI code, and `logos_bringup` coordinates runtime launch/config. Convenience entrypoints are in `bin/`. Robot assets and generated motion/face sequences live in `animations/`, `sound_files/`, `models/`, `festival/`, and `porcupine/`. Documentation belongs in `docs/`; one-off generation and validation utilities belong in `tools/`. Treat `build/` and `devel/` as generated catkin output.
There's docs scattered around the fs and in `docs/`. The robot is an augmented and reconfigured Turtlebot2 with Kobuki base and RGBD Orbecc Astra camera. Turtlebot2-level stuff lives in `~/tb2_ws/`.

## Logos Workspaces & Cognition Harness

Logos runtime instances are launched against per-agent workspaces under `~/robot_workspaces/<workspace_name>/`. This repo supplies the ROS nodes and harness; each workspace supplies the distinct LLM configuration, system prompt, persistent state, and Python API code used by that particular Logos instance. This is analogous to launching Claude Code or Codex agents in different repos: the same agent framework runs in different working directories, except Logos has a single persistent Python tool/API for robot actions instead of shell tools and MCP.

Common workspace contents include `.system/system_prompt.txt`, `.system/framework_config.json`, `config/`, `src/logos/`, `src/skills/`, `src/hook_routines/`, `state/`, `ipc/`, and `hypomnemata/`. A standard reference prompt lives at `/home/robot/robot_workspaces/Logos/.system/system_prompt.txt`; read it when changing cognition, prompt assembly, hooks, Python execution, or Logos-facing APIs. These workspaces are useful sandboxes for testing different LLM/robot configurations and API experiments, but they are not security sandboxes.

`logos_framework` is the core bridge: `cognition_node.py` assembles context and streams Gemini output, `python_worker_node.py` executes `<py>` blocks in the selected workspace with the workspace `src/` on `sys.path`, and `web_ui_node.py` exposes the browser UI. Prefer `docs/LOGOS_FRAMEWORK.md` for detailed node behavior before making framework changes.

## Claude or Codex Bridge for Live Testing

This workspace includes a small third party agent-to-Logos testing bridge documented in `docs/LOGOS_CODEX_BRIDGE.md`. Use `/usr/bin/python3 tools/codex_logos_exec.py ...` to send a uniquely tagged `<py>` block through the existing `/cognition/output` -> `python_worker_node.py` -> `/cognition/input` path. The bridge defaults to request type `codex_tool` and suppresses `loop_cognition` unless `--allow-loop` is passed, so it is appropriate for live debugging without intentionally waking the Logos LLM. Results intentionally enter the normal IO buffer.

Claude / Codex can also expose this through the local Logos MCP server. The MCP server itself runs under `/home/robot/robot_ws/.venv/bin/python3` because that venv has the MCP SDK, while its `logos_python` tool shells out to `/usr/bin/python3` for ROS imports (`rospy`, generated messages). If the active runtime is a checkpoint workspace such as `Logos_001`, pass `workspace` or `workspace_path_override` so `<file path="...">` results resolve to the correct workspace.

## Build, Test, and Development Commands

- `catkin_make`: build all ROS packages from the workspace root.
- `source devel/setup.bash`: load generated message types and package paths for the current shell.
- `roslaunch logos_bringup logos_core.launch`: start the core Logos stack.
- `roslaunch logos_framework start_framework.launch`: start the framework nodes.
- `roslaunch logos_framework start_framework.launch workspace:=Logos`: launch cognition, Python worker, and web UI against `~/robot_workspaces/Logos`.
- `bin/logos_cog.sh <workspace_name>`: create/checkpoint `~/robot_workspaces/<workspace_name>` from the template workspace if needed, then launch that runtime.
- `python3 tools/validate_animation.py <path>`: validate animation JSON before committing generated sequences.
- `python3 -m py_compile src/logos_framework/scripts/*.py src/logos_ui/scripts/*.py src/logos_hardware/scripts/*.py`: quick syntax check for Python node edits.

## Coding Style & Naming Conventions

Use existing ROS package conventions: lowercase package names, snake_case Python files and ROS node scripts, and descriptive message names in `src/logos_msgs/msg`. Keep Python code PEP 8 aligned with 4-space indentation. Keep C++ in the current ROS/C++11 style used by `src/logos_face/src/face_node.cpp`. Prefer explicit ROS topic, service, and parameter names; avoid hidden behavior in launch files.

For cognition and tool execution changes, preserve the separation between framework code in this catkin workspace and per-agent code/config in `~/robot_workspaces/<name>/`. Framework code should avoid hard-coding the `Logos` workspace except as an example or default template. Python worker behavior should respect the selected workspace path, persistent interpreter semantics, `<py reset="true">`, `<py timeout="...">`, `loop_cognition`, and file-tag image IPC conventions documented in the reference system prompt.

## Testing Guidelines

There is no centralized test runner yet. For changes that affect ROS messages, launch files, or C++ nodes, run `catkin_make` after sourcing the workspace. For Python-only changes, run `python3 -m py_compile` on the touched scripts and add small runnable checks near existing utilities when practical. For cognition-framework edits, test against a named workspace with `roslaunch logos_framework start_framework.launch workspace:=<name>` or `bin/logos_cog.sh <name>` and verify that the cognition node, Python worker, and web UI agree on the same `workspace_path`. Name ad hoc tests with a `test_*.py` pattern, as in `src/test_stuff/test_fov_calc.py`.

## Commit & Pull Request Guidelines

Recent history uses short imperative or descriptive commits, for example `doc stt_node` and `tts server fix for trailing punctuation after emoji split`. Keep commits focused on one behavior or package. Pull requests should describe the robot-facing behavior changed, list build/test commands run, mention affected launch files or hardware assumptions, and include screenshots or logs for UI, face, speech, or audio changes when relevant.

## Security & Configuration Tips

Do not commit secrets, API keys, local audio captures, or machine-specific credentials. `GEMINI_API_KEY` and other provider credentials should stay in the runtime environment, not in repo or workspace files. Be careful when editing `.system/` files inside `~/robot_workspaces/<name>/`; they define the behavior and permissions perceived by that Logos instance. Keep hardware calibration, model paths, and wake-word assets documented when they are required for a node to run.
