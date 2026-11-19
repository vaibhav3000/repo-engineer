# repo-engineer: Design FAQ

Answers reflect the actual implementation in `src/repo_engineer/`. Where the
honest answer is a limitation, the limitation is stated.

## Concepts

**What is an agent (vs a workflow)?**
A workflow is a fixed graph of steps written by a developer. An agent decides
its own next action at runtime from observations. This project is deliberately
in between: a bounded state machine (workflow skeleton) whose ACTION content is
chosen by a planner (agent). That split keeps the safety properties of a
workflow while allowing model-driven behavior.

**Why wrap an LLM in a state machine?**
Because the LLM should propose, not dispose. Every planner output is validated
(`ToolSpec.validate`), permission-checked, and jailed before execution. The
state machine also encodes the control rules: tests failing triggers REPLAN,
verification is a run the runtime performs itself, budgets force termination.

**Why not just use LangChain / an off-the-shelf agent framework?**
The interesting engineering here is exactly what frameworks hide: tool schemas,
rejection handling, the workspace jail, permission policy, verification and
budget semantics. Building them by hand (about 3,200 lines of Python under
`src/repo_engineer`, not counting the task fixtures) made every behavior
inspectable and testable. A framework would also have made the committed
benchmark results depend on a fast-moving third-party API.

## Tools and safety

**What happens on a hallucinated tool call?**
`ToolRegistry.execute` returns a rejection Observation (unknown tool, schema
violation, unknown argument, wrong type, out-of-range value, oversized string)
that is recorded in the trace and fed back to the planner as history. The
runtime never crashes on planner output. The scripted benchmark shows zero
failed tool calls because its recipes are valid; the rejection paths are
exercised by the tests and by the 39 recorded adversarial calls.

**How do you prevent arbitrary shell execution?**
There is no shell tool. `run_tests` is the only EXECUTION tool, and it does
not take a free-form command: the command must be exactly `python -m pytest`,
followed only by flags from a short allowlist and test paths that exist
inside the workspace. Flags such as `--junitxml`, `--basetemp`, `-p`, `-c`
and `-o`, argument files (`@args.txt`) and shell syntax are rejected before
any process starts. The runtime then runs its own interpreter with `-P` (so a
planted `pytest.py` cannot shadow pytest), disables the cache plugin, pins
rootdir and confcutdir to the workspace, passes an allowlisted environment,
and kills the whole process tree at the deadline.

**So can the agent still run arbitrary code?**
Yes, and this is the main residual risk. The tests `run_tests` executes can
include files the agent wrote, and that code runs as the host user with the
host's filesystem and network access. The recorded attack evidence includes
exactly this case and labels it as the residual. Containing it needs an
OS-level sandbox (a per-call container or similar), which is planned work.

**Path traversal?**
Every declared path argument is jailed centrally in `execute()`, before the
tool runs. The path is first checked as text, without touching the
filesystem: absolute, rooted, UNC, device and drive paths, `..` above the
root, `:` in a component (NTFS streams), trailing dots or spaces, reserved
device names and `.git` are all refused. Only then is it joined to the
workspace, and the result must be its own real path, which refuses symbolic
links and junctions. `search` and `list_tree` skip links as well, and writes
re-check the target right before opening it.

**Prompt injection via file content?**
File contents retrieved by tools come back as observations, and the runtime
never executes or follows instructions in them. For an LLM planner they still
reach the model's context verbatim (apart from redaction of credentials and
absolute host paths), so the model must treat them as data. That is a
residual risk; the runtime bounds what an injected instruction can achieve,
since every resulting proposal goes through the same gates.

**What does ASK policy do in a headless run?**
ASK requires `ask_callback` approval; with no callback the call is denied
(fail closed). Only an explicit `True` approves, and an approver that raises
counts as a denial. Note that the default policy is AUTO for every
permission class; ASK and DENY have to be configured.

## Behavior

**What if the planner claims the bug is fixed but tests still fail?**
Its claim is irrelevant. In VERIFY the runtime restores the files the task
protects (the existing tests, for a bug fix) and every pytest control file,
then runs the suite itself, with a fresh bytecode cache so that bytecode
planted in the workspace is never read. It accepts the run only if the exit
status is 0 and pytest's JUnit report, written outside the workspace, shows
at least one passing test and no failures. A failing verification goes to REPLAN; after the
replan budget, FAILED. A planner-issued test run that fails with no replans
left ends OBSERVE -> REPLAN -> FAILED.

