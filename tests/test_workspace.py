"""prepare_workspace: fresh, numbered copies of a pristine task repo."""

from __future__ import annotations

import shutil
from pathlib import Path

from repo_engineer.agent import prepare_workspace


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "pkg" / "__pycache__").mkdir(parents=True)
    (source / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    (source / "pkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"stale")
    return source


def test_numbering_ignores_trace_files_and_survives_cleanup(tmp_path: Path) -> None:
    source = make_source(tmp_path)
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "task_trace.json").write_text("{}", encoding="utf-8")
    first = prepare_workspace(source, runs, "task")
    second = prepare_workspace(source, runs, "task")
    assert (first.name, second.name) == ("task_1", "task_2")
    shutil.rmtree(first)
    third = prepare_workspace(source, runs, "task")
    assert third.name == "task_3"


def test_copies_skip_bytecode_caches(tmp_path: Path) -> None:
    workspace = prepare_workspace(make_source(tmp_path), tmp_path / "runs", "task")
    assert (workspace / "pkg" / "mod.py").exists()
    assert not (workspace / "pkg" / "__pycache__").exists()
