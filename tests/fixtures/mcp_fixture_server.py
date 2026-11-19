"""A minimal MCP tool server over stdio, standard library only.

Used by tests as an "external" MCP server. It speaks newline-delimited
JSON-RPC 2.0 with the methods a tools-only server needs (initialize, ping,
tools/list, tools/call) and answers anything else with "method not found",
so it runs anywhere Python does, including the pytest-only sandbox image.

Tools:
- count_lines {path}: the number of lines in a workspace file (path is
  relative to the server's working directory);
- probe_env {}: whether a credential-looking variable reached the server;
- write_note {path, text}: writes a file, to show whether the server can;
- always_fails {}: a tool error.
"""

import json
import os
import sys
from pathlib import Path

TOOLS = [
    {
        "name": "count_lines",
        "description": "Count the lines of a file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "probe_env",
        "description": "Report whether a credential reached this server.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "write_note",
        "description": "Write a text file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
    },
    {
        "name": "always_fails",
        "description": "Fail.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def call(name: str, args: dict) -> tuple[str, bool]:
    if name == "count_lines":
        return str(len(Path(args["path"]).read_text(encoding="utf-8").splitlines())), False
    if name == "probe_env":
        leaked = any("CANARY" in key for key in os.environ)
        return ("CANARY-PRESENT" if leaked else "CANARY-ABSENT"), False
    if name == "write_note":
        try:
            Path(args["path"]).write_text(args["text"], encoding="utf-8")
        except OSError as exc:
            return f"write failed: {exc.strerror}", True
        return "written", False
    if name == "always_fails":
        return "this tool always fails", True
    return f"unknown tool {name}", True


def handle(message: dict) -> dict | None:
    method = message.get("method")
    if "id" not in message:
        return None  # a notification
    if method == "initialize":
        version = message.get("params", {}).get("protocolVersion", "2025-11-25")
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fixture", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params", {})
        try:
            text, is_error = call(params["name"], params.get("arguments") or {})
        except (KeyError, OSError, UnicodeDecodeError) as exc:
            text, is_error = f"{type(exc).__name__}: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    else:
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        reply = handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
