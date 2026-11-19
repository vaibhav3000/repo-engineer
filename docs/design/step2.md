# Step 2 design: sandbox, native tools, repo intelligence, telemetry, benchmark

Status: accepted. This document records what Step 2 adds to the runtime, the
alternatives that were weighed, what is deliberately left out, and the threat
model the sandbox is built against. Terms are defined in
[GLOSSARY.md](../GLOSSARY.md); the decisions that are hard to reverse have
their own records in [docs/adr/](../adr/).

## Where Step 1 left the runtime

The runtime already refuses malformed, unpermitted and path-escaping tool
calls before anything runs, restores protected files before it verifies, and
records every outcome. Three gaps were documented rather than closed:

1. **Agent-written test code runs as the host user.** `run_tests` executes
   pytest, and pytest imports code the agent wrote. That code can read and
   write anything the host user can, open network connections, and (attack
   A39) write outside the workspace. The jail cannot help: it checks the
   arguments of tool calls, not what an allowed process does afterwards.
2. **Process control has holes.** A descendant that calls `setsid()` on POSIX,
   or a process created through WMI on Windows, leaves the process group or
   job object and survives the deadline.
3. **The verifier shares a trust domain with the code it judges.** Test code
   left running could swap a directory for a link between the runtime's check
   and its open (the narrowed D10 race), and on the host it could in principle
   reach the runtime process itself.

Around those gaps, the planner interface was a prompt that asked the model for
a JSON object; the agent saw the repository as a flat file listing; edits were
one exact string replacement at a time; the trace was one JSON document
written when the run ended; and the benchmark had four single-file tasks.

## Goals

1. Contain agent-written code in a real sandbox and keep the verifier outside
   its reach, with the jail suite re-run against every backend.
2. Let models call tools natively through each provider's function-calling
   API, and expose the same tools over MCP, so external clients and external
   tool servers meet the same gates.
3. Give the agent a structural view of the repository and a multi-file patch
   tool that either applies completely or not at all.
4. Stream an audit trace that is detailed enough to reproduce a run offline,
   and ship a real command-line entry point.
5. Grow the benchmark to about a dozen multi-file tasks and report pass@k for
   live models.

## Non-goals

- **Making a test result trustworthy against code built to fool it.** Code
  under test runs inside the pytest process. It can return objects whose
  `__eq__` always answers True, or write a convincing JUnit report and exit 0.
  No sandbox changes that, because the deception happens inside the process
  whose output is being judged. The defences that do apply are the ones Step
  1 built (tests the agent cannot edit, mutants and behaviour-preserving edits
  on add-a-test tasks) plus human review of the patch. The jail suite records
  report forging as a residual on every backend so the limit stays visible.
- **Kernel-level isolation beyond what Docker offers.** gVisor, Firecracker or
  a seccomp profile of our own would be stronger; they need privileges or
  infrastructure a laptop and a GitHub runner do not reliably have.
- **Third-party test dependencies inside the sandbox.** The sandbox image
  carries Python and pytest only, and test runs have no network. A repository
  whose tests need other packages needs its own image (`--sandbox-image`).
- **Languages other than Python** for the repo map and the benchmark.
- **A hosted service.** Everything runs on the developer's machine or in CI.

## 1. Sandbox

### Shape

`Sandbox` is the seam between the registry and the operating system. Its
interface is one method:

```
run_pytest(workspace, invocation, timeout_s) -> SandboxRun
```

`invocation` is a `PytestInvocation`: the checked flags and `./`-prefixed test
paths, the workspace-relative pytest config file (or none), the
workspace-relative import roots, and whether a JUnit report is wanted. It
carries no host paths and no interpreter, so the same invocation runs on any
backend. `SandboxRun` returns the bounded output tail, the exit code (None on
a timeout), and the parsed JUnit summary.

Everything that differs between backends sits behind that method: where the
interpreter lives, how the workspace and scratch space are mapped, how the
process tree is limited and killed, and which environment the child sees.
`ToolRegistry` builds invocations; it no longer builds argv or calls
`run_bounded` itself.

Two adapters satisfy the interface, so the seam is real:

