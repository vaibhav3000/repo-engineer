"""Benchmark harness: run the agent over the task suite and record results.

The committed results (results/benchmark_results.json and the traces under
results/benchmark_traces/) are golden files: regenerating them with
scripts/run_benchmark.py must reproduce them byte for byte on any machine.
They therefore carry no absolute paths, timestamps or wall times, and the two
things pytest prints that vary between runs and platforms are normalised:
the duration in its summary line and the path separator in traceback
locations. Wall times are printed to the console instead.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from ..agent import AgentRuntime, prepare_workspace
from ..planner import Planner, ScriptedPlanner
from ..sandbox import Sandbox
from ..state import AgentState
from ..tools import ToolRegistry

TASK_DIR = Path(__file__).resolve().parent / "tasks"

# "3 passed in 0.02s" (optionally framed by '=' and with an h:mm:ss suffix).
_DURATION = re.compile(r"(?m) in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?(?=(?: =+)?$)")
# Traceback locations such as "tests\test_x.py:6: AssertionError".
_LOCATION = re.compile(r"(?m)^([^\s:]+\.py)(?=:\d+: )")
# Separator lines ("=== FAILURES ===", "___ test_x ___"). pytest draws them one
# column narrower on Windows, so their padding is reduced to three characters.
_SEPARATOR = re.compile(r"(?m)^([=_-])\1{2,}(?: (.+?) \1{3,})?$")


def load_tasks() -> list[dict[str, Any]]:
    tasks = []
    for task_path in sorted(p for p in TASK_DIR.iterdir() if p.is_dir()):
        meta_file = task_path / "task.json"
        if not meta_file.exists():
            continue
        task = json.loads(meta_file.read_text(encoding="utf-8"))
        task["path"] = str(task_path)
        tasks.append(task)
    if not tasks:
        # A packaging mistake that drops task.json must fail loudly, not
        # report a 0/0 benchmark.
        raise RuntimeError(f"no benchmark tasks (task.json files) found in {TASK_DIR}")
    return tasks


def normalise_pytest_output(text: str) -> str:
    """Mask pytest's run duration, use '/' in traceback locations and
    shorten separator padding, so the text is the same on every platform."""
    text = _DURATION.sub(" in <duration>", text)
    text = _LOCATION.sub(lambda match: match.group(1).replace("\\", "/"), text)
    return _SEPARATOR.sub(_short_separator, text)


def _short_separator(match: re.Match[str]) -> str:
    fill = match.group(1) * 3
    title = match.group(2)
    return f"{fill} {title} {fill}" if title else fill


def golden_trace(state: AgentState) -> dict[str, Any]:
    """A trace with everything that varies between runs or machines removed."""
    trace = state.to_dict()
    trace.pop("started_at", None)
    for observation in trace["observations"]:
        if observation.get("output_excerpt"):
            observation["output_excerpt"] = normalise_pytest_output(observation["output_excerpt"])
        if observation.get("error"):
            observation["error"] = normalise_pytest_output(observation["error"])
    return trace


def run_benchmark(
    runs_root: str | Path,
    planner: Planner | None = None,
    results_path: str | Path | None = None,
    traces_dir: str | Path | None = None,
    max_replans: int = 4,
    sandbox: Sandbox | None = None,
) -> dict[str, Any]:
    """Run every task once in an isolated workspace copy; record real outcomes.

    The full runtime trace of each task goes to runs_root; the golden results
    and, when traces_dir is given, golden traces are written as well.
    """
    runs_root = Path(runs_root)
    runs_root.mkdir(parents=True, exist_ok=True)
    chosen = planner or ScriptedPlanner()
    results: dict[str, Any] = {"planner": chosen.name, "tasks": []}
    wall_times: dict[str, float] = {}
    for task in load_tasks():
        workspace = prepare_workspace(Path(task["path"]), runs_root, task["task_id"])
        registry = ToolRegistry(workspace, sandbox=sandbox)
        runtime = AgentRuntime(registry, chosen, max_replans=max_replans)
        started = time.perf_counter()
        state = runtime.run_task(task, trace_path=runs_root / f"{task['task_id']}_trace.json")
        wall_times[task["task_id"]] = time.perf_counter() - started
        summary = runtime.summary(state)
        summary["failed_tool_calls"] = sum(1 for o in state.observations if o.get("ok") is False)
        results["tasks"].append(summary)
        if traces_dir is not None:
            _write_json(Path(traces_dir) / f"{task['task_id']}_trace.json", golden_trace(state))

    n_success = sum(1 for t in results["tasks"] if t["success"])
    results["summary"] = {
        "n_tasks": len(results["tasks"]),
        "n_success": n_success,
        "success_rate": round(n_success / max(len(results["tasks"]), 1), 3),
        "total_replans": sum(t["replans"] for t in results["tasks"]),
        "total_failed_tool_calls": sum(t["failed_tool_calls"] for t in results["tasks"]),
    }
    if results_path is not None:
        _write_json(Path(results_path), results)
    results["wall_times_s"] = wall_times  # console only; not part of the golden file
    return results


def main(
    results_dir: Path,
    planner: Planner | None = None,
    max_replans: int = 4,
    sandbox: Sandbox | None = None,
) -> int:
    """Regenerate the golden results in results_dir; 0 when every task passed.

    The results do not depend on the sandbox backend: the same files come out
    of the local and the Docker sandbox.
    """
    with tempfile.TemporaryDirectory(prefix="repo_engineer_runs_") as runs_root:
        results = run_benchmark(
            runs_root,
            planner=planner,
            results_path=results_dir / "benchmark_results.json",
            traces_dir=results_dir / "benchmark_traces",
            max_replans=max_replans,
            sandbox=sandbox,
        )
    if sandbox is not None:
        print(f"sandbox: {sandbox.name}")
    print(json.dumps(results["summary"], indent=2))
    for task in results["tasks"]:
        status = "PASS" if task["success"] else "FAIL"
        seconds = results["wall_times_s"][task["task_id"]]
        print(
            f"[{status}] {task['task_id']}: steps={task['steps']} replans={task['replans']} "
            f"failed_tool_calls={task['failed_tool_calls']} ({seconds:.2f}s)"
        )
    return 0 if results["summary"]["n_success"] == results["summary"]["n_tasks"] else 1


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, indent=2) + "\n")
