"""MCP in both directions: the registry served over MCP, and external MCP tools mounted
into the registry. Either way every call meets the same validation, policy, jail and
sandbox as a built-in tool call."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import docker_ready  # noqa: F401  (used by the skipif condition)

pytest.importorskip("mcp")

import anyio
from mcp import Client

from repo_engineer.mcp_server import build_server
from repo_engineer.mcp_tools import ExternalServer, ExternalTools
from repo_engineer.sandbox import DockerSandbox, LocalSandbox
from repo_engineer.state import ToolCall
from repo_engineer.tools import Permission, Policy, ToolRegistry

FIXTURE = Path(__file__).with_name("fixtures") / "mcp_fixture_server.py"


def serve(registry: ToolRegistry, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Call the registry's MCP server in memory; return the listing and each result."""

    async def main() -> list[Any]:
        async with Client(build_server(registry), raise_exceptions=True) as client:
            out: list[Any] = [await client.list_tools()]
            for name, args in calls:
                out.append(await client.call_tool(name, args))
            return out

    return anyio.run(main)


def text(result: Any) -> str:
    return "\n".join(block.text for block in result.content)


def test_the_server_publishes_the_registry_schemas(workspace: Path) -> None:
    registry = ToolRegistry(workspace)
    (listing,) = serve(registry, [])
    published = {tool.name: tool.input_schema for tool in listing.tools}
    assert published == {s.name: s.parameters for s in registry.tool_schemas()}


def test_a_call_over_mcp_runs_through_the_registry(workspace: Path) -> None:
    _, result = serve(ToolRegistry(workspace), [("read_file", {"path": "app.py"})])
    assert not result.is_error and "VALUE = 1" in text(result)
    assert result.structured_content["ok"] is True


def test_the_jail_applies_to_mcp_clients(workspace: Path) -> None:
    _, escape, extra = serve(
        ToolRegistry(workspace),
        [("read_file", {"path": "../secret.txt"}), ("read_file", {"path": "a", "x": 1})],
    )
    assert escape.is_error and escape.structured_content["rejected"] is True
    assert "path escape rejected" in text(escape)
    assert extra.is_error and "unknown arguments" in text(extra)


def test_policy_applies_to_mcp_clients(workspace: Path) -> None:
    registry = ToolRegistry(workspace, policy={Permission.EXECUTION: Policy.DENY})
    _, result = serve(registry, [("run_tests", {"cmd": "python -m pytest -q tests/"})])
    assert result.is_error and "policy denies EXECUTION" in text(result)


def test_test_runs_over_mcp_report_their_exit_code(workspace: Path) -> None:
    _, result = serve(
        ToolRegistry(workspace), [("run_tests", {"cmd": "python -m pytest -q tests/"})]
    )
    assert not result.is_error and result.structured_content["exit_code"] == 0


# ------------------------------------------------------------- external tools


def mounted(
    workspace: Path, sandbox: Any, command: list[str], **config: Any
) -> tuple[ToolRegistry, ExternalServer]:
    shutil.copy(FIXTURE, workspace / "fixture_server.py")
    return ToolRegistry(workspace, sandbox=sandbox), ExternalServer(
        name="fx", command=tuple(command), **config
    )


def test_external_tools_are_registered_with_their_own_schemas(workspace: Path) -> None:
    registry, server = mounted(
        workspace,
        LocalSandbox(),
        [sys.executable, "fixture_server.py"],
        path_args={"count_lines": ("path",)},
    )
    with ExternalTools(registry, [server]):
        spec = registry.specs["mcp__fx__count_lines"]
        assert spec.permission is Permission.EXECUTION
        assert spec.json_schema()["required"] == ["path"]
        ok = registry.execute(ToolCall("mcp__fx__count_lines", {"path": "app.py"}))
        assert ok.ok and ok.output_excerpt == "1"
        bad = registry.execute(ToolCall("mcp__fx__count_lines", {"path": 3}))
        assert bad.rejected and "must be string" in (bad.error or "")


def test_declared_path_arguments_of_external_tools_are_jailed(workspace: Path) -> None:
    registry, server = mounted(
        workspace,
        LocalSandbox(),
        [sys.executable, "fixture_server.py"],
        path_args={"count_lines": ("path",)},
    )
    with ExternalTools(registry, [server]):
        obs = registry.execute(ToolCall("mcp__fx__count_lines", {"path": "../outside.txt"}))
        assert obs.rejected and "path escape rejected" in (obs.error or "")