| | `DockerSandbox` (default when Docker answers) | `LocalSandbox` (fallback) |
|---|---|---|
| Interpreter | `python -P -m pytest` in the sandbox image | the runtime's own `sys.executable -P -m pytest` |
| Workspace | bind mount at `/work`, **read-only** | the host directory |
| Writable space | `/tmp` tmpfs only (256 MiB, nosuid, nodev); there is no writable host mount | host temp directory |
| Network | `--network none` (loopback only) | host network |
| Identity | `--user` (host uid on POSIX, 65534 elsewhere), `--cap-drop ALL`, `no-new-privileges` | host user |
| Memory, CPU, processes | `--memory`=`--memory-swap`, `--cpus`, `--pids-limit`, `--ulimit nofile` | POSIX rlimits (address space, CPU time, file size, open files) set by a launcher that then execs pytest; Windows job object limits (job memory, active processes) |
| Tree kill | the container is the tree: `docker rm -f` after every run | process group (POSIX) or job object (Windows), as in Step 1 |
| Environment | a fixed list passed with `-e`; nothing from the host | Step 1 allowlist |

`select_sandbox("auto")` returns a `DockerSandbox` when `docker info` answers
with a Linux daemon, and a `LocalSandbox` otherwise. `"docker"` fails loudly
when Docker is missing; it never degrades. The chosen backend is recorded in
the audit trace.

### Decisions inside the Docker backend

- **CLI, not the Docker SDK.** The Python Docker SDK is another dependency and
  another client to pin. The CLI is already on every machine that has Docker,
  and argv lists keep it shell-free. The docker command is configurable
  (`REPO_ENGINEER_DOCKER_CMD`, for example `wsl -d Ubuntu -e docker` when the
  engine lives in WSL2), and Windows paths are translated to `/mnt/<drive>/...`
  when the command runs through `wsl`.
- **Read-only workspace during test runs.** Tests normally do not need to
  write into the repository, and taking that ability away removes a whole
  family of attacks at once: planting a conftest during a run, swapping a
  directory for a link (the D10 race), filling the workspace with a huge tree
  to stall later walks. Writes the agent makes go through the registry's own
  tools, which run on the host under the jail.
- **No writable host mount.** The only writable place is a size-limited
  tmpfs, so test code cannot fill the host's disk. The JUnit report is
  written there, and a short runtime-supplied wrapper prints it after pytest
  exits, behind a per-run random marker; the host takes the report from the
  end of the output. A report larger than 512 KiB counts as missing, which
  fails verification with that reason rather than passing it.
- **One container per pytest run, removed afterwards.** Nothing survives a run:
  a process that calls `setsid()`, double-forks or daemonises is still inside
  the container's PID namespace and dies with it. `docker rm -f` runs after
  every run, not only on timeouts, because killing the CLI process does not
  stop the container.
- **A pinned, locally built image.** `repo-engineer sandbox build` builds
  `repo-engineer-sandbox:<digest of its Dockerfile>` from the digest-pinned
  `python:3.13-slim` base plus a pinned pytest. The tag changes whenever the
  recipe does, so a stale image is never reused silently. `auto` mode builds
  it on first use; a failed build is an error, not a fallback.
- **Infrastructure failures are not test results.** Exit status 125 from
  `docker run` (daemon error, missing image) raises `SandboxError`; the
  observation says the sandbox failed instead of reporting a test failure.

### The verifier outside the agent's reach

Verification keeps its Step 1 logic (restore, run, require a JUnit pass, then
mutants and behaviour-preserving edits), and every pytest run it starts goes
through the same sandbox. Under Docker that means the code being judged never
shares a process, a filesystem view or a network with the runtime: the
snapshot, the restore logic and the decision live in the host process, the
test code lives in a disposable container that sees the workspace read-only
and can write nowhere but its own tmpfs.

### Jail suite against both backends

`scripts/run_jail_attacks.py --backend {local,docker}` runs the attack list
against one backend and writes `results/jail_attack_evidence.json` (local) or
`results/jail_attack_evidence_docker.json`. New attacks cover what the
sandbox is for: reading a host file outside the workspace, reaching the
network, a process that outlives the run (via `setsid` on POSIX and WMI on
Windows), exhausting memory, a fork bomb, filling the disk, and forging the
verification report. The local evidence must stay byte-identical on Windows
and Linux, so an attack whose local outcome depends on the operating system
(the fork bomb is stopped by the Windows job object but not by POSIX
rlimits) is recorded for the Docker backend only, and its local behaviour is
stated in the threat model instead.

## 2. Native tool calling and MCP

### Tool schemas

`ToolSpec` gains per-argument descriptions and produces a JSON Schema
(`type`, `properties`, `required`, `additionalProperties: false`, integer
`minimum`/`maximum`, string `maxLength`). The schema is derived from the same
fields `ToolSpec.validate` checks, so what a model is told and what the
registry enforces cannot drift apart.

