"""Shared fixtures for the repo_engineer test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repo_engineer.state import Observation


def error_text(obs: Observation) -> str:
    """The observation's error text; fails the test when there is none."""
    assert obs.error is not None, obs
    return obs.error


def make_dir_link(link: Path, target: Path) -> None:
    """A directory link the current user can always create: a junction on
    Windows, a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


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
