"""Benchmark harness: task loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_engineer.benchmark import harness


def test_load_tasks_finds_the_twelve_tasks() -> None:
    assert [t["task_id"] for t in harness.load_tasks()] == [
        "task_01_fix_off_by_one",
        "task_02_failing_test",
        "task_03_add_missing_test",
        "task_04_constrained_refactor",
        "task_05_cross_file_units",
        "task_06_rename_across_package",
        "task_07_add_test_duration",
        "task_08_extract_helper",
        "task_09_signature_change",
        "task_10_mutable_default",
        "task_11_add_test_semver",
        "task_12_import_after_move",
    ]


def test_every_task_has_a_recipe_and_protects_something() -> None:
    from repo_engineer.planner import RECIPES

    for task in harness.load_tasks():
        assert task["task_id"] in RECIPES, task["task_id"]
        assert task["protected_paths"], task["task_id"]


def test_load_tasks_refuses_to_report_an_empty_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "task_without_metadata").mkdir()
    monkeypatch.setattr(harness, "TASK_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="no benchmark tasks"):
        harness.load_tasks()


def test_results_and_traces_are_byte_stable_and_machine_independent(tmp_path: Path) -> None:
    outputs = []
    for run in ("first run", "second run"):
        out = tmp_path / run
        harness.run_benchmark(
            out / "runs",
            results_path=out / "results" / "benchmark_results.json",
            traces_dir=out / "results" / "traces",
        )
        files = sorted((out / "results").rglob("*.json"))
        outputs.append({f.relative_to(out).as_posix(): f.read_bytes() for f in files})
    assert outputs[0] == outputs[1]
    assert len(outputs[0]) == 1 + len(harness.load_tasks())  # results file plus one trace per task
    for name, data in outputs[0].items():
        text = data.decode("utf-8")
        assert str(tmp_path) not in text and "first run" not in text, name
        assert "wall_time_s" not in text and "started_at" not in text, name


def test_pytest_output_normalisation() -> None:
    raw = (
        "tests\\test_invoice.py:6: AssertionError\n"
        "1 failed in 0.05s\n"
        "=========== 3 passed, 1 warning in 12.34s ===========\n"
        "assert 'a in 1.5s' == 'b'\n"
    )
    assert harness.normalise_pytest_output(raw) == (
        "tests/test_invoice.py:6: AssertionError\n"
        "1 failed in <duration>\n"
        "=== 3 passed, 1 warning in <duration> ===\n"
        "assert 'a in 1.5s' == 'b'\n"
    )


def test_separator_lines_are_width_independent() -> None:
    windows = "=" * 34 + " FAILURES " + "=" * 35 + "\n" + "_" * 23 + " test_x " + "_" * 24 + "\n"
    linux = "=" * 35 + " FAILURES " + "=" * 35 + "\n" + "_" * 24 + " test_x " + "_" * 24 + "\n"
    normalised = harness.normalise_pytest_output(windows)
    assert normalised == harness.normalise_pytest_output(linux)
    assert normalised == "=== FAILURES ===\n___ test_x ___\n"
    assert harness.normalise_pytest_output("=" * 79 + "\n") == "===\n"


def test_main_exits_nonzero_when_a_task_fails(tmp_path: Path) -> None:
    from repo_engineer.planner import RecordedPlanner

    code = harness.main(tmp_path / "results", planner=RecordedPlanner([None]), max_replans=0)
    assert code == 1
    assert (tmp_path / "results" / "benchmark_results.json").exists()