### Provider adapters

`ModelAdapter.complete(conversation, tools) -> ModelReply` is the seam between
the planner and a provider. The conversation is provider-neutral (system,
user, assistant turns with tool calls, tool results); each adapter translates
it to its provider's request and translates the reply back to text, tool
calls with ids, a stop reason and normalised usage.

| Adapter | SDK (optional extra) | Notes |
|---|---|---|
| `GeminiAdapter` | `google-genai` 3.x | `FunctionDeclaration` with `parameters_json_schema`, automatic function calling disabled, `FunctionCallingConfig(mode="ANY")` |
| `OpenAIAdapter` | `openai` 3.x | chat completions `tools`, `tool_choice="required"`, `parallel_tool_calls=False` |
| `AnthropicAdapter` | `anthropic` 2.x | `tools` with `input_schema`, `tool_choice={"type": "any", "disable_parallel_tool_use": true}`; no temperature (removed in 2.x) |
| local | `openai` 3.x | `OpenAIAdapter` with a `base_url` (Ollama, vLLM, llama.cpp) |

The SDKs' own retries are switched off and every adapter maps its provider's
errors onto one `ProviderError(status, retryable)`. A single retry loop then
applies the Step 1 policy everywhere: retry rate limits, overload and
transient server errors (408, 409, 429, 500, 502, 503, 504, 529) and
connection failures with backoff; raise everything else at once; never retry
a malformed reply.

`NativeToolPlanner` drives the conversation. It offers the registry's tools
plus a planner-owned `finish` tool, forces a tool call on every turn, executes
one call per runtime step, answers every call id with its observation, and
treats `finish` as "verify now". Arguments that are not valid JSON become a
planner error the runtime counts, and the model is told what went wrong on
its next turn. The prompt-parsed `LLMPlanner` stays only as a fallback for
endpoints without function calling.

### MCP server

`repo-engineer mcp-serve <workspace>` exposes the registry over MCP (stdio by
default) with the low-level `mcp.server.Server`, publishing each tool's JSON
Schema as written. Every `tools/call` becomes a `ToolCall` executed by the
same `ToolRegistry`: the client gets exactly the validation, permission
policy, jail and sandbox an internal planner gets, and a rejection comes back
as an MCP tool error (`is_error`) with the rejection text.

### External MCP tools

A run can mount external MCP servers. Each server is started **through the
sandbox** (inside a container with the workspace mounted read-only and no
network under Docker; with the scrubbed environment and limits locally), its
tools are listed once, and each becomes a `ToolSpec` named
`mcp__<server>__<tool>` with:

- permission `EXECUTION` unless the configuration says otherwise, because a
  remote server's annotations are claims, not guarantees;
- argument validation against the tool's own input schema (a small validator
  for the JSON Schema subset MCP tools use, so no new dependency);
- the jail on any arguments the configuration declares as paths;
- the normal policy gate, observation, redaction and trace.

## 3. Repo intelligence and patching

- **Symbol index and repo map (`repo_map`, `find_symbol` tools).** The stdlib
  `ast` module indexes classes, functions and methods with line ranges,
  signatures and the first docstring line, plus imports and call sites. The
  repo map ranks files by how often their symbols are referenced and renders
  them within a character budget. Files that do not parse are listed without
  symbols. tree-sitter was considered and left out (ADR 0004).
- **`apply_patch`.** A strict unified-diff parser: file headers must pair up,
  hunk headers must match their line counts, context and removed lines must
  match the file exactly at the stated line (or, if not there, at exactly one
  other place, reported as an offset); there is no fuzz. Every target is
  jailed and write-checked like a declared path argument. All new contents are
  computed in memory first; if any hunk fails nothing is written, and if a
  write fails the files already written are rolled back. `dry_run: true`
  checks the whole patch and reports what would change without writing.
- **Context budgeting and compaction.** The native planner keeps an estimate
  of the conversation's size (characters / 4, labelled an estimate). Each
  tool result is cut when it enters the conversation (test output keeps its
  tail, other output its head). When the estimate passes the budget, the
  oldest tool results outside the last few turns are replaced by one-line
  stubs that keep their call ids, so every provider still accepts the
  conversation. The system prompt, the task and the repo map are never
  compacted. Each compaction is a trace event.

## 4. Telemetry and CLI

