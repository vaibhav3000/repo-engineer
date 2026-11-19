"""Replay the adversarial tool calls against one sandbox backend and regenerate its evidence.

    python scripts/run_jail_attacks.py                   # local backend
    python scripts/run_jail_attacks.py --backend docker  # needs a Linux Docker daemon

Writes results/jail_attack_evidence.json (local) or
results/jail_attack_evidence_docker.json. Running it again must leave
`git diff results/` empty. Exits 1 if any attack breached the jail or the
sandbox.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repo_engineer.benchmark.jail_attacks import BACKENDS, evidence_path, write_evidence
from repo_engineer.sandbox import Sandbox, select_sandbox

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=BACKENDS, default="local")
    args = parser.parse_args()
    sandbox: Sandbox = select_sandbox(args.backend)
    evidence = write_evidence(evidence_path(ROOT / "results", args.backend), args.backend, sandbox)
    print(json.dumps(evidence["summary"], indent=2))
    for attack in evidence["attacks"]:
        print(f"{attack['id']} {attack['outcome']:<9} {attack['tool']:<11} {attack['description']}")
    return 1 if evidence["summary"]["breached"] else 0


if __name__ == "__main__":
    sys.exit(main())
