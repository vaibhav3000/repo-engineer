"""Run the benchmark with a live model (needs a provider key and network).

    GEMINI_API_KEY=<key, never committed> python scripts/run_llm_benchmark.py \
        --provider gemini --model <model id> --attempts 3

The same as `repo-engineer bench --live`. Recorded SEPARATELY from the
deterministic benchmark: every run writes a new file
results/llm_runs/<UTC start>.json with pass@1 and pass@k, plus one trace per
attempt in results/llm_runs/<UTC start>/, and never overwrites an earlier
run, so failures stay on record. Each attempt uses a fresh planner. Without a
key, use `python scripts/run_benchmark.py` (deterministic mode).

results/llm_benchmark_results.json is the original September 2026 live run,
kept as recorded apart from the accounting correction described in its notes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from repo_engineer.cli import main

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.exit(main(["bench", "--live", "--results", str(ROOT / "results"), *sys.argv[1:]]))
