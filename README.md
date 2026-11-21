# repo-engineer

A coding agent whose model only proposes. Every tool call is validated,
permission-checked and confined to the workspace before it runs; code the
agent writes is executed in a sandbox; and a task is complete only when the
runtime's own verification run passes. The model's claim that it is done
changes nothing.

<p align="center">
  <a href="docs/figures/pipeline.svg"><img src="docs/figures/pipeline.png" width="100%" alt="System overview: the runtime state machine (INIT, ANALYZE_REPO, PLAN, ACT, OBSERVE, REPLAN, VERIFY, COMPLETE, FAILED) above the path of one tool call: proposal, JSON Schema, permission gate, path jail, host tools or the Docker or local sandbox, observation, VERIFY, and the A46 report-forging residual (ADR 0007)"></a>
</p>

## System Walkthrough (30-Second Overview)

<!-- generated:walkthrough -->
- **Proposal → JSON Schema → Permission gate → Path jail.** The model, through native tool calling or an MCP client, only proposes a call. The runtime checks the schema, the AUTO/ASK/DENY policy and any declared path arguments; a refusal returns to the planner as a rejected observation.
- **Host tools / Sandbox.** File tools run in-process under the jail. `run_tests` and external MCP servers run in a fresh Docker container (no network, read-only root and workspace, capabilities dropped, non-root, digest-pinned image) or, without Docker, a local fallback with rlimits or a Job Object.
- **Observation and the state machine band.** Each result returns as an observation plus one JSONL trace event. PLAN, ACT and OBSERVE repeat; failing tests go to REPLAN, and a spent step budget, repeated planner errors or no replans left end the run in FAILED.
- **VERIFY / Residual A46.** COMPLETE only follows VERIFY: restore protected paths, rerun the suite, require exit status 0 and a passing JUnit report (12/12 scripted tasks). Docker suite: 49 attacks, 29 rejected, 19 contained, 1 residual, 0 breached (local: 47 attacks, 29/13/5/0). Docker's residual is A46, code under test forging its report inside pytest ([ADR 0007](docs/adr/0007-report-forging-is-outside-the-sandbox.md)).
<!-- /generated:walkthrough -->

## The problem

Coding agents tend to fail in three ways: they parse free-text tool calls and
break on the first malformed one, they hand the model a shell, or they accept
the model's word that the bug is fixed. This project treats each as a design
constraint.

| Failure mode | Defence here |
|---|---|
| Malformed tool calls | Native function calling against JSON Schemas generated from the same tool specs the registry enforces; anything that still fails validation is rejected with a structured error and fed back |
| Shell access | No shell tool. `run_tests` accepts only `python -m pytest` with allowlisted flags and workspace test paths, and runs inside a sandbox |
| Unverified claims | VERIFY restores protected files and re-runs the suite itself; on add-a-test tasks seeded mutants must fail and behaviour-preserving edits must pass |
| Path escapes | Paths are checked as text before any filesystem access and must be direct paths inside the workspace (no links); `.git`, pytest control files and protected paths are read-only |
| Runaway runs | Step, replan and planner-error budgets end in FAILED |
| Code the agent wrote | A fresh Docker container per test run with no network and a read-only workspace; a local fallback with resource limits when Docker is absent |

## Results at a glance

<!-- generated:bench-line -->
Scripted mode: 12/12 tasks verified, 3 replans, 0 failed tool calls.
<!-- /generated:bench-line -->

<!-- generated:jail-summary -->
| sandbox | attacks | rejected | contained | residual | breached |
|---|---|---|---|---|---|
| local | 47 | 29 | 13 | 5 | 0 |
| docker | 49 | 29 | 19 | 1 | 0 |
<!-- /generated:jail-summary -->

[![Attack outcomes per backend: Docker and the local fallback, each bar labelled with its own number of attacks](docs/figures/attacks.png)](docs/figures/attacks.svg)

*Each bar is its own suite: Docker also runs the fork-bomb and disk-fill attacks and contains the host-file, network and orphan-process attacks that stay residual locally.<br>
Nothing is breached on either backend; Docker's only residual is report forging (A46). The counts are drawn from the evidence files in `results/`.*

<!-- generated:live-line -->
Recorded on 2026-09-17 with `gemini-2.5-flash` and the earlier prompt-parsed JSON planner: 2/2 tasks verified, 7 model calls, 3887 prompt and 518 completion tokens, one attempt per task.
<!-- /generated:live-line -->

Live pass@k with the native tool-calling planner is **not yet measured**. The
scripted benchmark replays known fix recipes: it validates the runtime, the
gates and the sandboxes, not a model's problem solving.

Every result number (benchmark, jail suite, live run, tool count) in this
README, the design FAQ, the technical report and the demo site is generated
from the committed files in `results/` and from the code by
`scripts/sync_docs.py`; `python scripts/sync_docs.py --check` runs in the test
suite and fails the build when anything is stale.

