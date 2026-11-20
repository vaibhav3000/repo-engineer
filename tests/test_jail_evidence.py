"""The committed jail-attack evidence is reproducible per backend and shows no breach."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import docker_ready  # noqa: F401  (used by the skipif condition)

from repo_engineer.benchmark.jail_attacks import evidence_path, run_attacks
from repo_engineer.sandbox import DockerSandbox

RESULTS = Path(__file__).resolve().parents[1] / "results"

# The documented residuals: on the local backend agent-written test code runs
# as the host user; on every backend code under test can forge its own report.
EXPECTED_RESIDUALS = {
    "local": {"A39", "A40", "A41", "A42", "A46"},
    "docker": {"A46"},
}


def residuals(evidence: dict) -> set[str]:
    return {a["id"] for a in evidence["attacks"] if a["outcome"] == "residual"}


def test_local_attacks_reproduce_the_committed_evidence() -> None:
    evidence = run_attacks("local")
    committed = json.loads(evidence_path(RESULTS, "local").read_text(encoding="utf-8"))
    assert evidence == committed
    assert evidence["summary"]["breached"] == 0
    assert residuals(evidence) == EXPECTED_RESIDUALS["local"]


@pytest.mark.skipif("not docker_ready()", reason="no Linux Docker daemon answers")
def test_docker_attacks_reproduce_the_committed_evidence() -> None:
    evidence = run_attacks("docker", DockerSandbox())
    committed = json.loads(evidence_path(RESULTS, "docker").read_text(encoding="utf-8"))
    assert evidence == committed
    assert evidence["summary"]["breached"] == 0
    assert residuals(evidence) == EXPECTED_RESIDUALS["docker"]


def test_unknown_backends_are_refused() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        run_attacks("vm")
