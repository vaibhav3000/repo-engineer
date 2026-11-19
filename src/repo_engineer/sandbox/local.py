"""The local sandbox: the host interpreter with limits, no isolation.

What it does: the runtime's own interpreter runs pytest with -P (a planted
pytest.py cannot shadow pytest), an allowlisted environment, a fresh bytecode
cache and tmp_path base outside the workspace, a deadline with a process-tree
kill, a bounded output tail, and resource limits (POSIX rlimits set by a
launcher that then execs pytest; a Windows job object capping memory and the
number of processes).

What it does not do: agent-written code still runs as the host user, with the
host's filesystem and network, and a process that calls setsid() (POSIX) or is
created through WMI (Windows) leaves the tree. Use the Docker sandbox for
untrusted repositories.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ..process import JobLimits, child_env, run_bounded
from .base import (
    PytestInvocation,
    SandboxLimits,
    SandboxRun,
    StdioCommand,
    pytest_options,
    read_junit,
)

LAUNCHER = Path(__file__).with_name("_launcher.py")
# CPU seconds allowed beyond the wall-clock deadline before RLIMIT_CPU fires.
CPU_GRACE_S = 5


class LocalSandbox:
    """Runs pytest on the host with the runtime's interpreter and resource limits."""

    name = "local"

    def __init__(self, limits: SandboxLimits | None = None) -> None:
        self.limits = limits or SandboxLimits()

    def pytest_argv(
        self, workspace: Path, invocation: PytestInvocation, extra: tuple[str, ...] = ()
    ) -> list[str]:
        """The interpreter and pytest arguments, without the limit launcher."""
        return [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            *pytest_options(invocation, root=workspace.as_posix(), devnull=os.devnull, extra=extra),
        ]

    def run_pytest(
        self, workspace: Path, invocation: PytestInvocation, timeout_s: float
    ) -> SandboxRun:
        scratch = Path(tempfile.mkdtemp(prefix="repo_engineer_pytest_"))
        try:
            env = child_env()
            # Bytecode planted in the workspace is never read, and pytest's
            # tmp_path base (whose default names the user) lives here too.
            env["PYTHONPYCACHEPREFIX"] = str(scratch / "pycache")
            extra = [f"--basetemp={scratch / 'tmp'}"]
            report_path = scratch / "junit.xml"
            if invocation.junit:
                extra.append(f"--junitxml={report_path}")
            argv = self.pytest_argv(workspace, invocation, tuple(extra))
            result = run_bounded(
                self._with_limits(argv, timeout_s),
                cwd=workspace,
                env=env,
                timeout_s=timeout_s,
                job_limits=JobLimits(
                    memory_bytes=self.limits.memory_mb * 1024 * 1024,
                    active_processes=self.limits.pids,
                ),
            )
            report = read_junit(report_path) if invocation.junit else None
        finally:
            shutil.rmtree(scratch, ignore_errors=True)  # scratch space only
        return SandboxRun(
            exit_code=result.exit_code,
            output=result.output,
            timed_out=result.timed_out,
            omitted_bytes=result.omitted_bytes,
            report=report,
        )

    def stdio_command(self, argv: Sequence[str], workspace: Path) -> StdioCommand:
        """A long-running stdio server (an external MCP server) on the host.

        It gets the allowlisted environment, the workspace as its working
        directory and, on POSIX, the memory, file-size and open-file limits.
        It is not isolated: it can reach the host filesystem and network, and
        on Windows it runs outside any job object (a documented residual).
        """
        return StdioCommand(
            argv=tuple(self._with_limits(list(argv), None)),
            env=child_env(),
            cwd=workspace,
        )

    def _with_limits(self, argv: list[str], timeout_s: float | None) -> list[str]:
        """On POSIX, prefix argv with the launcher that sets rlimits and execs it."""
        if sys.platform == "win32":
            return argv  # the job object carries the limits
        limits = {
            "memory_mb": self.limits.memory_mb,
            "cpu_seconds": 0 if timeout_s is None else math.ceil(timeout_s) + CPU_GRACE_S,
            "file_size_mb": self.limits.file_size_mb,
            "nofile": self.limits.nofile,
        }
        return [sys.executable, "-I", "-S", str(LAUNCHER), json.dumps(limits), "--", *argv]
