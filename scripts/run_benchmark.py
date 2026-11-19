"""Run the deterministic benchmark and regenerate the committed results.

    python scripts/run_benchmark.py

Writes results/benchmark_results.json and results/benchmark_traces/*.json.
Both are golden files: running this again must leave `git diff results/`
empty. Exits 1 when any task fails.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run from a checkout without installing

from repo_engineer.benchmark.harness import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(ROOT / "results"))
