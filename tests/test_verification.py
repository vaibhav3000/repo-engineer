"""VERIFY: the only way to COMPLETE, and the ways an agent might try to fake it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import error_text

from repo_engineer.agent import AgentRuntime, prepare_workspace
from repo_engineer.planner import RecordedPlanner
from repo_engineer.state import State, ToolCall
from repo_engineer.tools import ToolRegistry

TASKS = Path(__file__).resolve().parents[1] / "src" / "repo_engineer" / "benchmark" / "tasks"
RUN_TESTS = ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"})


def load_task(task_id: str) -> dict[str, Any]:
    task = json.loads((TASKS / task_id / "task.json").read_text(encoding="utf-8"))
    task["path"] = str(TASKS / task_id)
    return task


def run_real_task(
    tmp_path: Path, task_id: str, actions: list[ToolCall | None]
) -> tuple[Any, ToolRegistry]:
    workspace = prepare_workspace(TASKS / task_id, tmp_path / "runs", task_id)
    registry = ToolRegistry(workspace)
    runtime = AgentRuntime(registry, RecordedPlanner(actions), max_replans=0)
    return runtime.run_task(load_task(task_id), trace_path=tmp_path / "trace.json"), registry


def run_generic(workspace: Path, actions: list[ToolCall | None]) -> Any:
    runtime = AgentRuntime(ToolRegistry(workspace), RecordedPlanner(actions), max_replans=0)
    return runtime.run_task({"task_id": "t", "description": "keep the suite green"})


# ------------------------------------------------------------ gate on the real exit status


def test_failing_suite_that_prints_exit_0_cannot_complete(workspace: Path) -> None:
    liar = "def test_liar():\n    print('status: exit=0')\n    assert 'exit=0' == 'exit=1'\n"
    state = run_generic(
        workspace,
        [ToolCall("write_file", {"path": "tests/test_liar.py", "content": liar}), None],
    )
    assert state.state is State.FAILED and not state.verified


def test_a_suite_that_collects_no_tests_does_not_verify(workspace: Path) -> None:
    (workspace / "tests" / "test_app.py").unlink()
    state = run_generic(workspace, [None])
    assert state.state is State.FAILED
    verify = [r for r in state.test_results if r.get("verify")]
    assert verify and verify[-1]["exit_code"] == 5


def test_code_that_exits_0_at_import_does_not_verify(workspace: Path) -> None:
    exit_early = ToolCall(
        "apply_edit",
        {"path": "app.py", "old_text": "VALUE = 1", "new_text": "import os\nos._exit(0)"},
    )
    state = run_generic(workspace, [exit_early, None])
    assert state.state is State.FAILED and not state.verified


# ------------------------------------------------------------ protected files


@pytest.mark.parametrize(
    "path",
    [
        "conftest.py",
        "tests/conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.py",
        "evil.pth",
        "sitecustomize.py",
        "usercustomize.py",
    ],
)
def test_pytest_control_files_are_read_only_to_the_agent(workspace: Path, path: str) -> None:
    obs = ToolRegistry(workspace).execute(
        ToolCall("write_file", {"path": path, "content": "x = 1\n"})
    )
    assert obs.rejected and "pytest" in error_text(obs)
    assert not (workspace / path).exists()


def test_task_protected_paths_are_read_only_to_the_agent(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    reg.protect(["tests/"])
    write = reg.execute(ToolCall("write_file", {"path": "tests/test_new.py", "content": "x"}))
    edit = reg.execute(
        ToolCall(
            "apply_edit",
            {"path": "Tests/test_app.py", "old_text": "VALUE == 1", "new_text": "True"},
        )
    )
    assert write.rejected and edit.rejected
    assert "protected" in error_text(write)
    read = reg.execute(ToolCall("read_file", {"path": "tests/test_app.py"}))
    assert read.ok


def test_files_planted_by_test_code_are_removed_before_verify(workspace: Path) -> None:
    (workspace / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n", encoding="utf-8"
    )
    plant = (
        "from pathlib import Path\n\n\ndef test_plant():\n"
        "    Path('conftest.py').write_text(\n"
        "        'import pytest\\n'\n"
        "        '@pytest.hookimpl(hookwrapper=True)\\n'\n"
        "        'def pytest_runtest_makereport(item, call):\\n'\n"
        "        '    outcome = yield\\n'\n"
        "        '    outcome.get_result().outcome = \"passed\"\\n'\n"
        "    )\n"
    )
    state = run_generic(
        workspace,
        [
            ToolCall("write_file", {"path": "tests/test_plant.py", "content": plant}),
            ToolCall("run_tests", {"cmd": "python -m pytest -q tests/test_plant.py"}),
            None,
        ],
    )
    assert state.state is State.FAILED and not state.verified
    verify = [r for r in state.test_results if r.get("verify")][-1]
    assert "conftest.py" in verify["restored"]


def test_tests_cannot_be_rewritten_on_a_real_task(tmp_path: Path) -> None:
    """The audit's task_02 exploit: weaken the test instead of fixing the bug."""
    weaken = ToolCall(
        "apply_edit",
        {
            "path": "tests/test_invoice.py",
            "old_text": "assert total_with_tax(100.0) == 125.0",
            "new_text": "assert total_with_tax(100.0) == 120.0",
        },
    )
    state, registry = run_real_task(tmp_path, "task_02_failing_test", [weaken, None])
    assert state.observations[1]["rejected"] is True
    assert state.state is State.FAILED and not state.verified
    assert "TAX_RATE = 0.20" in (registry.workspace / "billing" / "invoice.py").read_text("utf-8")


# ------------------------------------------------------------ add-a-test task needs real tests


