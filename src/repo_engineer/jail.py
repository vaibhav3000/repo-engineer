"""Lexical rules of the workspace jail.

A path the planner supplies is checked as text before anything touches the
filesystem: resolving a UNC path such as \\\\host\\share already makes Windows
contact that host. The same rules apply on every platform, so the jail behaves
(and its recorded evidence reads) the same on Windows and POSIX.

The registry then joins the surviving components onto the workspace root and
refuses any target whose real path differs from that join, which rules out
symbolic links, junctions and other aliases.
"""

from __future__ import annotations

import re


class JailViolation(PermissionError):
    """A call the jail or the run_tests command check refused; reported as rejected."""


MAX_PATH_CHARS = 1024
MAX_GLOB_CHARS = 256

_DRIVE = re.compile(r"^[A-Za-z]:")
# Device names Windows reserves in every directory, with or without an
# extension. They are refused on every platform so the jail stays portable.
_RESERVED_DEVICES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in "123456789¹²³"}
    | {f"LPT{n}" for n in "123456789¹²³"}
)
_GLOB_CHARS = frozenset("*?[]")


def path_components(raw: object, *, glob: bool = False) -> tuple[str, ...]:
    """Validate an agent-supplied relative path and return its components.

    Backslashes count as separators everywhere. '.' components are dropped and
    '..' is resolved lexically; climbing above the workspace root is an escape.
    The empty tuple names the workspace root itself.
    """
    kind = "glob" if glob else "path"
    if not isinstance(raw, str) or not raw:
        raise JailViolation(f"{kind} must be a non-empty string")
    limit = MAX_GLOB_CHARS if glob else MAX_PATH_CHARS
    if len(raw) > limit:
        raise JailViolation(f"{kind} is longer than {limit} characters")
    if any(ord(char) < 32 for char in raw):
        raise JailViolation(f"path rejected: {raw!r} contains control characters")
    text = raw.replace("\\", "/")
    if text.startswith("/"):
        raise JailViolation(
            f"path escape rejected: {raw!r} is absolute (rooted, UNC or device path)"
        )
    if _DRIVE.match(text):
        raise JailViolation(f"path escape rejected: {raw!r} names a drive")
    parts: list[str] = []
    for component in text.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            if not parts:
                raise JailViolation(f"path escape rejected: {raw!r} resolves outside the workspace")
            parts.pop()
            continue
        _check_component(raw, component, glob=glob)
        parts.append(component)
    return tuple(parts)


def _check_component(raw: str, component: str, *, glob: bool) -> None:
    if ":" in component:
        raise JailViolation(
            f"path rejected: {raw!r} contains ':' (drive or alternate data stream syntax)"
        )
    if component[-1] in ". " and not (glob and component[-1] in _GLOB_CHARS):
        raise JailViolation(
            f"path rejected: {raw!r} has a component ending in a dot or space "
            "(Windows would silently alias it)"
        )
    if component.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_DEVICES:
        raise JailViolation(f"path rejected: {raw!r} uses a reserved device name")
    if component.lower() == ".git":
        raise JailViolation(f"path rejected: {raw!r} is inside .git, which is off limits")
