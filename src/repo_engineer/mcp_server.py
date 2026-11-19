"""Serve a ToolRegistry over MCP.

Every tool the registry knows is published with the JSON Schema the registry
enforces, and every tools/call becomes a ToolCall executed by that same
registry: an MCP client gets exactly the validation, permission policy, path
jail, sandbox and redaction an internal planner gets. A rejected or failed
call comes back as a tool error (is_error) carrying the registry's message;
structured_content mirrors the Observation.

The low-level mcp.server.Server is used (not MCPServer) because the tools are
dynamic and already carry explicit JSON Schemas.
"""

from __future__ import annotations

import threading
from typing import Any

from .state import Observation, ToolCall
from .tools import ToolRegistry

SERVER_NAME = "repo-engineer"


def build_server(registry: ToolRegistry, *, name: str = SERVER_NAME) -> Any:
    """A low-level MCP Server whose tools are the registry's tools."""
    import anyio
    import mcp_types as types
    from mcp.server import Server

    from . import __version__

    lock = threading.Lock()  # the registry runs one call at a time

    async def list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(
            tools=[
                types.Tool(name=s.name, description=s.description, input_schema=s.parameters)
                for s in registry.tool_schemas()
            ]
        )

    async def call_tool(_ctx: Any, params: Any) -> Any:
        call = ToolCall(params.name, dict(params.arguments or {}))

        def execute() -> Observation:
            with lock:
                return registry.execute(call)

        observation = await anyio.to_thread.run_sync(execute)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result_text(observation))],
            structured_content=structured(observation),
            is_error=not observation.ok,
        )

    return Server(name, version=__version__, on_list_tools=list_tools, on_call_tool=call_tool)


def result_text(observation: Observation) -> str:
    """The text an MCP client sees for one observation."""
    if observation.ok:
        return observation.output_excerpt or "(no output)"
    kind = "rejected" if observation.rejected else "error"
    detail = f"{kind}: {observation.error or 'the call failed'}"
    return f"{detail}\n{observation.output_excerpt}" if observation.output_excerpt else detail


def structured(observation: Observation) -> dict[str, Any]:
    return {
        "ok": observation.ok,
        "rejected": observation.rejected,
        "exit_code": observation.exit_code,
        "error": observation.error,
        "output": observation.output_excerpt,
    }


def serve_stdio(registry: ToolRegistry) -> None:
    """Run the registry's MCP server on stdin/stdout until the client disconnects."""
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(registry)

    async def main() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(main)
