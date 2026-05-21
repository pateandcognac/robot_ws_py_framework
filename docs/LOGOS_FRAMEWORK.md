# Logos Framework

ROS package that wires together the cognition loop used by the Logos agent. It exposes custom messages, multiple ROS nodes, and a browser- as well as terminal-based UI so that human input, LLM output, and background Python execution can cooperate.

The framework expects a writable *workspace* directory (passed in via the `~workspace_path` ROS parameter) that contains `.system/` configuration files and a `config/` directory for runtime artifacts.

## Directory Map

| Path | Purpose |
| --- | --- |
| `CMakeLists.txt`, `package.xml`, `setup.py` | Catkin metadata. Enables message generation (`CognitionInput`, `CognitionOutput`) and installs Python entrypoints. |
| `launch/start_framework.launch` | Launches the cognition node, Python worker, and web UI against a configurable workspace (`roslaunch logos_framework start_framework.launch workspace:=my_agent`). |
| `msg/` | ROS message definitions shared between all nodes. |
| `scripts/` | Python ROS nodes and tools (`cognition_node.py`, `python_worker_node.py`, `web_ui_node.py`, `urwid_tui.py`). |
| `src/` | Placeholder for future Python packages listed in `setup.py` (currently empty). |
| `web/` | Static assets that power the Socket.IO/Flask-based browser UI. |

## Custom Messages

### `CognitionInput.msg`
Published on `/cognition/input` by humans, tools, or the Python worker. Fields:

| Field | Description |
| --- | --- |
| `string type` | Origin of the message (`human`, `py_result`, `context`, etc.). |
| `string content` | Raw payload. |
| `string system_hint` | Optional inline instruction appended to the next LLM call. |
| `bool loop_cognition` | Request a new cognition cycle when `true`. |
| `string filename` | Metadata hook (e.g., hook name, file path). |

### `CognitionOutput.msg`
Published on `/cognition/output` by the cognition node. Fields:

| Field | Description |
| --- | --- |
| `string type` | Output classification (`ai`, `chunk`, `context`, etc.). |
| `string content` | LLM text or `<py>` code blocks. |
| `string filename` | Metadata hook matching `filename` in the input. |

## Node Overview (`scripts/`)

### `cognition_node.py`
Main orchestration node. Key components:

- **`ConfigManager`** loads `.system/framework_config.json`, `.system/system_prompt.txt`, and `config/my_config.yaml`, applies templating, and exposes agent, context, and model settings.
- **`IOManager`** keeps thread-safe history (`state/io_history.jsonl`) and prompt buffer (`state/io_buffer.jsonl`). Messages are truncated according to `io_safety_limits` and assigned Base36 IDs.
- **`ContextManager`** reads header/footer hook YAML files, tracks TTL-based execution rules (`ttl>0` re-run each cycle, `<0` cached, `99/-99` pinned), and persists updates back to disk.
- **Prompt assembly** stitches together header hooks, IO buffer, footer hooks, and the system hint. `<file path="...">` tags trigger inline image embedding with configurable caps (`global_max_io_buffer_media` vs. per-user overrides).
- **Gemini integration** uses `google.genai`, applies throttling/backoff rules from `framework_config["main_model"]`, and streams `CognitionOutput` messages (`thoughts`, `chunk`, final `me` content). Runtime model/API settings are session-only and can be changed through launch args or the web UI without writing secrets to disk.
- **ROS interfaces**
  - Subscribes to `/cognition/input` (`CognitionInput`) to queue messages, persist IO, and detect when to loop cognition.
  - Publishes `/cognition/output` (`CognitionOutput`) for LLM text, context hook requests (wrapping hook code in `<py>`), and streaming updates.
  - Publishes `/cognition/ui_state` (`std_msgs/String`) so the web UI can re-render header/buffer/footer snapshots with embedded images.
  - Publishes `/cognition/runtime_config/state` (`std_msgs/String`, latched JSON) and subscribes to `/cognition/runtime_config/set` (`std_msgs/String`, JSON) for session-only Gemini API profile, model, thinking, media, and Files API controls.

Successful cognition cycles:
1. Drain `/cognition/input` queue and persist content via `IOManager`.
2. Request context hooks that need to run by publishing `<py>` jobs on `/cognition/output`.
3. Wait for `context` replies (cached when TTL < 0), assemble the full Gemini prompt, respect throttling, and call the API with retry logic.
4. Stream `chunk` updates, then publish the final `me` response and return to `IDLE`.

