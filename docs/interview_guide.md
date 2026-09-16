# repo-engineer: Interview Defense Guide

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
verification requires re-running the suite, budgets force termination.

**Why not just use LangChain / an off-the-shelf agent framework?**
The interesting engineering here is exactly what frameworks hide: tool schemas,
rejection handling, the workspace jail, permission policy, verification and
budget semantics. Building them took ~700 lines and made every behavior
inspectable and testable. A framework would also have made the committed
benchmark results depend on a fast-moving third-party API.

## Tools and safety

**What happens on a hallucinated tool call?**
`ToolRegistry.execute` returns a rejection Observation (unknown tool, schema
violation, unknown argument, wrong type) that is recorded in the trace and fed
back to the planner as history. The runtime never crashes on planner output;
the benchmark results show zero failed tool calls because the scripted planner
emits valid calls, and tests inject malformed calls to prove rejection.

**How do you prevent arbitrary shell execution?**
There is no shell tool. `run_tests` is the only EXECUTION tool: it allowlists
command prefixes (`python -m pytest`, `python -m unittest`), splits the command
(no `shell=True`), sets a timeout, and captures output. `curl ... | sh` is
rejected by the allowlist (tested).

**Path traversal / prompt injection?**
Every path argument passes through `jailed_path`: resolve, then require the
workspace to be a parent. `../escape`, absolute paths and drive-letter paths
are rejected (tested). File contents retrieved by tools are data returned as
observations; the runtime never executes or follows instructions inside them,
and the scripted planner ignores them by construction. For an LLM planner this
is a documented residual risk: content-bearing observations still pass through
the model's context, so the model must be prompted to treat them as data.

**What does ASK policy do in a headless run?**
ASK requires `ask_callback` approval; with no callback the call is denied
(fail closed). Tests cover both directions.

## Behavior

**What if the planner claims the bug is fixed but tests still fail?**
Its claim is irrelevant. COMPLETE requires the VERIFY observation to end with
`exit=0`. A failing verification transitions to REPLAN; after the replan
budget, FAILED. `test_budget_exhaustion_fails` covers the exhausted path.

**What if the model repeatedly edits the wrong file?**
The trace records every edit (path + args) and the state accumulates
`files_changed`. Budgets bound the damage. There is no rollback today; that is
a stated limitation (the trace is the review artifact).

**How does the system know it is done?**
Only when the verification suite is green in the VERIFY state. The planner
returning None (no more actions) is necessary but not sufficient.

**What prevents infinite loops?**
Two budgets: `max_steps` (every tool execution counts) and `max_replans`
(failing verify can only trigger so many replans). Either triggers FAILED.

**Why is the scripted planner not "cheating"?**
The benchmark measures the runtime: schema validation, permissions, jail, state
transitions, verification and trace capture. Using deterministic recipes keeps
results reproducible at zero API cost. The honest claim is "the harness and
runtime work and are verified", not "an LLM solved these tasks". The
`LLMPlanner` stub marks the integration point.

## Design details

**Why apply_edit instead of full-file writes?**
Exact-match single replacement is unambiguous: zero matches and multiple
matches are both errors (tested), so a planner cannot silently clobber a file
it misread. New files use `write_file`, which refuses to overwrite.

**Why is AgentState serializable?**
Runs are auditable artifacts: the full state (plan, actions, observations,
test results, budgets) round-trips through JSON, enabling offline review and
later regression comparisons of agent behavior.

**What would 10x the context length do?**
`list_tree` and `search` are bounded/truncated by design. The planner history
is the unbounded part today; a production LLMPlanner would need history
summarization. Stated as a limitation.

## Deterministic vs real-LLM mode

**Why keep a scripted planner at all when a real LLM can drive the agent?**
Because they measure different things. The scripted benchmark is a regression
harness: byte-stable, free, offline, and it isolates the runtime machinery.
The LLM benchmark (`results/llm_benchmark_results.json`, gemini-2.5-flash)
demonstrates end-to-end capability but varies with the provider. Confusing the
two is how projects end up claiming un-reproducible numbers.

**What changed when the real LLM was wired in?**
Almost nothing in the runtime. `LLMPlanner` packages the task, tool schemas
and the last 10 history entries into a prompt, parses a JSON reply, and
returns a ToolCall - which flows through exactly the same validation, jail,
permission policy and verification as the scripted proposals. That is the
design claim proven in practice: the safety boundary does not care who
proposes the action.

**What did the live run demonstrate beyond the scripted one?**
Recovery from provider failures: the gemini run hit HTTP 429 twice mid-task;
the runtime counted them as recoverable planner errors, the provider's backoff
absorbed them, and the run still ended in COMPLETE with a green suite
(recorded in the committed results, `planner_errors` in the trace). The model
also chose its own edits (it wrote a test file with different cases than the
scripted recipe) - capability, not just machinery.

**What are the LLM-mode's limits?**
Sample size: two tasks, once each - a demonstration, not a leaderboard.
Nondeterminism: temperature 0 reduces but does not eliminate variance, and the
provider can change the model under the same name. Cost/latency: every step is
an API round trip. None of the safety properties depend on the model behaving.

**How do you keep the API key out of the repo?**
The key is read from an environment variable at call time (`GEMINI_API_KEY` by
default, configurable via `REPO_ENGINEER_LLM_KEY_ENV`). Traces and results
record the model name and token counts, never credentials.
