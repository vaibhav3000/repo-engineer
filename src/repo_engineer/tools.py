"""Tool registry: schemas, validation, permissions and the workspace jail.

Design rules:
- Every tool declares its schema, permission class and output contract.
- Malformed calls are REJECTED with structured errors (never executed, never
  crash the runtime) and recorded in the trace.
- Path and glob arguments are declared on the ToolSpec and jailed centrally in
  execute(): checked as text first, then required to be a direct path inside
  the workspace with no link on the way. Writes to .git, to pytest control
  files and to paths the task protects are refused.
- run_tests is the only EXECUTION tool. It accepts `python -m pytest` with an
  allowlist of flags and test paths inside the workspace, and runs the
  runtime's own interpreter without a shell, with a scrubbed environment, a
  deadline, a bounded output buffer and a process-tree kill.

Where pytest runs is the sandbox's business (sandbox/): the registry hands it
a checked PytestInvocation. With the local sandbox, code the agent wrote runs
as the host user; with the Docker sandbox it runs in a disposable container
that sees the workspace read-only and has no network.
"""

from __future__ import annotations

import configparser
import fnmatch
import functools
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .jail import JailViolation, path_components
from .patch import PatchError, apply_file_patch, parse_patch
from .process import child_env
from .redaction import Redactor
from .repo_map import SymbolIndex
from .sandbox import JUnitSummary, LocalSandbox, PytestInvocation, Sandbox, SandboxRun
from .schema import ToolSchema, validate_json_schema
from .snapshot import RestoreError, WorkspaceSnapshot
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
# Paths a single search may examine before it stops, and the most text it reads.
MAX_SEARCH_CANDIDATES = 20_000
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_SEARCH_TOTAL_BYTES = 16_000_000
MAX_PATTERN_CHARS = 512
SEARCH_WORKER = Path(__file__).with_name("_search_worker.py")
# Largest text a single edit or new file may carry.
MAX_TEXT_CHARS = 1_000_000
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


# Files that change how pytest collects, configures or imports tests. The agent
# may never write them, and the runtime restores (or removes) them before VERIFY.
PYTEST_CONTROL_FILES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "pytest.py",
        "sitecustomize.py",
        "usercustomize.py",
    }
)


def controls_pytest(rel: str) -> bool:
    """True for a workspace path whose file name can steer a pytest run."""
    name = rel.rsplit("/", 1)[-1].lower()
    return name in PYTEST_CONTROL_FILES or name.endswith(".pth")


@dataclass(frozen=True)
class Verification:
    """Outcome of ToolRegistry.verify()."""

    passed: bool
    observation: Observation
    reason: str  # why it failed; empty when it passed
    restored: tuple[str, ...]  # protected paths put back before the run
    mutants_total: int
    mutants_killed: int
    neutral_edits_total: int = 0
    neutral_edits_passed: int = 0


@dataclass(frozen=True)
class WriteResult:
    """What a MUTATING tool returns: its message and the files it changed."""

    text: str
    files: tuple[str, ...]


# Default size of the repo map a planner is shown, in characters.
REPO_MAP_CHARS = 6000


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
    # Arguments that name workspace paths or globs. execute() jails them
    # before the tool runs, so a tool cannot forget to.
    path_args: tuple[str, ...] = ()
    glob_args: tuple[str, ...] = ()
    # Inclusive bounds for integer arguments and length caps for strings.
    int_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)
    max_chars: dict[str, int] = field(default_factory=dict)
    # One line per argument, shown to models in the tool's JSON Schema.
    arg_docs: dict[str, str] = field(default_factory=dict)
    # A tool that brings its own JSON Schema (an external MCP tool) is
    # validated against it instead of the typed fields above.
    schema: dict[str, Any] | None = None

    def json_schema(self) -> dict[str, Any]:
        """The JSON Schema for this tool's arguments, as published to models."""
        if self.schema is not None:
            return self.schema
        properties: dict[str, Any] = {}
        for name, kind in {**self.required_args, **self.optional_args}.items():
            prop: dict[str, Any] = {"type": _JSON_TYPES[kind]}
            if name in self.int_ranges:
                prop["minimum"], prop["maximum"] = self.int_ranges[name]
            if name in self.max_chars:
                prop["maxLength"] = self.max_chars[name]
            if name in self.arg_docs:
                prop["description"] = self.arg_docs[name]
            properties[name] = prop
        return {
            "type": "object",
            "properties": properties,
            "required": list(self.required_args),
            "additionalProperties": False,
        }

    def validate(self, args: dict[str, Any]) -> str | None:
        """Return an error string when args violate the schema; None when valid."""
        if not isinstance(args, dict):
            return f"{self.name}: args must be an object, got {type(args).__name__}"
        if self.schema is not None:
            error = validate_json_schema(args, self.schema)
            return f"{self.name}: {error}" if error else None
        for key, expected in self.required_args.items():
            if key not in args:
                return f"{self.name}: missing required argument '{key}'"
            if not _is_instance(args[key], expected):
                return (
                    f"{self.name}: argument '{key}' must be {expected.__name__}, "
                    f"got {type(args[key]).__name__}"
                )
        for key, expected in self.optional_args.items():
            if key in args and not _is_instance(args[key], expected):
                return (
                    f"{self.name}: optional argument '{key}' must be {expected.__name__}, "
                    f"got {type(args[key]).__name__}"
                )
        unknown = set(args) - set(self.required_args) - set(self.optional_args)
        if unknown:
            return f"{self.name}: unknown arguments {sorted(unknown)}"
        for key, (low, high) in self.int_ranges.items():
            if key in args and not low <= args[key] <= high:
                return f"{self.name}: argument '{key}' is out of range ({low}..{high})"
        for key, limit in self.max_chars.items():
            if key in args and len(args[key]) > limit:
                return f"{self.name}: argument '{key}' is longer than {limit} characters"
        return None


