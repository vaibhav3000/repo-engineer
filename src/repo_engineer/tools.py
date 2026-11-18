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

import configparser
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .process import child_env, run_bounded
from .redaction import Redactor
from .state import Observation, ToolCall


class Permission(StrEnum):
    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"
    EXECUTION = "EXECUTION"


class Policy(StrEnum):
    AUTO = "AUTO"  # execute without confirmation
    ASK = "ASK"  # require ask_callback approval; deny when headless
    DENY = "DENY"  # never execute


# Longest text an observation carries. Command output keeps its tail, where
# pytest prints failures and the summary; other tools keep their head.
OUTPUT_LIMIT = 4000

# run_tests accepts exactly this prefix, then flags from the allowlist below and
# test paths inside the workspace. Anything else is rejected before a process
# starts. pytest also collects unittest-style tests, so no other runner is needed.
TEST_COMMAND_PREFIX = ("python", "-m", "pytest")
MAX_COMMAND_CHARS = 2000
ALLOWED_PYTEST_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "--quiet",
        "-v",
        "-vv",
        "--verbose",
        "-x",
        "--exitfirst",
        "-s",
        "-l",
        "--showlocals",
        "--no-header",
    }
)
ALLOWED_PYTEST_FLAG_FORMS = (
    re.compile(r"-r[fEsxXpPaAwN]+"),
    re.compile(r"--tb=(auto|long|short|line|native|no)"),
    re.compile(r"--maxfail=[0-9]{1,4}"),
    re.compile(r"--capture=(no|sys|fd|tee-sys)"),
)
PYTEST_FLAGS_WITH_VALUE = frozenset({"-k", "-m"})
SHELL_SYNTAX = frozenset(";|&<>`$\n\r")
ALLOWED_FLAGS_HELP = (
    "-q, -v, -x, -s, -l, -r<chars>, --tb=<style>, --maxfail=<n>, --capture=<method>, "
    "--no-header, -k <expr>, -m <expr>"
)

# pytest config files the runtime will honour, in pytest's own search order,
# with the text that marks a file as carrying pytest settings.
PYTEST_CONFIG_FILES = (
    ("pytest.ini", ""),
    (".pytest.ini", ""),
    ("pyproject.toml", "[tool.pytest"),
    ("tox.ini", "[pytest]"),
    ("setup.cfg", "[tool:pytest]"),
)


class JailViolation(PermissionError):
    """A call the jail or the run_tests command check refused; reported as rejected."""


@dataclass(frozen=True)
class CommandResult:
    """What an EXECUTION tool returns: the output tail and the real exit status."""

    output: str
    exit_code: int


# Applied to every permission class a caller's policy does not mention, so a
# partial policy such as {EXECUTION: DENY} restricts one class and keeps the
# others usable.
DEFAULT_POLICY: dict[Permission, Policy] = {
    Permission.READ_ONLY: Policy.AUTO,
    Permission.MUTATING: Policy.AUTO,
    Permission.EXECUTION: Policy.AUTO,
}


