"""Replay a recorded run offline and check that it reproduces.

The planner decisions in an audit trace (calls, "done", planner errors) are
fed to the real runtime against a fresh copy of the starting workspace, whose
digest must match the recorded one. Nothing calls a model. Every tool
observation, every verification verdict and the final state are compared
with the recording; pytest's run durations are normalised first, the same
way the golden benchmark files are (docs/adr/0006).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent import AgentRuntime, prepare_workspace
from .benchmark.harness import TASK_DIR, normalise_pytest_output
from .planner import Planner
from .sandbox import Sandbox
from .state import ToolCall
from .tools import ToolRegistry
from .trace import MemorySink, read_trace, workspace_digest

COMPARED_OBSERVATION_FIELDS = ("ok", "rejected", "error", "exit_code", "output_excerpt")


class ReplayError(RuntimeError):
    """The trace cannot be replayed (no decisions, wrong starting workspace)."""


class ReplayedPlannerError(RuntimeError):
    """A planner failure that was recorded in the trace, raised again on replay."""


class ReplayPlanner(Planner):
    """Returns recorded planner decisions in order."""

    name = "replay"

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        self.decisions = list(decisions)
        self._next = 0

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        if self._next >= len(self.decisions):
            raise ReplayError("the run asked for more planner decisions than were recorded")
        decision = self.decisions[self._next]
        self._next += 1
        if decision["decision"] == "error":
            raise ReplayedPlannerError(decision.get("error", "recorded planner error"))
        if decision["decision"] == "done":
            return None
        return ToolCall(decision["tool"], dict(decision.get("args") or {}))


@dataclass(frozen=True)
class ReplayReport:
    reproduced: bool
    compared: int  # events compared
    first_difference: str | None
    final_state: str
    recorded_final_state: str


def replay(
    trace: str | os.PathLike[str],
    *,
    repo: Path | None = None,
    runs_root: Path | None = None,
    sandbox: Sandbox | None = None,
) -> ReplayReport:
    """Re-execute a recorded run and compare it with its trace."""
    events = read_trace(trace)
    start = next((e for e in events if e["kind"] == "run_start"), None)
    if start is None:
        raise ReplayError("the trace has no run_start event")
    task: dict[str, Any] = dict(start["task"])
    source = repo if repo is not None else TASK_DIR / task["task_id"]
    if not source.is_dir():
        raise ReplayError(
            f"no starting workspace for task {task['task_id']!r}: pass the repository with --repo"
        )
    decisions = [e for e in events if e["kind"] == "planner_decision"]
    with tempfile.TemporaryDirectory(prefix="repo_engineer_replay_") as scratch:
        root = runs_root if runs_root is not None else Path(scratch)
        workspace = prepare_workspace(source, root, _safe_name(task["task_id"]))
        digest = workspace_digest(workspace)
        if digest != start["workspace_digest"]:
            raise ReplayError(
                "the starting workspace differs from the recorded one "
                f"(digest {digest[:12]} vs {start['workspace_digest'][:12]})"
            )
        sink = MemorySink()
        runtime = AgentRuntime(
            ToolRegistry(workspace, sandbox=sandbox),
            ReplayPlanner(decisions),
            max_steps=start.get("max_steps", 40),
            max_replans=start.get("max_replans", 4),
            events=sink,
        )
        state = runtime.run_task(task)
    recorded_end = next((e for e in reversed(events) if e["kind"] == "run_end"), {})
    compared, difference = _compare(events, sink.events)
    return ReplayReport(
        reproduced=difference is None,
        compared=compared,
        first_difference=difference,
        final_state=state.state.value,
        recorded_final_state=recorded_end.get("final_state", "unknown"),
    )


def _comparable(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for event in events:
        if event["kind"] == "tool_call":
            out.append(
                {
                    "kind": "tool_call",
                    "tool": event["tool"],
                    "args": event["args"],
                    **_observation(event["observation"]),
                }
            )
        elif event["kind"] == "verify":
            out.append(
                {"kind": "verify", "passed": event["passed"], **_observation(event["observation"])}
            )
        elif event["kind"] == "run_end":
            out.append({"kind": "run_end", "final_state": event["final_state"]})
    return out


def _observation(observation: dict[str, Any]) -> dict[str, Any]:
    fields = {name: observation.get(name) for name in COMPARED_OBSERVATION_FIELDS}
    for name in ("output_excerpt", "error"):
        value = fields[name]
        if isinstance(value, str):
            fields[name] = normalise_pytest_output(value)
    return fields


def _compare(
    recorded: list[dict[str, Any]], replayed: list[dict[str, Any]]
) -> tuple[int, str | None]:
    left, right = _comparable(recorded), _comparable(replayed)
    for index, (old, new) in enumerate(zip(left, right, strict=False)):
        if old != new:
            changed = sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))
            label = old.get("tool") or old["kind"]
            return index, f"event {index} ({label}) differs in {', '.join(changed)}"
    if len(left) != len(right):
        return min(len(left), len(right)), (
            f"the recording has {len(left)} comparable events and the replay {len(right)}"
        )
    return len(left), None


def _safe_name(task_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id) or "run"