def test_external_tool_errors_are_failed_calls(workspace: Path) -> None:
    registry, server = mounted(workspace, LocalSandbox(), [sys.executable, "fixture_server.py"])
    with ExternalTools(registry, [server]):
        obs = registry.execute(ToolCall("mcp__fx__always_fails", {}))
        assert not obs.ok and not obs.rejected and "always fails" in (obs.error or "")


def test_policy_gates_external_tools(workspace: Path) -> None:
    shutil.copy(FIXTURE, workspace / "fixture_server.py")
    registry = ToolRegistry(workspace, policy={Permission.EXECUTION: Policy.DENY})
    server = ExternalServer("fx", (sys.executable, "fixture_server.py"))
    with ExternalTools(registry, [server]):
        obs = registry.execute(ToolCall("mcp__fx__probe_env", {}))
        assert obs.rejected and "policy denies EXECUTION" in (obs.error or "")


def test_external_servers_do_not_see_credentials(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPO_ENGINEER_CANARY_TOKEN", "canary-token-not-a-real-secret")
    registry, server = mounted(workspace, LocalSandbox(), [sys.executable, "fixture_server.py"])
    with ExternalTools(registry, [server]):
        obs = registry.execute(ToolCall("mcp__fx__probe_env", {}))
        assert obs.ok and obs.output_excerpt == "CANARY-ABSENT"


def test_the_tools_are_removed_and_the_server_stopped_on_exit(workspace: Path) -> None:
    registry, server = mounted(workspace, LocalSandbox(), [sys.executable, "fixture_server.py"])
    with ExternalTools(registry, [server]):
        assert "mcp__fx__probe_env" in registry.specs
    assert "mcp__fx__probe_env" not in registry.specs


def test_a_server_that_cannot_start_is_an_error(workspace: Path) -> None:
    registry = ToolRegistry(workspace)
    server = ExternalServer("fx", (sys.executable, "does_not_exist.py"))
    with pytest.raises(RuntimeError, match="fx"), ExternalTools(registry, [server]):
        pass


@pytest.mark.skipif("not docker_ready()", reason="no Linux Docker daemon answers")
def test_external_servers_run_inside_the_docker_sandbox(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPO_ENGINEER_CANARY_TOKEN", "canary-token-not-a-real-secret")
    registry, server = mounted(
        workspace,
        DockerSandbox(),
        ["python", "fixture_server.py"],
        path_args={"count_lines": ("path",)},
    )
    with ExternalTools(registry, [server]):
        assert (
            registry.execute(ToolCall("mcp__fx__count_lines", {"path": "app.py"})).output_excerpt
            == "1"
        )
        assert (
            registry.execute(ToolCall("mcp__fx__probe_env", {})).output_excerpt == "CANARY-ABSENT"
        )
        write = registry.execute(ToolCall("mcp__fx__write_note", {"path": "note.txt", "text": "x"}))
        assert not write.ok and "Read-only file system" in (write.error or "")
    assert not (workspace / "note.txt").exists()


def test_local_external_servers_can_write_the_workspace(workspace: Path) -> None:
    """The local backend does not isolate external servers (a documented residual)."""
    registry, server = mounted(workspace, LocalSandbox(), [sys.executable, "fixture_server.py"])
    with ExternalTools(registry, [server]):
        obs = registry.execute(ToolCall("mcp__fx__write_note", {"path": "note.txt", "text": "x"}))
    assert obs.ok and (workspace / "note.txt").exists()


def test_a_flooding_external_server_is_cut_off(workspace: Path) -> None:
    """Found by the Step 2 review: output with no newline was buffered on the
    host without a cap; the relay now stops a server whose message grows past
    the limit."""
    import time

    registry, server = mounted(
        workspace, LocalSandbox(), [sys.executable, "fixture_server.py"], timeout_s=60.0
    )
    with ExternalTools(registry, [server]):
        started = time.monotonic()
        obs = registry.execute(ToolCall("mcp__fx__flood", {"megabytes": 64}))
        assert not obs.ok and time.monotonic() - started < 45
        later = registry.execute(ToolCall("mcp__fx__probe_env", {}))
        assert not later.ok  # the server was stopped
