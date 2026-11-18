"""AgentRuntime loop: budgets, verification gate, accounting and trace contents."""

from __future__ import annotations

import json
from pathlib import Path

from repo_engineer.agent import AgentRuntime
from repo_engineer.planner import RecordedPlanner
from repo_engineer.state import State, ToolCall
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
