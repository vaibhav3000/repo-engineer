"""The Docker sandbox: argv, path mapping, report transport and real containers.

The first half needs no Docker: it checks the command the sandbox builds and
how it reads results, with the process runner replaced. The second half runs
real containers and skips when no Linux Docker daemon answers (set
REPO_ENGINEER_DOCKER_CMD when the engine is reached another way, for example
"wsl -d Ubuntu -e docker").
"""

from __future__ import annotations

import base64
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import docker_ready  # noqa: F401  (used by the skipif condition)

from repo_engineer.process import ProcessResult, run_bounded
from repo_engineer.sandbox import (
    DockerSandbox,
    PytestInvocation,
    SandboxError,
    SandboxLimits,
)
from repo_engineer.sandbox.docker import default_image, split_report, wsl_path
from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry


def invocation(*args: str, junit: bool = False) -> PytestInvocation:
    return PytestInvocation(args=tuple(args), config=None, import_roots=(".",), junit=junit)


class FakeDocker:
    """Stands in for the docker CLI: records commands, replays one run result."""

    def __init__(self, result: ProcessResult) -> None:
        self.result = result
        self.runs: list[list[str]] = []
        self.calls: list[list[str]] = []

    def runner(self, argv: Sequence[str], **_kwargs: object) -> ProcessResult:
        self.runs.append(list(argv))
        return self.result

    def call(self, argv: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


def fake_sandbox(
    result: ProcessResult, limits: SandboxLimits | None = None
) -> tuple[DockerSandbox, FakeDocker]:
    fake = FakeDocker(result)
    sandbox = DockerSandbox(
        limits, "img:tag", docker_cmd=["docker"], runner=fake.runner, call=fake.call
    )
    return sandbox, fake


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == flag]


def test_the_container_is_isolated(workspace: Path) -> None:
    sandbox, fake = fake_sandbox(ProcessResult(0, "1 passed", False, 0))
    sandbox.run_pytest(workspace, invocation("-q", "./tests"), 30.0)
    (argv,) = fake.runs
    assert argv[:3] == ["docker", "run", "--rm"]
    assert flag_values(argv, "--network") == ["none"]
    assert "--read-only" in argv and flag_values(argv, "--cap-drop") == ["ALL"]
    assert flag_values(argv, "--security-opt") == ["no-new-privileges"]
    assert flag_values(argv, "--memory") == ["2048m"] == flag_values(argv, "--memory-swap")
    assert flag_values(argv, "--pids-limit") == ["256"]
    assert flag_values(argv, "--cpus") == ["1.5"]
    assert flag_values(argv, "--tmpfs") == ["/tmp:rw,nosuid,nodev,size=256m"]
    (mount,) = flag_values(argv, "--mount")
    assert mount == f"type=bind,source={workspace},target=/work,readonly"
    assert flag_values(argv, "-w") == ["/work"]
    # Nothing from the host environment reaches the container.
    env = flag_values(argv, "-e")
    assert "HOME=/tmp" in env and all("KEY" not in item for item in env)
    assert argv[argv.index("img:tag") + 1 : argv.index("img:tag") + 3] == ["python", "-I"]
    assert argv[-2:] == ["-q", "./tests"]


def test_the_container_is_removed_after_every_run(workspace: Path) -> None:
    sandbox, fake = fake_sandbox(ProcessResult(0, "", False, 0))
    sandbox.run_pytest(workspace, invocation("-q"), 30.0)
    name = flag_values(fake.runs[0], "--name")[0]
    assert ["docker", "rm", "-f", name] in fake.calls


def test_limits_are_configurable(workspace: Path) -> None:
    limits = SandboxLimits(memory_mb=512, cpus=1.0, pids=64, nofile=128, tmpfs_mb=32)
    sandbox, fake = fake_sandbox(ProcessResult(0, "", False, 0), limits=limits)
    sandbox.run_pytest(workspace, invocation(), 30.0)
    argv = fake.runs[0]
    assert flag_values(argv, "--memory") == ["512m"] and flag_values(argv, "--pids-limit") == ["64"]
    assert flag_values(argv, "--ulimit") == ["nofile=128:128"]
    assert flag_values(argv, "--tmpfs") == ["/tmp:rw,nosuid,nodev,size=32m"]


