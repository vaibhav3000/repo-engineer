"""Replay the adversarial tool calls and regenerate the committed evidence.

    python scripts/run_jail_attacks.py

Writes results/jail_attack_evidence.json. Running it again must leave
`git diff results/` empty. Exits 1 if any attack breached the jail.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))  # run from a checkout without installing

from repo_engineer.benchmark.jail_attacks import write_evidence  # noqa: E402

if __name__ == "__main__":
    evidence = write_evidence(ROOT / "results" / "jail_attack_evidence.json")
    print(json.dumps(evidence["summary"], indent=2))
    for attack in evidence["attacks"]:
        print(f"{attack['id']} {attack['outcome']:<9} {attack['tool']:<11} {attack['description']}")
    sys.exit(1 if evidence["summary"]["breached"] else 0)
