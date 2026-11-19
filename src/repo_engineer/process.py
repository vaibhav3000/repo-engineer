"""Child processes for run_tests: scrubbed environment, bounded output, tree kill.

These controls protect the runtime from the processes it starts: a test run
cannot read the runtime's credentials, exhaust its memory with output, or keep
it waiting after the deadline. They are the local sandbox's building blocks
and do not isolate the code the tests execute: that code runs as the host
user, with the host user's filesystem and network access. The Docker sandbox
(sandbox/docker.py) is the one that isolates it.
"""

from __future__ import annotations

import collections
import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

# Variables a Python test run needs to start and behave normally. Everything
# else in the runtime's environment is withheld from the child, including
# provider API keys, PYTHONPATH, PYTEST_ADDOPTS and PYTEST_PLUGINS, and the
# user-name variables (pytest would otherwise put the name in tmp_path).
ENV_PASSTHROUGH = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    }
)

# Fixed settings that make test output deterministic and keep the workspace
# free of bytecode caches.
ENV_FIXED = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "COLUMNS": "80",
    "PY_COLORS": "0",
    "NO_COLOR": "1",
}

# How long to keep reading after the process exits or is killed. A descendant
# that escaped the kill can hold the pipe open forever; the runtime stops
# waiting for it instead.
READER_GRACE_S = 3.0


def child_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a test run: the allowlisted variables plus fixed settings."""
    src = os.environ if source is None else source
    env = {name: value for name, value in src.items() if name.upper() in ENV_PASSTHROUGH}
    env.update(ENV_FIXED)
    return env


@dataclass(frozen=True)
class JobLimits:
    """Windows job object limits for a run's whole process tree (0 = no limit)."""

    memory_bytes: int = 0  # committed memory of all processes together
    active_processes: int = 0  # processes alive at once, the first one included


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None  # None when the run was killed at the deadline
    output: str  # tail of combined stdout and stderr, with "\n" line endings
    timed_out: bool
    omitted_bytes: int  # bytes dropped from the front to keep the tail bounded


class _TailBuffer:
    """Keeps only the last `limit` bytes of a stream; counts what it dropped."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.chunks: collections.deque[bytes] = collections.deque()
        self.size = 0
        self.total = 0
        self.lock = threading.Lock()

    def drain(self, stream: IO[bytes]) -> None:
        try:
            while chunk := os.read(stream.fileno(), 65536):
                with self.lock:
                    self.chunks.append(chunk)
                    self.size += len(chunk)
                    self.total += len(chunk)
                    while self.size > self.limit:
                        excess = self.size - self.limit
                        head = self.chunks[0]
                        if len(head) <= excess:
                            self.chunks.popleft()
                            self.size -= len(head)
                        else:
                            self.chunks[0] = head[excess:]
                            self.size -= excess
        except OSError:
            return  # pipe closed underneath us; keep what was read

    def text(self) -> tuple[str, int]:
        with self.lock:
            data = b"".join(self.chunks)
            omitted = self.total - len(data)
        return data.decode("utf-8", "replace").replace("\r\n", "\n"), omitted


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
    max_output_bytes: int = 262_144,
    job_limits: JobLimits | None = None,
) -> ProcessResult:
    """Run argv with no shell, a deadline, a bounded output tail and tree kill.

    job_limits apply on Windows, where the whole tree runs in one job object;
    POSIX limits are set by the command itself (see sandbox/_launcher.py).
    """
    group = _ProcessGroup(job_limits or JobLimits())
    proc = group.start(list(argv), cwd=cwd, env=dict(env))
    tail = _TailBuffer(max_output_bytes)
    assert proc.stdout is not None
    reader = threading.Thread(target=tail.drain, args=(proc.stdout,), daemon=True)
    reader.start()
    timed_out = False
    try:
        exit_code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
    # Kill everything the run started, on the timeout path and also after a
    # normal exit, so no descendant outlives the tool call.
    group.kill()
    if timed_out:
        proc.wait()
    reader.join(timeout=READER_GRACE_S)
    group.close()
    output, omitted = tail.text()
    return ProcessResult(
        exit_code=exit_code, output=output, timed_out=timed_out, omitted_bytes=omitted
    )


class _ProcessGroup:
    """A started process plus everything it spawns, killable as one unit.

    POSIX: the child leads a new session, and kill() signals the whole process
    group. A descendant that calls setsid() itself leaves the group; only an
    OS sandbox can contain that.
    Windows: the child starts suspended, joins a job object that kills every
    member when terminated, and only then resumes, so every process it
    starts, detached or not, stays in the job. A process created on its
    behalf by another service (WMI's Win32_Process.Create, for example) is
    not its descendant and escapes, like a POSIX process that calls setsid;
    only an OS sandbox can contain those. If the job cannot be created,
    kill() falls back to `taskkill /T`, which only reaches descendants whose
    parent is still alive.
    """

    def __init__(self, limits: JobLimits | None = None) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self._job: int | None = None
        self._limits = limits or JobLimits()

    def start(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen[bytes]:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED,
            )
            self._job = _job_for_suspended_process(proc.pid, self._limits)
        else:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                start_new_session=True,
            )
        self.proc = proc
        return proc

    def kill(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if sys.platform == "win32":
            if self._job is not None:
                _terminate_job(self._job)
            if proc.poll() is None:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
        else:
            # An empty group raises ProcessLookupError; nothing is left to kill.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    def close(self) -> None:
        if sys.platform == "win32" and self._job is not None:
            _close_handle(self._job)
            self._job = None


_CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x2000
_LIMIT_ACTIVE_PROCESS = 0x0008
_LIMIT_JOB_MEMORY = 0x0200
_DIE_ON_UNHANDLED_EXCEPTION = 0x0400  # no error-reporting dialogs that hang the run
_JOB_EXTENDED_LIMIT_INFORMATION_CLASS = 9

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _job_for_suspended_process(pid: int, limits: JobLimits) -> int | None:
        """Put a suspended process in a new kill-on-close job, then resume it.

        The job also caps the tree's committed memory and the number of
        processes alive at once when the limits ask for it. Returns the job
        handle, or None (with the process resumed) when the job cannot be set
        up, in which case kill() falls back to taskkill.
        """
        rights = 0x0100 | 0x0001 | 0x0800  # SET_QUOTA | TERMINATE | SUSPEND_RESUME
        process = _kernel32.OpenProcess(rights, False, pid)
        if not process:
            return None
        try:
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                return None
            info = _ExtendedLimitInformation()
            flags = _KILL_ON_JOB_CLOSE | _DIE_ON_UNHANDLED_EXCEPTION
            if limits.memory_bytes > 0:
                flags |= _LIMIT_JOB_MEMORY
                info.JobMemoryLimit = limits.memory_bytes
            if limits.active_processes > 0:
                flags |= _LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = limits.active_processes
            info.BasicLimitInformation.LimitFlags = flags
            configured = _kernel32.SetInformationJobObject(
                job,
                _JOB_EXTENDED_LIMIT_INFORMATION_CLASS,
                ctypes.byref(info),
                ctypes.sizeof(info),
            )
            if not (configured and _kernel32.AssignProcessToJobObject(job, process)):
                _kernel32.CloseHandle(job)
                return None
            return int(job)
        finally:
            _ntdll.NtResumeProcess(process)
            _kernel32.CloseHandle(process)

    def _terminate_job(job: int) -> None:
        _kernel32.TerminateJobObject(job, 1)

    def _close_handle(handle: int) -> None:
        _kernel32.CloseHandle(handle)
