"""The Docker sandbox: one disposable, isolated container per pytest run.

Each run gets a container with no network, a read-only root filesystem, the
workspace bind-mounted read-only at /work, a size-limited tmpfs at /tmp as
the only writable place, all capabilities dropped, no-new-privileges, a
non-root user, memory (no swap), CPU, process and open-file limits, and a
fixed environment that shares nothing with the host. The container is
removed after every run, so no process the tests start can outlive it.

pytest writes its JUnit report into the tmpfs. A short wrapper, supplied by
the runtime and started with -I so nothing in the workspace can shadow its
imports, prints the report after pytest exits, behind a random per-run
marker; the host takes it from the end of the output. Code under test could
print a forged trailer of its own, which is the in-process forging residual
(docs/adr/0007), not something a container can prevent.

The docker CLI is driven with argv lists, never a shell. Its command is
configurable through REPO_ENGINEER_DOCKER_CMD (for example
"wsl -d Ubuntu -e docker" when the engine runs inside WSL2); Windows paths are
then translated to /mnt/<drive>/... for the bind mount.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from ..process import ENV_FIXED, ProcessResult, run_bounded
from .base import (
    PytestInvocation,
    SandboxError,
    SandboxLimits,
    SandboxRun,
    parse_junit,
    pytest_options,
)

DOCKERFILE = Path(__file__).with_name("Dockerfile")
DOCKER_CMD_ENV = "REPO_ENGINEER_DOCKER_CMD"
WORKDIR = "/work"
REPORT_PATH = "/tmp/junit.xml"
# Largest JUnit report the wrapper sends back; a larger one counts as missing.
MAX_REPORT_BYTES = 512 * 1024
# Output kept from a container run: large enough for the report trailer.
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
# Time allowed for the container to start on top of the test deadline.
STARTUP_GRACE_S = 30.0
LABEL = "repo-engineer.sandbox"
# The identity tests run as when the host's own uid cannot be used.
NOBODY = "65534:65534"

CONTAINER_ENV = {
    **ENV_FIXED,
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "PYTHONPYCACHEPREFIX": "/tmp/pycache",
}

# Runs pytest under the test deadline, then prints the JUnit report behind the
# nonce ("timeout" when the deadline passed first). A signal death of pytest
# (the OOM killer, say) becomes the usual 128+N exit status.
WRAPPER = f"""\
import base64, subprocess, sys
nonce, deadline = sys.argv[1], float(sys.argv[2])
proc = subprocess.Popen([sys.executable, "-P", "-m", "pytest", *sys.argv[3:]])
try:
    rc = proc.wait(timeout=deadline)
except subprocess.TimeoutExpired:
    proc.kill()
    sys.stdout.flush()
    sys.stdout.write("\\n" + nonce + " timeout\\n")
    sys.stdout.flush()
    sys.exit(124)
sys.stdout.flush()
try:
    with open({REPORT_PATH!r}, "rb") as handle:
        data = handle.read({MAX_REPORT_BYTES} + 1)
    payload = "too-large" if len(data) > {MAX_REPORT_BYTES} else base64.b64encode(data).decode()
except OSError:
    payload = "-"
sys.stdout.write("\\n" + nonce + " " + payload + "\\n")
sys.stdout.flush()
sys.exit(128 - rc if rc < 0 else rc)
"""


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        max_output_bytes: int = ...,
    ) -> ProcessResult: ...


Call = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _call(argv: Sequence[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
    """Run a short docker CLI command and capture its output."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def default_docker_cmd() -> list[str]:
    configured = os.environ.get(DOCKER_CMD_ENV, "").strip()
    return shlex.split(configured) if configured else ["docker"]


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8").replace("\r\n", "\n")


def default_image() -> str:
    """repo-engineer-sandbox:<first 12 hex digits of the recipe's SHA-256>."""
    digest = hashlib.sha256(dockerfile_text().encode("utf-8")).hexdigest()[:12]
    return f"repo-engineer-sandbox:{digest}"


def wsl_path(path: str) -> str:
    """A Windows drive path as WSL sees it under its default automount root."""
    match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", path)
    if match is None:
        raise SandboxError(f"cannot map {path!r} into WSL: only drive paths are supported")
    return f"/mnt/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/')}"


def split_report(output: str, nonce: str) -> tuple[str, bytes | None, bool]:
    """Separate pytest's output from the wrapper's trailer.

    The LAST line that starts with the nonce is the trailer; everything before
    it is the run's output. Returns (output, report bytes or None, whether the
    wrapper stopped pytest at the deadline).
    """
    marker = f"\n{nonce} "
    text = "\n" + output
    at = text.rfind(marker)
    if at == -1:
        return output, None, False
    payload = text[at + len(marker) :].split("\n", 1)[0].strip()
    before = text[1:at]
    if payload == "timeout":
        return before, None, True
    if payload in ("-", "too-large"):
        return before, None, False
    try:
        return before, base64.b64decode(payload, validate=True), False
    except (binascii.Error, ValueError):
        return before, None, False


