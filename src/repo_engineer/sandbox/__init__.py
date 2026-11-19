"""Sandboxes: where pytest runs when the agent's code is under test.

The registry decides WHAT may run (checked flags and test paths, the
workspace's own pytest config); a Sandbox decides WHERE and HOW: which
interpreter, how the workspace is mapped, which limits apply and how the whole
process tree is killed. The seam is one method, run_pytest, and two adapters
satisfy it:

- LocalSandbox runs the runtime's own interpreter on the host with a scrubbed
  environment, resource limits and a process-tree kill. Agent code still runs
  as the host user with the host's filesystem and network.
- DockerSandbox runs every pytest invocation in a fresh container with no
  network, a read-only root and workspace, a size-limited tmpfs as the only
  writable place, dropped capabilities and memory, CPU and process limits.

select_sandbox("auto") prefers Docker whenever a Linux daemon answers.
"""

from __future__ import annotations

from .base import (
    JUnitSummary,
    PytestInvocation,
    Sandbox,
    SandboxError,
    SandboxLimits,
    SandboxRun,
    parse_junit,
    pytest_options,
    read_junit,
)
from .docker import DockerSandbox
from .local import LocalSandbox

SANDBOX_MODES = ("auto", "docker", "local")


def select_sandbox(
    mode: str = "auto", *, limits: SandboxLimits | None = None, image: str | None = None
) -> Sandbox:
    """The sandbox for a mode: "local", "docker" (never falls back) or "auto".

    "auto" returns a DockerSandbox when a Linux Docker daemon answers and a
    LocalSandbox otherwise.
    """
    limits = limits or SandboxLimits()
    if mode not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode {mode!r}; expected one of {SANDBOX_MODES}")
    if mode == "local":
        return LocalSandbox(limits)
    docker = DockerSandbox(limits=limits, image=image)
    if mode == "docker":
        docker.require_available()
        return docker
    return docker if docker.available() else LocalSandbox(limits)


__all__ = [
    "SANDBOX_MODES",
    "DockerSandbox",
    "JUnitSummary",
    "LocalSandbox",
    "PytestInvocation",
    "Sandbox",
    "SandboxError",
    "SandboxLimits",
    "SandboxRun",
    "parse_junit",
    "pytest_options",
    "read_junit",
    "select_sandbox",
]
