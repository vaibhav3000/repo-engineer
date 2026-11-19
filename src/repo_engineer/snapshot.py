"""In-memory snapshot of a workspace, for diffs and for restoring files.

The registry snapshots the workspace when it is created, before the agent
acts. git_diff compares the current tree with that snapshot in-process, so it
needs no git binary, reads no repository configuration and cannot see outside
the workspace. The runtime also uses the snapshot to put protected files back
before verification, so the snapshot records every file that existed (of any
size) and keeps full copies of the files it may have to restore.
"""

from __future__ import annotations

import difflib
import os
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path

# Directories that are never snapshotted or diffed.
SKIPPED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
# Size limits for the copies used by git_diff. Files the runtime may have to
# restore are pinned in full regardless (up to MAX_PINNED_BYTES in total).
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 64_000_000
MAX_PINNED_BYTES = 256_000_000


class RestoreError(RuntimeError):
    """The workspace could not be returned to its snapshot state."""


def read_tree(root: Path) -> dict[str, bytes]:
    """Map POSIX relative path -> bytes for the regular files under root.

    Links and junctions are neither followed nor recorded, files larger than
    MAX_FILE_BYTES are left out, and reading stops at MAX_TOTAL_BYTES.
    """
    files: dict[str, bytes] = {}
    total = 0
    for rel in sorted(list_files(root, skip=SKIPPED_DIRS)):
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            break
        files[rel] = data
    return files


def list_files(root: Path, skip: frozenset[str] = frozenset({".git"})) -> set[str]:
    """POSIX relative paths of every regular file under root, of any size.

    Links and junctions are not followed or listed; directories named in skip
    (compared case-insensitively) are not entered.
    """
    found: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in skip and _is_direct(Path(directory, name))
        ]
        for name in filenames:
            path = Path(directory, name)
            if _is_direct(path) and path.is_file():
                found.add(path.relative_to(root).as_posix())
    return found


def _links(root: Path) -> set[str]:
    """POSIX relative paths of links and junctions under root (not followed)."""
    found: set[str] = set()
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            path = Path(directory, name)
            if not _is_direct(path):
                found.add(path.relative_to(root).as_posix())
            elif name.lower() != ".git":
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(directory, name)
            if not _is_direct(path):
                found.add(path.relative_to(root).as_posix())
    return found


def _cache_dirs(root: Path) -> set[str]:
    """POSIX relative paths of __pycache__ directories under root."""
    found: set[str] = set()
    for directory, dirnames, _ in os.walk(root, followlinks=False):
        for name in list(dirnames):
            path = Path(directory, name)
            if not _is_direct(path) or name.lower() == ".git":
                dirnames.remove(name)
            elif name == "__pycache__":
                found.add(path.relative_to(root).as_posix())
                dirnames.remove(name)
    return found


def _is_direct(path: Path) -> bool:
    return os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))


def _make_writable(path: Path) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _remove_file(path: Path) -> None:
    """Remove a file even when it was made read-only."""
    try:
        path.unlink()
    except PermissionError:
        _make_writable(path)
        path.unlink()


def _remove_link(path: Path) -> None:
    """Remove a link or junction itself, never what it points to."""
    try:
        path.unlink()
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)  # a Windows junction or directory symlink


def _remove_tree(path: Path) -> None:
    """Remove a directory tree, clearing read-only bits that block deletion."""

    def retry_writable(func: Callable[..., object], target: str, _exc: object) -> None:
        _make_writable(Path(target))
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)


class WorkspaceSnapshot:
    """The workspace as it was when the snapshot was taken."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files = read_tree(root)
        self.paths = list_files(root)
        self.links = _links(root)
        self.caches = _cache_dirs(root)
        self.pinned: dict[str, bytes] = {}

    def pin(self, selected: Callable[[str], bool]) -> None:
        """Keep full copies of the selected starting files, whatever their size."""
        total = sum(len(data) for data in self.pinned.values())
        for rel in sorted(self.paths):
            if rel in self.pinned or not selected(rel):
                continue
            data = self.files.get(rel)
            if data is None:
                data = (self.root / rel).read_bytes()
            total += len(data)
            if total > MAX_PINNED_BYTES:
                raise RestoreError("files to protect exceed the snapshot size limit")
            self.pinned[rel] = data

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

    def remove_new_links(self) -> list[str]:
        """Remove links and junctions that did not exist when the snapshot was taken."""
        removed: list[str] = []
        for rel in sorted(_links(self.root) - self.links):
            _remove_link(self.root / rel)
            removed.append(rel)
        return removed

    def remove_new_caches(self) -> list[str]:
        """Remove __pycache__ directories created during the run.

        Test runs do not write bytecode, so a new cache directory was planted,
        and a planted pytest cache entry could replace a test's code.
        """
        removed: list[str] = []
        for rel in sorted(_cache_dirs(self.root) - self.caches):
            _remove_tree(self.root / rel)
            removed.append(rel)
        return removed

    def restore(self, selected: Callable[[str], bool]) -> list[str]:
        """Return every selected path to its snapshot state.

        Selected files that changed are rewritten, selected files that were
        deleted are recreated, and selected files that did not exist when the
        snapshot was taken are removed, whatever their size. Returns the paths
        that were touched.
        """
        touched: list[str] = []
        for rel in sorted(self.paths | list_files(self.root)):
            if not selected(rel):
                continue
            target = self.root / rel
            if rel not in self.paths:
                _remove_file(target)
                touched.append(rel)
                continue
            original = self.pinned.get(rel, self.files.get(rel))
            if original is None:
                raise RestoreError(f"no copy of {rel} was kept, so it cannot be restored")
            try:
                current: bytes | None = target.read_bytes()
            except FileNotFoundError:
                current = None
            if current == original:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if current is not None and not os.access(target, os.W_OK):
                _make_writable(target)
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
