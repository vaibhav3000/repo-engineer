"""Agent state and execution trace for the Autonomous Repository Engineer.

The state machine is the safety boundary: the planner (LLM or scripted)
proposes actions, but only the runtime transitions states, validates tool
calls, enforces budgets and decides when the task is verified.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class State(str, Enum):
    INIT = "INIT"
    ANALYZE_REPO = "ANALYZE_REPO"
    PLAN = "PLAN"
    ACT = "ACT"                # executing a tool call proposed by the planner
    OBSERVE = "OBSERVE"        # recording the tool result
    REPLAN = "REPLAN"          # tests failed or an action was rejected
    VERIFY = "VERIFY"          # running the verification suite
    COMPLETE = "COMPLETE"
    HUMAN_REVIEW = "HUMAN_REVIEW"  # policy requested approval that is pending
    FAILED = "FAILED"          # budget exhausted or unrecoverable error


# Legal transitions. Anything else is a programming error, so the machine
# raises instead of drifting silently.
TRANSITIONS: dict[State, set[State]] = {
    State.INIT: {State.ANALYZE_REPO, State.FAILED},
    State.ANALYZE_REPO: {State.PLAN, State.FAILED},
    State.PLAN: {State.ACT, State.VERIFY, State.FAILED},
    State.ACT: {State.OBSERVE, State.HUMAN_REVIEW, State.FAILED},
    State.HUMAN_REVIEW: {State.ACT, State.FAILED},
    State.OBSERVE: {State.PLAN, State.REPLAN, State.VERIFY},
    State.REPLAN: {State.PLAN, State.FAILED},
    State.VERIFY: {State.COMPLETE, State.REPLAN, State.FAILED},
    State.COMPLETE: set(),
    State.FAILED: set(),
}


@dataclass
class ToolCall:
    """A planner-proposed action. The runtime validates it before execution."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """The runtime's record of one tool execution (or rejection)."""

    tool: str
    ok: bool
    output_excerpt: str = ""
    error: str | None = None
    rejected: bool = False       # True when validation/permissions blocked the call
    latency_ms: float = 0.0


@dataclass
class AgentState:
    """Serializable memory of one agent run."""

    task_id: str
    task_description: str
    workspace: str
    state: State = State.INIT
    plan: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    test_results: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hypothesis: str = ""
    files_changed: list[str] = field(default_factory=list)
    steps_used: int = 0
    replans: int = 0
    verified: bool = False
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentState":
        d = dict(d)
        d["state"] = State(d["state"])
        return cls(**d)


class StateMachine:
    """Enforces legal transitions and a step budget."""

    def __init__(self, state: AgentState, max_steps: int = 40, max_replans: int = 4) -> None:
        self.agent_state = state
        self.max_steps = max_steps
        self.max_replans = max_replans

    def transition(self, to: State) -> None:
        current = self.agent_state.state
        if to not in TRANSITIONS[current]:
            raise RuntimeError(f"illegal state transition {current.value} -> {to.value}")
        self.agent_state.state = to

    def budget_exhausted(self) -> bool:
        return self.agent_state.steps_used >= self.max_steps

    def can_replan(self) -> bool:
        return self.agent_state.replans < self.max_replans


def write_trace(state: AgentState, path) -> None:
    """Persist the full execution trace as JSON (one state snapshot at the end)."""
    import json
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
