"""The streamed JSONL audit trace, workspace digests and cost accounting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_engineer.agent import AgentRuntime
from repo_engineer.models import Usage
from repo_engineer.planner import RecordedPlanner
from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry
from repo_engineer.trace import MemorySink, PriceTable, TraceWriter, read_trace, workspace_digest


def test_every_event_is_a_flushed_line_with_a_sequence_and_the_state(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    writer = TraceWriter(path)
    writer.emit("state", to="PLAN")
    writer.emit("tool_call", tool="list_tree")
    # Readable before close: a crashed run still leaves its history.
    events = read_trace(path)
    assert [(e["seq"], e["kind"], e["state"]) for e in events] == [
        (0, "state", "PLAN"),
        (1, "tool_call", "PLAN"),
    ]
    assert all("t_ms" in e for e in events)
    writer.close()


def test_a_run_streams_its_whole_story(workspace: Path) -> None:
    sink = MemorySink()
    planner = RecordedPlanner([ToolCall("read_file", {"path": "app.py"}), None])
    AgentRuntime(ToolRegistry(workspace), planner, events=sink).run_task({"task_id": "t"})
    kinds = [e["kind"] for e in sink.events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"state", "planner_decision", "tool_call", "verify"} <= set(kinds)
    start = sink.events[0]
    assert start["task"]["task_id"] == "t" and start["sandbox"] == "local"
    assert start["workspace_digest"] == workspace_digest(workspace)
    decisions = [e for e in sink.events if e["kind"] == "planner_decision"]
    assert [d["decision"] for d in decisions] == ["call", "done"]
    call = next(e for e in sink.events if e["kind"] == "tool_call" and e["tool"] == "read_file")
    assert call["observation"]["ok"] is True and call["latency_ms"] >= 0
    end = sink.events[-1]
    assert end["final_state"] == "COMPLETE" and end["verified"] is True


def test_planner_errors_are_recorded_as_decisions(workspace: Path) -> None:
    class Failing(RecordedPlanner):
        def __init__(self) -> None:
            super().__init__([None])
            self.failures = 1

        def next_action(self, task, history):  # type: ignore[no-untyped-def]
            if self.failures:
                self.failures -= 1
                raise RuntimeError("provider down")
            return super().next_action(task, history)

    sink = MemorySink()
    AgentRuntime(ToolRegistry(workspace), Failing(), events=sink).run_task({"task_id": "t"})
    decisions = [e for e in sink.events if e["kind"] == "planner_decision"]
    assert decisions[0]["decision"] == "error" and "provider down" in decisions[0]["error"]


def test_the_digest_changes_with_content_not_with_caches(workspace: Path) -> None:
    before = workspace_digest(workspace)
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "x.pyc").write_bytes(b"x")
    assert workspace_digest(workspace) == before
    (workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert workspace_digest(workspace) != before


def test_prices_turn_usage_into_cost(tmp_path: Path) -> None:
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {"m": {"input_per_mtok": 2.0, "output_per_mtok": 10.0, "cache_read_per_mtok": 0.5}}
        ),
        encoding="utf-8",
    )
    table = PriceTable.load(prices)
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=400_000)
    # 600k uncached input at 2.0, 400k cached at 0.5, 100k output at 10.0
    assert table.cost("m", usage) == pytest.approx(1.2 + 0.2 + 1.0)
    assert table.cost("unknown-model", usage) is None
    assert PriceTable.empty().cost("m", usage) is None
