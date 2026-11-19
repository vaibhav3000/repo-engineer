"""git_diff: an in-process diff against the workspace as it was handed to the agent."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry


def diff(reg: ToolRegistry) -> str:
    obs = reg.execute(ToolCall("git_diff", {}))
    assert obs.ok, obs
    return obs.output_excerpt


def test_reports_no_changes_on_a_fresh_workspace(workspace: Path) -> None:
    assert diff(ToolRegistry(workspace)) == "(no changes)"


def test_shows_edits_new_files_and_deletions(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    reg.execute(
        ToolCall("apply_edit", {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"})
    )
    reg.execute(ToolCall("write_file", {"path": "pkg/new.py", "content": "NEW = True\n"}))
    (workspace / "tests" / "test_app.py").unlink()
    out = diff(reg)
    assert "--- a/app.py\n+++ b/app.py" in out
    assert "-VALUE = 1\n+VALUE = 2" in out
    assert "--- /dev/null\n+++ b/pkg/new.py" in out and "+NEW = True" in out
    assert "--- a/tests/test_app.py\n+++ /dev/null" in out
    assert out.index("a/app.py") < out.index("b/pkg/new.py") < out.index("a/tests/test_app.py")


def test_runs_no_subprocess_and_ignores_git_metadata(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_dir = workspace / ".git"
    git_dir.mkdir()
    marker = workspace.parent / "FSMONITOR_RAN"
    (git_dir / "config").write_text(
        f"[core]\n\tfsmonitor = python -c \"open(r'{marker}', 'w')\"\n", encoding="utf-8"
    )
    reg = ToolRegistry(workspace)
    (workspace / "app.py").write_text("VALUE = 3\n", encoding="utf-8")

    def no_processes(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("git_diff must not start a process")

    monkeypatch.setattr(subprocess, "run", no_processes)
    monkeypatch.setattr(subprocess, "Popen", no_processes)
    out = diff(reg)
    assert "+VALUE = 3" in out
    assert ".git" not in out
    assert not marker.exists()


def test_does_not_show_changes_outside_the_workspace(tmp_path: Path, workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    (tmp_path / "private_notes.txt").write_text("outside change\n", encoding="utf-8")
    assert diff(reg) == "(no changes)"


# Found by the Step 2 review: files the snapshot could not read (over the
# per-file cap, or past the total budget) came out of the diff as deletions,
# and `run --apply` then deleted them from the original repository.


def test_a_file_that_grows_past_the_size_cap_is_not_a_deletion(tmp_path: Path) -> None:
    from repo_engineer import snapshot

    (tmp_path / "log.md").write_text("x\n", encoding="utf-8")
    snap = snapshot.WorkspaceSnapshot(tmp_path)
    (tmp_path / "log.md").write_text("x\n" * (snapshot.MAX_FILE_BYTES // 2 + 1), encoding="utf-8")
    text, not_compared = snap.diff_report()
    assert "/dev/null" not in text and not_compared == ["log.md"]


def test_a_file_pushed_past_the_budget_is_not_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repo_engineer import snapshot

    monkeypatch.setattr(snapshot, "MAX_TOTAL_BYTES", 100)
    (tmp_path / "b.txt").write_text("b" * 60, encoding="utf-8")
    (tmp_path / "z.txt").write_text("z" * 30, encoding="utf-8")
    snap = snapshot.WorkspaceSnapshot(tmp_path)
    (tmp_path / "a.txt").write_text("a" * 30, encoding="utf-8")  # sorts first, moves the cut-off
    text, not_compared = snap.diff_report()
    assert "+++ b/a.txt" in text
    assert "z.txt" not in text and not_compared == ["z.txt"]


def test_real_deletions_and_untouched_large_files(tmp_path: Path) -> None:
    from repo_engineer import snapshot

    (tmp_path / "gone.txt").write_text("bye\n", encoding="utf-8")
    (tmp_path / "big.bin").write_bytes(b"x" * (snapshot.MAX_FILE_BYTES + 1))
    snap = snapshot.WorkspaceSnapshot(tmp_path)
    (tmp_path / "gone.txt").unlink()
    text, not_compared = snap.diff_report()
    assert "--- a/gone.txt\n+++ /dev/null" in text
    assert not_compared == []  # the large file did not change
