"""Run the benchmark with the live LLM planner (needs a provider key and network).

    GEMINI_API_KEY=<key, never committed> python scripts/run_llm_benchmark.py --attempts 3

Recorded SEPARATELY from the deterministic benchmark: every run writes a new
file results/llm_runs/<UTC start>.json plus one trace per attempt in
results/llm_runs/<UTC start>/, and never overwrites an earlier run, so
failures stay on record. Each attempt uses a fresh planner. Without a key,
use `python scripts/run_benchmark.py` (deterministic mode).

results/llm_benchmark_results.json is the original September 2026 live run,
kept as recorded apart from the accounting correction described in its notes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run from a checkout without installing

from repo_engineer.benchmark.harness import load_tasks  # noqa: E402
from repo_engineer.benchmark.live import run_live_benchmark  # noqa: E402
from repo_engineer.planner import LLMPlanner  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", nargs="+", default=[t["task_id"] for t in load_tasks()], help="task ids"
    )
    parser.add_argument("--attempts", type=int, default=3, help="attempts per task")
    parser.add_argument("--out-dir", default=str(ROOT / "results" / "llm_runs"))
    args = parser.parse_args()
    LLMPlanner()  # fail fast with ConfigurationError when no credentials are set
    path = run_live_benchmark(args.tasks, args.attempts, Path(args.out_dir), LLMPlanner)
    summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
    print(json.dumps(summary, indent=2))
    print(f"wrote {path}")
    return 0 if summary["passes"] == summary["attempts"] else 1


if __name__ == "__main__":
    sys.exit(main())
