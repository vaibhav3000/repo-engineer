"""Argument validation and resource bounds on tool calls."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from conftest import error_text

from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"path": "app.py", "start_line": True}),
        ("read_file", {"path": "app.py", "end_line": False}),
        ("list_tree", {"max_depth": True}),
    ],
)
def test_booleans_are_not_accepted_as_integers(
    workspace: Path, tool: str, args: dict[str, Any]
) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall(tool, args))
    assert obs.rejected and "must be int" in error_text(obs)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("read_file", {"path": "app.py", "start_line": 0}),
        ("read_file", {"path": "app.py", "end_line": -1}),
        ("list_tree", {"max_depth": 0}),
        ("list_tree", {"max_depth": -3}),
        ("list_tree", {"max_depth": 7}),
    ],
)
def test_out_of_range_integers_are_rejected(
    workspace: Path, tool: str, args: dict[str, Any]
) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall(tool, args))
    assert obs.rejected and "out of range" in error_text(obs)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write_file", {"path": "big.txt", "content": "x" * 1_000_001}),
        ("apply_edit", {"path": "app.py", "old_text": "VALUE", "new_text": "x" * 1_000_001}),
        ("search", {"pattern": "a" * 513}),
        ("run_tests", {"cmd": "python -m pytest " + "-q " * 1000}),
    ],
)
def test_oversized_strings_are_rejected(workspace: Path, tool: str, args: dict[str, Any]) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall(tool, args))
    assert obs.rejected and "longer than" in error_text(obs)
    assert not (workspace / "big.txt").exists()


def test_catastrophic_regex_hits_the_search_deadline(workspace: Path) -> None:
    (workspace / "evil.py").write_text("a" * 40 + "b\n", encoding="utf-8")
    reg = ToolRegistry(workspace, search_timeout_s=2.0)
    started = time.monotonic()
    obs = reg.execute(ToolCall("search", {"pattern": "^(a+)+$"}))
    assert time.monotonic() - started < 15
    assert not obs.ok and not obs.rejected
    assert "timed out" in error_text(obs)


def test_search_still_finds_matches(workspace: Path) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("search", {"pattern": "value"}))
    assert obs.ok and "app.py:1:VALUE = 1" in obs.output_excerpt


def test_a_regex_that_overflows_the_compiler_is_a_failure_not_a_crash(workspace: Path) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("search", {"pattern": "a{4294967295}"}))
    assert not obs.ok and not obs.rejected
    assert "invalid regex" in error_text(obs)


def test_an_unexpected_tool_exception_becomes_an_observation(workspace: Path) -> None:
    from repo_engineer.tools import Permission, ToolSpec

    def explode() -> str:
        raise KeyError("boom")

    reg = ToolRegistry(workspace)
    reg.specs["explode"] = ToolSpec(
        name="explode",
        description="always fails",
        permission=Permission.READ_ONLY,
        required_args={},
        run=explode,
    )
    obs = reg.execute(ToolCall("explode", {}))
    assert not obs.ok and not obs.rejected
    assert "KeyError" in error_text(obs)


def test_nested_double_star_globs_stay_fast(workspace: Path) -> None:
    deep = workspace.joinpath(*(["a"] * 40))
    deep.mkdir(parents=True)
    (deep / "x.txt").write_text("needle\n", encoding="utf-8")
    reg = ToolRegistry(workspace)
    started = time.monotonic()
    obs = reg.execute(ToolCall("search", {"pattern": "needle", "glob": "**/a/" * 4 + "zzz"}))
    assert time.monotonic() - started < 5
    assert obs.ok and obs.output_excerpt == "(no matches)"
    found = reg.execute(ToolCall("search", {"pattern": "needle", "glob": "**/a/**/x.txt"}))
    assert "a/a/" in found.output_excerpt and found.output_excerpt.endswith("x.txt:1:needle")


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("**/*.py", ["app.py", "tests/test_app.py"]),
        ("*.py", ["app.py"]),
        ("tests/*", ["tests/test_app.py"]),
        ("tests/**", ["tests/test_app.py"]),
        ("**", ["app.py", "tests/test_app.py"]),
        ("t?sts/test_[a-z]*.py", ["tests/test_app.py"]),
    ],
)
def test_glob_semantics(workspace: Path, glob: str, expected: list[str]) -> None:
    obs = ToolRegistry(workspace).execute(ToolCall("search", {"pattern": ".", "glob": glob}))
    found = sorted({line.split(":", 1)[0] for line in obs.output_excerpt.splitlines()})
    assert found == expected
