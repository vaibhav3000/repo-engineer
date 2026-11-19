"""Credentials and host paths never reach test code, observations, traces or prompts."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from repo_engineer.state import Observation, ToolCall
from repo_engineer.tools import ToolRegistry


def run(reg: ToolRegistry, cmd: str) -> Observation:
    return reg.execute(ToolCall("run_tests", {"cmd": cmd}))


def write_test(ws: Path, name: str, body: str) -> None:
    (ws / "tests" / name).write_text(body, encoding="utf-8")


# ------------------------------------------------------------ environment and redaction


def test_child_environment_carries_no_credentials(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "canary-gemini-key-0123456789")
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "canary-token-0123456789")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    write_test(
        workspace,
        "test_env.py",
        "import json, os\nfrom pathlib import Path\n\n\ndef test_env():\n"
        "    Path('env.json').write_text(json.dumps(sorted(os.environ)), encoding='utf-8')\n"
        "    print(dict(os.environ))\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q -s tests/test_env.py")
    assert obs.ok and obs.exit_code == 0, obs
    names = (workspace / "env.json").read_text(encoding="utf-8")
    for leaked in ("GEMINI_API_KEY", "SERVICE_AUTH_TOKEN", "PYTEST_ADDOPTS"):
        assert leaked not in names
    assert "canary" not in obs.output_excerpt


def test_known_secret_values_are_redacted_from_output(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "canary-gemini-key-0123456789")
    (workspace / "leak.txt").write_text("canary-gemini-key-0123456789", encoding="utf-8")
    write_test(
        workspace,
        "test_leak.py",
        "from pathlib import Path\n\n\ndef test_leak():\n"
        "    print('LEAK', Path('leak.txt').read_text(encoding='utf-8'))\n",
    )
    reg = ToolRegistry(workspace)
    obs = run(reg, "python -m pytest -q -s tests/test_leak.py")
    assert "LEAK <redacted>" in obs.output_excerpt
    read = reg.execute(ToolCall("read_file", {"path": "leak.txt"}))
    assert "canary" not in read.output_excerpt


def test_absolute_host_paths_are_redacted_from_output(workspace: Path) -> None:
    write_test(
        workspace,
        "test_where.py",
        "import os, sys\n\n\ndef test_where():\n    print('CWD', os.getcwd())\n"
        "    print('EXE', sys.executable)\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q -s tests/test_where.py")
    assert obs.ok, obs
    assert "CWD <workspace>" in obs.output_excerpt
    assert str(workspace) not in obs.output_excerpt
    assert os.path.dirname(sys.executable) not in obs.output_excerpt


def test_failing_tmp_path_output_does_not_name_the_user(workspace: Path) -> None:
    import getpass

    write_test(
        workspace,
        "test_tmp.py",
        "def test_tmp(tmp_path):\n    assert str(tmp_path) == ''\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q tests/test_tmp.py")
    assert obs.exit_code == 1, obs
    user = getpass.getuser().lower()
    assert user not in obs.output_excerpt.lower()
    assert "pytest-of-" not in obs.output_excerpt
