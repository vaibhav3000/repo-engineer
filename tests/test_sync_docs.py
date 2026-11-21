"""Every published number is generated from the committed results and is current."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_docs.py"


def sync_docs() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_docs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_numbers_match_the_committed_results() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_an_attack_without_a_table_group_fails_the_generator() -> None:
    module = sync_docs()
    with pytest.raises(module.StaleError, match="A99"):
        module.group_of({"id": "A99", "category": "residual"})
    with pytest.raises(module.StaleError, match="new kind"):
        module.group_of({"id": "A98", "category": "new kind"})


def test_the_state_figure_draws_exactly_the_runtime_edges() -> None:
    """The report's and README's state diagram is drawn from the transition table."""
    from repo_engineer.state import TRANSITIONS

    edges_tex = sync_docs().tex_fsm_edges({})
    drawn = {
        line.split("% edge ", 1)[1].strip() for line in edges_tex.splitlines() if "% edge " in line
    }
    assert drawn == {
        f"{a.value} -> {b.value}" for a, targets in TRANSITIONS.items() for b in targets
    }
    assert edges_tex.count("\\draw") == len(drawn)


def test_an_undrawn_transition_fails_the_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    module = sync_docs()
    from repo_engineer.state import State

    routes = dict(module.FSM_ROUTES)
    routes.pop((State.REPLAN, State.PLAN))
    monkeypatch.setattr(module, "FSM_ROUTES", routes)
    with pytest.raises(module.StaleError, match="REPLAN -> PLAN"):
        module.tex_fsm_edges({})


def test_the_walkthrough_refuses_a_docker_residual_it_does_not_name() -> None:
    """The README walkthrough describes A46; another residual must stop the generator."""
    module = sync_docs()
    summary = {"total": 1, "rejected": 0, "contained": 0, "residual": 1, "breached": 0}
    facts = {
        "bench": {"summary": {"n_success": 1, "n_tasks": 1}},
        "jail": {
            "docker": {"summary": summary, "attacks": [{"id": "A99", "outcome": "residual"}]},
            "local": {"summary": summary, "attacks": []},
        },
    }
    with pytest.raises(module.StaleError, match="A99"):
        module.md_walkthrough(facts)


def test_the_demo_data_matches_the_committed_results() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "demo-site" / "build.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
