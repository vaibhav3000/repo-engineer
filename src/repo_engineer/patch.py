"""Strict unified diffs: parse them, and apply one file's hunks in memory.

Strict means (docs/adr/0005):
- every file section is a `--- ` line followed by a `+++ ` line; `/dev/null`
  marks a created or deleted file, and renames are refused;
- hunk headers must agree with the lines that follow them;
- outside hunks only git's own header lines (diff, index, mode lines) and
  blank lines are allowed, so prose mixed into a patch is an error;
- a hunk applies where its header says, or at exactly one other place
  (reported as an offset), and nowhere else: there is no fuzz.

Applying to the workspace (jailing every path, refusing protected files,
writing all files or none) is the registry's job; this module only works on
text, so it can be tested on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*")
NO_NEWLINE = "\\ No newline at end of file"
GIT_HEADER_PREFIXES = (
    "diff ",
    "index ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
)
MAX_FILES = 50


class PatchError(ValueError):
    """The patch is malformed or does not apply."""


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]  # each starts with " ", "-", "+" or "\\"


@dataclass(frozen=True)
class FilePatch:
    old_path: str | None  # None for a created file
    new_path: str | None  # None for a deleted file
    hunks: tuple[Hunk, ...]

    @property
    def path(self) -> str:
        return self.new_path if self.new_path is not None else self.old_path or ""


def parse_patch(text: str) -> list[FilePatch]:
    """Parse a unified diff into file patches; raise PatchError on anything unexpected."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    patches: list[FilePatch] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == "" or line.startswith(GIT_HEADER_PREFIXES):
            i += 1
            continue
        if not line.startswith("--- "):
            raise PatchError(f"line {i + 1}: expected a '--- ' file header, got {line[:60]!r}")
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise PatchError(f"line {i + 2}: a '--- ' header must be followed by '+++ '")
        old_path = _header_path(line[4:], "a/")
        new_path = _header_path(lines[i + 1][4:], "b/")
        if old_path is None and new_path is None:
            raise PatchError(f"line {i + 1}: both sides are /dev/null")
        if old_path is not None and new_path is not None and old_path != new_path:
            raise PatchError(f"line {i + 1}: renames are not supported ({old_path} -> {new_path})")
        i += 2
        hunks: list[Hunk] = []
        while i < len(lines) and lines[i].startswith("@@"):
            hunk, i = _parse_hunk(lines, i)
            hunks.append(hunk)
        if not hunks:
            raise PatchError(f"line {i + 1}: file {new_path or old_path} has no hunks")
        patches.append(FilePatch(old_path, new_path, tuple(hunks)))
    if not patches:
        raise PatchError("the patch contains no file changes")
    paths = [p.path for p in patches]
    if len(set(paths)) != len(paths):
        raise PatchError("the patch changes the same file in two sections")
    if len(patches) > MAX_FILES:
        raise PatchError(f"the patch changes more than {MAX_FILES} files")
    return patches


def _header_path(raw: str, prefix: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(prefix):
        path = path[len(prefix) :]
    if not path:
        raise PatchError("a file header names no path")
    return path


def _parse_hunk(lines: list[str], i: int) -> tuple[Hunk, int]:
    match = HUNK_HEADER.fullmatch(lines[i])
    if match is None:
        raise PatchError(f"line {i + 1}: malformed hunk header {lines[i][:60]!r}")
    old_start, new_start = int(match.group(1)), int(match.group(3))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    body: list[str] = []
    old_seen = new_seen = 0
    j = i + 1
    while old_seen < old_count or new_seen < new_count:
        if j >= len(lines):
            raise PatchError(f"line {i + 1}: hunk ends before its {old_count},{new_count} lines")
        line = lines[j]
        if line == "":
            line = " "  # a blank context line whose leading space was stripped
        tag = line[0]
        if tag == " ":
            old_seen += 1
            new_seen += 1
        elif tag == "-":
            old_seen += 1
        elif tag == "+":
            new_seen += 1
        elif line == NO_NEWLINE:
            pass
        else:
            raise PatchError(f"line {j + 1}: hunk ends before its {old_count},{new_count} lines")
        if old_seen > old_count or new_seen > new_count:
            raise PatchError(f"line {j + 1}: hunk has more lines than its header says")
        body.append(line)
        j += 1
    if j < len(lines) and lines[j] == NO_NEWLINE:
        body.append(NO_NEWLINE)
        j += 1
    return Hunk(old_start, old_count, new_start, new_count, tuple(body)), j


def apply_file_patch(original: str | None, patch: FilePatch) -> tuple[str | None, list[str]]:
    """Apply one file's hunks to its text ("\\n" line endings).

    original is None when the file does not exist. Returns the new text (None
    when the patch deletes the file) and notes about hunks applied at an
    offset. Raises PatchError when a hunk does not apply exactly.
    """
    if patch.old_path is None:
        if original is not None:
            raise PatchError(f"{patch.path} already exists; the patch would create it")
        original = ""
    elif original is None:
        raise PatchError(f"{patch.path} does not exist")
    source, ends_with_newline = _split(original)
    result: list[str] = []
    notes: list[str] = []
    cursor = 0
    for number, hunk in enumerate(patch.hunks, 1):
        old = [line[1:] for line in hunk.lines if line[:1] in (" ", "-")]
        new = [line[1:] for line in hunk.lines if line[:1] in (" ", "+")]
        at = _locate(source, old, hunk, cursor, number, patch.path)
        if at != _expected_index(hunk):
            notes.append(
                f"hunk {number} applied at line {at + 1} (offset {at - _expected_index(hunk):+d})"
            )
        result += source[cursor:at] + new
        cursor = at + len(old)
        ends_with_newline = _newline_after(hunk, cursor == len(source), ends_with_newline)
    result += source[cursor:]
    if patch.new_path is None:
        if result:
            raise PatchError(f"the deletion of {patch.path} does not match the whole file")
        return None, notes
    text = "\n".join(result)
    return (text + "\n" if result and ends_with_newline else text), notes


def _split(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], True
    ends = text.endswith("\n")
    lines = text.split("\n")
    if ends:
        lines.pop()
    return lines, ends


def _expected_index(hunk: Hunk) -> int:
    # A pure insertion (-N,0) goes after line N; anything else starts at line N.
    return hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1


def _locate(
    source: list[str], old: list[str], hunk: Hunk, cursor: int, number: int, path: str
) -> int:
    expected = _expected_index(hunk)
    fits = cursor <= expected and expected + len(old) <= len(source)
    if fits and source[expected : expected + len(old)] == old:
        return expected
    if not old:
        raise PatchError(f"{path}: hunk {number} inserts at line {hunk.old_start}, past the end")
    matches = [
        at for at in range(cursor, len(source) - len(old) + 1) if source[at : at + len(old)] == old
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PatchError(f"{path}: hunk {number} does not match the file")
    raise PatchError(
        f"{path}: hunk {number} does not match at line {hunk.old_start} and matches "
        f"{len(matches)} places elsewhere; add context to make it unique"
    )


def _newline_after(hunk: Hunk, at_end: bool, current: bool) -> bool:
    """Whether the file ends with a newline after this hunk, from its markers."""
    if not at_end:
        return current
    last_new = None
    for index, line in enumerate(hunk.lines):
        if line[:1] in (" ", "+"):
            last_new = index
    if last_new is None:
        return current
    following = hunk.lines[last_new + 1 : last_new + 2]
    return following != (NO_NEWLINE,)
