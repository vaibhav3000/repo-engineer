"""In-memory snapshot of a workspace, for diffs and for restoring files.

The registry snapshots the workspace when it is created, before the agent
acts. git_diff compares the current tree with that snapshot in-process, so it
needs no git binary, reads no repository configuration and cannot see outside
the workspace. The runtime also uses the snapshot to put protected files back
before verification.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Callable
from pathlib import Path

# Directories that are never snapshotted, diffed or restored.
SKIPPED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 64_000_000


def read_tree(root: Path) -> dict[str, bytes]:
    """Map POSIX relative path -> bytes for the regular files under root.

    Links and junctions are neither followed nor recorded, files larger than
    MAX_FILE_BYTES are left out, and reading stops at MAX_TOTAL_BYTES.
    """
    files: dict[str, bytes] = {}
    total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name.lower() not in SKIPPED_DIRS and _is_direct(Path(directory, name))
        )
        for name in sorted(filenames):
            path = Path(directory, name)
            if not _is_direct(path) or not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                return files
            files[path.relative_to(root).as_posix()] = data
    return files


def _links(root: Path) -> list[str]:
    """POSIX relative paths of links and junctions under root (not followed)."""
    found: list[str] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            path = Path(directory, name)
            if not _is_direct(path):
                found.append(path.relative_to(root).as_posix())
            elif name.lower() not in SKIPPED_DIRS:
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(directory, name)
            if not _is_direct(path):
                found.append(path.relative_to(root).as_posix())
    return found


def _is_direct(path: Path) -> bool:
    return os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))


class WorkspaceSnapshot:
    """The workspace as it was when the snapshot was taken."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = read_tree(root)
        self.links = set(_links(root))

    def remove_new_links(self) -> list[str]:
        """Remove links and junctions that did not exist when the snapshot was taken.

        Only the link itself is removed, never what it points to.
        """
        removed: list[str] = []
        for rel in sorted(set(_links(self.root)) - self.links):
            link = self.root / rel
            try:
                link.unlink()
            except (IsADirectoryError, PermissionError):
                os.rmdir(link)  # a Windows junction or directory symlink
            removed.append(rel)
        return removed

    def diff(self) -> str:
        """Unified diff from the snapshot to the current tree, sorted by path."""
        current = read_tree(self.root)
        chunks: list[str] = []
        for rel in sorted(set(self.files) | set(current)):
            before, after = self.files.get(rel), current.get(rel)
            if before == after:
                continue
            chunks.append(_file_diff(rel, before, after))
        return "\n".join(chunks) if chunks else "(no changes)"

    def restore(self, selected: Callable[[str], bool]) -> list[str]:
        """Return every selected path to its snapshot state.

        Selected files that changed are rewritten, selected files that were
        deleted are recreated, and selected files that did not exist when the
        snapshot was taken are removed. Returns the paths that were touched.
        """
        current = read_tree(self.root)
        touched: list[str] = []
        for rel in sorted(set(self.files) | set(current)):
            if not selected(rel) or self.files.get(rel) == current.get(rel):
                continue
            target = self.root / rel
            original = self.files.get(rel)
            if original is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(original)
            touched.append(rel)
        return touched


def _file_diff(rel: str, before: bytes | None, after: bytes | None) -> str:
    old_name = f"a/{rel}" if before is not None else "/dev/null"
    new_name = f"b/{rel}" if after is not None else "/dev/null"
    try:
        old_lines = _lines(before)
        new_lines = _lines(after)
    except UnicodeDecodeError:
        return f"Binary files {old_name} and {new_name} differ"
    diff = difflib.unified_diff(old_lines, new_lines, old_name, new_name, lineterm="")
    return "\n".join(diff)


def _lines(data: bytes | None) -> list[str]:
    if data is None:
        return []
    return data.decode("utf-8").splitlines()
