"""The Sandbox seam and the local backend: invocations, reports, resource limits."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from repo_engineer.sandbox import (
    LocalSandbox,
    PytestInvocation,
    SandboxLimits,
    SandboxRun,
    StdioCommand,
    select_sandbox,
)
from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry


def invocation(*args: str, junit: bool = False) -> PytestInvocation:
    return PytestInvocation(args=tuple(args), config=None, import_roots=(".",), junit=junit)


def write_test(ws: Path, name: str, body: str) -> None:
    (ws / "tests" / name).write_text(body, encoding="utf-8")


class RecordingSandbox:
    """A sandbox that records what the registry asks it to run."""

    name = "recording"
    output_aliases: dict[str, str] = {}  # noqa: RUF012

    def __init__(self) -> None:
        self.calls: list[tuple[Path, PytestInvocation, float]] = []

    def run_pytest(
        self, workspace: Path, invocation: PytestInvocation, timeout_s: float
    ) -> SandboxRun:
        self.calls.append((workspace, invocation, timeout_s))
        return SandboxRun(exit_code=0, output="1 passed", timed_out=False, omitted_bytes=0)

    def stdio_command(self, argv: Sequence[str], workspace: Path) -> StdioCommand:
        return StdioCommand(argv=tuple(argv), env={}, cwd=workspace)


def test_local_sandbox_runs_pytest_and_reads_the_junit_report(workspace: Path) -> None:
    run = LocalSandbox().run_pytest(workspace, invocation("-q", "./tests", junit=True), 60.0)
    assert run.exit_code == 0 and not run.timed_out
    assert run.report is not None and run.report.passed == 1 and run.report.failures == 0


def test_local_sandbox_reports_a_failing_suite(workspace: Path) -> None:
    write_test(workspace, "test_bad.py", "def test_bad():\n    assert False\n")
    run = LocalSandbox().run_pytest(workspace, invocation("-q", "./tests", junit=True), 60.0)
    assert run.exit_code == 1
    assert run.report is not None and run.report.failures == 1


def test_registry_hands_checked_invocations_to_its_sandbox(workspace: Path) -> None:
    sandbox = RecordingSandbox()
    registry = ToolRegistry(workspace, sandbox=sandbox, exec_timeout_s=7.0)
    obs = registry.execute(ToolCall("run_tests", {"cmd": "python -m pytest -q tests/"}))
    assert obs.ok and obs.exit_code == 0
    ((ws, inv, timeout),) = sandbox.calls
    assert ws == workspace.resolve() and timeout == 7.0
    assert inv == PytestInvocation(
        args=("-q", "./tests"), config=None, import_roots=(".",), junit=False
    )


def test_invocations_name_the_workspace_config_and_its_pythonpath(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "pytest.ini").write_text("[pytest]\npythonpath = src ../outside\n", encoding="utf-8")
    sandbox = RecordingSandbox()
    ToolRegistry(ws, sandbox=sandbox).execute(ToolCall("run_tests", {"cmd": "python -m pytest"}))
    ((_, inv, _),) = sandbox.calls
    assert inv.config == "pytest.ini" and inv.import_roots == (".", "src")


def test_verification_runs_through_the_sandbox(workspace: Path) -> None:
    sandbox = RecordingSandbox()
    result = ToolRegistry(workspace, sandbox=sandbox).verify("python -m pytest -q tests/")
    # The recording sandbox writes no JUnit report, so the run cannot pass.
    assert not result.passed and "no test report" in result.reason
    ((_, inv, _),) = sandbox.calls
    assert inv.junit


def test_select_local_never_needs_docker() -> None:
    assert select_sandbox("local").name == "local"


def test_select_rejects_unknown_modes() -> None:
    with pytest.raises(ValueError, match="sandbox mode"):
        select_sandbox("vm")


MEMORY_BOMB = (
    "def test_bomb():\n    data = b'x' * (1536 * 1024 * 1024)\n    print('ALLOCATED', len(data))\n"
)


def test_memory_limit_stops_an_oversized_allocation(workspace: Path) -> None:
    write_test(workspace, "test_bomb.py", MEMORY_BOMB)
    sandbox = LocalSandbox(SandboxLimits(memory_mb=512))
    run = sandbox.run_pytest(workspace, invocation("-q", "-s", "./tests/test_bomb.py"), 60.0)
    assert "ALLOCATED" not in run.output
    assert run.exit_code != 0


@pytest.mark.skipif(os.name != "nt", reason="the job object process limit is Windows only")
def test_windows_job_caps_the_number_of_processes(workspace: Path) -> None:
    write_test(
        workspace,
        "test_spawn.py",
        "import subprocess, sys\n\n\ndef test_spawn():\n    started = []\n"
        "    for _ in range(12):\n        try:\n"
        "            started.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']))\n"
        "        except OSError:\n            break\n"
        "    print('STARTED', len(started))\n"
        "    for p in started:\n        p.kill()\n",
    )
    sandbox = LocalSandbox(SandboxLimits(pids=6))
    run = sandbox.run_pytest(workspace, invocation("-q", "-s", "./tests/test_spawn.py"), 60.0)
    assert "STARTED 12" not in run.output
    assert "STARTED" in run.output


@pytest.mark.skipif(sys.platform == "win32", reason="rlimits are POSIX only")
def test_posix_launcher_applies_rlimits(workspace: Path) -> None:
    write_test(
        workspace,
        "test_limits.py",
        "import resource\n\n\ndef test_limits():\n"
        "    print('NOFILE', resource.getrlimit(resource.RLIMIT_NOFILE)[0])\n"
        "    print('FSIZE', resource.getrlimit(resource.RLIMIT_FSIZE)[0])\n",
    )
    sandbox = LocalSandbox(SandboxLimits(nofile=256, file_size_mb=8))
    run = sandbox.run_pytest(workspace, invocation("-q", "-s", "./tests/test_limits.py"), 60.0)
    assert run.exit_code == 0, run.output
    assert "NOFILE 256" in run.output and f"FSIZE {8 * 1024 * 1024}" in run.output
