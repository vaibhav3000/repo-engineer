# External MCP servers run inside the sandbox and their tools go through the registry

A run may mount external MCP tool servers. Each server process is started
through the active sandbox (a container with the workspace read-only and no
network under Docker), and each of its tools is registered as an ordinary
`ToolSpec`, so every call passes the same validation, permission policy, jail,
redaction and trace as a built-in tool. External tools default to the
`EXECUTION` permission class, whatever annotations the server publishes,
because those annotations are the server's own claims.

Calling the external client directly from the planner would have been less
code, and it would have created a second path to side effects that none of
the gates sees. Arguments are checked against the tool's published input
schema with a small validator for the JSON Schema subset tool schemas use,
which avoids a new dependency; anything the configuration marks as a path is
also jailed.
