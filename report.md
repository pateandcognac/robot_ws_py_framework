# Logos Framework Review Report

## Project Understanding
- ROS Noetic stack meant to host a proactive embodied agent called Logos.
- Two main ROS nodes: `cognition_node.py` for LLM prompting + telemetry, and `python_worker_node.py` for executing `<py>` blocks in a persistent interpreter.
- Agent context resides under `~/robot_workspaces/<workspace>/` with `.system` config, `state` YAML-defined snippets, and future `preload_api` helpers.
- Gemini serves as the LLM backend; cognition cycles gather header/footer snippets, stitch prompt, stream thoughts/output, and enqueue results back to the Python worker.

## Architecture Notes
- `ConfigManager` loads framework/system prompt plus header/footer snippet definitions, but templating and snippet TTL caching are not yet wired.
- `IOManager` appends every message to `io_history.jsonl` and `io_buffer.jsonl` but never prunes by `max_cells`/token targets.
- `CognitionNode` orchestrates context gathering, assembles prompt (including optional images), then streams Gemini responses over `/cognition/output`.
- `PythonWorkerNode` listens on `/cognition/output`, executes `<py>` blocks, and returns stdout/stderr plus `loop_cognition` flags to `/cognition/input`.

## Findings (Bugs, Inconsistencies, Ambiguities)
1. Gemini client import/usage mixes the new `google.genai` SDK with the old `google.generativeai` patterns (`genai.configure`, `GenerativeModel`). This likely raises at runtime and needs alignment with one SDK.
2. `last_received_system_hint` is set but never inserted into the prompt, so hints (including the default "Ready for Logos's reply") never reach the LLM.
3. The system prompt still contains `{{header_name}}`, `{{footer_name}}`, etc. placeholders; without replacement the LLM sees raw braces instead of resolved context labels.
4. Context snippet TTL semantics are ignored—every snippet runs every cycle, even the `ttl: -1` cache-only prelude.
5. Context results capture the Python worker's entire `# stdout`/`# Execution finished` wrapper, so header/footer content is noisy and may break XML structure.
6. IO buffer management ignores `max_cells`/token limits, so the buffer will grow without bound.
7. Gemini safety + generation config likely mis-specified (`genai.types.GenerationConfig(**cfg)` with extra keys, safety settings dict instead of SDK objects, missing thinking config forwarding).
8. Config references `preload_api/core.py`, but no such file/folder ships with the template; initialization just warns.
9. Timeout handling publishes an error while the execution thread keeps running and will publish again later, causing duplicate responses and unclear state.
10. Regex for `<py timeout="...">` only accepts integers; fractional seconds will log a warning and fall back silently.
11. `Gemini_SDK_example.py` illustrates `FREE_GEMINI_API_KEY`, but the production code expects `GEMINI_API_KEY`; mismatch may confuse setup instructions.

## TODO Inventory
- `ConfigManager.load_configs` (system prompt templating).
- `CognitionNode`: add message metadata, better snippet request IDs.
- `PythonWorkerNode`: async stdout polling, custom interrupt, better `<py>` error handling, improved tracebacks, truncated output, smarter formatting, system hints on repeated errors.
- Message definitions suggest adding metadata fields.

## Questions / Clarifications
- Should TTL semantics support caching on the Cognition side, or does the Python worker need awareness of cached snippets?
- What is the intended format for system hints (HTML comment vs. plain text)? Need spec before wiring them into the prompt.
- Are streamed `thoughts` meant for UI only, or should they also reach the IO buffer/history?
- How strict must IO pruning be (hard cap vs. heuristic) given `my_config.yaml` limits?

## Suggestions / Next Steps
1. Decide on the Gemini SDK version, fix imports, and adapt the API call accordingly (generation config + safety settings).
2. Inject `last_received_system_hint` into the assembled prompt just before the `<me>` tag, and consider resetting only after a full response.
3. Implement snippet caching: respect negative TTL once-run caching, honor TTL counters, and store cached outputs on disk to survive restarts.
4. Normalize context outputs—strip the Python worker wrappers or introduce a dedicated payload structure instead of parsing console text.
5. Add IO buffer pruning respecting `max_cells`/token limits, possibly moving overflow to history only.
6. Create the missing `preload_api/` scaffolding or remove it from config until available.
7. Revisit timeout handling so duplicate publications do not confuse the cognition loop (e.g., mark timed-out threads and skip their later output).