- **Audit trace.** `TraceWriter` appends one JSON object per event to a
  `.jsonl` file and flushes it, so a crashed run still leaves its history.
  Events: `run_start` (task, configuration, sandbox, planner, model, a digest
  of the starting workspace), `state`, `model_call` (latency, tokens, cost when
  a price is configured, stop reason, the tool calls the model proposed),
  `tool_call` (arguments, observation, latency), `verify`, `compaction`,
  `run_end`. Every event carries a sequence number and the runtime state. Cost
  is computed from a user-supplied price table and is `null` otherwise; no
  prices are built in.
- **Replay.** `repo-engineer replay <trace.jsonl>` copies the starting
  workspace (a benchmark task by id, or `--repo`), checks its digest, and runs
  the real runtime with a planner that returns the recorded model decisions in
  order. Nothing calls a model. Every tool observation and the final state are
  compared with the recording; the command reports "reproduced" or the first
  event that differs.
- **CLI.** `repo-engineer run <repo> --task ...` copies the repository to a
  workspace, runs the agent there, writes the trace and the verified patch,
  and with `--apply` applies that patch to the original repository after a
  dry run. Other subcommands: `replay`, `mcp-serve`, `bench`, `sandbox
  build|status`.

## 5. Benchmark

About twelve tasks, each a small multi-module repository: bug fixes whose
cause and symptom sit in different files, refactors that rename or move code
across call sites, add-a-test tasks guarded by mutants and
behaviour-preserving edits, and API changes that ripple through callers.
Scripted mode replays known-good recipes (now using `apply_patch`,
`find_symbol` and `repo_map`) and stays the CI gate with byte-identical golden
results. Live mode runs each task k >= 3 times with a fresh planner per
attempt and reports pass@1 and pass@k with the unbiased estimator
`1 - C(n-c, k) / C(n, k)`.

## Threat model

**Adversary.** The planner, whether a model, a prompt injection that reached
the model through repository content, or an MCP client. It controls every
tool call's arguments, and through `write_file`, `apply_edit` and
`apply_patch` it controls code that pytest will import. External MCP servers
are also untrusted.

**Assets.** The host filesystem outside the workspace, credentials in the
runtime's environment, the network, the host's availability (memory, CPU,
processes, disk), the runtime process and its snapshot, and the integrity of
the verification verdict.

**Trust boundaries.**

1. Planner to registry: every call is validated, permission-checked and
   jailed before it runs (Step 1, unchanged).
2. Registry to sandbox: the only code the agent can make run is pytest (and
   configured external MCP servers), and it runs behind the sandbox.
3. Sandbox to verifier: the verdict is computed in the host process from a
   restored workspace.

**What each backend guarantees.**

| Threat | Docker | Local (POSIX) | Local (Windows) |
|---|---|---|---|
| Write or read host files outside the workspace | contained (not mounted) | **residual** | **residual** |
| Change the workspace during a test run | contained (read-only mount) | restored before VERIFY | restored before VERIFY |
| Network access | contained (`--network none`) | **residual** | **residual** |
| Credentials in the environment | contained (fixed env) | contained (allowlist) | contained (allowlist) |
| Process that outlives the run | contained (container removed) | **residual** (`setsid`) | **residual** (WMI) |
| Memory exhaustion | contained (cgroup memory) | contained (`RLIMIT_AS`) | contained (job memory) |
| Fork bomb | contained (`--pids-limit`) | **residual** (no per-tree limit without cgroups) | contained (job process limit) |
| Disk fill | contained (tmpfs size; no writable host mount) | partly (`RLIMIT_FSIZE` per file) | **residual** |
| Reach the runtime process | contained (separate namespaces) | **residual** (same user) | **residual** (same user) |
| Forge the verification report from inside the test process | **residual** | **residual** | **residual** |

The local backend's residuals are why Docker is the default wherever it is
available, and why the README tells people to run untrusted repositories
only with Docker.

## Testing strategy

- Each module is tested through its interface: sandboxes through
  `run_pytest`, adapters through `complete` with the real SDK client wired to
  an in-process `httpx2.MockTransport` (no network, real request
  serialisation), the planner through the runtime, MCP through the SDK's
  in-memory client.
- Docker-dependent tests skip with a reason when no Linux Docker daemon
  answers; CI on Ubuntu builds the sandbox image and runs them.
- The jail suite (both backends) and the scripted benchmark are regenerated
  in CI and must reproduce the committed files exactly; replay is tested by
  recording a scripted run's audit trace and replaying it.
