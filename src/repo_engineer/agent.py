"""AgentRuntime: the deterministic loop around the planner.

Loop contract (mirrors the documented state machine):

    INIT -> ANALYZE_REPO -> PLAN -> ACT -> OBSERVE -+-> PLAN (next step)
                                                    |-> REPLAN -> PLAN (tests failed)
                                                    |-> REPLAN -> FAILED (no replans left)
                                                    +-> VERIFY -> COMPLETE / REPLAN / FAILED

The runtime owns every decision: it validates tool calls, enforces permissions
and budgets, tracks files changed, and trusts only its own verification run,
never a planner's claim that the work is done.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import Any

from . import __version__
from .planner import Planner, PlannerUsage
from .state import AgentState, Observation, State, StateMachine, ToolCall, write_trace
from .tools import ToolRegistry, Verification
from .trace import EventSink, NullSink, jsonable, workspace_digest

logger = logging.getLogger(__name__)

# Consecutive planner failures (provider errors, malformed replies) that end a run.
MAX_CONSECUTIVE_PLANNER_ERRORS = 3
# How much of each observation goes into the planner's history. For run_tests
# the end of the output is kept, because pytest prints failures and the
# summary last; for other tools the beginning is kept.
HISTORY_OUTPUT_CHARS = 1500


class AgentRuntime:
    """Run one task against one workspace copy."""

    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        max_steps: int = 40,
        max_replans: int = 4,
        *,
        events: EventSink | None = None,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.max_steps = max_steps
        self.max_replans = max_replans
        self.events: EventSink = events or NullSink()

    def run_task(self, task: dict[str, Any], trace_path: Path | None = None) -> AgentState:
        state = AgentState(
            task_id=task["task_id"],
            task_description=task.get("description", ""),
            # The directory name only: absolute host paths stay out of traces.
            workspace=self.registry.workspace.name,
        )
        usage_at_start = self.planner.usage()
        events = self.events
        events.emit(
            "run_start",
            version=__version__,
            task={k: v for k, v in task.items() if k != "path"},
            planner=self.planner.name,
            model=usage_at_start.model if usage_at_start is not None else None,
            sandbox=self.registry.sandbox.name,
            workspace=state.workspace,
            workspace_digest=workspace_digest(self.registry.workspace),
            max_steps=self.max_steps,
            max_replans=self.max_replans,
        )
        machine = StateMachine(
            state,
            max_steps=self.max_steps,
            max_replans=self.max_replans,
            on_transition=lambda old, new: events.emit(
                "state", **{"from": old.value, "to": new.value}
            ),
        )
        self.registry.protect(task.get("protected_paths", []))
        self.planner.prepare(self.registry.tool_schemas(), events)
        machine.transition(State.ANALYZE_REPO)
        state.actions.append({"step": "analyze", "tool": "list_tree"})
        tree = self.registry.execute(ToolCall("list_tree", {}))
        state.observations.append({"tool": "list_tree", "ok": tree.ok})
        state.hypothesis = f"workspace has {len(tree.output_excerpt.splitlines())} entries"
        # The planner sees the task together with the runtime's own listing
        # and a map of the code (not traced: it is derived from the workspace).
        planner_task = {**task, "repo_tree": tree.output_excerpt}
        try:
            planner_task["repo_map"] = self.registry.repo_map_text()
        except Exception as exc:
            # The map is an aid for the planner; a run never fails for want of it.
            logger.warning("no repo map for the planner: %s: %s", type(exc).__name__, exc)

        history: list[dict[str, Any]] = []
        consecutive_planner_errors = 0
        while True:
            if machine.budget_exhausted():
                state.errors.append("budget exhausted")
                machine.transition(State.FAILED)
                break
            if state.state is not State.PLAN:
                machine.transition(State.PLAN)
            action = None
            try:
                action = self.planner.next_action(planner_task, history)
            except Exception as exc:
                state.planner_errors += 1
                consecutive_planner_errors += 1
                message = self.registry.redact(f"planner error: {type(exc).__name__}: {exc}")
                state.errors.append(message[:200])
                events.emit("planner_decision", decision="error", error=message[:500])
                if consecutive_planner_errors >= MAX_CONSECUTIVE_PLANNER_ERRORS:
                    machine.transition(State.FAILED)
                    break
                continue
            consecutive_planner_errors = 0
            if action is None:
                events.emit("planner_decision", decision="done")
            else:
                events.emit(
                    "planner_decision",
                    decision="call",
                    tool=action.tool,
                    args=jsonable(action.args),
                )
            if action is None:
                machine.transition(State.VERIFY)
            else:
                machine.transition(State.ACT)
                state.steps_used += 1
                logger.info("step %d: %s %s", state.steps_used, action.tool, action.args)
                state.actions.append(
                    {"step": state.steps_used, "tool": action.tool, "args": _jsonable(action.args)}
                )

                # A MUTATING/EXECUTION call under ASK policy that is denied
                # comes back as a rejected Observation from the registry
                # (synchronous approval via ask_callback), recorded like any
                # other rejection.
                observation = self.registry.execute(action)
                events.emit(
                    "tool_call",
                    step=state.steps_used,
                    tool=action.tool,
                    args=jsonable(action.args),
                    observation=_observation_dict(observation)
                    | {"files_written": list(observation.files_written)},
                    latency_ms=round(observation.latency_ms, 1),
                )
                machine.transition(State.OBSERVE)
                state.observations.append(_observation_dict(observation))
                history.append(_history_entry(action.tool, action.args, observation))
                for changed in observation.files_written if observation.ok else ():
                    if changed not in state.files_changed:
                        state.files_changed.append(changed)

                if action.tool == "run_tests":
                    passed = observation.ok and observation.exit_code == 0
                    state.test_results.append(
                        {
                            "passed": passed,
                            "exit_ok": observation.ok,
                            "exit_code": observation.exit_code,
                        }
                    )
                    if passed:
                        machine.transition(State.VERIFY)
                    elif machine.can_replan():
                        state.replans += 1
                        machine.transition(State.REPLAN)
                        self.planner.observe_replan(
                            f"tests failed (exit {observation.exit_code}):\n"
                            + _tail(observation.output_excerpt or observation.error or ""),
                            history,
                        )
                        machine.transition(State.PLAN)
                        continue
                    else:
                        # OBSERVE has no edge to FAILED: the run ends through
                        # REPLAN, which finds no budget left.
                        state.errors.append("tests failed and replan budget exhausted")
                        machine.transition(State.REPLAN)
                        machine.transition(State.FAILED)
                        break
                else:
                    # Non-test step: return to PLAN for the next action.
                    machine.transition(State.PLAN)
                    continue

            # VERIFY reached: the runtime restores protected files and re-runs
            # the suite itself; only its evidence can open COMPLETE.
            state.steps_used += 1
            verify_cmd = task.get("verify_cmd", "python -m pytest tests/ -q")
            mutants = task.get("mutants", [])
            verify_started = time.perf_counter()
            try:
                result = self.registry.verify(verify_cmd, mutants, task.get("neutral_edits", []))
            except Exception as exc:
                # A failure inside the verifier is a failed verification: it
                # must end the run cleanly with a trace, never crash it.
                error = self.registry.redact(f"verification error: {type(exc).__name__}: {exc}")
                result = Verification(
                    passed=False,
                    observation=Observation(tool="run_tests", ok=False, error=error),
                    reason=error,
                    restored=(),
                    mutants_total=len(mutants),
                    mutants_killed=0,
                )
            state.actions.append(
                {"step": "verify", "tool": "run_tests", "args": {"cmd": verify_cmd}}
            )
            state.observations.append(_observation_dict(result.observation))
            history.append(
                _history_entry("run_tests", {"cmd": verify_cmd}, result.observation, verify=True)
            )
            state.test_results.append(
                {
                    "verify": True,
                    "passed": result.passed,
                    "exit_code": result.observation.exit_code,
                    "restored": list(result.restored),
                    "mutants_total": result.mutants_total,
                    "mutants_killed": result.mutants_killed,
                    "neutral_edits_total": result.neutral_edits_total,
                    "neutral_edits_passed": result.neutral_edits_passed,
                }
            )
            events.emit(
                "verify",
                cmd=verify_cmd,
                passed=result.passed,
                reason=result.reason,
                observation=_observation_dict(result.observation),
                restored=list(result.restored),
                mutants_total=result.mutants_total,
                mutants_killed=result.mutants_killed,
                neutral_edits_total=result.neutral_edits_total,
                neutral_edits_passed=result.neutral_edits_passed,
                latency_ms=round((time.perf_counter() - verify_started) * 1000, 1),
            )
            if not result.passed:
                state.errors.append(f"verification failed: {result.reason}")
            state.verified = result.passed
            if result.passed:
                machine.transition(State.COMPLETE)
            elif machine.can_replan():
                state.replans += 1
                machine.transition(State.REPLAN)
                self.planner.observe_replan(
                    f"verification failed: {result.reason}\n"
                    + _tail(result.observation.output_excerpt or result.observation.error or ""),
                    history,
                )
                machine.transition(State.PLAN)
                continue
            else:
                machine.transition(State.FAILED)
            break

        state.llm_usage = _usage_delta(usage_at_start, self.planner.usage())
        events.emit(
            "run_end",
            final_state=state.state.value,
            verified=state.verified,
            steps=state.steps_used,
            replans=state.replans,
            planner_errors=state.planner_errors,
            files_changed=list(state.files_changed),
            errors=list(state.errors),
            usage=state.llm_usage,
        )
        if trace_path is not None:
            write_trace(state, trace_path)
        return state

    def summary(self, state: AgentState) -> dict[str, Any]:
        out: dict[str, Any] = {
            "task_id": state.task_id,
            "success": state.verified and state.state is State.COMPLETE,
            "final_state": state.state.value,
            "steps": state.steps_used,
            "replans": state.replans,
            "planner_errors": state.planner_errors,
            "files_changed": state.files_changed,
            "errors": state.errors,
        }
        # Model-backed planners report usage for this run only; deterministic
        # planners report none.
        if state.llm_usage is not None:
            out["llm_calls"] = state.llm_usage["calls"]
            out["llm_prompt_tokens"] = state.llm_usage["prompt_tokens"]
            out["llm_completion_tokens"] = state.llm_usage["completion_tokens"]
            out["llm_model"] = state.llm_usage["model"]
            if "cost_usd" in state.llm_usage:
                out["llm_cost_usd"] = state.llm_usage["cost_usd"]
        return out


def _usage_delta(start: PlannerUsage | None, end: PlannerUsage | None) -> dict[str, Any] | None:
    """Usage between two snapshots of a planner's cumulative counters."""
    if end is None:
        return None
    calls, prompt, completion = (
        (0, 0, 0)
        if start is None
        else (
            start.calls,
            start.prompt_tokens,
            start.completion_tokens,
        )
    )
    delta: dict[str, Any] = {
        "model": end.model,
        "calls": end.calls - calls,
        "prompt_tokens": end.prompt_tokens - prompt,
        "completion_tokens": end.completion_tokens - completion,
    }
    if end.cost_usd is not None:
        before = start.cost_usd if start is not None and start.cost_usd is not None else 0.0
        delta["cost_usd"] = round(end.cost_usd - before, 6)
    return delta


