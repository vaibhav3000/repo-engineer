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


def test_the_state_diagram_has_exactly_the_runtime_edges() -> None:
    from repo_engineer.state import TRANSITIONS

    diagram = sync_docs().md_state_diagram({})
    edges = {line.strip() for line in diagram.splitlines() if "-->" in line and "[*]" not in line}
    assert edges == {
        f"{a.value} --> {b.value}" for a, targets in TRANSITIONS.items() for b in targets
    }