@dataclass
class ToolSpec:
    name: str
    description: str
    permission: Permission
    required_args: dict[str, type]
    run: Callable[..., Any]
    optional_args: dict[str, type] = field(default_factory=dict)

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
        exec_timeout_s: float = 120.0,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy: dict[Permission, Policy] = {**DEFAULT_POLICY, **(policy or {})}
        self.ask_callback = ask_callback
        self.exec_timeout_s = exec_timeout_s
        self.redact = Redactor(self.workspace)
        self.specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    # ------------------------------------------------------------- jail

    def jailed_path(self, raw: str) -> Path:
        """Resolve raw inside the workspace; raise PermissionError on escape."""
        if not isinstance(raw, str) or not raw:
            raise PermissionError("path must be a non-empty string")
        # A Windows-absolute path (drive letter) cannot be resolved faithfully
        # on a POSIX host, where it would silently degrade to a workspace-
        # relative name. Reject it rather than guess platform semantics.
        if PureWindowsPath(raw).is_absolute() and os.name != "nt":
            raise PermissionError(f"path escape rejected: {raw!r} is a Windows absolute path")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.workspace / candidate).resolve()
        if resolved != self.workspace and self.workspace not in resolved.parents:
            raise PermissionError(f"path escape rejected: {raw!r} resolves outside the workspace")
        return resolved

    def jailed_glob(self, pattern: str) -> None:
        """Reject glob patterns that could scope the search outside the
        workspace: absolute paths, drive letters, .. components, or Windows
        backslash separators. Checked under both path dialects so a pattern
        cannot slip through on the 'other' platform's semantics."""
        if not isinstance(pattern, str) or not pattern:
            raise PermissionError("glob must be a non-empty string")
        win = PureWindowsPath(pattern)
        posix = PurePosixPath(pattern)
        if win.is_absolute() or posix.is_absolute() or ".." in win.parts or ".." in posix.parts:
            raise PermissionError(f"path escape rejected: glob {pattern!r} leaves the workspace")

    # ------------------------------------------------------------- registry

    def _register_defaults(self) -> None:
        self._register(
            ToolSpec(
                name="list_tree",
                description="List repository files up to a bounded depth.",
                permission=Permission.READ_ONLY,
                required_args={},
                optional_args={"max_depth": int},
                run=self._list_tree,
            )
        )
        self._register(
            ToolSpec(
                name="search",
                description="Regex search over text files; returns file:line:text matches.",
                permission=Permission.READ_ONLY,
                required_args={"pattern": str},
                optional_args={"glob": str},
                run=self._search,
            )
        )
        self._register(
            ToolSpec(
                name="read_file",
                description="Read a text file, optionally a 1-based inclusive line range.",
                permission=Permission.READ_ONLY,
                required_args={"path": str},
                optional_args={"start_line": int, "end_line": int},
                run=self._read_file,
            )
        )
        self._register(
            ToolSpec(
                name="apply_edit",
                description="Replace exactly one occurrence of old_text with new_text.",
                permission=Permission.MUTATING,
                required_args={"path": str, "old_text": str, "new_text": str},
                run=self._apply_edit,
            )
        )
        self._register(
            ToolSpec(
                name="write_file",
                description="Create a NEW text file (refuses to overwrite existing files).",
                permission=Permission.MUTATING,
                required_args={"path": str, "content": str},
                run=self._write_file,
            )
        )
        self._register(
            ToolSpec(
                name="git_diff",
                description="Unified diff of workspace changes (empty when git is absent).",
                permission=Permission.READ_ONLY,
                required_args={},
                run=self._git_diff,
            )
        )
        self._register(
            ToolSpec(
                name="run_tests",
                description="Run a whitelisted test command inside the workspace.",
                permission=Permission.EXECUTION,
                required_args={"cmd": str},
                run=self._run_tests,
            )
        )

    def _register(self, spec: ToolSpec) -> None:
        self.specs[spec.name] = spec

    # ------------------------------------------------------------- execution

    def execute(self, call: ToolCall) -> Observation:
        """Validate and execute one tool call; rejection is an Observation, not an exception."""
        import time as _time

        started = _time.perf_counter()
        spec = self.specs.get(call.tool)
        if spec is None:
            return Observation(
                tool=call.tool, ok=False, rejected=True, error=f"unknown tool '{call.tool}'"
            )
        schema_error = spec.validate(call.args)
        if schema_error:
            return Observation(tool=call.tool, ok=False, rejected=True, error=schema_error)

        policy = self.policy.get(spec.permission, Policy.DENY)
        if policy == Policy.ASK:
            denial = self._ask_approval(spec, call.args)
            if denial is not None:
                return Observation(tool=call.tool, ok=False, rejected=True, error=denial)
        elif policy != Policy.AUTO:
            # DENY, and any value that is not a recognised policy: fail closed.
            return Observation(
                tool=call.tool,
                ok=False,
                rejected=True,
                error=f"policy denies {spec.permission.value} tools",
            )

        try:
            output = spec.run(**call.args)
            if isinstance(output, CommandResult):
                prefix = f"exit={output.exit_code}\n"
                return Observation(
                    tool=call.tool,
                    ok=True,
                    output_excerpt=prefix
                    + _tail(self.redact(output.output), OUTPUT_LIMIT - len(prefix)),
                    exit_code=output.exit_code,
                    latency_ms=(_time.perf_counter() - started) * 1000,
                )
            return Observation(
                tool=call.tool,
                ok=True,
                output_excerpt=self.redact(str(output))[:OUTPUT_LIMIT],
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except PermissionError as exc:
            return Observation(
                tool=call.tool,
                ok=False,
                rejected=True,
                error=self.redact(str(exc)),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except FileNotFoundError as exc:
            return Observation(
                tool=call.tool,
                ok=False,
                error=self.redact(str(exc)),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except subprocess.TimeoutExpired as exc:
            # Execution-class failure with a real duration: an Observation,
            # never a crash of the runtime loop.
            partial = exc.output if isinstance(exc.output, str) else ""
            return Observation(
                tool=call.tool,
                ok=False,
                output_excerpt=_tail(self.redact(partial), OUTPUT_LIMIT),
                error=f"command timed out after {exc.timeout:.0f}s; the process tree was killed",
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except OSError as exc:
            # Remaining filesystem failures (IsADirectoryError,
            # NotADirectoryError, WinError 183, disk-full, ...). PermissionError
            # and FileNotFoundError are caught above.
            return Observation(
                tool=call.tool,
                ok=False,
                error=self.redact(str(exc)),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except (ValueError, RuntimeError) as exc:
            return Observation(
                tool=call.tool,
                ok=False,
                error=self.redact(str(exc)),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )

    def _ask_approval(self, spec: ToolSpec, args: dict[str, Any]) -> str | None:
        """Ask the approver; return None when approved, else the denial reason.

        Only an explicit True approves. A missing approver (headless run), any
        other return value, or an approver that raises is a denial.
        """
        if self.ask_callback is None:
            return "policy ASK: no approval granted (headless denies)"
        try:
            approved = self.ask_callback(spec, dict(args))
        except Exception as exc:
            return f"policy ASK: approver raised {type(exc).__name__}, so no approval was granted"
        if approved is not True:
            return "policy ASK: no approval granted"
        return None

    # ------------------------------------------------------------- tools

    def _list_tree(self, max_depth: int = 3) -> str:
        max_depth = min(max_depth, 6)
        lines: list[str] = []

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
        self.jailed_glob(glob)
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
            try:
                rel = path.relative_to(self.workspace)
            except ValueError as exc:
                # Belt and braces: jailed_glob should make this unreachable.
                raise PermissionError(
                    f"path escape rejected: {str(path)!r} leaves the workspace"
                ) from exc
            for line_no, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{rel}:{line_no}:{line.strip()[:200]}")
                    if len(matches) >= 100:
                        return "\n".join(matches) + "\n(truncated at 100 matches)"
        return "\n".join(matches) or "(no matches)"

    def _read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        resolved = self.jailed_path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        lines = resolved.read_text(encoding="utf-8").splitlines()
        start = max((start_line or 1) - 1, 0)
        end = min(end_line or len(lines), len(lines))
        numbered = [f"{i + 1:4d}: {lines[i]}" for i in range(start, end)]
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
            raise ValueError(f"write_file refuses to overwrite existing {path}; use apply_edit")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"created {path} ({len(content)} chars)"

    def _git_diff(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "diff", "--no-color"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.stdout or "(no changes)"
        except (OSError, subprocess.TimeoutExpired):
            return "(git unavailable)"

    def _run_tests(self, cmd: str) -> CommandResult:
        args = self.parse_test_command(cmd)
        result = run_bounded(
            self.pytest_argv(args),
            cwd=self.workspace,
            env=child_env(),
            timeout_s=self.exec_timeout_s,
        )
        if result.timed_out or result.exit_code is None:
            raise subprocess.TimeoutExpired(cmd, self.exec_timeout_s, output=result.output)
        return CommandResult(
            output=_note_omitted(result.output, result.omitted_bytes),
            exit_code=result.exit_code,
        )

    # ------------------------------------------------------------- run_tests command

    def parse_test_command(self, cmd: str) -> list[str]:
        """Check a planner command and return the pytest arguments it may pass.

        The command must be `python -m pytest`, followed only by allowlisted
        flags and test paths (optionally with ::node ids) that exist inside
        the workspace. Raises JailViolation before any process starts.
        """
        if len(cmd) > MAX_COMMAND_CHARS:
            raise JailViolation(f"command is longer than {MAX_COMMAND_CHARS} characters")
        try:
            tokens = _split_command(cmd)
        except ValueError as exc:
            raise JailViolation(f"command could not be parsed: {exc}") from exc
        if tuple(tokens[:3]) != TEST_COMMAND_PREFIX:
            raise JailViolation(
                "command not in allowlist: only 'python -m pytest [flags] [test paths]' "
                f"may run, got {cmd!r}"
            )
        rest = tokens[3:]
        for token in rest:
            if any(char in SHELL_SYNTAX for char in token):
                raise JailViolation(
                    f"command not in allowlist: shell syntax in {token!r} is not supported "
                    "(commands run without a shell)"
                )
        args: list[str] = []
        i = 0
        while i < len(rest):
            token = rest[i]
            if token.startswith("@"):
                raise JailViolation(f"argument files such as {token!r} are not allowed")
            if token in PYTEST_FLAGS_WITH_VALUE:
                value = rest[i + 1] if i + 1 < len(rest) else ""
                if not value or value.startswith(("-", "@")):
                    raise JailViolation(f"pytest flag {token} needs a value")
                args += [token, value]
                i += 2
                continue
            if token.startswith("-"):
                if token in ALLOWED_PYTEST_FLAGS or any(
                    form.fullmatch(token) for form in ALLOWED_PYTEST_FLAG_FORMS
                ):
                    args.append(token)
                    i += 1
                    continue
                raise JailViolation(
                    f"pytest flag {token!r} is not allowed; allowed flags: {ALLOWED_FLAGS_HELP}"
                )
            path_part, separator, node = token.partition("::")
            target = self.jailed_path(path_part)
            if not target.exists():
                raise JailViolation(f"test path {path_part!r} does not exist in the workspace")
            args.append(self._display(target) + separator + node)
            i += 1
        return args

    def pytest_argv(self, args: list[str], extra: tuple[str, ...] = ()) -> list[str]:
        """Full argv for a pytest run in the workspace.

        The runtime's own interpreter runs pytest with -P, so the workspace is
        not on sys.path when pytest is imported and a planted pytest.py cannot
        shadow it. The cache plugin is disabled, rootdir and confcutdir are
        pinned to the workspace so no config or conftest.py above it is read,
        and the workspace root goes on sys.path through pytest's pythonpath
        setting so task code stays importable.
        """
        config = self._workspace_pytest_config()
        import_roots = [self.workspace]
        if config is not None:
            import_roots += self._configured_pythonpath(config)
        roots = " ".join(shlex.quote(root.as_posix()) for root in import_roots)
        return [
            sys.executable,
            "-P",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-c",
            str(config) if config is not None else os.devnull,
            f"--rootdir={self.workspace}",
            f"--confcutdir={self.workspace}",
            "-o",
            f"pythonpath={roots}",
            *extra,
            *args,
        ]

    def _workspace_pytest_config(self) -> Path | None:
        """The workspace's own pytest config file, if its root has one."""
        for name, marker in PYTEST_CONFIG_FILES:
            candidate = self.workspace / name
            if not candidate.is_file() or candidate.is_symlink():
                continue
            if not marker:
                return candidate
            try:
                if marker in candidate.read_text(encoding="utf-8"):
                    return candidate
            except (OSError, UnicodeDecodeError):
                continue
        return None

    def _configured_pythonpath(self, config: Path) -> list[Path]:
        """Entries of the config's own pythonpath setting that stay inside the workspace."""
        try:
            text = config.read_text(encoding="utf-8")
            if config.suffix == ".toml":
                pytest_table = tomllib.loads(text).get("tool", {}).get("pytest", {})
                value = pytest_table.get("ini_options", pytest_table).get("pythonpath", [])
            else:
                parser = configparser.ConfigParser(interpolation=None)
                parser.read_string(text)
                section = "tool:pytest" if config.name == "setup.cfg" else "pytest"
                value = parser.get(section, "pythonpath", fallback="")
        except (OSError, UnicodeDecodeError, ValueError, configparser.Error, AttributeError):
            return []
        entries = shlex.split(value) if isinstance(value, str) else [str(v) for v in value]
        roots: list[Path] = []
        for entry in entries:
            try:
                roots.append(self.jailed_path(entry))
            except PermissionError:
                continue  # a configured path outside the workspace is ignored
        return roots

    def _display(self, path: Path) -> str:
        """Workspace-relative POSIX form of a jailed path ('.' for the root)."""
        rel = path.relative_to(self.workspace).as_posix()
        return rel if rel else "."


def _split_command(cmd: str) -> list[str]:
    """Split like a POSIX shell, but keep backslashes literal (Windows paths)."""
    lexer = shlex.shlex(cmd, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[len(text) - limit :]


def _note_omitted(output: str, omitted_bytes: int) -> str:
    if omitted_bytes <= 0:
        return output
    return f"[{omitted_bytes} bytes of earlier output omitted]\n{output}"
