"""AgentRuntime: the deterministic loop around the planner.

Loop contract (mirrors the documented state machine):

    INIT -> ANALYZE_REPO -> PLAN -> ACT -> OBSERVE -+-> PLAN (next step)
                                                    |-> REPLAN -> PLAN (tests failed)
                                                    +-> VERIFY -> COMPLETE / REPLAN / FAILED

The runtime owns every decision: it validates tool calls, enforces permissions
and budgets, tracks files changed, and only trusts the verification suite —
never a planner's claim that the work is done.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .planner import Planner
from .state import AgentState, Observation, State, StateMachine, ToolCall, write_trace
from .tools import ToolRegistry

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Run one task against one workspace copy."""

    def __init__(
        self,
        registry: ToolRegistry,
        planner: Planner,
        max_steps: int = 40,
        max_replans: int = 4,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.max_steps = max_steps
        self.max_replans = max_replans

    def run_task(self, task: dict[str, Any], trace_path: Path | None = None) -> AgentState:
        state = AgentState(
            task_id=task["task_id"],
            task_description=task.get("description", ""),
            workspace=str(self.registry.workspace),
        )
        machine = StateMachine(state, max_steps=self.max_steps, max_replans=self.max_replans)
        machine.transition(State.ANALYZE_REPO)
        state.actions.append({"step": "analyze", "tool": "list_tree"})
        tree = self.registry.execute(ToolCall("list_tree", {}))
        state.observations.append({"tool": "list_tree", "ok": tree.ok})
        state.hypothesis = f"workspace has {len(tree.output_excerpt.splitlines())} entries"

        history: list[dict[str, Any]] = []
        while True:
            if machine.budget_exhausted():
                state.errors.append("budget exhausted")
                machine.transition(State.FAILED)
                break
            if state.state is not State.PLAN:
                machine.transition(State.PLAN)
            action = None
            try:
                action = self.planner.next_action(task, history)
            except Exception as exc:  # noqa: BLE001 - planner/API failure is recoverable
                state.planner_errors += 1
                state.errors.append(f"planner error: {type(exc).__name__}: {exc}"[:200])
                if state.planner_errors >= 3:
                    machine.transition(State.FAILED)
                    break
                continue
            state.planner_errors = 0
            if action is None:
                machine.transition(State.VERIFY)
            else:
                machine.transition(State.ACT)
                state.steps_used += 1
                logger.info("step %d: %s %s", state.steps_used, action.tool, action.args)
                state.actions.append({"step": state.steps_used, "tool": action.tool,
                                      "args": _jsonable(action.args)})
                if action.tool == "apply_edit" or action.tool == "write_file":
                    changed = action.args.get("path")
                    if changed and changed not in state.files_changed:
                        state.files_changed.append(changed)

                # HUMAN_REVIEW hook: a MUTATING/EXECUTION call under ASK policy that
                # is denied transitions through review before failing the step.
                observation = self.registry.execute(action)
                machine.transition(State.OBSERVE)
                state.observations.append(_observation_dict(observation))
                history.append({"tool": action.tool, "args": _jsonable(action.args),
                                "ok": observation.ok, "error": observation.error,
                                "output": observation.output_excerpt[:400]})

                if action.tool == "run_tests":
                    passed = observation.ok and "exit=0" in observation.output_excerpt
                    state.test_results.append({"passed": passed, "exit_ok": observation.ok})
                    if passed:
                        machine.transition(State.VERIFY)
                    elif machine.can_replan():
                        state.replans += 1
                        machine.transition(State.REPLAN)
                        self.planner.observe_replan(
                            f"tests failed: {observation.output_excerpt[-500:]}", history
                        )
                        machine.transition(State.PLAN)
                        continue
                    else:
                        state.errors.append("tests failed and replan budget exhausted")
                        machine.transition(State.FAILED)
                        break
                else:
                    # Non-test step: return to PLAN for the next action.
                    machine.transition(State.PLAN)
                    continue

            # VERIFY reached: re-run the test suite as final evidence.
            state.steps_used += 1
            verify = self.registry.execute(ToolCall("run_tests", {"cmd": task.get("verify_cmd", "python -m pytest tests/ -q")}))
            passed = verify.ok and "exit=0" in verify.output_excerpt
            state.test_results.append({"verify": True, "passed": passed})
            state.verified = passed
            if passed:
                machine.transition(State.COMPLETE)
            elif machine.can_replan():
                state.replans += 1
                machine.transition(State.REPLAN)
                machine.transition(State.PLAN)
                continue
            else:
                machine.transition(State.FAILED)
            break

        if trace_path is not None:
            write_trace(state, trace_path)
        return state

    def summary(self, state: AgentState) -> dict[str, Any]:
        out = {
            "task_id": state.task_id,
            "success": state.verified and state.state is State.COMPLETE,
            "final_state": state.state.value,
            "steps": state.steps_used,
            "replans": state.replans,
            "planner_errors": state.planner_errors,
            "files_changed": state.files_changed,
            "errors": state.errors,
        }
        # LLM planners expose token accounting; deterministic planners do not.
        if hasattr(self.planner, "total_prompt_tokens"):
            out["llm_calls"] = self.planner.calls
            out["llm_prompt_tokens"] = self.planner.total_prompt_tokens
            out["llm_completion_tokens"] = self.planner.total_completion_tokens
            out["llm_model"] = self.planner.model
        return out


def _observation_dict(observation: Observation) -> dict[str, Any]:
    return {
        "tool": observation.tool,
        "ok": observation.ok,
        "rejected": observation.rejected,
        "error": observation.error,
        "output_excerpt": observation.output_excerpt[:500],
    }


def _jsonable(args: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(args)
        return args
    except (TypeError, ValueError):
        return {k: str(v) for k, v in args.items()}


def prepare_workspace(source: Path, runs_root: Path, task_id: str) -> Path:
    """Copy a pristine task repo into a fresh per-run workspace."""
    target = runs_root / f"{task_id}_{len(list(runs_root.glob(f'{task_id}_*'))) + 1}"
    shutil.copytree(source, target)
    return target