class DockerSandbox:
    """Runs each pytest invocation in a fresh, locked-down container."""

    name = "docker"

    def __init__(
        self,
        limits: SandboxLimits | None = None,
        image: str | None = None,
        *,
        docker_cmd: Sequence[str] | None = None,
        runner: Runner = run_bounded,
        call: Call = _call,
    ) -> None:
        self.limits = limits or SandboxLimits()
        self.image = image or default_image()
        self.docker_cmd = list(docker_cmd) if docker_cmd is not None else default_docker_cmd()
        self.runner = runner
        self.call = call
        self._available: bool | None = None
        self._image_ready = False

    # ------------------------------------------------------------- availability

    @property
    def through_wsl(self) -> bool:
        return Path(self.docker_cmd[0]).name.lower() in ("wsl", "wsl.exe")

    def available(self) -> bool:
        """True when the docker command reaches a daemon that runs Linux containers."""
        if self._available is None:
            try:
                info = self.call([*self.docker_cmd, "info", "--format", "{{.OSType}}"], 30.0)
                self._available = info.returncode == 0 and info.stdout.strip() == "linux"
            except (OSError, subprocess.TimeoutExpired):
                self._available = False
        return self._available

    def require_available(self) -> None:
        if not self.available():
            raise SandboxError(
                f"Docker is not available through {' '.join(self.docker_cmd)!r}: no Linux "
                f"daemon answered `docker info`. Start Docker, set {DOCKER_CMD_ENV}, or "
                "choose the local sandbox explicitly."
            )

    def ensure_image(self) -> None:
        """Build the sandbox image from its pinned recipe unless it already exists."""
        if self._image_ready:
            return
        inspect = self.call([*self.docker_cmd, "image", "inspect", self.image], 60.0)
        if inspect.returncode != 0:
            if self.image != default_image():
                raise SandboxError(f"sandbox image {self.image!r} does not exist")
            self.build_image()
        self._image_ready = True

    def build_image(self) -> None:
        """docker build -t <image> - (the recipe on stdin, no build context)."""
        try:
            built = subprocess.run(
                [*self.docker_cmd, "build", "-t", self.image, "-"],
                input=dockerfile_text(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"building the sandbox image failed: {exc}") from exc
        if built.returncode != 0:
            raise SandboxError(f"building the sandbox image failed:\n{built.stderr[-2000:]}")

    # ------------------------------------------------------------- runs

    def host_path(self, path: Path) -> str:
        """The bind-mount source for a host path, as the daemon must see it."""
        text = str(path)
        if "," in text or '"' in text:
            raise SandboxError(f"cannot bind-mount {text!r}: it contains a comma or quote")
        return wsl_path(text) if self.through_wsl else text

    def user(self) -> str:
        """Run as the host user on POSIX hosts (files stay theirs), else as nobody."""
        getuid = getattr(os, "getuid", None)
        getgid = getattr(os, "getgid", None)
        if getuid is None or getgid is None or self.through_wsl:
            return NOBODY
        uid = getuid()
        return NOBODY if uid == 0 else f"{uid}:{getgid()}"

    def run_argv(
        self,
        workspace: Path,
        invocation: PytestInvocation,
        *,
        name: str,
        nonce: str,
        timeout_s: float = 120.0,
    ) -> list[str]:
        """The full docker command for one invocation."""
        limits = self.limits
        extra = ["--basetemp=/tmp/pytest"]
        if invocation.junit:
            extra.append(f"--junitxml={REPORT_PATH}")
        env: list[str] = []
        for key, value in CONTAINER_ENV.items():
            env += ["-e", f"{key}={value}"]
        return [
            *self.docker_cmd,
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--label",
            LABEL,
            "--network",
            "none",
            "--memory",
            f"{limits.memory_mb}m",
            "--memory-swap",
            f"{limits.memory_mb}m",
            "--cpus",
            f"{limits.cpus:g}",
            "--pids-limit",
            str(limits.pids),
            "--ulimit",
            f"nofile={limits.nofile}:{limits.nofile}",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={limits.tmpfs_mb}m",
            "--user",
            self.user(),
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=bind,source={self.host_path(workspace)},target={WORKDIR},readonly",
            "-w",
            WORKDIR,
            *env,
            self.image,
            "python",
            "-I",
            "-c",
            WRAPPER,
            nonce,
            f"{timeout_s:g}",
            *pytest_options(invocation, root=WORKDIR, devnull="/dev/null", extra=tuple(extra)),
        ]

    def run_pytest(
        self, workspace: Path, invocation: PytestInvocation, timeout_s: float
    ) -> SandboxRun:
        self.ensure_image()
        name = f"repo-engineer-sbx-{secrets.token_hex(6)}"
        nonce = secrets.token_hex(16)
        argv = self.run_argv(workspace, invocation, name=name, nonce=nonce, timeout_s=timeout_s)
        try:
            result = self.runner(
                argv,
                cwd=workspace,
                env=dict(os.environ),  # for the docker CLI only; the container gets CONTAINER_ENV
                timeout_s=timeout_s + STARTUP_GRACE_S,
                max_output_bytes=MAX_OUTPUT_BYTES,
            )
        finally:
            # Killing the CLI does not stop a container; this does, on every path.
            self._remove(name)
        if result.exit_code == 125:
            raise SandboxError(f"docker could not run the sandbox: {result.output.strip()[-1000:]}")
        output, report_bytes, deadline_passed = split_report(result.output, nonce)
        report = parse_junit(report_bytes) if invocation.junit and report_bytes else None
        timed_out = result.timed_out or deadline_passed
        return SandboxRun(
            exit_code=None if timed_out else result.exit_code,
            output=output,
            timed_out=timed_out,
            omitted_bytes=result.omitted_bytes,
            report=report,
        )

    def _remove(self, name: str) -> None:
        try:
            self.call([*self.docker_cmd, "rm", "-f", name], 60.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"could not remove sandbox container {name}: {exc}") from exc