## Architecture

### The state machine

The runtime owns every transition. Any edge not in `state.TRANSITIONS` raises,
and every arrow in the figure below is generated from that table.

[![The runtime state machine: INIT, ANALYZE_REPO, PLAN, ACT, OBSERVE, REPLAN and VERIFY, ending in COMPLETE or FAILED](docs/figures/fsm.png)](docs/figures/fsm.svg)

*Every arrow comes from `state.TRANSITIONS` through `scripts/sync_docs.py`, and a test fails if the drawing and the table disagree.<br>
COMPLETE is reachable only from VERIFY, which restores the protected files, re-runs the suite and needs exit status 0 plus a passing JUnit report.*

### Every tool call passes the same gates

The diagram at the top shows the path. A call from the native planner or an
MCP client runs only after the schema check, the AUTO/ASK/DENY gate and the
jail on its declared path arguments; a refusal goes back to the planner.
`run_tests` and external MCP servers run in the sandbox, yet code under test
can still forge its report from inside pytest (residual A46,
[ADR 0007](docs/adr/0007-report-forging-is-outside-the-sandbox.md)).

### Sandbox backends

```mermaid
flowchart LR
    REG["ToolRegistry<br/>(checked PytestInvocation,<br/>no host paths)"] --> SEL{"select_sandbox<br/>auto / docker / local"}
    SEL -->|"Linux Docker daemon answers"| D["DockerSandbox<br/>fresh container per run<br/>no network, read-only root and workspace,<br/>tmpfs only, no capabilities, non-root,<br/>memory, CPU and process limits,<br/>removed after every run"]
    SEL -->|"no Docker"| L["LocalSandbox<br/>host interpreter, scrubbed env,<br/>POSIX rlimits or Windows job object,<br/>process-tree kill"]
    D --> V["verifier in the host process:<br/>restore, run, JUnit report,<br/>mutants, neutral edits"]
    L --> V
```

`auto` picks Docker whenever a Linux daemon answers and never silently swaps
an explicit `--sandbox docker` for the local backend. The verifier, the
starting snapshot and the restore logic stay in the host process; under
Docker the code being judged shares no process, filesystem view or network
with them. Design, trade-offs and the per-backend threat model are in
[docs/design/step2.md](docs/design/step2.md).

## Tools, models and MCP

<!-- generated:tools -->
The registry has 10 built-in tools: `apply_edit`, `apply_patch`, `find_symbol`, `git_diff`, `list_tree`, `read_file`, `repo_map`, `run_tests`, `search`, `write_file`.
<!-- /generated:tools -->

- **Native tool calling.** `NativeToolPlanner` talks to Gemini, OpenAI,
  Anthropic or a local OpenAI-compatible server (Ollama, vLLM) through
  provider adapters in `src/repo_engineer/models/`. Tool calls are forced,
  every call id is answered with the runtime's observation, a planner-owned
  `finish` tool sends the run to VERIFY, and one retry policy covers every
  provider (rate limits, overload and transient server errors only). The SDKs
  are optional extras and are tested offline through their real clients.
- **MCP, both directions.** `repo-engineer mcp-serve` exposes the registry to
  any MCP client with exactly the gates an internal planner gets. External MCP
  servers listed in `--mcp-config` are started through the sandbox, behind a
  relay that stops a flooding server, and their tools are registered as
  `mcp__<server>__<tool>` under the same validation, policy, jail and trace.
- **Repo map and symbols.** `repo_map` and `find_symbol` index Python files
  with the stdlib `ast` module (definitions, line ranges, signatures, call
  sites); the planner's first message carries a ranked, size-limited map.
- **Strict patches.** `apply_patch` takes a unified diff that must match
  exactly (or at one unique offset, never with fuzz), jails and write-checks
  every target, and writes all files or none; `dry_run` checks first.
- **Context budget.** Old tool results are replaced by stubs that keep their
  call ids once the conversation outgrows its budget.

## Command line

```bash
# Run the agent on a copy of a repository; the original is untouched
repo-engineer run path/to/repo --task "fix the failing invoice total" \
    --provider gemini --model <model id> --protect tests/ --verify-cmd "python -m pytest -q"
# ... add --apply to write the verified patch back (after a dry run)

repo-engineer replay runs/<run>.trace.jsonl --repo path/to/repo   # re-execute offline, compare
repo-engineer mcp-serve path/to/workspace --sandbox auto            # MCP server on stdio
repo-engineer bench                                                 # scripted benchmark
repo-engineer bench --live --provider openai --model <id> --attempts 3   # live pass@k
repo-engineer sandbox status                                        # which backend auto picks
repo-engineer sandbox build                                         # build the pinned sandbox image
```

