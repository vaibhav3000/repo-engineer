"""Replay: recorded planner decisions re-executed offline against the same workspace."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_engineer.agent import AgentRuntime, prepare_workspace
from repo_engineer.benchmark.harness import load_tasks
from repo_engineer.planner import ScriptedPlanner
from repo_engineer.replay import ReplayError, replay
from repo_engineer.tools import ToolRegistry
from repo_engineer.trace import TraceWriter, read_trace

TASK = next(t for t in load_tasks() if t["task_id"] == "task_02_failing_test")


def record(tmp_path: Path) -> Path:
    workspace = prepare_workspace(Path(TASK["path"]), tmp_path / "runs", TASK["task_id"])
    trace = tmp_path / "run.jsonl"
    with TraceWriter(trace) as writer:
        AgentRuntime(ToolRegistry(workspace), ScriptedPlanner(), events=writer).run_task(TASK)
    return trace


def test_a_recorded_run_replays_identically(tmp_path: Path) -> None:
    trace = record(tmp_path)
    report = replay(trace, runs_root=tmp_path / "replays")
    assert report.reproduced, report.first_difference
    assert report.compared >= 5 and report.final_state == "COMPLETE"


def test_a_changed_observation_is_reported(tmp_path: Path) -> None:
    trace = record(tmp_path)
    events = read_trace(trace)
    tampered = next(e for e in events if e["kind"] == "tool_call" and e["tool"] == "search")
    tampered["observation"]["output_excerpt"] = "something else"
    trace.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    report = replay(trace, runs_root=tmp_path / "replays")
    assert not report.reproduced
    assert report.first_difference is not None and "search" in report.first_difference


def test_a_different_starting_workspace_is_refused(tmp_path: Path) -> None:
    trace = record(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="digest"):
        replay(trace, repo=other, runs_root=tmp_path / "replays")