Debug cycles from simulated web output still run hooks and publish refreshed UI state, but skip Gemini API calls even when debug code sets `loop_cognition = True`.

### `python_worker_node.py`
Executes `<py>` blocks emitted by the cognition node.

- Injects the workspace `src/` folder onto `sys.path`, changes CWD to the workspace, and imports the `logos` preload API (raising a fatal error if unavailable).
- Permanently redirects `sys.stdout`/`sys.stderr` into buffers so *any* thread output can be routed through ROS.
- Maintains a re-usable `code.InteractiveInterpreter` guarded by a lock, with optional resets triggered by `<py reset="true">`.
- Watches `/python/interrupt` (`std_msgs/String`) for cooperative interrupts serialized as JSON.
- When `/cognition/output` contains a `<py>` block, it extracts attributes (`timeout`, `reset`), runs the code on a worker thread, supports configurable timeouts (default from `framework_config["python"]["default_timeout"]`), and publishes results back on `/cognition/input`.
- Output formatting:
  - `context` executions send raw stdout/stderr.
  - Regular runs wrap sections in `# stdout`, `# stderr`, append timing info, and propagate the `loop_cognition` magic variable if set by user code.
  - Async prints flushed while idle are published as `py_async` events every 0.5s.
- Tracks consecutive failures to auto-inject instructional `system_hint` messages encouraging better debugging strategies.

### `web_ui_node.py`
Bridges ROS topics to the browser UI.

- Spins up a Flask + Socket.IO app (templates served from `../web`) and a ROS node simultaneously (Flask runs in a background thread).
- Publishes human/web input on `/cognition/input` and can optionally publish simulated debug output on `/cognition/output` when the browser toggles "Simulate AI Output". Simulated output is server-wrapped as `<py>...</py>` and forced to type `debug`.
- Subscribes to `/cognition/ui_state`, `/cognition/input`, and `/cognition/output`, forwarding:
  - `full_update` events containing complete header/buffer/footer HTML fragments.
  - `append_io` events for incremental cell additions.
  - `stream_chunk` events for live token updates.

### `urwid_tui.py`
Lightweight terminal interface using `urwid`.

- Displays a scrollable log with color-coded message types (human, ai chunks/final, thoughts, context, etc.).
- ALT+Enter submits multiline input as a `CognitionInput` message (`loop_cognition=True`).
- Subscribes to both `/cognition/input` and `/cognition/output` for visibility into system chatter and LLM responses.

## Browser UI (`web/`)

- `index.html` lays out header / IO buffer / footer panes plus a control bar with message type selection, a compact runtime Gemini config popover, a "Simulate AI Output" toggle (hides loop-cognition controls when enabled), and Ctrl+Enter submission.
- `style.css` provides a dark theme, Split.js gutters, adaptive textarea sizing, and styles for `io-cell` components appended to the buffer.
- `script.js` connects to the Socket.IO backend, renders streamed HTML + inline images, highlights code blocks via Highlight.js, auto-resizes the input textarea, and emits `human_input` events with the current mode (`input` vs `output`).

## Launching & Runtime Notes

1. Ensure the selected Gemini API profile has an exported key (`FREE_GEMINI_API_KEY` for `api_profile:=free`, `PAID_GEMINI_API_KEY` for `api_profile:=paid`). Key values are not sent to the browser UI.
2. Prepare a workspace: `~/robot_workspaces/<name>/.system/framework_config.json`, `.system/system_prompt.txt`, and `config/` with hook YAML files (`<header_name>_config.yaml`, `<footer_name>_config.yaml`).
3. Start the stack:
   ```bash
   roslaunch logos_framework start_framework.launch workspace:=Logos
   ```
   Useful optional launch args include `api_profile`, `fallback_api_profile`, `key_failover`, `model`, `thinking_level`, `media_resolution`, and `use_files_api`.
4. Optional interfaces:
   - Terminal UI: `rosrun logos_ui urwid_tui.py`
   - Browser UI: http://localhost:5000 (served by `web_ui_node.py`)
   - Speech-to-Text: `rosrun logos_ui stt_node.py`

The cognition loop uses ROS topics for decoupling, so you can inject additional tools by publishing to `/cognition/input` (following `CognitionInput.msg`) or by subscribing to `/cognition/output` for streaming updates.
