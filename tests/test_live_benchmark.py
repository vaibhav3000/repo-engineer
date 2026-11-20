"""Live-planner benchmark runner, exercised offline with a fake planner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from repo_engineer.benchmark.live import run_live_benchmark
from repo_engineer.planner import Planner, PlannerUsage
from repo_engineer.state import ToolCall

RESULTS = Path(__file__).resolve().parents[1] / "results"


class FakeModelPlanner(Planner):
    """Edits the off-by-one bug, runs the tests, then says done; counts calls."""

    name = "fake-model"

    def __init__(self) -> None:
        self.calls = 0
        self.script: list[ToolCall | None] = [
            ToolCall(
                "apply_edit",
                {
                    "path": "shop/cart.py",
                    "old_text": "range(len(items) - 1)",
                    "new_text": "range(len(items))",
                },
            ),
            None,
        ]

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        self.calls += 1
        return self.script.pop(0) if self.script else None

    def usage(self) -> PlannerUsage:
        return PlannerUsage(
            model="fake", calls=self.calls, prompt_tokens=10 * self.calls, completion_tokens=0
        )


def test_attempts_are_recorded_with_traces_and_never_overwritten(tmp_path: Path) -> None:
    first = run_live_benchmark(
        ["task_01_fix_off_by_one"], 2, tmp_path, FakeModelPlanner, stamp="20261119T000000Z"
    )
    data = json.loads(first.read_text(encoding="utf-8"))
    task = data["tasks"][0]
    assert task["task_id"] == "task_01_fix_off_by_one"
    assert [a["success"] for a in task["attempts"]] == [True, True]
    assert task["passes"] == 2 and data["summary"]["attempts"] == 2
    assert [a["llm_calls"] for a in task["attempts"]] == [2, 2]
    for attempt in task["attempts"]:
        assert (first.parent / attempt["trace"]).exists()
    with pytest.raises(FileExistsError):
        run_live_benchmark(
            ["task_01_fix_off_by_one"], 1, tmp_path, FakeModelPlanner, stamp="20261119T000000Z"
        )
    second = run_live_benchmark(
        ["task_01_fix_off_by_one"], 1, tmp_path, FakeModelPlanner, stamp="20261119T000100Z"
    )
    assert second != first and first.exists()


def test_committed_live_results_are_internally_consistent() -> None:
    data = json.loads((RESULTS / "llm_benchmark_results.json").read_text(encoding="utf-8"))
    tasks = data["tasks"]
    summary = data["summary"]
    assert summary["total_llm_calls"] == sum(t["llm_calls"] for t in tasks)
    assert summary["total_prompt_tokens"] == sum(t["llm_prompt_tokens"] for t in tasks)
    assert summary["total_completion_tokens"] == sum(t["llm_completion_tokens"] for t in tasks)
    for task in tasks:
        # Every successful planner call is one step or the final "done", and
        # VERIFY adds a step, so a run cannot make more calls than steps.
        assert task["llm_calls"] <= task["steps"], task["task_id"]
        planner_errors = sum(1 for e in task["errors"] if e.startswith("planner error"))
        assert task["planner_errors"] == planner_errors, task["task_id"]


def test_pass_at_k_uses_the_unbiased_estimator() -> None:
    from repo_engineer.benchmark.live import pass_at_k

    assert pass_at_k(3, 1, 1) == pytest.approx(1 / 3)
    assert pass_at_k(3, 1, 2) == pytest.approx(2 / 3)
    assert pass_at_k(3, 1, 3) == 1.0
    assert pass_at_k(3, 0, 3) == 0.0
    assert pass_at_k(5, 5, 1) == 1.0
    with pytest.raises(ValueError):
        pass_at_k(2, 1, 3)


def test_live_results_report_pass_at_k(tmp_path: Path) -> None:
    path = run_live_benchmark(
        ["task_01_fix_off_by_one"], 3, tmp_path, FakeModelPlanner, stamp="20261120T000000Z"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    task = data["tasks"][0]
    assert task["pass_at_1"] == 1.0 and task["pass_at_3"] == 1.0
    assert data["summary"]["pass_at_1"] == 1.0 and data["summary"]["pass_at_3"] == 1.0
    assert data["summary"]["k"] == 3