def test_daemon_failure_is_a_sandbox_error_not_a_test_result(workspace: Path) -> None:
    sandbox, _ = fake_sandbox(ProcessResult(125, "Unable to find image 'img:tag'", False, 0))
    with pytest.raises(SandboxError, match="Unable to find image"):
        sandbox.run_pytest(workspace, invocation(), 30.0)


def test_a_sandbox_error_reaches_the_planner_as_a_failed_call(workspace: Path) -> None:
    sandbox, _ = fake_sandbox(ProcessResult(125, "daemon gone", False, 0))
    obs = ToolRegistry(workspace, sandbox=sandbox).execute(
        ToolCall("run_tests", {"cmd": "python -m pytest -q tests/"})
    )
    assert not obs.ok and not obs.rejected and obs.error is not None
    assert "daemon gone" in obs.error


def test_the_report_comes_back_behind_the_run_nonce(workspace: Path) -> None:
    report = b'<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>'
    holder: dict[str, str] = {}

    def runner(argv: Sequence[str], **_kwargs: object) -> ProcessResult:
        nonce = list(argv)[list(argv).index("-c") + 2]
        holder["nonce"] = nonce
        forged = f"\n{nonce} {base64.b64encode(b'not xml').decode()}\n"
        trailer = f"\n{nonce} {base64.b64encode(report).decode()}\n"
        return ProcessResult(0, "2 passed" + forged + "more output" + trailer, False, 0)

    sandbox = DockerSandbox(
        docker_cmd=["docker"],
        image="i",
        runner=runner,
        call=FakeDocker(ProcessResult(0, "", False, 0)).call,
    )
    run = sandbox.run_pytest(workspace, invocation("-q", junit=True), 30.0)
    assert run.report is not None and run.report.passed == 2
    assert holder["nonce"] not in run.output.splitlines()[-1]


def test_split_report_takes_the_last_trailer() -> None:
    text = "out\nN Zm9v\nmore\nN YmFy\n"
    assert split_report(text, "N") == ("out\nN Zm9v\nmore", b"bar", False)
    assert split_report("no trailer", "N") == ("no trailer", None, False)
    assert split_report("x\nN -\n", "N") == ("x", None, False)
    assert split_report("x\nN too-large\n", "N") == ("x", None, False)
    assert split_report("x\nN timeout\n", "N") == ("x", None, True)


def test_wsl_paths_are_translated() -> None:
    assert wsl_path(r"C:\Users\a b\ws") == "/mnt/c/Users/a b/ws"
    assert wsl_path("D:/x") == "/mnt/d/x"
    with pytest.raises(SandboxError):
        wsl_path(r"\\server\share\ws")


def test_wsl_docker_commands_get_translated_mounts(tmp_path: Path) -> None:
    fake = FakeDocker(ProcessResult(0, "", False, 0))
    sandbox = DockerSandbox(
        docker_cmd=["wsl", "-d", "Ubuntu", "-e", "docker"],
        image="i",
        runner=fake.runner,
        call=fake.call,
    )
    ws = Path(r"C:\Users\a b\ws")
    argv = sandbox.run_argv(ws, invocation(), name="n", nonce="x")
    assert argv[:6] == ["wsl", "-d", "Ubuntu", "-e", "docker", "run"]
    assert "type=bind,source=/mnt/c/Users/a b/ws,target=/work,readonly" in argv


def test_a_comma_in_the_workspace_path_is_refused(tmp_path: Path) -> None:
    sandbox, _ = fake_sandbox(ProcessResult(0, "", False, 0))
    with pytest.raises(SandboxError, match="comma"):
        sandbox.run_argv(tmp_path / "a,b", invocation(), name="n", nonce="x")


def test_the_image_tag_follows_its_recipe() -> None:
    tag = default_image()
    assert tag.startswith("repo-engineer-sandbox:") and len(tag.split(":")[1]) == 12


# ------------------------------------------------------------- real containers


