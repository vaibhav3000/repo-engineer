"""run_tests: command allowlist, argument jail, interpreter, environment and process control."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from conftest import error_text

from repo_engineer.state import Observation, ToolCall
from repo_engineer.tools import ToolRegistry


def run(reg: ToolRegistry, cmd: str) -> Observation:
    return reg.execute(ToolCall("run_tests", {"cmd": cmd}))


def write_test(ws: Path, name: str, body: str) -> None:
    (ws / "tests" / name).write_text(body, encoding="utf-8")


# ------------------------------------------------------------ command shape


@pytest.mark.parametrize(
    "cmd",
    [
        "python -m pytestx tests/",
        "python -m unittest discover",
        "python3 -m pytest tests/",
        "python -c 'import os'",
        "pytest tests/",
        "",
    ],
)
def test_only_the_exact_pytest_prefix_is_allowed(workspace: Path, cmd: str) -> None:
    obs = run(ToolRegistry(workspace), cmd)
    assert obs.rejected and not obs.ok
    assert "not in allowlist" in error_text(obs)


@pytest.mark.parametrize(
    "flags",
    [
        "--junitxml=../escaped.xml",
        "-p evilplugin",
        "--basetemp=../victim",
        "-c ../evil.ini",
        "--rootdir=..",
        "-o cache_dir=../cache",
        "--confcutdir=..",
        "--override-ini=python_files=*.txt",
        "--import-mode=append",
        "--pyargs os",
        "--trace-config",
    ],
)
def test_flags_outside_the_allowlist_are_rejected(workspace: Path, flags: str) -> None:
    obs = run(ToolRegistry(workspace), f"python -m pytest {flags} tests/")
    assert obs.rejected and not obs.ok, obs
    assert obs.exit_code is None
    assert not (workspace.parent / "escaped.xml").exists()


def test_flag_missing_its_value_is_rejected(workspace: Path) -> None:
    obs = run(ToolRegistry(workspace), "python -m pytest tests/ -k")
    assert obs.rejected and not obs.ok


def test_allowed_flags_run(workspace: Path) -> None:
    cmd = "python -m pytest -q -x -l --tb=short --maxfail=1 -rA -k value tests/"
    obs = run(ToolRegistry(workspace), cmd)
    assert obs.ok and obs.exit_code == 0, obs


@pytest.mark.parametrize(
    "target", ["../outside_pkg", "/etc", "C:/Windows", "missing_dir/", "tests/nope.py"]
)
def test_test_paths_are_jailed_and_must_exist(workspace: Path, target: str) -> None:
    outside = workspace.parent / "outside_pkg"
    outside.mkdir()
    (outside / "test_outside.py").write_text(
        "from pathlib import Path\n\n\ndef test_x():\n"
        "    Path(__file__).with_name('RAN').write_text('x')\n",
        encoding="utf-8",
    )
    obs = run(ToolRegistry(workspace), f"python -m pytest {target}")
    assert obs.rejected and not obs.ok, obs
    assert not (outside / "RAN").exists()


def test_node_ids_inside_the_workspace_are_accepted(workspace: Path) -> None:
    obs = run(ToolRegistry(workspace), "python -m pytest -q tests/test_app.py::test_value")
    assert obs.ok and obs.exit_code == 0, obs


def test_argument_files_are_rejected(workspace: Path) -> None:
    (workspace / "args.txt").write_text("-p evilplugin\n", encoding="utf-8")
    obs = run(ToolRegistry(workspace), "python -m pytest @args.txt tests/")
    assert obs.rejected and "@" in error_text(obs)


@pytest.mark.parametrize(
    "cmd", ["python -m pytest tests/ ; rm -rf /", "python -m pytest tests/|sh"]
)
def test_shell_syntax_is_rejected(workspace: Path, cmd: str) -> None:
    obs = run(ToolRegistry(workspace), cmd)
    assert obs.rejected and not obs.ok


def test_quoted_paths_with_spaces_are_supported(workspace: Path) -> None:
    (workspace / "tests" / "more tests").mkdir()
    write_test(workspace, "more tests/test_more.py", "def test_more():\n    assert True\n")
    obs = run(ToolRegistry(workspace), "python -m pytest -q 'tests/more tests'")
    assert obs.ok and obs.exit_code == 0, obs


# ------------------------------------------------------------ interpreter and isolation


def test_runs_under_the_runtime_interpreter(workspace: Path) -> None:
    write_test(
        workspace,
        "test_prefix.py",
        "import sys\nfrom pathlib import Path\n\n\ndef test_prefix():\n"
        "    Path('prefix.txt').write_text(sys.prefix, encoding='utf-8')\n",
    )
    obs = run(ToolRegistry(workspace), "python -m pytest -q tests/test_prefix.py")
    assert obs.ok and obs.exit_code == 0, obs
    child_prefix = (workspace / "prefix.txt").read_text(encoding="utf-8")
    assert os.path.normcase(child_prefix) == os.path.normcase(sys.prefix)


def test_a_planted_pytest_module_cannot_shadow_pytest(workspace: Path) -> None:
    (workspace / "pytest.py").write_text("raise SystemExit('shadow pytest imported')\n", "utf-8")
    obs = run(ToolRegistry(workspace), "python -m pytest -q tests/")
    assert obs.ok and obs.exit_code == 0, obs
    assert "shadow" not in obs.output_excerpt


def test_workspace_imports_work_when_the_path_has_spaces(tmp_path: Path) -> None:
    ws = tmp_path / "dir with spaces" / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    write_test(
        ws, "test_app.py", "from app import VALUE\n\n\ndef test_v():\n    assert VALUE == 1\n"
    )
    obs = run(ToolRegistry(ws), "python -m pytest -q tests/")
    assert obs.ok and obs.exit_code == 0, obs


def test_host_pytest_config_conftest_and_cache_are_not_used(tmp_path: Path) -> None:
    host = tmp_path / "host"
    ws = host / "runs" / "ws"
    (ws / "tests").mkdir(parents=True)
    (host / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n", encoding="utf-8")
    (host / "conftest.py").write_text(
        "from pathlib import Path\nPath(__file__).with_name('HOST_CONFTEST_RAN').write_text('x')\n",
        encoding="utf-8",
    )
    write_test(ws, "test_ok.py", "def test_ok():\n    assert True\n")
    obs = run(ToolRegistry(ws), "python -m pytest -q tests/")
    assert obs.ok and obs.exit_code == 0, obs
    assert "1 passed" in obs.output_excerpt
    assert not (host / "HOST_CONFTEST_RAN").exists()
    assert not (host / ".pytest_cache").exists() and not (ws / ".pytest_cache").exists()


def test_workspace_pytest_config_and_its_pythonpath_are_honoured(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "src" / "pkg").mkdir(parents=True)
    (ws / "tests").mkdir()
    (ws / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "src" / "pkg" / "mod.py").write_text("ANSWER = 42\n", encoding="utf-8")
    (ws / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src", "../outside"]\n', encoding="utf-8"
    )
    write_test(
        ws,
        "test_mod.py",
        "from pkg.mod import ANSWER\n\n\ndef test_a():\n    assert ANSWER == 42\n",
    )
    reg = ToolRegistry(ws)
    obs = run(reg, "python -m pytest -q tests/")
    assert obs.ok and obs.exit_code == 0, obs
    argv = reg.pytest_argv([])
    assert "outside" not in argv[argv.index("-o") + 1]
