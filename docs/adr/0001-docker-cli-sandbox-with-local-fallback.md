# Run agent-written code in Docker through the CLI, with a local fallback

Agent-written test code needs OS-level containment, and the only option that
works unprivileged on a laptop and a GitHub runner alike is a container.
Pytest runs go through a `Sandbox` interface with two adapters: `DockerSandbox`
(one disposable container per run, driven through the `docker` CLI) is chosen
automatically whenever a Linux Docker daemon answers, and `LocalSandbox`
(rlimits, process groups, Windows job objects) is the fallback. Asking for
`docker` explicitly never falls back.

## Considered options

- **Docker SDK for Python.** Rejected: another dependency to pin and audit for
  a handful of `run`, `rm`, `build` and `info` calls that argv lists express
  without a shell.
- **Docker only.** Rejected: Windows CI runners cannot run Linux containers,
  and the runtime should stay usable on machines without Docker. The local
  backend's residuals are recorded as such instead.
- **gVisor, Firecracker, nsjail or bubblewrap.** Stronger or lighter, but each
  needs privileges, kernel features or installs that neither target reliably
  has.

## Consequences

- The docker command is configurable (`REPO_ENGINEER_DOCKER_CMD`), because on
  some machines the engine lives inside WSL2 and is reached as
  `wsl -d <distro> -e docker`; host paths are then translated to `/mnt/...`.
- Every pytest run pays container start-up (about a second); a run that needs
  third-party packages needs its own image.
