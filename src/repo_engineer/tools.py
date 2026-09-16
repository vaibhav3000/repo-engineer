"""Tool registry: schemas, validation, permissions and the workspace jail.

Design rules:
- Every tool declares its schema, permission class and output contract.
- Malformed calls are REJECTED with structured errors (never executed, never
  crash the runtime) and recorded in the trace.
- Every path argument is jailed: resolved against the workspace root, escapes
  rejected. The jail is the security boundary, not the LLM's good behavior.
- EXECUTION tools run a command allowlist with subprocess timeouts and no
  shell; anything else is denied.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .state import Observation, ToolCall


class Permission(str, Enum):
    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"
    EXECUTION = "EXECUTION"


class Policy(str, Enum):
    AUTO = "AUTO"    # execute without confirmation
    ASK = "ASK"      # require ask_callback approval; deny when headless
    DENY = "DENY"    # never execute


@dataclass
class ToolSpec:
    name: str
    description: str
    permission: Permission
    required_args: dict[str, type]
    optional_args: dict[str, type] = field(default_factory=dict)
    run: Callable[..., Any] = None  # type: ignore[assignment]

    def validate(self, args: dict[str, Any]) -> str | None:
        """Return an error string when args violate the schema; None when valid."""
        if not isinstance(args, dict):
            return f"{self.name}: args must be an object, got {type(args).__name__}"
        for key, expected in self.required_args.items():
            if key not in args:
                return f"{self.name}: missing required argument '{key}'"
            if not isinstance(args[key], expected):
                return (
                    f"{self.name}: argument '{key}' must be {expected.__name__}, "
                    f"got {type(args[key]).__name__}"
                )
        for key, expected in self.optional_args.items():
            if key in args and not isinstance(args[key], expected):
                return (
                    f"{self.name}: optional argument '{key}' must be {expected.__name__}, "
                    f"got {type(args[key]).__name__}"
                )
        unknown = set(args) - set(self.required_args) - set(self.optional_args)
        if unknown:
            return f"{self.name}: unknown arguments {sorted(unknown)}"
        return None


class ToolRegistry:
    """Validates, permission-checks and executes tool calls inside a jail."""

    def __init__(
        self,
        workspace: str | Path,
        policy: dict[Permission, Policy] | None = None,
        ask_callback: Callable[[ToolSpec, dict[str, Any]], bool] | None = None,
        test_command_allowlist: tuple[str, ...] = ("python -m pytest", "python -m unittest"),
        exec_timeout_s: float = 120.0,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy or {
            Permission.READ_ONLY: Policy.AUTO,
            Permission.MUTATING: Policy.AUTO,
            Permission.EXECUTION: Policy.AUTO,
        }
        self.ask_callback = ask_callback
        self.test_command_allowlist = test_command_allowlist
        self.exec_timeout_s = exec_timeout_s
        self.specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    # ------------------------------------------------------------- jail

    def jailed_path(self, raw: str) -> Path:
        """Resolve raw inside the workspace; raise PermissionError on escape."""
        if not isinstance(raw, str) or not raw:
            raise PermissionError("path must be a non-empty string")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.workspace / candidate).resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise PermissionError(
                f"path escape rejected: {raw!r} resolves outside the workspace"
            )
        return resolved

    # ------------------------------------------------------------- registry

    def _register_defaults(self) -> None:
        self._register(ToolSpec(
            name="list_tree",
            description="List repository files up to a bounded depth.",
            permission=Permission.READ_ONLY,
            required_args={},
            optional_args={"max_depth": int},
            run=self._list_tree,
        ))
        self._register(ToolSpec(
            name="search",
            description="Regex search over text files; returns file:line:text matches.",
            permission=Permission.READ_ONLY,
            required_args={"pattern": str},
            optional_args={"glob": str},
            run=self._search,
        ))
        self._register(ToolSpec(
            name="read_file",
            description="Read a text file, optionally a 1-based inclusive line range.",
            permission=Permission.READ_ONLY,
            required_args={"path": str},
            optional_args={"start_line": int, "end_line": int},
            run=self._read_file,
        ))
        self._register(ToolSpec(
            name="apply_edit",
            description="Replace exactly one occurrence of old_text with new_text.",
            permission=Permission.MUTATING,
            required_args={"path": str, "old_text": str, "new_text": str},
            run=self._apply_edit,
        ))
        self._register(ToolSpec(
            name="write_file",
            description="Create a NEW text file (refuses to overwrite existing files).",
            permission=Permission.MUTATING,
            required_args={"path": str, "content": str},
            run=self._write_file,
        ))
        self._register(ToolSpec(
            name="git_diff",
            description="Unified diff of workspace changes (empty when git is absent).",
            permission=Permission.READ_ONLY,
            required_args={},
            run=self._git_diff,
        ))
        self._register(ToolSpec(
            name="run_tests",
            description="Run a whitelisted test command inside the workspace.",
            permission=Permission.EXECUTION,
            required_args={"cmd": str},
            run=self._run_tests,
        ))

    def _register(self, spec: ToolSpec) -> None:
        self.specs[spec.name] = spec

    # ------------------------------------------------------------- execution

    def execute(self, call: ToolCall) -> Observation:
        """Validate and execute one tool call; rejection is an Observation, not an exception."""
        import time as _time

        started = _time.perf_counter()
        spec = self.specs.get(call.tool)
        if spec is None:
            return Observation(tool=call.tool, ok=False, rejected=True,
                               error=f"unknown tool '{call.tool}'")
        schema_error = spec.validate(call.args)
        if schema_error:
            return Observation(tool=call.tool, ok=False, rejected=True, error=schema_error)

        policy = self.policy[spec.permission]
        if policy is Policy.DENY:
            return Observation(tool=call.tool, ok=False, rejected=True,
                               error=f"policy denies {spec.permission.value} tools")
        if policy is Policy.ASK:
            allowed = bool(self.ask_callback and self.ask_callback(spec, call.args))
            if not allowed:
                return Observation(tool=call.tool, ok=False, rejected=True,
                                   error="policy ASK: no approval granted (headless denies)")

        try:
            output = spec.run(**call.args)
            return Observation(
                tool=call.tool, ok=True,
                output_excerpt=str(output)[:4000],
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except PermissionError as exc:
            return Observation(tool=call.tool, ok=False, rejected=True,
                               error=str(exc),
                               latency_ms=(_time.perf_counter() - started) * 1000)
        except FileNotFoundError as exc:
            return Observation(tool=call.tool, ok=False, error=str(exc),
                               latency_ms=(_time.perf_counter() - started) * 1000)
        except (ValueError, RuntimeError) as exc:
            return Observation(tool=call.tool, ok=False, error=str(exc),
                               latency_ms=(_time.perf_counter() - started) * 1000)

    # ------------------------------------------------------------- tools

    def _list_tree(self, max_depth: int = 3) -> str:
        max_depth = min(max_depth, 6)
        lines: list[str] = []
        base_depth = len(self.workspace.parts)

        def walk(directory: Path, depth: int) -> None:
            if depth > max_depth or len(lines) > 400:
                return
            try:
                entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name))
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                rel = entry.relative_to(self.workspace)
                lines.append(("[dir]  " if entry.is_dir() else "       ") + str(rel))
                if entry.is_dir():
                    walk(entry, depth + 1)

        walk(self.workspace, 1)
        return "\n".join(lines) or "(empty workspace)"

    def _search(self, pattern: str, glob: str = "**/*.py") -> str:
        import re

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
        matches: list[str] = []
        for path in sorted(self.workspace.glob(glob)):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = path.relative_to(self.workspace)
            for line_no, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{rel}:{line_no}:{line.strip()[:200]}")
                    if len(matches) >= 100:
                        return "\n".join(matches) + "\n(truncated at 100 matches)"
        return "\n".join(matches) or "(no matches)"

    def _read_file(self, path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        resolved = self.jailed_path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        lines = resolved.read_text(encoding="utf-8").splitlines()
        start = max((start_line or 1) - 1, 0)
        end = min(end_line or len(lines), len(lines))
        numbered = [f"{i+1:4d}: {lines[i]}" for i in range(start, end)]
        return "\n".join(numbered) or "(empty file)"

    def _apply_edit(self, path: str, old_text: str, new_text: str) -> str:
        resolved = self.jailed_path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        text = resolved.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise ValueError(f"apply_edit: old_text not found in {path}")
        if occurrences > 1:
            raise ValueError(
                f"apply_edit: old_text matches {occurrences} times in {path}; "
                "edits must be unambiguous"
            )
        resolved.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"edited {path}: 1 replacement"

    def _write_file(self, path: str, content: str) -> str:
        resolved = self.jailed_path(path)
        if resolved.exists():
            raise ValueError(
                f"write_file refuses to overwrite existing {path}; use apply_edit"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"created {path} ({len(content)} chars)"

    def _git_diff(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "diff", "--no-color"], cwd=self.workspace,
                capture_output=True, text=True, timeout=30,
            )
            return proc.stdout or "(no changes)"
        except (OSError, subprocess.TimeoutExpired):
            return "(git unavailable)"

    def _run_tests(self, cmd: str) -> str:
        allowed = any(cmd.startswith(prefix) for prefix in self.test_command_allowlist)
        if not allowed:
            raise PermissionError(
                f"command not in allowlist {list(self.test_command_allowlist)}: {cmd!r}"
            )
        proc = subprocess.run(
            cmd.split(), cwd=self.workspace, capture_output=True, text=True,
            timeout=self.exec_timeout_s, shell=False,
        )
        tail = (proc.stdout + proc.stderr)[-4000:]
        return f"exit={proc.returncode}\n{tail}"
