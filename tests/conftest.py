"""Shared fixtures for the repo_engineer test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_engineer.state import Observation


def error_text(obs: Observation) -> str:
    """The observation's error text; fails the test when there is none."""
    assert obs.error is not None, obs
    return obs.error


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A tiny repo with one module and one passing test that imports it."""
    ws = tmp_path / "ws"
    (ws / "tests").mkdir(parents=True)
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ws / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    return ws
