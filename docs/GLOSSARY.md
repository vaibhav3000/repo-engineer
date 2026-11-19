# repo-engineer

A runtime that lets a planner change a code repository only through validated
tool calls, runs agent-written code in a sandbox, and accepts the work only
when its own verification passes.

## Runs

**Runtime**:
The deterministic loop that owns every decision in a run: state transitions,
budgets, tool execution and the verdict.
_Avoid_: agent (when the loop is meant), orchestrator

**Planner**:
The part that proposes the next tool call, whether a model, a recorded
trajectory or a scripted recipe. It proposes; it never decides.
_Avoid_: agent, brain, policy

**Task**:
A description of the change wanted in one repository, with the command that
verifies it and the paths the agent may not modify.

**Workspace**:
The copy of a repository a run works in. The original repository is never
modified by a run.
_Avoid_: repo (when the copy is meant), sandbox

**Attempt**:
One run of one task with a fresh planner; live benchmarks make several
attempts per task.

**Replan**:
The runtime sending the planner back to planning after a failed test run or a
failed verification, with the reason.

**Recipe**:
The fixed list of tool calls the scripted planner replays for one benchmark
task. A recipe already contains the fix; it measures the runtime, not a model.

## Tools

**Tool call**:
One proposed action: a tool name and its arguments.
_Avoid_: action (in prose), command

**Observation**:
The runtime's record of what one tool call did: success, rejection, exit
status, and the redacted output or error.
_Avoid_: result, response

**Rejection**:
An observation for a tool call that was refused before anything ran, because
it was unknown, malformed, not permitted or outside the jail.

**Permission class**:
What a tool can affect: read only, mutating, or execution. Policy applies per
class.

**Policy**:
Whether calls of a permission class run automatically, need approval, or are
denied.

**External tool**:
A tool published by an MCP server the run mounts, registered under the same
gates as a built-in tool.

**Patch**:
A unified diff the agent submits to change one or more files at once; it is
applied completely or not at all.

**Hunk**:
One contiguous change inside a patch, with the context lines that locate it.

## Containment

**Jail**:
The rules that confine every path a tool call names to the workspace, checked
as text before the filesystem is touched.
_Avoid_: sandbox

**Sandbox**:
The isolation that code the agent wrote runs inside when tests execute.
_Avoid_: jail, container (unless the Docker backend is meant)

**Sandbox backend**:
One way of providing the sandbox: Docker containers, or local process limits
when Docker is unavailable.

**Protected path**:
A file or directory the task forbids the agent to modify; it is restored
before verification.

**Pytest control file**:
A file whose presence changes how pytest collects or configures tests
(conftest.py, pytest.ini and similar); always protected.

**Attack**:
A recorded adversarial tool-call sequence in the jail suite, with the evidence
that shows whether it achieved its goal.

**Rejected / contained / residual / breached**:
The four outcomes of an attack: refused before running; ran but gained
nothing; achieved its goal as a documented limitation; achieved its goal
without being documented (a bug).

## Verification

**Verification**:
The runtime's own test run on a restored workspace; the only way a run can
complete.
_Avoid_: validation (that is checking tool-call arguments)

**Mutant**:
A deliberate one-place bug the verifier seeds into protected code to check
that the agent's new tests detect it.

**Neutral edit**:
A behaviour-preserving change the verifier seeds into protected code to check
that the agent's new tests do not merely pin the source text.

**pass@k**:
The probability that at least one of k attempts at a task succeeds, estimated
from n attempts with c successes.

## Records

**Audit trace**:
The event-by-event record of a run, written as it happens, sufficient to
replay the run.
_Avoid_: log

**Golden file**:
A committed result that a deterministic run must reproduce byte for byte.

**Replay**:
Re-executing the planner decisions recorded in an audit trace against the
same starting workspace, without a model, and comparing the outcome with the
record.

**Repo map**:
A compact, ranked outline of a repository's files and the symbols they
define, sized to fit a budget.

**Symbol**:
A class, function or method definition, located by file and line range.

**Compaction**:
Replacing old tool results in a planner's conversation with short stubs so
the conversation fits its context budget.
