"""The committed jail-attack evidence is reproducible and shows no breach."""

from __future__ import annotations

import json
from pathlib import Path

from repo_engineer.benchmark.jail_attacks import run_attacks

EVIDENCE = Path(__file__).resolve().parents[1] / "results" / "jail_attack_evidence.json"


def test_attacks_reproduce_the_committed_evidence() -> None:
    evidence = run_attacks()
    assert evidence == json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["summary"]["breached"] == 0
    assert evidence["summary"]["residual"] == 1