needs_docker = pytest.mark.skipif("not docker_ready()", reason="no Linux Docker daemon answers")


def write_test(ws: Path, name: str, body: str) -> None:
    (ws / "tests" / name).write_text(body, encoding="utf-8")


@needs_docker
def test_a_passing_and_a_failing_suite_in_a_container(workspace: Path) -> None:
    sandbox = DockerSandbox()
    run = sandbox.run_pytest(workspace, invocation("-q", "./tests", junit=True), 120.0)
    assert run.exit_code == 0, run.output
    assert run.report is not None and run.report.passed == 1
    write_test(workspace, "test_bad.py", "def test_bad():\n    assert False\n")
    run = sandbox.run_pytest(workspace, invocation("-q", "./tests", junit=True), 120.0)
    assert run.exit_code == 1
    assert run.report is not None and run.report.failures == 1


@needs_docker
def test_the_workspace_is_read_only_and_the_network_absent(workspace: Path) -> None:
    write_test(
        workspace,
        "test_probe.py",
        "import socket\nfrom pathlib import Path\n\n\ndef test_probe():\n"
        "    try:\n        Path('planted.txt').write_text('x')\n        print('WROTE')\n"
        "    except OSError:\n        print('READ-ONLY')\n"
        "    try:\n        socket.create_connection(('1.1.1.1', 80), timeout=3)\n        print('NET-OK')\n"
        "    except OSError:\n        print('NO-NET')\n",
    )
    run = DockerSandbox().run_pytest(workspace, invocation("-q", "-s", "./tests"), 120.0)
    assert "READ-ONLY" in run.output and "NO-NET" in run.output, run.output
    assert not (workspace / "planted.txt").exists()


@needs_docker
def test_a_timeout_removes_the_container(workspace: Path) -> None:
    write_test(
        workspace, "test_hang.py", "import time\n\n\ndef test_hang():\n    time.sleep(120)\n"
    )
    names: list[str] = []

    def runner(argv: Sequence[str], **kwargs: Any) -> ProcessResult:
        names.extend(flag_values(list(argv), "--name"))
        return run_bounded(argv, **kwargs)

    sandbox = DockerSandbox(runner=runner)
    run = sandbox.run_pytest(workspace, invocation("-q", "./tests/test_hang.py"), 8.0)
    assert run.timed_out and run.exit_code is None
    # Only this run's container: other sandbox runs may be alive on the same daemon.
    (name,) = names
    listed = sandbox.call([*sandbox.docker_cmd, "ps", "-aq", "--filter", f"name=^{name}$"], 30.0)
    assert listed.stdout.strip() == ""


@needs_docker
def test_the_registry_verifies_inside_docker(workspace: Path) -> None:
    result = ToolRegistry(workspace, sandbox=DockerSandbox()).verify("python -m pytest -q tests/")
    assert result.passed, result.reason


def test_a_daemon_error_message_does_not_carry_the_nonce(workspace: Path) -> None:
    def runner(argv: Sequence[str], **_kwargs: object) -> ProcessResult:
        nonce = list(argv)[list(argv).index("-c") + 2]
        return ProcessResult(125, f"Error response from daemon\n{nonce} -\n", False, 0)

    sandbox = DockerSandbox(
        docker_cmd=["docker"],
        image="i",
        runner=runner,
        call=FakeDocker(ProcessResult(0, "", False, 0)).call,
    )
    with pytest.raises(SandboxError) as info:
        sandbox.run_pytest(workspace, invocation(), 30.0)
    assert "Error response from daemon" in str(info.value) and " -" not in str(info.value)


@needs_docker
def test_test_code_exiting_with_dockers_own_statuses_is_a_test_failure(workspace: Path) -> None:
    """Found by the Step 2 review: os._exit(125) in test code looked like a Docker failure."""
    write_test(workspace, "test_exit.py", "import os\n\n\ndef test_exit():\n    os._exit(125)\n")
    run = DockerSandbox().run_pytest(workspace, invocation("-q", "./tests/test_exit.py"), 120.0)
    assert run.exit_code == 1 and "status 125" in run.output
