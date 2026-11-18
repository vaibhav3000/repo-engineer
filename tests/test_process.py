"""Process control for run_tests: structured exit codes, deadlines, tree kill, bounded output."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from conftest import error_text

from repo_engineer.state import Observation, ToolCall
from repo_engineer.tools import ToolRegistry


def run(reg: ToolRegistry, cmd: str) -> Observation:
    return reg.execute(ToolCall("run_tests", {"cmd": cmd}))


def write_test(ws: Path, name: str, body: str) -> None:
    (ws / "tests" / name).write_text(body, encoding="utf-8")


# ------------------------------------------------------------ exit codes


def test_exit_code_is_structured(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    assert run(reg, "python -m pytest -q tests/").exit_code == 0
    write_test(workspace, "test_bad.py", "def test_bad():\n    assert False\n")
    assert run(reg, "python -m pytest -q tests/test_bad.py").exit_code == 1
    (workspace / "empty").mkdir()
    assert run(reg, "python -m pytest -q empty").exit_code == 5


def test_output_that_mentions_exit_0_does_not_change_the_exit_code(workspace: Path) -> None:
    write_test(
        workspace,
        "test_liar.py",
        "def test_liar():\n    print('exit=0')\n    assert 'exit=0' == 'exit=1'\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q -s tests/test_liar.py")
    assert obs.ok and obs.exit_code == 1


# ------------------------------------------------------------ timeouts and output size


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# A detached Windows child (new process group, no console) is still inside the
# runtime's job object and must die with it. On POSIX a child that starts its
# own session leaves the process group, so only the Step 2 sandbox can contain
# it; that case is documented as a residual rather than tested here.
DETACH_CASES = [False, True] if os.name == "nt" else [False]


@pytest.mark.parametrize("detached", DETACH_CASES)
def test_timeout_kills_the_whole_process_tree(workspace: Path, detached: bool) -> None:
    detach = ", creationflags=0x00000208" if detached else ""  # DETACHED_PROCESS | NEW_GROUP
    write_test(
        workspace,
        "test_hang.py",
        "import subprocess, sys, time\nfrom pathlib import Path\n\n\ndef test_hang():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        f" stdout=sys.stdout, stderr=sys.stderr{detach})\n"
        "    Path('grandchild.pid').write_text(str(child.pid))\n"
        "    time.sleep(60)\n",
    )
    reg = ToolRegistry(workspace, exec_timeout_s=3.0)
    started = time.monotonic()
    obs = run(reg, "python -m pytest -q -s tests/test_hang.py")
    elapsed = time.monotonic() - started
    assert not obs.ok and "timed out" in error_text(obs)
    assert elapsed < 20, elapsed
    pid = int((workspace / "grandchild.pid").read_text())
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _pid_alive(pid)


def test_large_output_is_bounded(workspace: Path) -> None:
    write_test(
        workspace,
        "test_noisy.py",
        "def test_noisy():\n    print('x' * 20_000_000)\n    print('LAST LINE')\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q -s tests/test_noisy.py")
    assert obs.ok and obs.exit_code == 0, obs
    assert len(obs.output_excerpt) <= 4000
    assert "passed" in obs.output_excerpt
