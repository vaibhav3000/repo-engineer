"""Run the SWE benchmark: python scripts/run_benchmark.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from repo_engineer.benchmark.harness import run_benchmark  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    results = run_benchmark(
        runs_root=ROOT / "runs",
        results_path=ROOT / "results" / "benchmark_results.json",
    )
    import json

    print(json.dumps(results["summary"], indent=2))
    for task in results["tasks"]:
        status = "PASS" if task["success"] else "FAIL"
        print(f"[{status}] {task['task_id']}: steps={task['steps']} "
              f"replans={task['replans']} failed_tool_calls={task['failed_tool_calls']}")
