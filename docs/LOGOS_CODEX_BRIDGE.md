# Logos Codex Bridge

`tools/codex_logos_exec.py` is a small debugging bridge that lets Codex or a
human send one Python block to the live Logos Python worker.

It uses the existing framework path:

1. Publish a uniquely tagged `logos_framework/CognitionOutput` on
   `/cognition/output`.
2. Let `python_worker_node.py` execute the embedded `<py>` block in the
   selected workspace interpreter.
3. Wait for the matching `logos_framework/CognitionInput` result on
   `/cognition/input`.

The default request type is `codex_tool`, and the default behavior suppresses
`loop_cognition` so this path does not wake the Logos LLM unless
`--allow-loop` is passed.

## Examples

```bash
/usr/bin/python3 tools/codex_logos_exec.py 'print("hello from Codex")'
```

```bash
/usr/bin/python3 tools/codex_logos_exec.py --json 'print(logos.base.get_battery())'
```

```bash
/usr/bin/python3 tools/codex_logos_exec.py --timeout 10 --file /tmp/probe.py
```

```bash
/usr/bin/python3 tools/codex_logos_exec.py --reset 'print("fresh interpreter")'
```

Use the system ROS Python for the CLI bridge. It needs `rospy` and the generated
`logos_framework` message package, which are available in the ROS Python
environment. The repo virtualenv is used for MCP hosting, not for ROS topic I/O.

If the output contains tags like:

```xml
<file path="ipc/capture.png">camera probe</file>
```

the CLI prints the workspace-resolved absolute path in text mode and includes
it in `file_tags` in JSON mode.

## MCP Shape

`tools/logos_mcp_server.py` wraps the CLI and exposes the first useful MCP
tools:

- `logos_python(code, timeout=None, reset=False, allow_loop=False)`
- `logos_debug_vision(topic=None)`
- `logos_io_tail(count=20)`

The MCP server runs with the repo virtualenv because that is where the MCP SDK
is installed:

```bash
/home/robot/robot_ws/.venv/bin/python3 /home/robot/robot_ws/tools/logos_mcp_server.py
```

Inside `logos_python`, the MCP server calls the CLI bridge with
`/usr/bin/python3` by default so ROS imports work. Override this with
`LOGOS_ROS_PYTHON` if a different ROS-capable Python is needed:

```bash
LOGOS_ROS_PYTHON=/usr/bin/python3 /home/robot/robot_ws/.venv/bin/python3 /home/robot/robot_ws/tools/logos_mcp_server.py
```

A Codex MCP config entry looks like:

```toml
[mcp_servers.logos]
command = "/home/robot/robot_ws/.venv/bin/python3"
args = ["/home/robot/robot_ws/tools/logos_mcp_server.py"]
```

After changing this MCP server file or environment, restart Codex so it relaunches
the server process.

The current bridge deliberately writes results through the normal IO buffer,
which is useful for debugging and matches the existing framework design.

If the active Logos runtime is launched against a workspace other than `Logos`
(for example `Logos_001`), pass `workspace` or `workspace_path_override` to the
MCP tools so file-tag paths resolve to the correct workspace.
