"""Benchmark harness: run the agent over the task suite and record results."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..agent import AgentRuntime, prepare_workspace
from ..planner import ScriptedPlanner
from ..tools import ToolRegistry

TASK_DIR = Path(__file__).resolve().parent / "tasks"


def load_tasks() -> list[dict[str, Any]]:
    tasks = []
    for task_path in sorted(p for p in TASK_DIR.iterdir() if p.is_dir()):
        meta_file = task_path / "task.json"
        if not meta_file.exists():
            continue
        task = json.loads(meta_file.read_text(encoding="utf-8"))
        task["path"] = str(task_path)
        tasks.append(task)
    return tasks


def run_benchmark(
    runs_root: str | Path,
    planner: Any = None,
    results_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run every task once in an isolated workspace copy; record real outcomes."""
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"environment": {"python_results_root": str(runs_root)},
                               "tasks": []}
    all_success = True
    for task in load_tasks():
        workspace = prepare_workspace(Path(task["path"]), runs_root, task["task_id"])
        registry = ToolRegistry(workspace)
        runtime = AgentRuntime(registry, planner or ScriptedPlanner())
        started = time.perf_counter()
        state = runtime.run_task(task, trace_path=runs_root / f"{task['task_id']}_trace.json")
        elapsed = time.perf_counter() - started
        summary = runtime.summary(state)
        summary["wall_time_s"] = round(elapsed, 2)
        summary["failed_tool_calls"] = sum(
            1 for o in state.observations if o.get("ok") is False
        )
        results["tasks"].append(summary)
        all_success = all_success and summary["success"]

    n_success = sum(1 for t in results["tasks"] if t["success"])
    results["summary"] = {
        "n_tasks": len(results["tasks"]),
        "n_success": n_success,
        "success_rate": round(n_success / max(len(results["tasks"]), 1), 3),
        "total_replans": sum(t["replans"] for t in results["tasks"]),
        "total_failed_tool_calls": sum(t["failed_tool_calls"] for t in results["tasks"]),
    }
    if results_path is not None:
        path = Path(results_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results
