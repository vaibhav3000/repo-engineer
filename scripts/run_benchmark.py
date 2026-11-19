"""Run the deterministic benchmark and regenerate the committed results.

    python scripts/run_benchmark.py [--sandbox auto|docker|local]

Writes results/benchmark_results.json and results/benchmark_traces/*.json.
Both are golden files: running this again, with any sandbox backend, must
leave `git diff results/` empty. Exits 1 when any task fails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from repo_engineer.benchmark.harness import main
from repo_engineer.sandbox import SANDBOX_MODES, select_sandbox

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox", choices=SANDBOX_MODES, default="auto")
    args = parser.parse_args()
    sys.exit(main(ROOT / "results", sandbox=select_sandbox(args.sandbox)))
