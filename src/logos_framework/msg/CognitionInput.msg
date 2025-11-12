# Input to the Cognition Node from any source (human, python worker, etc.)
# topic: published on /cognition/input
string type           # E.g., "human", "py_result", "py_async", "context", "system"
string content        # The actual message content.
string system_hint    # Ephemeral prompt injection for guiding the next LLM output.
bool loop_cognition   # If true, triggers a full cognition cycle. If false, just appends to buffer.
string filename         # Additional metadata (msg-id, context hook_name, etc)