"""The workspace jail: lexical path rules, links, .git, and central enforcement."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from conftest import error_text

from repo_engineer.state import ToolCall
from repo_engineer.tools import Permission, ToolRegistry, ToolSpec


def make_dir_link(link: Path, target: Path) -> None:
    """A directory link the current user can always create: a junction on
    Windows, a symlink elsewhere."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "secret.py").write_text("SECRET = 'outside-the-workspace'\n", encoding="utf-8")
    return secret_dir


# ------------------------------------------------------------ lexical rules


@pytest.mark.parametrize(
    "raw",
    [
        "\\\\192.0.2.1\\share\\x.txt",
        "//192.0.2.1/share/x.txt",
        "\\\\?\\C:\\Windows\\win.ini",
        "\\\\.\\PhysicalDrive0",
        "/etc/passwd",
        "C:/Windows/win.ini",
        "C:relative.txt",
        "../outside.txt",
        "tests/../../outside.txt",
    ],
)
def test_escapes_are_rejected_before_touching_the_filesystem(
    workspace: Path, raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    touched: list[str] = []
    real_realpath = os.path.realpath

    def spy(path: Any, *args: Any, **kwargs: Any) -> str:
        touched.append(str(path))
        return real_realpath(path, *args, **kwargs)

    reg = ToolRegistry(workspace)
    monkeypatch.setattr(os.path, "realpath", spy)
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(spy(self)))
    obs = reg.execute(ToolCall("read_file", {"path": raw}))
    assert obs.rejected and not obs.ok
    assert "path escape rejected" in error_text(obs)
    assert touched == [], touched


@pytest.mark.parametrize(
    "raw",
    [
        "app.py:hidden",
        "app.py::$DATA",
        "app.py.",
        "app.py ",
        "app.py. ",
        "CON",
        "nul.txt",
        "Com1.py",
        "tests/LPT9",
        ".git/config",
        "sub/.GIT/HEAD",
        "app\x00.py",
    ],
)
def test_aliases_streams_devices_and_git_paths_are_rejected(workspace: Path, raw: str) -> None:
    reg = ToolRegistry(workspace)
    for call in (
        ToolCall("read_file", {"path": raw}),
        ToolCall("write_file", {"path": raw, "content": "x"}),
        ToolCall("apply_edit", {"path": raw, "old_text": "VALUE", "new_text": "X"}),
    ):
        obs = reg.execute(call)
        assert obs.rejected and not obs.ok, (call, obs)
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert sorted(p.name for p in workspace.iterdir()) == ["app.py", "tests"]


@pytest.mark.parametrize("raw", ["./app.py", "tests/../app.py", "tests\\..\\app.py"])
def test_equivalent_spellings_inside_the_workspace_are_accepted(workspace: Path, raw: str) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("read_file", {"path": raw}))
    assert obs.ok and "VALUE = 1" in obs.output_excerpt, obs


def test_backslash_separators_work_for_test_files(workspace: Path) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("read_file", {"path": "tests\\test_app.py"}))
    assert obs.ok and "def test_value" in obs.output_excerpt


@pytest.mark.parametrize("glob", ["../outside/*.py", "/etc/*", "C:/x/*", "a:b/*", ".git/**"])
def test_glob_escapes_are_rejected(workspace: Path, glob: str) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("search", {"pattern": "x", "glob": glob}))
    assert obs.rejected and not obs.ok


# ------------------------------------------------------------ links


def test_paths_through_a_directory_link_are_rejected(workspace: Path, outside: Path) -> None:
    make_dir_link(workspace / "vendor", outside)
    reg = ToolRegistry(workspace)
    read = reg.execute(ToolCall("read_file", {"path": "vendor/secret.py"}))
    assert read.rejected and "link" in error_text(read)
    write = reg.execute(ToolCall("write_file", {"path": "vendor/new.py", "content": "x = 1\n"}))
    assert write.rejected and not (outside / "new.py").exists()
    edit = reg.execute(
        ToolCall("apply_edit", {"path": "vendor/secret.py", "old_text": "SECRET", "new_text": "X"})
    )
    assert edit.rejected
    assert "SECRET" in (outside / "secret.py").read_text(encoding="utf-8")


def test_search_and_list_tree_do_not_follow_links(workspace: Path, outside: Path) -> None:
    make_dir_link(workspace / "vendor", outside)
    reg = ToolRegistry(workspace)
    for glob in ("vendor/*", "**/*.py", "**/*"):
        obs = reg.execute(ToolCall("search", {"pattern": "SECRET", "glob": glob}))
        assert obs.ok, obs
        assert "outside-the-workspace" not in obs.output_excerpt, glob
    tree = reg.execute(ToolCall("list_tree", {}))
    assert tree.ok and "secret.py" not in tree.output_excerpt


def test_search_and_list_tree_report_posix_paths(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    found = reg.execute(ToolCall("search", {"pattern": "def test_value"}))
    assert "tests/test_app.py:4:" in found.output_excerpt
    tree = reg.execute(ToolCall("list_tree", {}))
    assert "tests/test_app.py" in tree.output_excerpt


# ------------------------------------------------------------ central enforcement


def test_path_arguments_are_jailed_before_any_tool_runs(workspace: Path) -> None:
    calls: list[Any] = []
    reg = ToolRegistry(workspace)
    reg.specs["peek"] = ToolSpec(
        name="peek",
        description="test tool that forgets to jail its own argument",
        permission=Permission.READ_ONLY,
        required_args={"path": str},
        path_args=("path",),
        run=lambda path: calls.append(path) or "ran",
    )
    obs = reg.execute(ToolCall("peek", {"path": "../outside.txt"}))
    assert obs.rejected and calls == []
    obs = reg.execute(ToolCall("peek", {"path": "app.py"}))
    assert obs.ok and calls == [workspace / "app.py"]
