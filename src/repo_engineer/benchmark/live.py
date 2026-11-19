"""Benchmark runs with a live (model-backed) planner.

Every invocation writes a new results file named by its UTC start time under
results/llm_runs/, together with the golden-style trace of every attempt, and
refuses to overwrite an existing file. Repeated runs therefore accumulate a
record, failures included, instead of replacing each other. Each attempt gets a
fresh planner, so no state or usage leaks from one attempt into the next.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..agent import AgentRuntime, prepare_workspace
from ..planner import Planner
from ..tools import ToolRegistry
from .harness import golden_trace, load_tasks


def run_live_benchmark(
    task_ids: Sequence[str],
    attempts: int,
    out_dir: Path,
    planner_factory: Callable[[], Planner],
    stamp: str | None = None,
) -> Path:
    """Run each task `attempts` times; return the path of the new results file."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    known = {task["task_id"]: task for task in load_tasks()}
    unknown = [task_id for task_id in task_ids if task_id not in known]
    if unknown:
        raise ValueError(f"unknown task ids: {unknown}")
    stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    results_path = out_dir / f"{stamp}.json"
    traces_dir = out_dir / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    # Claim the results file first so two runs can never share a name.
    with results_path.open("x", encoding="utf-8"):
        pass
    record: dict[str, Any] = {"mode": "llm", "started_utc": stamp, "tasks": []}
    with tempfile.TemporaryDirectory(prefix="repo_engineer_live_") as runs_root:
        for task_id in task_ids:
            task = known[task_id]
            entry: dict[str, Any] = {"task_id": task_id, "attempts": []}
            for attempt in range(1, attempts + 1):
                planner = planner_factory()
                workspace = prepare_workspace(Path(task["path"]), Path(runs_root), task_id)
                runtime = AgentRuntime(ToolRegistry(workspace), planner)
                started = time.perf_counter()
                state = runtime.run_task(task)
                summary = runtime.summary(state)
                summary["attempt"] = attempt
                summary["wall_time_s"] = round(time.perf_counter() - started, 1)
                summary["failed_tool_calls"] = sum(
                    1 for o in state.observations if o.get("ok") is False
                )
                trace_name = f"{stamp}/{task_id}_attempt{attempt}.json"
                _write_json(out_dir / trace_name, golden_trace(state))
                summary["trace"] = trace_name
                entry["attempts"].append(summary)
            entry["passes"] = sum(1 for a in entry["attempts"] if a["success"])
            record["tasks"].append(entry)
    all_attempts = [a for t in record["tasks"] for a in t["attempts"]]
    record["summary"] = {
        "tasks": len(record["tasks"]),
        "attempts": len(all_attempts),
        "passes": sum(1 for a in all_attempts if a["success"]),
        "tasks_passed_at_least_once": sum(1 for t in record["tasks"] if t["passes"] > 0),
        "total_llm_calls": sum(a.get("llm_calls", 0) for a in all_attempts),
        "total_prompt_tokens": sum(a.get("llm_prompt_tokens", 0) for a in all_attempts),
        "total_completion_tokens": sum(a.get("llm_completion_tokens", 0) for a in all_attempts),
        "total_planner_errors": sum(a["planner_errors"] for a in all_attempts),
    }
    traces_dir.mkdir(parents=True, exist_ok=True)
    _write_json(results_path, record)
    return results_path


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