_JSON_TYPES: dict[type, str] = {str: "string", int: "integer", bool: "boolean", float: "number"}


def _is_instance(value: Any, expected: type) -> bool:
    """isinstance, except that a bool is never accepted where an int is expected."""
    if expected is int and isinstance(value, bool):
        return False
    return isinstance(value, expected)


class ToolRegistry:
    """Validates, permission-checks and executes tool calls inside a jail."""

    def __init__(
        self,
        workspace: str | Path,
        policy: dict[Permission, Policy] | None = None,
        ask_callback: Callable[[ToolSpec, dict[str, Any]], bool] | None = None,
        exec_timeout_s: float = 120.0,
        search_timeout_s: float = 10.0,
        *,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.sandbox: Sandbox = sandbox or LocalSandbox()
        self.policy: dict[Permission, Policy] = {**DEFAULT_POLICY, **(policy or {})}
        self.ask_callback = ask_callback
        self.exec_timeout_s = exec_timeout_s
        self.search_timeout_s = search_timeout_s
        self.redact = Redactor(self.workspace)
        self.snapshot = WorkspaceSnapshot(self.workspace)
        self.snapshot.pin(controls_pytest)
        self.protected_prefixes: tuple[str, ...] = ()
        self.specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    # ------------------------------------------------------------- jail

    def jailed_path(self, raw: str) -> Path:
        """Map an agent path to a real path inside the workspace.

        The text is validated lexically first (see jail.py), so absolute, UNC,
        drive, device and stream paths are refused without touching the
        filesystem. The target must then be exactly the workspace join: a
        symbolic link, junction or other alias anywhere on the way is refused.
        Raises JailViolation.
        """
        target = self.workspace.joinpath(*path_components(raw))
        if not self._is_direct(target):
            raise JailViolation(
                f"path rejected: {raw!r} goes through a symbolic link, junction or other alias"
            )
        return target

    def jailed_glob(self, pattern: str) -> str:
        """Validate a search glob with the same lexical rules; return it normalised."""
        parts = path_components(pattern, glob=True)
        if not parts:
            raise JailViolation(f"glob {pattern!r} matches nothing")
        return "/".join(parts)

    def _is_direct(self, path: Path) -> bool:
        """True when path's real location is the path itself (no link on the way)."""
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))

    # ------------------------------------------------------------- registry

    def _register_defaults(self) -> None:
        self._register(
            ToolSpec(
                name="list_tree",
                description="List repository files up to a bounded depth.",
                permission=Permission.READ_ONLY,
                required_args={},
                optional_args={"max_depth": int},
                arg_docs={"max_depth": "How many directory levels to list (default 3)."},
                int_ranges={"max_depth": (1, 6)},
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
                glob_args=("glob",),
                arg_docs={
                    "pattern": "Case-insensitive Python regular expression, matched per line.",
                    "glob": "Workspace-relative glob of files to search (default **/*.py).",
                },
                max_chars={"pattern": MAX_PATTERN_CHARS},
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
                arg_docs={
                    "path": "Workspace-relative file path.",
                    "start_line": "First line to show, 1-based (default 1).",
                    "end_line": "Last line to show, inclusive (default: end of file).",
                },
                path_args=("path",),
                int_ranges={"start_line": (1, 10_000_000), "end_line": (1, 10_000_000)},
                run=self._read_file,
            )
        )
        self._register(
            ToolSpec(
                name="apply_edit",
                description="Replace exactly one occurrence of old_text with new_text.",
                permission=Permission.MUTATING,
                required_args={"path": str, "old_text": str, "new_text": str},
                arg_docs={
                    "path": "Workspace-relative file path.",
                    "old_text": "Exact text to replace; it must occur exactly once in the file.",
                    "new_text": "Replacement text.",
                },
                path_args=("path",),
                max_chars={"old_text": MAX_TEXT_CHARS, "new_text": MAX_TEXT_CHARS},
                run=self._apply_edit,
            )
        )
        self._register(
            ToolSpec(
                name="write_file",
                description="Create a NEW text file (refuses to overwrite existing files).",
                permission=Permission.MUTATING,
                required_args={"path": str, "content": str},
                arg_docs={
                    "path": "Workspace-relative path of a file that does not exist yet.",
                    "content": "Full text of the new file.",
                },
                path_args=("path",),
                max_chars={"content": MAX_TEXT_CHARS},
                run=self._write_file,
            )
        )
        self._register(
            ToolSpec(
                name="apply_patch",
                description=(
                    "Apply a unified diff to one or more files, all or nothing. Hunks must "
                    "match the files exactly (no fuzz); use dry_run to check first."
                ),
                permission=Permission.MUTATING,
                required_args={"patch": str},
                optional_args={"dry_run": bool},
                max_chars={"patch": MAX_TEXT_CHARS},
                arg_docs={
                    "patch": (
                        "Unified diff with '--- a/path' and '+++ b/path' headers and '@@' hunks; "
                        "/dev/null creates or deletes a file."
                    ),
                    "dry_run": "Check that the whole patch applies without writing (default false).",
                },
                run=self._apply_patch,
            )
        )
        self._register(
            ToolSpec(
                name="repo_map",
                description="Outline of the repository's Python files and their symbols, most-used first.",
                permission=Permission.READ_ONLY,
                required_args={},
                optional_args={"max_chars": int},
                int_ranges={"max_chars": (500, 20_000)},
                arg_docs={"max_chars": f"Size budget for the map (default {REPO_MAP_CHARS})."},
                run=self._repo_map,
            )
        )
        self._register(
            ToolSpec(
                name="find_symbol",
                description="Definitions of a class, function or method and every place it is used.",
                permission=Permission.READ_ONLY,
                required_args={"name": str},
                max_chars={"name": 200},
                arg_docs={"name": "Plain or qualified name, for example total or Cart.add."},
                run=self._find_symbol,
            )
        )
        self._register(
            ToolSpec(
                name="git_diff",
                description="Unified diff of workspace changes since the run started.",
                permission=Permission.READ_ONLY,
                required_args={},
                run=self._git_diff,
            )
        )
        self._register(
            ToolSpec(
                name="run_tests",
                description="Run pytest on workspace test paths (allowlisted flags, no shell).",
                permission=Permission.EXECUTION,
                required_args={"cmd": str},
                arg_docs={
                    "cmd": (
                        "Exactly 'python -m pytest' followed only by allowed flags "
                        f"({ALLOWED_FLAGS_HELP}) and test paths inside the workspace."
                    )
                },
                max_chars={"cmd": MAX_COMMAND_CHARS},
                run=self._run_tests,
            )
        )

    def _register(self, spec: ToolSpec) -> None:
        self.specs[spec.name] = spec

    def register(self, spec: ToolSpec) -> None:
        """Add a tool; it gets the same validation, policy, jail and trace as the built-ins."""
        if spec.name in self.specs:
            raise ValueError(f"a tool named {spec.name!r} is already registered")
        self._register(spec)

    def unregister(self, name: str) -> None:
        """Remove a tool added with register()."""
        self.specs.pop(name, None)

    def tool_schemas(self) -> list[ToolSchema]:
        """Every registered tool as a model sees it."""
        return [
            ToolSchema(spec.name, spec.description, spec.json_schema())
            for spec in self.specs.values()
        ]

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
            output = spec.run(**self._jail_arguments(spec, call.args))
            if isinstance(output, WriteResult):
                return Observation(
                    tool=call.tool,
                    ok=True,
                    output_excerpt=self.redact(output.text)[:OUTPUT_LIMIT],
                    latency_ms=(_time.perf_counter() - started) * 1000,
                    files_written=output.files,
                )
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
        except JailViolation as exc:
            # Refused by the jail or the command check: nothing was executed.
            return Observation(
                tool=call.tool,
                ok=False,
                rejected=True,
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
            # Filesystem failures, including an OS-level PermissionError on a
            # read-only or locked file: the call was allowed and then failed,
            # so it is ok=False but not a rejection.
            return Observation(
                tool=call.tool,
                ok=False,
                error=self._describe_os_error(exc),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except (ValueError, RuntimeError) as exc:
            return Observation(
                tool=call.tool,
                ok=False,
                error=self.redact(str(exc)),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            # Any other failure inside a tool is still an observation: a
            # planner-supplied call must never crash the runtime loop.
            return Observation(
                tool=call.tool,
                ok=False,
                error=self.redact(f"tool failed with {type(exc).__name__}: {exc}"),
                latency_ms=(_time.perf_counter() - started) * 1000,
            )

    def _describe_os_error(self, exc: OSError) -> str:
        """An OS error with its file shown relative to the workspace."""
        if exc.filename is None:
            return self.redact(str(exc))
        name = Path(os.fsdecode(exc.filename))
        try:
            where = name.relative_to(self.workspace).as_posix()
        except ValueError:
            where = self.redact(str(name))
        return f"{exc.strerror or type(exc).__name__}: {where}"

    def _jail_arguments(self, spec: ToolSpec, args: dict[str, Any]) -> dict[str, Any]:
        """Replace path and glob arguments with their jailed forms (or raise)."""
        jailed = dict(args)
        for name in spec.path_args:
            if name in jailed:
                jailed[name] = self.jailed_path(jailed[name])
        for name in spec.glob_args:
            if name in jailed:
                jailed[name] = self.jailed_glob(jailed[name])
        if spec.permission is Permission.MUTATING:
            for name in spec.path_args:
                if name in jailed:
                    self._check_writable(jailed[name])
        return jailed

    # ------------------------------------------------------------- protected paths

    def protect(self, paths: Iterable[str]) -> None:
        """Make task paths (files or directory prefixes) read-only to the agent.

        Protected paths, like pytest control files, are also restored from the
        starting snapshot before every verification run.
        """
        prefixes = {"/".join(path_components(raw)).lower() for raw in paths}
        self.protected_prefixes = tuple(sorted(prefix for prefix in prefixes if prefix))
        self.snapshot.pin(self.is_protected)

    def is_protected(self, rel: str) -> bool:
        low = rel.lower()
        return any(low == p or low.startswith(p + "/") for p in self.protected_prefixes)

    def _check_writable(self, path: Path) -> None:
        rel = self._display(path)
        if controls_pytest(rel):
            raise JailViolation(
                f"path rejected: {rel!r} controls how pytest runs and is read-only to the agent"
            )
        if self.is_protected(rel):
            raise JailViolation(
                f"path rejected: {rel!r} is protected by the task and read-only to the agent"
            )

    def restore_protected(self) -> list[str]:
        """Put protected paths and pytest control files back as they started.

        Changed files are rewritten, deleted ones recreated, and ones created
        during the run (by the agent's test code, say) removed, whatever their
        size, together with any links, junctions and __pycache__ directories
        that did not exist at the start.
        """
        restored = self.snapshot.remove_new_links()
        restored += self.snapshot.remove_new_caches()
        restored += self.snapshot.restore(
            lambda rel: controls_pytest(rel) or self.is_protected(rel)
        )
        return restored

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
                # Hidden entries (.git included) and links are never listed or entered.
                if entry.name.startswith(".") or not self._is_direct(entry):
                    continue
                is_dir = entry.is_dir()
                lines.append(("[dir]  " if is_dir else "       ") + self._display(entry))
                if is_dir:
                    walk(entry, depth + 1)

        walk(self.workspace, 1)
        return "\n".join(lines) or "(empty workspace)"

    def _search(self, pattern: str, glob: str = "**/*.py") -> str:
        try:
            re.compile(pattern, re.IGNORECASE)
        except (re.error, OverflowError, RecursionError) as exc:
            raise ValueError(f"invalid regex: {type(exc).__name__}: {exc}") from exc
        files: list[list[str]] = []
        total = 0
        for path in sorted(self._search_candidates(glob)):
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            total += len(text)
            if total > MAX_SEARCH_TOTAL_BYTES:
                break
            files.append([self._display(path), text])
        if not files:
            return "(no matches)"
        job = json.dumps({"pattern": pattern, "files": files}).encode("utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-S", str(SEARCH_WORKER)],
                input=job,
                capture_output=True,
                cwd=self.workspace,
                env=child_env(),
                timeout=self.search_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"search timed out after {self.search_timeout_s:.0f}s; use a simpler pattern"
            ) from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"search failed: {detail}")
        result = json.loads(proc.stdout.decode("utf-8"))
        lines = [f"{rel}:{line_no}:{text}" for rel, line_no, text in result["matches"]]
        if result["truncated"]:
            lines.append("(truncated at 100 matches)")
        return "\n".join(lines) or "(no matches)"

    def _search_candidates(self, glob: str) -> list[Path]:
        """Regular files matching glob that are really inside the workspace.

        One walk of the tree, never into links, junctions or .git, matched
        with a polynomial-time glob matcher (Path.glob is exponential in the
        number of '**' segments on deep trees). The walk stops after
        MAX_SEARCH_CANDIDATES entries.
        """
        pattern = glob.split("/")
        files: list[Path] = []
        examined = 0
        for directory, dirnames, filenames in os.walk(self.workspace, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name.lower() != ".git" and self._is_direct(Path(directory, name))
            )
            examined += 1 + len(filenames)
            if examined > MAX_SEARCH_CANDIDATES:
                break
            for name in filenames:
                path = Path(directory, name)
                parts = path.relative_to(self.workspace).parts
                if glob_match(pattern, parts) and path.is_file() and self._is_direct(path):
                    files.append(path)
        return files

    def _read_file(
        self, path: Path, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {self._display(path)}")
        lines = path.read_text(encoding="utf-8").splitlines()
        start = max((start_line or 1) - 1, 0)
        end = min(end_line or len(lines), len(lines))
        numbered = [f"{i + 1:4d}: {lines[i]}" for i in range(start, end)]
        return "\n".join(numbered) or "(empty file)"

    def _apply_edit(self, path: Path, old_text: str, new_text: str) -> WriteResult:
        shown = self._display(path)
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {shown}")
        text, newline = self._read_text(path)
        occurrences = text.count(old_text)
        if occurrences == 0:
            raise ValueError(f"apply_edit: old_text not found in {shown}")
        if occurrences > 1:
            raise ValueError(
                f"apply_edit: old_text matches {occurrences} times in {shown}; "
                "edits must be unambiguous"
            )
        # Encode before opening: a failure must not leave a truncated file.
        data = _encode(text.replace(old_text, new_text, 1), newline)
        self._write_bytes(path, data, create=False)
        return WriteResult(f"edited {shown}: 1 replacement", (shown,))

    def _write_file(self, path: Path, content: str) -> WriteResult:
        shown = self._display(path)
        if path.exists():
            raise ValueError(f"write_file refuses to overwrite existing {shown}; use apply_edit")
        data = _encode(content, "\n")  # new files use LF on every platform
        self._require_direct(path)  # before mkdir, which would follow a swapped-in link
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_bytes(path, data, create=True)
        return WriteResult(f"created {shown} ({len(content)} chars)", (shown,))

    def _apply_patch(self, patch: str, dry_run: bool = False) -> WriteResult:
        """Apply a unified diff to every file it names, or to none of them.

        Each target is jailed and write-checked like a declared path argument
        (the paths live inside the patch text, so the tool does it itself).
        New contents are computed in memory for every file first; only then
        are files written, and a failed write rolls back the ones before it.
        """
        try:
            file_patches = parse_patch(patch)
        except PatchError as exc:
            raise ValueError(f"apply_patch: {exc}") from exc
        plans: list[tuple[Path, str, str | None, str | None, str]] = []
        report: list[str] = []
        for file_patch in file_patches:
            target = self.jailed_path(file_patch.path)
            rel = self._display(target)
            self._check_writable(target)
            if target.exists() and not target.is_file():
                raise ValueError(f"apply_patch: {rel} is not a file")
            original, newline = self._read_text(target) if target.exists() else (None, "\n")
            try:
                new_text, notes = apply_file_patch(original, file_patch)
            except PatchError as exc:
                raise ValueError(f"apply_patch: {exc}") from exc
            plans.append((target, rel, original, new_text, newline))
            added = sum(1 for h in file_patch.hunks for line in h.lines if line[:1] == "+")
            removed = sum(1 for h in file_patch.hunks for line in h.lines if line[:1] == "-")
            action = "create" if original is None else "delete" if new_text is None else "change"
            report.append(
                f"{action} {rel} (+{added} -{removed})" + "".join(f"; {n}" for n in notes)
            )
        if dry_run:
            return WriteResult("dry run, nothing written; would " + "\nwould ".join(report), ())
        self._write_plans(plans)
        return WriteResult("applied: " + "; ".join(report), tuple(plan[1] for plan in plans))

    def _write_plans(self, plans: list[tuple[Path, str, str | None, str | None, str]]) -> None:
        """Write every planned file; on a failure put back the ones already written."""
        undo: list[tuple[Path, bytes | None]] = []
        try:
            for target, _rel, original, new_text, newline in plans:
                before = target.read_bytes() if original is not None else None
                if new_text is None:
                    self._require_direct(target)
                    target.unlink()
                elif original is None:
                    self._require_direct(target)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._write_bytes(target, _encode(new_text, "\n"), create=True)
                else:
                    self._write_bytes(target, _encode(new_text, newline), create=False)
                undo.append((target, before))
        except (OSError, ValueError):
            for target, before in reversed(undo):
                if before is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(before)
            raise

    def _python_sources(self) -> dict[str, str]:
        """Workspace-relative path -> text of the Python files a search would read."""
        sources: dict[str, str] = {}
        total = 0
        for path in sorted(self._search_candidates("**/*.py")):
            try:
                if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            total += len(text)
            if total > MAX_SEARCH_TOTAL_BYTES:
                break
            sources[self._display(path)] = text
        return sources

    def repo_map_text(self, max_chars: int = REPO_MAP_CHARS) -> str:
        """The repo map, as the repo_map tool returns it."""
        return SymbolIndex.build(self._python_sources()).render_map(max_chars)

    def _repo_map(self, max_chars: int = REPO_MAP_CHARS) -> str:
        return self.repo_map_text(max_chars)

    def _find_symbol(self, name: str) -> str:
        definitions, references = SymbolIndex.build(self._python_sources()).find(name)
        if not definitions:
            lines = [f"no definition of {name!r} in the workspace's Python files"]
        else:
            lines = [
                f"definition {d.path}:{d.line}-{d.end_line} {d.kind} {d.signature}"
                for d in definitions
            ]
        lines += [f"reference {r.path}:{r.line}" for r in references[:100]]
        if len(references) > 100:
            lines.append(f"({len(references) - 100} more references)")
        return "\n".join(lines)

    # ------------------------------------------------------------- link-safe file access

    def _require_direct(self, path: Path) -> None:
        """Re-check a jailed path immediately before it is opened.

        jailed_path() already refused links, but a concurrent process (test
        code left running, for example) could swap a directory for a link
        after that check. Checking again right before the open narrows that
        window; O_NOFOLLOW refuses a link at the final component where the OS
        supports it. Fully race-free access needs the Step 2 sandbox.
        """
        if not self._is_direct(path):
            raise JailViolation(
                f"path rejected: {self._display(path)!r} goes through a symbolic link, "
                "junction or other alias"
            )

    def _read_text(self, path: Path) -> tuple[str, str]:
        """The file's text with \n line endings, and the line ending it uses."""
        self._require_direct(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = handle.read()
        newline = "\r\n" if b"\r\n" in data else "\n"
        return data.decode("utf-8").replace("\r\n", "\n"), newline

    def _write_bytes(self, path: Path, data: bytes, *, create: bool) -> None:
        """Write bytes; with create=True, fail rather than replace an existing file."""
        self._require_direct(path)
        flags = os.O_WRONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= (os.O_CREAT | os.O_EXCL) if create else os.O_TRUNC
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)

    def _git_diff(self) -> str:
        """Diff against the snapshot taken when the registry was created.

        Computed in-process: no git binary runs, so repository configuration
        (core.fsmonitor, diff.external) cannot execute anything, and nothing
        outside the workspace is read.
        """
        return self.snapshot.diff()

    def _run_tests(self, cmd: str) -> CommandResult:
        args = self.parse_test_command(cmd)
        result, _ = self._run_pytest(args)
        if result.timed_out or result.exit_code is None:
            raise subprocess.TimeoutExpired(cmd, self.exec_timeout_s, output=result.output)
        return CommandResult(
            output=_note_omitted(result.output, result.omitted_bytes),
            exit_code=result.exit_code,
        )

    # ------------------------------------------------------------- verification

    def verify(
        self,
        cmd: str,
        mutants: Sequence[Mapping[str, str]] = (),
        neutral_edits: Sequence[Mapping[str, str]] = (),
    ) -> Verification:
        """Run the verification suite on a workspace the agent cannot have steered.

        Protected paths and pytest control files are restored first. The suite
        must exit 0 AND pytest's JUnit report, written outside the workspace,
        must show at least one passing test and no failures or errors, so code
        that ends the process early with status 0 does not count as a pass.
        Each seeded mutant (an exact one-place edit of a protected file) must
        then make the suite fail, which rejects vacuous tests, and each
        behaviour-preserving edit must leave it passing, which rejects tests
        that only pin the source text.

        Code under test still runs in the same process as pytest, so a
        determined agent could forge the report itself; only running the
        verifier outside the agent's reach (Step 2 sandbox) closes that.
        """
        total = len(mutants)
        try:
            restored = tuple(self.restore_protected())
        except (OSError, RestoreError) as exc:
            return self._unrestorable(exc, total)
        try:
            args = self.parse_test_command(cmd)
        except JailViolation as exc:
            rejected = Observation(
                tool="run_tests", ok=False, rejected=True, error=self.redact(str(exc))
            )
            return Verification(
                False, rejected, f"verify command rejected: {exc}", restored, total, 0
            )
        observation, report = self._verification_run(args)
        reason = _verification_failure(observation, report)
        if reason:
            return Verification(False, observation, reason, restored, total, 0)
        try:
            killed, survivors = self._run_mutants(args, mutants)
        except (OSError, RestoreError) as exc:
            return self._unrestorable(exc, total)
        if survivors:
            reason = "the tests did not detect seeded mutants: " + "; ".join(survivors)
            return Verification(False, observation, reason, restored, total, killed)
        neutral_total = len(neutral_edits)
        try:
            neutral_passed, broken = self._run_neutral_edits(args, neutral_edits)
        except (OSError, RestoreError) as exc:
            return self._unrestorable(exc, total)
        if broken:
            reason = "the tests failed on behaviour-preserving edits: " + "; ".join(broken)
            return Verification(
                False, observation, reason, restored, total, killed, neutral_total, neutral_passed
            )
        return Verification(
            True, observation, "", restored, total, killed, neutral_total, neutral_passed
        )

    def _unrestorable(self, exc: Exception, mutants_total: int) -> Verification:
        error = self.redact(f"could not restore protected files: {type(exc).__name__}: {exc}")
        observation = Observation(tool="run_tests", ok=False, error=error)
        return Verification(False, observation, error, (), mutants_total, 0)

    def _verification_run(self, args: list[str]) -> tuple[Observation, JUnitSummary | None]:
        result, report = self._run_pytest(args, junit_report=True)
        return self._command_observation(result), report

    def _run_pytest(
        self, args: list[str], *, junit_report: bool = False
    ) -> tuple[SandboxRun, JUnitSummary | None]:
        """Run pytest on checked args in the registry's sandbox."""
        invocation = self.pytest_invocation(args, junit=junit_report)
        run = self.sandbox.run_pytest(self.workspace, invocation, self.exec_timeout_s)
        return run, run.report

    def _run_mutants(
        self, args: list[str], mutants: Sequence[Mapping[str, str]]
    ) -> tuple[int, list[str]]:
        """Each mutant must make the suite fail; returns (killed, survivor names)."""
        return self._run_variants(args, mutants, must_fail=True)

    def _run_neutral_edits(
        self, args: list[str], edits: Sequence[Mapping[str, str]]
    ) -> tuple[int, list[str]]:
        """Each behaviour-preserving edit must leave the suite passing.

        A test that only pins the source text (a hash, say) fails here, so it
        cannot pass verification by "detecting" every mutant.
        """
        return self._run_variants(args, edits, must_fail=False)

    def _run_variants(
        self, args: list[str], variants: Sequence[Mapping[str, str]], *, must_fail: bool
    ) -> tuple[int, list[str]]:
        """Apply each one-place edit to its pristine protected file, run the
        suite, put the file back. Returns how many behaved as required and
        the names of those that did not."""
        as_required = 0
        failures: list[str] = []
        for variant in variants:
            # Undo anything the previous run planted (test code runs in every
            # run), so a planted conftest cannot steer the next run.
            self.restore_protected()
            name = str(variant.get("name") or variant.get("find", "unnamed"))
            rel = "/".join(path_components(variant["path"]))
            original = self.snapshot.pinned.get(rel, self.snapshot.files.get(rel))
            text = original.decode("utf-8") if original is not None else ""
            if original is None or text.count(variant["find"]) != 1:
                failures.append(f"{name} (the edit does not apply to {rel})")
                continue
            if not (self.is_protected(rel) or controls_pytest(rel)):
                # Only protected files are restored after the run.
                failures.append(f"{name} (edits must target a protected file, not {rel})")
                continue
            target = self.workspace / rel
            target.write_bytes(text.replace(variant["find"], variant["replace"], 1).encode("utf-8"))
            try:
                observation, report = self._verification_run(args)
            finally:
                self.restore_protected()  # also puts the edited file back
            if must_fail:
                behaved = (
                    observation.exit_code == 1
                    and report is not None
                    and report.failures + report.errors > 0
                )
            else:
                behaved = not _verification_failure(observation, report)
            if behaved:
                as_required += 1
            else:
                failures.append(name)
        return as_required, failures

    def _command_observation(self, result: SandboxRun) -> Observation:
        """Observation for a runtime-initiated pytest run, formatted like run_tests."""
        output = self.redact(_note_omitted(result.output, result.omitted_bytes))
        if result.timed_out or result.exit_code is None:
            return Observation(
                tool="run_tests",
                ok=False,
                output_excerpt=_tail(output, OUTPUT_LIMIT),
                error=f"command timed out after {self.exec_timeout_s:.0f}s; "
                "the process tree was killed",
            )
        prefix = f"exit={result.exit_code}\n"
        return Observation(
            tool="run_tests",
            ok=True,
            output_excerpt=prefix + _tail(output, OUTPUT_LIMIT - len(prefix)),
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
            # Always "./"-prefixed: a file named "--co" or "@args" must reach
            # pytest as a path, never as an option or an argument file.
            rel = self._display(target)
            args.append(("." if rel == "." else f"./{rel}") + separator + node)
            i += 1
        return args

    def pytest_invocation(self, args: list[str], *, junit: bool = False) -> PytestInvocation:
        """Describe a pytest run on checked args without naming any host path.

        The workspace's own pytest config file is honoured, and so is its
        pythonpath setting for entries that stay inside the workspace; the
        workspace root is always the first import root.
        """
        config = self._workspace_pytest_config()
        import_roots = ["."]
        if config is not None:
            import_roots += [self._display(root) for root in self._configured_pythonpath(config)]
        return PytestInvocation(
            args=tuple(args),
            config=self._display(config) if config is not None else None,
            import_roots=tuple(dict.fromkeys(import_roots)),
            junit=junit,
        )

    def pytest_argv(self, args: list[str], extra: tuple[str, ...] = ()) -> list[str]:
        """The argv the local sandbox would run for checked args (for inspection)."""
        return LocalSandbox().pytest_argv(self.workspace, self.pytest_invocation(args), extra)

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

    def workspace_relative(self, raw: str) -> str:
        """The jailed, workspace-relative POSIX form of an agent path."""
        return self._display(self.jailed_path(raw))

    def _display(self, path: Path) -> str:
        """Workspace-relative POSIX form of a jailed path ('.' for the root)."""
        rel = path.relative_to(self.workspace).as_posix()
        return rel if rel else "."


def _encode(text: str, newline: str) -> bytes:
    """UTF-8 bytes with the given line ending; ValueError if text cannot be encoded."""
    try:
        return text.replace("\n", newline).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"text cannot be encoded as UTF-8 ({exc.reason})") from exc


def glob_match(pattern: Sequence[str], parts: Sequence[str]) -> bool:
    """Whether path components match glob components ('**' spans any depth).

    Dynamic programming over (pattern index, path index), so the cost is at
    most len(pattern) * len(parts) name comparisons. Names compare
    case-insensitively on Windows, as the filesystem does.
    """

    @functools.cache
    def match(i: int, j: int) -> bool:
        if i == len(pattern):
            return j == len(parts)
        if pattern[i] == "**":
            return match(i + 1, j) or (j < len(parts) and match(i, j + 1))
        return (
            j < len(parts)
            and fnmatch.fnmatchcase(os.path.normcase(parts[j]), os.path.normcase(pattern[i]))
            and match(i + 1, j + 1)
        )

    return match(0, 0)


def _split_command(cmd: str) -> list[str]:
    """Split like a POSIX shell, but keep backslashes literal (Windows paths)."""
    lexer = shlex.shlex(cmd, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def _verification_failure(observation: Observation, report: JUnitSummary | None) -> str:
    """Why a verification run does not count as a pass; empty when it does."""
    if observation.exit_code is None:
        return observation.error or "the verification run did not finish"
    if observation.exit_code != 0:
        return f"the suite exited with status {observation.exit_code}"
    if report is None:
        return "pytest exited 0 but wrote no test report, so the run cannot be trusted"
    if report.failures or report.errors:
        return "the test report lists failures or errors despite exit status 0"
    if report.passed < 1:
        return "no test passed"
    return ""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[len(text) - limit :]


def _note_omitted(output: str, omitted_bytes: int) -> str:
    if omitted_bytes <= 0:
        return output
    return f"[{omitted_bytes} bytes of earlier output omitted]\n{output}"
