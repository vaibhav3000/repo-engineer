"""prepare_workspace: fresh, numbered copies of a pristine task repo."""

from __future__ import annotations

import contextlib
import os
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


def test_links_in_the_source_repo_are_not_followed(tmp_path: Path) -> None:
    """Found by the Step 2 review: copying followed links, so a repo could pull
    host files (credentials) into the workspace the model reads."""
    from conftest import make_dir_link

    outside = tmp_path / "home" / ".aws"
    outside.mkdir(parents=True)
    (outside / "credentials").write_text("SECRET-OUTSIDE\n", encoding="utf-8")
    source = tmp_path / "repo"
    source.mkdir()
    (source / "app.py").write_text("x = 1\n", encoding="utf-8")
    make_dir_link(source / "fixtures", outside)
    # File symlinks need a privilege on Windows; the junction covers that platform.
    with contextlib.suppress(OSError):
        os.symlink(outside / "credentials", source / "settings.ini")
    workspace = prepare_workspace(source, tmp_path / "runs", "t")
    copied = {p.relative_to(workspace).as_posix() for p in workspace.rglob("*")}
    assert copied == {"app.py"}
    assert not any(
        "SECRET-OUTSIDE" in p.read_text(encoding="utf-8")
        for p in workspace.rglob("*")
        if p.is_file()
    )


def test_a_source_spelled_through_a_short_name_is_still_copied(tmp_path: Path) -> None:
    import tempfile

    source = Path(tempfile.mkdtemp(dir=tempfile.gettempdir())) / "repo"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    try:
        workspace = prepare_workspace(source, tmp_path / "runs", "t")
        assert (workspace / "pkg" / "m.py").read_text(encoding="utf-8") == "x = 1\n"
    finally:
        shutil.rmtree(source.parent)