**Could the agent write a trivial test to finish the add-a-test task?**
Not a trivial one. For that task the code under test is protected and the
runtime applies three seeded mutants to it during VERIFY; each one must make
the new tests fail, so `assert True` fails. It also applies two
behaviour-preserving edits, which must leave the tests passing, so a test
that only pins the file's hash fails too. A test written to inspect the
source text for specific tokens could still pass both checks; that takes
deliberate gaming and is a documented residual.

**What if the model repeatedly edits the wrong file?**
The trace records every proposed edit, and `files_changed` lists the files
that successful edits actually changed, by their workspace path. Budgets bound
the damage. There is no rollback today; that is a stated limitation (the
trace is the review artifact).

**How does the system know it is done?**
Only when its own verification run passes. The planner returning `done` (or
None) is necessary but not sufficient.

**What prevents infinite loops?**
Three budgets: `max_steps` (every tool execution and every VERIFY counts),
`max_replans` (failing test runs and failed verifications can only trigger
so many replans) and three consecutive planner errors. Any of them ends the
run in FAILED.

**Why is the scripted planner not "cheating"?**
It would be if it were presented as problem solving, so it is not. The
recipes contain the fixes; the scripted benchmark measures the runtime:
schema validation, state transitions, verification and trace capture,
reproducibly and at zero API cost. The honest claim is "the harness and
runtime work and are verified", not "an LLM solved these tasks". The live
`LLMPlanner` run is the problem-solving evidence, and it is small.

## Design details

**Why apply_edit instead of full-file writes?**
Exact-match single replacement is unambiguous: zero matches and multiple
matches are both errors (tested), so a planner cannot silently clobber a file
it misread. New files use `write_file`, which refuses to overwrite.

**Why is AgentState serializable?**
Runs are auditable artifacts: the full state (actions, observations, test
results, budgets, files changed, model usage) round-trips through JSON,
enabling offline review and regression comparisons of agent behavior. The
committed benchmark traces are golden files that regenerate byte for byte.

**What would 10x the context length do?**
`list_tree` and `search` are bounded and truncated by design, and
`LLMPlanner` sends only the last ten history entries, each capped at 1,500
characters (the end of test output, where pytest reports failures). That
window drops older context rather than growing; a production planner would
summarize instead. Stated as a limitation.

## Deterministic vs real-LLM mode

**Why keep a scripted planner at all when a real LLM can drive the agent?**
Because they measure different things. The scripted benchmark is a regression
harness: byte-stable, free, offline, and it isolates the runtime machinery.
The LLM benchmark (`results/llm_benchmark_results.json`, gemini-2.5-flash)
demonstrates end-to-end capability but varies with the provider. Confusing the
two is how projects end up claiming un-reproducible numbers.

**What changed when the real LLM was wired in?**
Almost nothing in the runtime. `LLMPlanner` packages the task, the repository
listing, the tool schemas, any replan reason and the last 10 history entries
into a prompt, parses the first JSON object in the reply, and returns a
ToolCall, which flows through exactly the same validation, jail, permission
policy and verification as the scripted proposals.

**What did the live run demonstrate beyond the scripted one?**
Recovery from provider failures: the gemini run hit HTTP 429 twice in the
second task. Each error surfaced after the provider client's own retries were
exhausted; the runtime counted them as recoverable planner errors
(`planner_errors` is 2 in the corrected results) and the run still ended in
COMPLETE with a green suite. The model also chose its own edits (it wrote a
test file with different cases than the scripted recipe): capability, not
just machinery.

**What are the LLM-mode's limits?**
Sample size: two tasks, once each, recorded with an earlier revision of the
runtime and without a trace; it is a demonstration, not a leaderboard. The
live script now runs k attempts per task and keeps every run with its traces,
but it has not been re-run. Nondeterminism: temperature 0 reduces but does not
eliminate variance, and the provider can change the model under the same
name. Cost and latency: every step is an API round trip.

**How do you keep the API key out of the repo and the traces?**
The key is read from an environment variable at call time (`GEMINI_API_KEY` by
default, configurable via `REPO_ENGINEER_LLM_KEY_ENV`), and `.env` files are
ignored by git and Docker. Test processes get an allowlisted environment, so
the key is not visible to agent-written test code, and every observation is
redacted (credential values and absolute host paths) before it reaches the
trace or the next prompt. Traces and results record the model name and token
counts, never credentials.
