"""Types shared by the sandbox backends and the registry."""

from __future__ import annotations

import shlex
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class SandboxError(RuntimeError):
    """The sandbox itself failed (daemon unreachable, image missing): not a test result."""


@dataclass(frozen=True)
class SandboxLimits:
    """Resource limits for one pytest run. Each backend applies what it can."""

    memory_mb: int = 2048  # Docker memory (no swap), POSIX address space, Windows job memory
    cpus: float = 1.5  # Docker only
    pids: int = 256  # Docker pids limit, Windows active processes
    nofile: int = 1024  # open files (Docker ulimit, POSIX rlimit)
    file_size_mb: int = 256  # largest file a process may write (POSIX rlimit)
    tmpfs_mb: int = 256  # size of the container's only writable place (Docker)


@dataclass(frozen=True)
class PytestInvocation:
    """A checked pytest run, described without any host path or interpreter.

    args are the planner's allowlisted flags and "./"-prefixed test paths;
    config and import_roots are workspace-relative POSIX paths ("." is the
    workspace root, always the first import root).
    """

    args: tuple[str, ...]
    config: str | None
    import_roots: tuple[str, ...]
    junit: bool = False


@dataclass(frozen=True)
class JUnitSummary:
    """Counts from the JUnit XML report pytest writes for a verification run."""

    tests: int
    failures: int
    errors: int
    skipped: int

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped


@dataclass(frozen=True)
class SandboxRun:
    """What one pytest run produced."""

    exit_code: int | None  # None when the run was killed at the deadline
    output: str  # tail of combined stdout and stderr, "\n" line endings
    timed_out: bool
    omitted_bytes: int  # bytes dropped from the front of the output
    report: JUnitSummary | None = None  # only when the invocation asked for one


def _no_cleanup() -> None:
    return None


@dataclass(frozen=True)
class StdioCommand:
    """How to start a long-running stdio process (an external MCP server) in a sandbox."""

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: Path
    cleanup: Callable[[], None] = field(default=_no_cleanup, compare=False)


class Sandbox(Protocol):
    """Runs a checked pytest invocation, or a stdio tool server, against a workspace."""

    name: str

    def run_pytest(
        self, workspace: Path, invocation: PytestInvocation, timeout_s: float
    ) -> SandboxRun: ...

    def stdio_command(self, argv: Sequence[str], workspace: Path) -> StdioCommand: ...


def pytest_options(
    invocation: PytestInvocation,
    *,
    root: str,
    devnull: str,
    extra: Sequence[str] = (),
) -> list[str]:
    """pytest's own arguments for an invocation run with the workspace at root.

    The cache plugin is disabled, rootdir and confcutdir are pinned to the
    workspace so no config or conftest.py above it is read, and the import
    roots go on sys.path through pytest's pythonpath setting.
    """
    roots = " ".join(shlex.quote(join_root(root, rel)) for rel in invocation.import_roots)
    config = join_root(root, invocation.config) if invocation.config else devnull
    return [
        "-p",
        "no:cacheprovider",
        "-c",
        config,
        f"--rootdir={root}",
        f"--confcutdir={root}",
        "-o",
        f"pythonpath={roots}",
        *extra,
        *invocation.args,
    ]


def join_root(root: str, rel: str) -> str:
    """root joined with a workspace-relative POSIX path ("." is root itself)."""
    return root if rel in ("", ".") else f"{root.rstrip('/')}/{rel}"


def read_junit(path: Path) -> JUnitSummary | None:
    """Totals from a JUnit XML report file, or None when missing or unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return parse_junit(data)


def parse_junit(data: bytes) -> JUnitSummary | None:
    """Totals from JUnit XML bytes, or None when they do not parse."""
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None
    try:
        return JUnitSummary(
            tests=sum(int(s.get("tests", 0)) for s in suites),
            failures=sum(int(s.get("failures", 0)) for s in suites),
            errors=sum(int(s.get("errors", 0)) for s in suites),
            skipped=sum(int(s.get("skipped", 0)) for s in suites),
        )
    except ValueError:
        return None
