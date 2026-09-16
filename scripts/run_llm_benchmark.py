"""Run a SMALL benchmark of the real LLM planner on selected tasks.

This is the "real LLM mode" benchmark, recorded SEPARATELY from the
deterministic benchmark (results/benchmark_results.json):

    REPO_ENGINEER_LLM_PROVIDER=gemini \
    GEMINI_API_KEY=<key from environment, never committed> \
    python scripts/run_llm_benchmark.py --tasks task_01_fix_off_by_one task_03_add_missing_test

Writes results/llm_benchmark_results.json. Requires network access and a
valid provider key in the environment; without it, use
`python scripts/run_benchmark.py` (deterministic mode).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repo_engineer.agent import AgentRuntime, prepare_workspace  # noqa: E402
from repo_engineer.benchmark.harness import TASK_DIR, load_tasks  # noqa: E402
from repo_engineer.planner import LLMPlanner  # noqa: E402
from repo_engineer.tools import ToolRegistry  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", nargs="+", required=True, help="task ids to run")
    ap.add_argument("--out", default=str(ROOT / "results" / "llm_benchmark_results.json"))
    args = ap.parse_args()

    planner = LLMPlanner()  # raises ConfigurationError without credentials
    tasks = {t["task_id"]: t for t in load_tasks()}
    results = {
        "mode": "llm",
        "provider": {
            "model": planner.model,
            "temperature": planner.provider.temperature,
            "key_source": "environment variable (value never recorded)",
        },
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "tasks": [],
    }
    all_success = True
    for task_id in args.tasks:
        task = dict(tasks[task_id])
        task["path"] = str(TASK_DIR / task_id)
        workspace = prepare_workspace(TASK_DIR / task_id, ROOT / "runs", task_id)
        runtime = AgentRuntime(ToolRegistry(workspace), planner)
        started = time.perf_counter()
        state = runtime.run_task(task, trace_path=ROOT / "runs" / f"{task_id}_llm_trace.json")
        summary = runtime.summary(state)
        summary["wall_time_s"] = round(time.perf_counter() - started, 1)
        summary["failed_tool_calls"] = sum(1 for o in state.observations if o.get("ok") is False)
        results["tasks"].append(summary)
        all_success = all_success and summary["success"]
        print(f"[{'PASS' if summary['success'] else 'FAIL'}] {task_id}: "
              f"steps={summary['steps']} replans={summary['replans']} "
              f"llm_calls={summary.get('llm_calls')}", flush=True)

    n_success = sum(1 for t in results["tasks"] if t["success"])
    results["summary"] = {
        "n_tasks": len(results["tasks"]),
        "n_success": n_success,
        "success_rate": round(n_success / max(len(results["tasks"]), 1), 3),
        "total_llm_calls": sum(t.get("llm_calls", 0) for t in results["tasks"]),
        "total_prompt_tokens": sum(t.get("llm_prompt_tokens", 0) for t in results["tasks"]),
        "total_completion_tokens": sum(t.get("llm_completion_tokens", 0) for t in results["tasks"]),
        "total_failed_tool_calls": sum(t["failed_tool_calls"] for t in results["tasks"]),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results["summary"], indent=2))


if __name__ == "__main__":
    main()
