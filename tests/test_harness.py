"""Benchmark harness: task loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_engineer.benchmark import harness


def test_load_tasks_finds_the_four_tasks() -> None:
    assert [t["task_id"] for t in harness.load_tasks()] == [
        "task_01_fix_off_by_one",
        "task_02_failing_test",
        "task_03_add_missing_test",
        "task_04_constrained_refactor",
    ]


def test_load_tasks_refuses_to_report_an_empty_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task_without_metadata").mkdir()
    monkeypatch.setattr(harness, "TASK_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="no benchmark tasks"):
        harness.load_tasks()
