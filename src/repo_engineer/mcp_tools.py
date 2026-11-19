"""Mount external MCP tool servers into a ToolRegistry.

Each configured server is started through the registry's sandbox (inside a
container with the workspace read-only and no network under Docker; with the
allowlisted environment and limits locally), its tools are listed once, and
each tool is registered as an ordinary ToolSpec named mcp__<server>__<tool>.
Every call therefore meets the same gates as a built-in tool
(docs/adr/0008):

- arguments are checked against the tool's own input schema;
- the permission class is EXECUTION unless the configuration says otherwise,
  whatever annotations the server publishes;
- arguments the configuration declares as paths are jailed, and reach the
  server as workspace-relative POSIX paths;
- the policy gate, redaction, output limits and the trace apply as usual.

The MCP client is asynchronous and the runtime is not, so the connections
live on an event loop in a background thread and calls are submitted to it
with a deadline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from .sandbox import StdioCommand
from .tools import Permission, ToolRegistry, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
START_TIMEOUT_S = 120.0
STOP_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class ExternalServer:
    """One external MCP server and how its tools are gated."""

    name: str
    command: tuple[str, ...]
    permission: Permission = Permission.EXECUTION
    permissions: dict[str, Permission] = field(default_factory=dict)  # per-tool overrides
    path_args: dict[str, tuple[str, ...]] = field(default_factory=dict)  # tool -> path arguments
    timeout_s: float = 60.0


class _Connection:
    """One running server: its stdio command, client and lifetime on the loop."""

    def __init__(self, server: ExternalServer, command: StdioCommand) -> None:
        self.server = server
        self.command = command
        self.client: Any = None
        self.stop: asyncio.Event | None = None
        self.done: Future[None] | None = None

    async def run(self, ready: Future[list[Any]]) -> None:
        from mcp import Client, StdioServerParameters

        self.stop = asyncio.Event()
        params = StdioServerParameters(
            command=self.command.argv[0],
            args=list(self.command.argv[1:]),
            env=self.command.env,
            cwd=str(self.command.cwd),
        )
        try:
            async with Client(params, read_timeout_seconds=self.server.timeout_s) as client:
                self.client = client
                tools: list[Any] = []
                cursor: str | None = None
                while True:
                    listing = await client.list_tools(cursor=cursor)
                    tools.extend(listing.tools)
                    cursor = listing.next_cursor
                    if not cursor:
                        break
                ready.set_result(tools)
                await self.stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            if not isinstance(exc, Exception):
                raise


class ExternalTools:
    """Context manager that mounts external MCP servers' tools into a registry."""

    def __init__(self, registry: ToolRegistry, servers: Sequence[ExternalServer]) -> None:
        self.registry = registry
        self.servers = list(servers)
        self.registered: list[str] = []
        self.skipped: list[str] = []  # tools whose names cannot be offered to models
        self._connections: list[_Connection] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> ExternalTools:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        try:
            for server in self.servers:
                self._mount(server)
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _mount(self, server: ExternalServer) -> None:
        assert self._loop is not None
        if not TOOL_NAME.fullmatch(f"mcp__{server.name}__x"):
            raise ValueError(f"external server name {server.name!r} is not a valid tool prefix")
        command = self.registry.sandbox.stdio_command(server.command, self.registry.workspace)
        connection = _Connection(server, command)
        self._connections.append(connection)
        ready: Future[list[Any]] = Future()
        connection.done = asyncio.run_coroutine_threadsafe(connection.run(ready), self._loop)
        try:
            tools = ready.result(timeout=START_TIMEOUT_S)
        except Exception as exc:
            raise RuntimeError(
                f"external MCP server {server.name!r} did not start: {type(exc).__name__}: {exc}"
            ) from exc
        for tool in tools:
            name = f"mcp__{server.name}__{tool.name}"
            if not TOOL_NAME.fullmatch(name) or name in self.registry.specs:
                self.skipped.append(name)
                continue
            self.registry.register(self._spec(connection, tool, name))
            self.registered.append(name)

    def _spec(self, connection: _Connection, tool: Any, name: str) -> ToolSpec:
        server = connection.server
        schema = dict(tool.input_schema or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        workspace = self.registry.workspace

        def run(**kwargs: Any) -> str:
            arguments = {
                key: _relative(value, workspace) if isinstance(value, Path) else value
                for key, value in kwargs.items()
            }
            result = self._call(connection, tool.name, arguments)
            text = "\n".join(
                block.text for block in result.content if getattr(block, "type", "") == "text"
            )
            if result.is_error:
                raise RuntimeError(text or "the external tool reported an error")
            if text:
                return text
            if result.structured_content is not None:
                return json.dumps(result.structured_content, sort_keys=True)
            return "(no output)"

        return ToolSpec(
            name=name,
            description=f"[external MCP tool from {server.name}] {tool.description or ''}".strip(),
            permission=server.permissions.get(tool.name, server.permission),
            required_args={},
            run=run,
            path_args=server.path_args.get(tool.name, ()),
            schema=schema,
        )

    def _call(self, connection: _Connection, tool: str, arguments: dict[str, Any]) -> Any:
        assert self._loop is not None and connection.client is not None
        future = asyncio.run_coroutine_threadsafe(
            connection.client.call_tool(tool, arguments), self._loop
        )
        try:
            return future.result(timeout=connection.server.timeout_s)
        except TimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                f"external tool {tool!r} timed out after {connection.server.timeout_s:.0f}s"
            ) from exc

    def close(self) -> None:
        """Unregister the tools, stop every server and the loop."""
        for name in self.registered:
            self.registry.unregister(name)
        self.registered = []
        loop = self._loop
        for connection in self._connections:
            if loop is not None and connection.stop is not None:
                loop.call_soon_threadsafe(connection.stop.set)
            if connection.done is not None:
                try:
                    connection.done.result(timeout=STOP_TIMEOUT_S)
                except Exception as exc:  # the server's own failure was reported at start
                    logger.warning(
                        "external MCP server %s stopped uncleanly: %s", connection.server.name, exc
                    )
                    connection.done.cancel()
            connection.command.cleanup()
        self._connections = []
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=STOP_TIMEOUT_S)
            loop.close()
        self._loop = None
        self._thread = None


def _relative(path: Path, workspace: Path) -> str:
    rel = path.relative_to(workspace).as_posix()
    return rel if rel else "."
