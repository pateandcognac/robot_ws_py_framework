# Output from the Cognition Node, primarily for the Python Worker
# topic: published on /cognition/output
string type           # E.g., "llm", "context", "state", "thoughts", "chunk"
string content        # The actual message content, which may contain a <py> block.
# TODO: add string meta field? make it the literal msg-id when type is 'llm', snippet_name when 'context' to use a filename in Intepreter, 
# string meta        # Additional metadata, e.g. snippet name or LLM message ID...?