`run` writes `<run>.trace.jsonl`, `<run>.state.json` and `<run>.patch` next
to each other and exits 0 only when verification passed. It refuses `--apply`
when the diff is incomplete, and applying respects `--protect`. Provider keys
come from environment variables (`GEMINI_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`), are withheld from test processes and are redacted from
observations, traces and prompts. When Docker is reached another way, set
`REPO_ENGINEER_DOCKER_CMD` (for example `wsl -d Ubuntu -e docker`).

### Audit trace and replay

Each run streams one JSON object per event (run start with a workspace digest,
state changes, planner decisions, model calls with latency, tokens and cost,
tool calls with observations, compaction, verification, run end) and flushes
it, so a crashed run still leaves its history. Costs come only from a price
table you supply. `repo-engineer replay` copies the starting workspace,
refuses it unless the digest matches, feeds the recorded decisions to the real
runtime without calling a model, and reports the first event that differs.

## Benchmark

**Scripted mode** runs every task in a fresh workspace copy with recipes that
already contain the fix, under either sandbox, and must reproduce the golden
files in `results/` byte for byte on Windows and Linux. It measures the
runtime (validation, gates, sandbox, verification, traces), not a model.

<!-- generated:bench-table -->
| task | verified | steps | replans | failed tool calls |
|---|---|---|---|---|
| fix an off-by-one loop | yes | 5 | 0 | 0 |
| repair a failing test | yes | 5 | 1 | 0 |
| add missing tests (mutants) | yes | 5 | 0 | 0 |
| constrained rename | yes | 7 | 0 | 0 |
| bug behind another module | yes | 6 | 1 | 0 |
| rename across a package (one patch) | yes | 5 | 0 | 0 |
| add tests for a parser (mutants) | yes | 4 | 0 | 0 |
| extract a shared helper | yes | 4 | 0 | 0 |
| signature change and its callers | yes | 4 | 0 | 0 |
| shared mutable default | yes | 5 | 1 | 0 |
| add tests for a validator (mutants) | yes | 4 | 0 | 0 |
| stale imports after a move | yes | 4 | 0 | 0 |
| **total** | **12/12** | **58** | **3** | **0** |
<!-- /generated:bench-table -->

**Live mode** (`repo-engineer bench --live`) runs each task several times with
a fresh planner per attempt and reports pass@1 and pass@k with the unbiased
estimator. Every run is written to a new file under `results/llm_runs/` with a
trace per attempt and is never overwritten. None has been recorded yet.

The only live run on record predates the native planner:

<!-- generated:live-line -->
Recorded on 2026-09-17 with `gemini-2.5-flash` and the earlier prompt-parsed JSON planner: 2/2 tasks verified, 7 model calls, 3887 prompt and 518 completion tokens, one attempt per task.
<!-- /generated:live-line -->

<!-- generated:live-table -->
| task | verified | steps | model calls | failed tool calls | wall time (s) |
|---|---|---|---|---|---|
| fix an off-by-one loop | yes | 5 | 4 | 0 | 10.0 |
| add missing tests (mutants) | yes | 4 | 3 | 0 | 49.6 |
<!-- /generated:live-table -->

It used an earlier runtime revision and kept no trace; its accounting was
corrected later from its own recorded counters, as its `notes` field explains.

## Jail suite

`python scripts/run_jail_attacks.py --backend local|docker` replays hostile
tool calls against a real registry and sandbox and writes
`results/jail_attack_evidence.json` and
`results/jail_attack_evidence_docker.json`; tests rerun both and require them
to match exactly. Rejected: refused before anything ran. Contained: ran, and
the recorded evidence shows no gain. Residual: achieved its goal as a
documented limitation. Breached: anything else, which fails the tests.

<!-- generated:jail-groups -->
| category | local (rejected / contained / residual) | docker (rejected / contained / residual) |
|---|---|---|
| path escape | 6 / 0 / 0 | 6 / 0 / 0 |
| shell commands | 2 / 0 / 0 | 2 / 0 / 0 |
| malformed calls | 3 / 1 / 0 | 3 / 1 / 0 |
| test command abuse | 8 / 1 / 0 | 8 / 1 / 0 |
| path aliases | 3 / 1 / 0 | 3 / 1 / 0 |
| git metadata | 2 / 1 / 0 | 2 / 1 / 0 |
| links | 2 / 3 / 0 | 2 / 3 / 0 |
| verification tampering | 2 / 1 / 0 | 2 / 1 / 0 |
| resource limits | 1 / 3 / 0 | 1 / 3 / 0 |
| credentials | 0 / 1 / 0 | 0 / 1 / 0 |
| agent-written test code | 0 / 1 / 4 | 0 / 7 / 0 |
| report forging | 0 / 0 / 1 | 0 / 0 / 1 |
<!-- /generated:jail-groups -->

