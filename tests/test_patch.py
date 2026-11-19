"""Strict unified-diff parsing and the all-or-nothing apply_patch tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_engineer.patch import PatchError, apply_file_patch, parse_patch
from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry

ORIGINAL = "a\nb\nc\nd\ne\n"

EDIT = """--- a/m.py
+++ b/m.py
@@ -2,3 +2,3 @@
 b
-c
+C
 d
"""


def test_a_simple_hunk_applies_at_its_line() -> None:
    (fp,) = parse_patch(EDIT)
    assert (fp.old_path, fp.new_path) == ("m.py", "m.py")
    new, notes = apply_file_patch(ORIGINAL, fp)
    assert new == "a\nb\nC\nd\ne\n" and notes == []


def test_a_hunk_that_moved_applies_at_its_unique_new_place() -> None:
    (fp,) = parse_patch(EDIT)
    new, notes = apply_file_patch("x\ny\n" + ORIGINAL, fp)
    assert new == "x\ny\na\nb\nC\nd\ne\n" and notes == ["hunk 1 applied at line 4 (offset +2)"]


def test_a_hunk_that_matches_nowhere_is_refused() -> None:
    (fp,) = parse_patch(EDIT)
    with pytest.raises(PatchError, match="hunk 1 does not match"):
        apply_file_patch("a\nb\nX\nd\n", fp)


def test_a_hunk_that_matches_in_two_other_places_is_refused() -> None:
    (fp,) = parse_patch(EDIT)
    with pytest.raises(PatchError, match="matches 2 places"):
        apply_file_patch("q\nq\nX\nb\nc\nd\nb\nc\nd\n", fp)


def test_counts_must_agree_with_the_header() -> None:
    bad = "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n a\n-b\n+B\n"
    with pytest.raises(PatchError, match="ends before"):
        parse_patch(bad)


def test_garbage_between_files_is_refused() -> None:
    with pytest.raises(PatchError, match="line 8"):
        parse_patch(EDIT + "Some prose from the model\n")


def test_renames_and_empty_patches_are_refused() -> None:
    with pytest.raises(PatchError, match="rename"):
        parse_patch("--- a/x.py\n+++ b/y.py\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError, match="no file"):
        parse_patch("")


def test_new_and_deleted_files() -> None:
    patch = (
        "diff --git a/new.py b/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+one\n+two\n"
        "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
    )
    created, deleted = parse_patch(patch)
    assert apply_file_patch(None, created) == ("one\ntwo\n", [])
    assert apply_file_patch("x\ny\n", deleted) == (None, [])
    with pytest.raises(PatchError, match="does not match the whole file"):
        apply_file_patch("x\ny\nz\n", deleted)


def test_no_newline_at_end_of_file_markers() -> None:
    patch = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b\n"
    (fp,) = parse_patch(patch)
    assert apply_file_patch("a", fp) == ("b\n", [])
    keep = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file\n"
    assert apply_file_patch("a\n", parse_patch(keep)[0]) == ("b", [])


def test_blank_context_lines_without_their_space_are_accepted() -> None:
    patch = "--- a/m.py\n+++ b/m.py\n@@ -1,3 +1,3 @@\n a\n\n-c\n+C\n"
    assert apply_file_patch("a\n\nc\n", parse_patch(patch)[0]) == ("a\n\nC\n", [])


# ------------------------------------------------------------- the tool


def two_file_patch(second_context: str = "VALUE = 1") -> str:
    return (
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
        f"--- a/lib.py\n+++ b/lib.py\n@@ -1 +1 @@\n-{second_context}\n+DONE = True\n"
    )


def lib(workspace: Path) -> None:
    (workspace / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_the_tool_applies_every_file_and_records_them(workspace: Path) -> None:
    lib(workspace)
    obs = ToolRegistry(workspace).execute(ToolCall("apply_patch", {"patch": two_file_patch()}))
    assert obs.ok, obs
    assert (workspace / "app.py").read_text() == "VALUE = 2\n"
    assert (workspace / "lib.py").read_text() == "DONE = True\n"
    assert obs.files_written == ("app.py", "lib.py")


def test_one_failing_hunk_leaves_every_file_untouched(workspace: Path) -> None:
    lib(workspace)
    obs = ToolRegistry(workspace).execute(
        ToolCall("apply_patch", {"patch": two_file_patch("NOT THERE")})
    )
    assert not obs.ok and "lib.py" in (obs.error or "")
    assert (workspace / "app.py").read_text() == "VALUE = 1\n"


def test_a_dry_run_reports_without_writing(workspace: Path) -> None:
    lib(workspace)
    obs = ToolRegistry(workspace).execute(
        ToolCall("apply_patch", {"patch": two_file_patch(), "dry_run": True})
    )
    assert obs.ok and "would change app.py" in obs.output_excerpt
    assert (workspace / "app.py").read_text() == "VALUE = 1\n" and obs.files_written == ()


def test_patch_targets_are_jailed(workspace: Path) -> None:
    patch = "--- a/../evil.py\n+++ b/../evil.py\n@@ -0,0 +1 @@\n+x\n"
    obs = ToolRegistry(workspace).execute(ToolCall("apply_patch", {"patch": patch}))
    assert obs.rejected and "path escape rejected" in (obs.error or "")
    assert not (workspace.parent / "evil.py").exists()


def test_protected_and_control_files_cannot_be_patched(workspace: Path) -> None:
    registry = ToolRegistry(workspace)
    registry.protect(["tests/"])
    patch = (
        "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n"
        "@@ -5 +5 @@\n-    assert VALUE == 1\n+    assert True\n"
    )
    obs = registry.execute(ToolCall("apply_patch", {"patch": patch}))
    assert obs.rejected and "protected by the task" in (obs.error or "")
    conftest = "--- /dev/null\n+++ b/conftest.py\n@@ -0,0 +1 @@\n+x = 1\n"
    obs = registry.execute(ToolCall("apply_patch", {"patch": conftest}))
    assert obs.rejected and "controls how pytest runs" in (obs.error or "")


def test_crlf_files_keep_their_line_endings(workspace: Path) -> None:
    (workspace / "app.py").write_bytes(b"VALUE = 1\r\nOTHER = 3\r\n")
    patch = "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 2\n OTHER = 3\n"
    obs = ToolRegistry(workspace).execute(ToolCall("apply_patch", {"patch": patch}))
    assert obs.ok, obs
    assert (workspace / "app.py").read_bytes() == b"VALUE = 2\r\nOTHER = 3\r\n"


def test_a_patch_creating_an_existing_file_is_refused(workspace: Path) -> None:
    patch = "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1 @@\n+x\n"
    obs = ToolRegistry(workspace).execute(ToolCall("apply_patch", {"patch": patch}))
    assert not obs.ok and "already exists" in (obs.error or "")


def test_files_written_by_a_patch_reach_the_trace(workspace: Path) -> None:
    from repo_engineer.agent import AgentRuntime
    from repo_engineer.planner import RecordedPlanner

    lib(workspace)
    planner = RecordedPlanner([ToolCall("apply_patch", {"patch": two_file_patch()}), None])
    (workspace / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n", encoding="utf-8"
    )
    state = AgentRuntime(ToolRegistry(workspace), planner).run_task({"task_id": "t"})
    assert state.files_changed == ["app.py", "lib.py"] and state.verified


def test_one_file_named_twice_is_refused_after_jailing(workspace: Path) -> None:
    """Found by the Step 2 review: 'lib.py' and './lib.py' both applied."""
    lib(workspace)
    patch = (
        "--- a/lib.py\n+++ b/lib.py\n@@ -1 +1 @@\n-VALUE = 1\n+A = 2\n"
        "--- a/./lib.py\n+++ b/./lib.py\n@@ -1 +1 @@\n-VALUE = 1\n+A = 3\n"
    )
    obs = ToolRegistry(workspace).execute(ToolCall("apply_patch", {"patch": patch}))
    assert not obs.ok and "twice" in (obs.error or "")
    assert (workspace / "lib.py").read_text() == "VALUE = 1\n"