def test_a_vacuous_test_does_not_complete_the_add_a_test_task(tmp_path: Path) -> None:
    vacuous = ToolCall(
        "write_file",
        {"path": "tests/test_slug.py", "content": "def test_nothing():\n    assert True\n"},
    )
    state, _ = run_real_task(tmp_path, "task_03_add_missing_test", [vacuous, None])
    assert state.state is State.FAILED and not state.verified
    verify = [r for r in state.test_results if r.get("verify")][-1]
    assert verify["mutants_killed"] == 0 and verify["mutants_total"] >= 3


def test_the_code_under_test_cannot_be_changed_on_the_add_a_test_task(tmp_path: Path) -> None:
    edit = ToolCall(
        "apply_edit", {"path": "web/slug.py", "old_text": "text.lower()", "new_text": "text"}
    )
    state, _ = run_real_task(tmp_path, "task_03_add_missing_test", [edit, None])
    assert state.observations[1]["rejected"] is True


def test_real_tests_kill_every_mutant(tmp_path: Path) -> None:
    content = (
        "from web.slug import slugify\n\n\n"
        "def test_basic():\n    assert slugify('Hello World') == 'hello-world'\n\n\n"
        "def test_punctuation():\n    assert slugify('A B & C!') == 'a-b-c'\n\n\n"
        "def test_spaces():\n    assert slugify('  many   spaces ') == 'many-spaces'\n"
    )
    write = ToolCall("write_file", {"path": "tests/test_slug.py", "content": content})
    state, registry = run_real_task(tmp_path, "task_03_add_missing_test", [write, None])
    assert state.state is State.COMPLETE and state.verified, state.errors
    verify = [r for r in state.test_results if r.get("verify")][-1]
    assert verify["mutants_killed"] == verify["mutants_total"] >= 3
    original = (TASKS / "task_03_add_missing_test" / "web" / "slug.py").read_bytes()
    assert (registry.workspace / "web" / "slug.py").read_bytes() == original


def test_verify_run_is_recorded_in_the_trace(workspace: Path) -> None:
    state = run_generic(workspace, [None])
    assert state.state is State.COMPLETE
    assert state.actions[-1]["step"] == "verify"
    assert state.observations[-1]["tool"] == "run_tests"
    assert state.observations[-1]["exit_code"] == 0
    assert len(state.actions) == len(state.observations)


# ------------------------------------------------------------ plants that test code leaves behind
# Found by the post-fix adversarial review: agent test code (outside the
# protected tests/) can leave files that the restore step used to miss.

PYC_PLANT = """
import importlib.util
from pathlib import Path

import _pytest.assertion.rewrite as rw


def test_plant():
    src = Path("tests/test_cart.py")
    fake = "def test_sum_single_item(): pass\\ndef test_sum_many_items(): pass\\ndef test_empty_cart(): pass\\n"
    code = compile(fake, str(src.resolve()), "exec")
    pyc = src.parent / "__pycache__" / ("test_cart" + rw.PYC_TAIL)
    pyc.parent.mkdir(exist_ok=True)
    with open(pyc, "wb") as fp:
        rw._write_pyc_fp(fp, importlib.util.source_hash(src.read_bytes()), code)
"""

PASS_HOOK = (
    "import pytest\n"
    "@pytest.hookimpl(hookwrapper=True)\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    outcome.get_result().outcome = 'passed'\n"
)

BIG_CONFTEST_PLANT = f"""
from pathlib import Path


def test_plant():
    Path("conftest.py").write_text({PASS_HOOK!r} + "#" * 1_100_000 + "\\n")
"""

READ_ONLY_PLANT = """
import os
import stat
from pathlib import Path


def test_plant():
    Path("conftest.py").write_text("x = 1\\n")
    os.chmod("conftest.py", stat.S_IREAD)
"""


def plant_actions(plant: str) -> list[ToolCall | None]:
    return [
        ToolCall("write_file", {"path": "hack/test_plant.py", "content": plant}),
        ToolCall("run_tests", {"cmd": "python -m pytest -q hack/test_plant.py"}),
        None,
    ]


@pytest.mark.parametrize(
    "plant", [PYC_PLANT, BIG_CONFTEST_PLANT], ids=["bytecode-cache", "oversized-conftest"]
)
def test_planted_files_cannot_fake_a_pass(tmp_path: Path, plant: str) -> None:
    state, registry = run_real_task(tmp_path, "task_01_fix_off_by_one", plant_actions(plant))
    assert state.observations[2]["exit_code"] == 0, "the plant itself must have run"
    assert state.state is State.FAILED and not state.verified
    assert "range(len(items) - 1)" in (registry.workspace / "shop" / "cart.py").read_text("utf-8")


def test_a_read_only_planted_file_does_not_crash_the_run(tmp_path: Path) -> None:
    state, registry = run_real_task(
        tmp_path, "task_01_fix_off_by_one", plant_actions(READ_ONLY_PLANT)
    )
    assert state.observations[2]["exit_code"] == 0, "the plant itself must have run"
    assert state.state is State.FAILED
    assert not (registry.workspace / "conftest.py").exists()


def test_files_planted_during_verify_cannot_kill_the_mutants(tmp_path: Path) -> None:
    # Every test that runs after this conftest appears errors out, so each
    # mutant run would look like a kill.
    fail_hook = (
        "import pytest\n\n\n@pytest.fixture(autouse=True)\n"
        "def _planted():\n    raise AssertionError('planted')\n"
    )
    vacuous = (
        "from pathlib import Path\n\n\ndef test_trivial():\n"
        f"    Path('conftest.py').write_text({fail_hook!r})\n"
    )
    write = ToolCall("write_file", {"path": "tests/test_slug.py", "content": vacuous})
    state, _ = run_real_task(tmp_path, "task_03_add_missing_test", [write, None])
    assert state.state is State.FAILED and not state.verified