def _tail(text: str, limit: int = HISTORY_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[len(text) - limit :]


def _history_entry(
    tool: str, args: dict[str, Any], observation: Observation, *, verify: bool = False
) -> dict[str, Any]:
    """What the planner sees about one step (already redacted by the registry)."""
    excerpt = observation.output_excerpt
    output = _tail(excerpt) if tool == "run_tests" else excerpt[:HISTORY_OUTPUT_CHARS]
    entry: dict[str, Any] = {
        "tool": tool,
        "args": _jsonable(args),
        "ok": observation.ok,
        "error": observation.error,
        "output": output,
    }
    if observation.exit_code is not None:
        entry["exit_code"] = observation.exit_code
    if verify:
        entry["verify"] = True
    return entry


def _observation_dict(observation: Observation) -> dict[str, Any]:
    return {
        "tool": observation.tool,
        "ok": observation.ok,
        "rejected": observation.rejected,
        "error": observation.error,
        "exit_code": observation.exit_code,
        "output_excerpt": observation.output_excerpt,
    }


def _jsonable(args: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(args)
        return args
    except (TypeError, ValueError):
        return {k: str(v) for k, v in args.items()}


def prepare_workspace(source: Path, runs_root: Path, task_id: str) -> Path:
    """Copy a pristine task repo into a fresh workspace runs_root/<task_id>_<n>.

    n is one more than the highest existing workspace number for the task, so
    trace files next to the workspaces and deleted older runs do not matter.
    Bytecode and pytest caches in the source are not copied.
    """
    runs_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"{re.escape(task_id)}_([0-9]+)")
    taken = [
        int(match.group(1))
        for entry in runs_root.iterdir()
        if entry.is_dir() and (match := pattern.fullmatch(entry.name))
    ]
    number = max(taken, default=0) + 1
    while True:
        target = runs_root / f"{task_id}_{number}"
        try:
            target.mkdir()
        except FileExistsError:
            number += 1  # created concurrently; take the next number
            continue
        shutil.copytree(
            source,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        return target