Residuals by backend:

<!-- generated:jail-residuals -->
- **local**: A39 agent-written test code writes outside the workspace; A40 agent-written test code reads a host file outside the workspace; A41 agent-written test code opens a network connection; A42 agent-written test code starts a process that outlives the run (a new session on POSIX, WMI on Windows); A46 code under test forges a passing JUnit report and exits 0 during verification.
- **docker**: A46 code under test forges a passing JUnit report and exits 0 during verification.
<!-- /generated:jail-residuals -->

Report forging happens inside the test process, so no sandbox can prevent it
([ADR 0007](docs/adr/0007-report-forging-is-outside-the-sandbox.md)). The fork
bomb and disk-fill attacks are recorded for Docker only, because their local
outcome depends on the operating system. The last attacks in the list come
from an independent review of this runtime, which also found the issues fixed
in the commits that mention it.

## Honest limitations

- CI has never run on GitHub; the workflows are written and the same gate
  runs locally (`make check`), but the first remote run is still ahead.
- Live pass@k with the native planner has not been measured; the live result
  above is one run of an earlier planner on part of the original task set.
- No sandbox makes a verdict trustworthy against code written to fool it
  (forged reports, objects that compare equal to anything); protected tests,
  mutants, neutral edits and human review of the patch are the defences.
- Without Docker, agent-written test code runs as the host user with the
  host's files and network, and a process that starts its own session (POSIX)
  or is created through WMI (Windows) outlives the tree kill.
- The repo map is Python only (stdlib `ast`); tests that need third-party
  packages need their own sandbox image (`--sandbox-image`).
- Links in a repository are skipped when it is copied into a workspace, so
  code that relies on in-repo links loses them.

## Development

```bash
make setup         # uv sync --locked --all-extras
make check         # the full gate: lint, types, tests, benchmark x2, jail suites, docs check, report
make bench         # regenerate the scripted results
make jail          # regenerate the attack evidence (local, and docker when available)
make report        # rebuild the PDF with latexmk
make sandbox-image # build the Docker sandbox image
make docker        # build the benchmark image
```

Python 3.11 or newer; dependencies are locked in `uv.lock`. The make targets
work from Git Bash on Windows and from Linux.

## Documentation

- [Step 2 design: approaches, decisions, non-goals, threat model](docs/design/step2.md)
- ADRs: [0001 Docker CLI sandbox with a local fallback](docs/adr/0001-docker-cli-sandbox-with-local-fallback.md),
  [0002 read-only workspace and tmpfs-only writes](docs/adr/0002-read-only-workspace-and-tmpfs-only-writes.md),
  [0003 native tool calling with one retry policy](docs/adr/0003-native-tool-calling-with-one-retry-policy.md),
  [0004 stdlib ast for the repo map](docs/adr/0004-stdlib-ast-for-the-repo-map.md),
  [0005 strict all-or-nothing patches](docs/adr/0005-strict-all-or-nothing-patches.md),
  [0006 replay re-executes recorded decisions](docs/adr/0006-replay-re-executes-recorded-decisions.md),
  [0007 report forging is outside the sandbox](docs/adr/0007-report-forging-is-outside-the-sandbox.md),
  [0008 external MCP servers run in the sandbox](docs/adr/0008-external-mcp-servers-run-in-the-sandbox.md)
- [Glossary](docs/GLOSSARY.md), [architecture notes](docs/architecture.md),
  [design FAQ](docs/design_faq.md)
- [Technical report (PDF)](reports/Autonomous_Repository_Engineer.pdf), LaTeX
  source in `reports/`
- Demo site: `demo-site/` replays the committed traces and attack evidence

## Project structure

```
src/repo_engineer/   runtime (agent, state, tools, jail, snapshot, patch, repo_map, context),
                     sandbox/ (docker, local), models/ (provider adapters), native_planner,
                     mcp_server, mcp_tools, trace, replay, cli, benchmark/ (harness, tasks, live, jail attacks)
scripts/             run_benchmark, run_jail_attacks, run_llm_benchmark, sync_docs
tests/               unit, integration, sandbox, MCP, CLI and generated-docs tests
results/             committed benchmark results, traces and attack evidence
docs/                design, ADRs, glossary, architecture, FAQ
reports/             technical report (generated tables under reports/generated/)
demo-site/           static replay site and its Playwright tests
```

## Docker image for the benchmark

```bash
docker build -t repo-engineer .
docker run --rm --network none repo-engineer          # scripted benchmark
docker build --target test -t repo-engineer-test .
docker run --rm --network none repo-engineer-test     # test suite
```

This image is separate from the sandbox image. There is no Docker daemon
inside it, so the runtime uses the local sandbox there, which is why the
commands disable networking.

## License

MIT. See LICENSE.
