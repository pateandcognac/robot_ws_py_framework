# Output from the Cognition Node, primarily for the Python Worker
# topic: published on /cognition/output
string type           # E.g., "llm", "context", "state", "thoughts", "chunk"
string content        # The actual message content, which may contain a <py> block.
string filename         # Additional metadata (msg-id, context snippet_name, etc)