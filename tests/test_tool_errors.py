"""Error classification and write-time checks in the tool layer."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from conftest import error_text, make_dir_link

from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry

EDIT = ToolCall("apply_edit", {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"})


def test_os_permission_error_is_a_failure_not_a_jail_rejection(workspace: Path) -> None:
    target = workspace / "app.py"
    os.chmod(target, stat.S_IREAD)
    try:
        obs = ToolRegistry(workspace).execute(EDIT)
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    assert not obs.ok and not obs.rejected
    error = error_text(obs)
    assert "app.py" in error
    assert str(workspace) not in error and "<workspace>" not in error


def test_jail_rejections_stay_rejections(workspace: Path) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("read_file", {"path": "../x"}))
    assert obs.rejected and not obs.ok


@pytest.fixture
def unchecked_registry(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    """A registry whose up-front jail check is blind to links, standing in for
    a directory swapped for a link after the check and before the write."""
    reg = ToolRegistry(workspace)
    monkeypatch.setattr(
        reg, "jailed_path", lambda raw: workspace.joinpath(*Path(raw.replace("\\", "/")).parts)
    )
    return reg


def test_write_rechecks_the_target_right_before_writing(
    workspace: Path, tmp_path: Path, unchecked_registry: ToolRegistry
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_link(workspace / "sub", outside)
    obs = unchecked_registry.execute(
        ToolCall("write_file", {"path": "sub/new.py", "content": "x = 1\n"})
    )
    assert obs.rejected and "link" in error_text(obs)
    assert list(outside.iterdir()) == []


def test_edit_rechecks_the_target_right_before_writing(
    workspace: Path, tmp_path: Path, unchecked_registry: ToolRegistry
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "settings.py").write_text("A = 1\n", encoding="utf-8")
    make_dir_link(workspace / "sub", outside)
    obs = unchecked_registry.execute(
        ToolCall(
            "apply_edit", {"path": "sub/settings.py", "old_text": "A = 1", "new_text": "A = 2"}
        )
    )
    assert obs.rejected
    assert (outside / "settings.py").read_text(encoding="utf-8") == "A = 1\n"


def test_write_file_never_replaces_a_file_that_appears_meanwhile(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    obs = reg.execute(ToolCall("write_file", {"path": "app.py", "content": "clobbered"}))
    assert not obs.ok
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
