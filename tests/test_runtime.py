"""AgentRuntime loop: budgets, verification gate, accounting and trace contents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from repo_engineer.agent import AgentRuntime
from repo_engineer.planner import Planner, PlannerUsage, RecordedPlanner
from repo_engineer.state import AgentState, State, ToolCall
from repo_engineer.tools import ToolRegistry

RUN_TESTS = ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"})
TASK = {"task_id": "t", "description": "keep the suite green"}


def break_suite(ws: Path) -> None:
    (ws / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n",
        encoding="utf-8",
    )


def test_failing_tests_with_no_replan_budget_end_failed_with_trace(
    workspace: Path, tmp_path: Path
) -> None:
    """Running out of replans on a failing run_tests is a clean FAILED, not a crash."""
    break_suite(workspace)
    runtime = AgentRuntime(ToolRegistry(workspace), RecordedPlanner([RUN_TESTS]), max_replans=0)
    trace = tmp_path / "trace.json"
    state = runtime.run_task(TASK, trace_path=trace)
    assert state.state is State.FAILED
    assert any("replan budget exhausted" in e for e in state.errors)
    assert json.loads(trace.read_text(encoding="utf-8"))["state"] == "FAILED"


def test_repeated_failing_tests_exhaust_replans_cleanly(workspace: Path, tmp_path: Path) -> None:
    break_suite(workspace)
    planner = RecordedPlanner([RUN_TESTS] * 4)
    runtime = AgentRuntime(ToolRegistry(workspace), planner, max_replans=2)
    state = runtime.run_task(TASK, trace_path=tmp_path / "trace.json")
    assert state.state is State.FAILED
    assert state.replans == 2


# ------------------------------------------------------------ accounting (P5)


class CountingPlanner(Planner):
    """Model-like planner: cumulative usage counters, scripted replies and failures."""

    name = "counting"

    def __init__(self, script: list[ToolCall | Exception | None]) -> None:
        self.script = list(script)
        self.calls = 0
        self.prompt_tokens = 0

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        self.calls += 1
        self.prompt_tokens += 100
        return step

    def usage(self) -> PlannerUsage:
        return PlannerUsage(
            model="fake", calls=self.calls, prompt_tokens=self.prompt_tokens, completion_tokens=0
        )


def test_usage_is_reported_per_task_when_a_planner_is_shared(tmp_path: Path) -> None:
    planner = CountingPlanner(
        [
            ToolCall("list_tree", {}),
            None,
            ToolCall("list_tree", {}),
            ToolCall("list_tree", {}),
            None,
        ]
    )
    summaries = []
    for name in ("one", "two"):
        ws = tmp_path / name
        (ws / "tests").mkdir(parents=True)
        (ws / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n", "utf-8")
        runtime = AgentRuntime(ToolRegistry(ws), planner)
        summaries.append(runtime.summary(runtime.run_task({"task_id": name})))
    assert [s["llm_calls"] for s in summaries] == [2, 3]
    assert [s["llm_prompt_tokens"] for s in summaries] == [200, 300]


def test_planner_errors_are_counted_in_total(workspace: Path) -> None:
    planner = CountingPlanner(
        [RuntimeError("HTTP 429"), ToolCall("list_tree", {}), RuntimeError("HTTP 429"), None]
    )
    runtime = AgentRuntime(ToolRegistry(workspace), planner)
    state = runtime.run_task(TASK)
    assert state.state is State.COMPLETE
    assert runtime.summary(state)["planner_errors"] == 2


def test_consecutive_planner_errors_still_end_the_run(workspace: Path) -> None:
    planner = CountingPlanner([RuntimeError("down")] * 3)
    state = AgentRuntime(ToolRegistry(workspace), planner).run_task(TASK)
    assert state.state is State.FAILED and state.planner_errors == 3


# ------------------------------------------------------------ files_changed (P9)


def test_files_changed_lists_only_successful_edits_by_workspace_path(workspace: Path) -> None:
    planner = RecordedPlanner(
        [
            ToolCall("apply_edit", {"path": "../outside.py", "old_text": "a", "new_text": "b"}),
            ToolCall("apply_edit", {"path": "app.py", "old_text": "nope", "new_text": "b"}),
            ToolCall(
                "apply_edit",
                {"path": "./app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 1  # ok"},
            ),
            ToolCall(
                "apply_edit", {"path": "app.py", "old_text": "# ok", "new_text": "# still ok"}
            ),
            None,
        ]
    )
    state = AgentRuntime(ToolRegistry(workspace), planner).run_task(TASK)
    assert state.files_changed == ["app.py"]


# ------------------------------------------------------------ planner feedback (P10, M1)


class SpyPlanner(Planner):
    name = "spy"

    def __init__(self, script: list[ToolCall | None]) -> None:
        self.script = list(script)
        self.seen_tasks: list[dict[str, Any]] = []
        self.seen_history: list[list[dict[str, Any]]] = []
        self.replans: list[str] = []

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        self.seen_tasks.append(dict(task))
        self.seen_history.append([dict(h) for h in history])
        return self.script.pop(0) if self.script else None

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:
        self.replans.append(reason)


def test_failed_verify_is_fed_back_to_the_planner(workspace: Path) -> None:
    break_suite(workspace)
    planner = SpyPlanner([None, None, None])
    state = AgentRuntime(ToolRegistry(workspace), planner, max_replans=2).run_task(TASK)
    assert state.state is State.FAILED
    assert len(planner.replans) == 2
    assert "assert 1 == 2" in planner.replans[0] or "VALUE == 2" in planner.replans[0]
    last_history = planner.seen_history[-1]
    assert last_history and last_history[-1].get("verify") is True
    assert last_history[-1]["exit_code"] == 1


def test_planner_sees_the_repository_tree(workspace: Path) -> None:
    planner = SpyPlanner([None])
    AgentRuntime(ToolRegistry(workspace), planner).run_task(TASK)
    assert "tests/test_app.py" in planner.seen_tasks[0]["repo_tree"]


def test_history_keeps_the_end_of_test_output(workspace: Path) -> None:
    (workspace / "tests" / "test_app.py").write_text(
        "def test_noisy():\n    print('x' * 3000)\n    assert 'left' == 'right'\n", "utf-8"
    )
    planner = SpyPlanner([ToolCall("run_tests", {"cmd": "python -m pytest -q -s tests/"}), None])
    AgentRuntime(ToolRegistry(workspace), planner, max_replans=1).run_task(TASK)
    entry = planner.seen_history[1][0]
    assert entry["exit_code"] == 1
    assert "1 failed" in entry["output"]
    assert str(workspace) not in entry["output"]


def test_agent_state_has_no_unused_plan_field() -> None:
    assert "plan" not in AgentState.__dataclass_fields__


def test_an_error_inside_verification_ends_the_run_failed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = ToolRegistry(workspace)

    def broken_verify(cmd: str, mutants: Any = ()) -> Any:
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(registry, "verify", broken_verify)
    trace = tmp_path / "trace.json"
    state = AgentRuntime(registry, RecordedPlanner([None])).run_task(TASK, trace_path=trace)
    assert state.state is State.FAILED and not state.verified
    assert any("verifier exploded" in e for e in state.errors)
    assert trace.exists()
