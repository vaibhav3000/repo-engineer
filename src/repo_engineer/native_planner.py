"""A planner that drives a model through native function calling.

The model is offered the registry's tools (their schemas come from the same
ToolSpec fields the registry enforces) plus a planner-owned `finish` tool,
and every reply is expected to be a tool call. The runtime executes one call
per step; the planner answers every call id with the observation the runtime
recorded, so the conversation stays valid for every provider:

- several calls in one reply are handed to the runtime in order, and the
  model is asked again only when all of them have results;
- `finish` makes the planner return None, which sends the runtime to VERIFY;
  if verification fails, the finish call is answered with the reason;
- a call whose arguments are not a JSON object is answered with an error
  and raised as a ProviderError, which the runtime counts as a planner error;
- a reply without any tool call gets one reminder, then ends planning.

The model proposes; the runtime still validates, permission-checks, jails
and sandboxes every call, and only its own verification completes a task.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from .context import compact
from .models import (
    Message,
    ModelAdapter,
    ModelToolCall,
    ProviderError,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from .planner import Planner, PlannerUsage
from .schema import ToolSchema
from .state import ToolCall

FINISH_TOOL = "finish"
FINISH_SCHEMA = ToolSchema(
    FINISH_TOOL,
    "Call this when the task is complete. The runtime then runs the verification "
    "suite; the task counts as done only if verification passes.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "One sentence on what you changed."}
        },
        "required": [],
        "additionalProperties": False,
    },
)
SYSTEM_PROMPT = (
    "You are an autonomous software engineering agent working in a repository through "
    "tool calls. Locate the relevant code, make the smallest correct change, and check it "
    "with run_tests before calling finish. Paths are relative to the repository root. "
    "Every call is validated and may be rejected; read the error and adjust."
)
NUDGE = f"Reply with a tool call. When the task is complete, call {FINISH_TOOL}."
# Estimated tokens (characters / 4) the conversation may use before old tool
# results are compacted; well inside every supported model's context window.
DEFAULT_CONTEXT_BUDGET_TOKENS = 100_000


class NativeToolPlanner(Planner):
    """Planner backed by a ModelAdapter using the provider's native tool calling."""

    name = "native"

    def __init__(
        self,
        adapter: ModelAdapter,
        *,
        max_nudges: int = 1,
        context_budget_tokens: int = DEFAULT_CONTEXT_BUDGET_TOKENS,
    ) -> None:
        self.adapter = adapter
        self.max_nudges = max_nudges
        self.context_budget_tokens = context_budget_tokens
        self.compactions = 0  # tool results stubbed out to fit the budget, over all runs
        self.model = adapter.model
        self.calls = 0
        self.total_usage = Usage()
        self._tools: list[ToolSchema] = [FINISH_SCHEMA]
        self._reset()

    def _reset(self) -> None:
        self._messages: list[Message] = []
        self._queue: deque[ModelToolCall] = deque()  # calls from the last reply not yet handed out
        self._awaiting: deque[ModelToolCall] = deque()  # handed out, result not yet seen
        self._finish: ModelToolCall | None = None  # finish call waiting for the verdict
        self._seen = 0  # history entries already turned into tool results
        self._replan_reason = ""
        self._nudges = 0

    # ------------------------------------------------------------- Planner

    def prepare(self, tools: Sequence[ToolSchema]) -> None:
        self._tools = [*tools, FINISH_SCHEMA]
        self._reset()

    def usage(self) -> PlannerUsage:
        return PlannerUsage(
            model=self.model,
            calls=self.calls,
            prompt_tokens=self.total_usage.input_tokens,
            completion_tokens=self.total_usage.output_tokens,
        )

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:
        self._replan_reason = reason

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        if not self._messages:
            self._messages.append(UserMessage(_task_text(task)))
        self._collect(history)
        while True:
            if self._queue:
                return self._hand_out(self._queue.popleft())
            if self._awaiting:
                raise RuntimeError("the runtime did not report a result for every tool call")
            self._messages, stubbed = compact(self._messages, self.context_budget_tokens)
            self.compactions += stubbed
            reply = self.adapter.complete(SYSTEM_PROMPT, self._messages, self._tools)
            self.calls += 1
            self.total_usage = _add(self.total_usage, reply.usage)
            self._messages.append(reply.to_message())
            if reply.tool_calls:
                self._queue.extend(reply.tool_calls)
                self._nudges = 0
                continue
            if self._nudges < self.max_nudges:
                self._nudges += 1
                self._messages.append(UserMessage(NUDGE))
                continue
            self._nudges = 0
            return None

    # ------------------------------------------------------------- internals

    def _collect(self, history: list[dict[str, Any]]) -> None:
        """Turn new history entries into tool results, and answer a pending finish."""
        for entry in history[self._seen :]:
            if entry.get("verify") or not self._awaiting:
                continue  # the verdict reaches the model through the finish answer
            call = self._awaiting.popleft()
            self._messages.append(
                ToolResultMessage(call.id, call.name, _result_text(entry), not entry["ok"])
            )
        self._seen = len(history)
        reason, self._replan_reason = self._replan_reason, ""
        if self._finish is not None:
            finish, self._finish = self._finish, None
            text = "The runtime ran verification and the task is not complete."
            self._messages.append(
                ToolResultMessage(finish.id, finish.name, f"{text}\n{reason}".strip(), True)
            )
        elif reason:
            self._messages.append(UserMessage(f"The runtime sent you back to planning: {reason}"))

    def _hand_out(self, call: ModelToolCall) -> ToolCall | None:
        if call.name == FINISH_TOOL:
            self._finish = call
            return None
        if call.arguments is None:
            self._messages.append(
                ToolResultMessage(
                    call.id,
                    call.name,
                    "error: the arguments were not a JSON object, so nothing ran",
                    True,
                )
            )
            raise ProviderError(f"malformed tool call {call.name!r}: arguments not a JSON object")
        self._awaiting.append(call)
        return ToolCall(call.name, dict(call.arguments))


def _task_text(task: dict[str, Any]) -> str:
    sections = [f"Task: {task.get('description', task.get('task_id', ''))}"]
    if task.get("repo_map"):
        sections.append(f"Repository map (from the runtime):\n{task['repo_map']}")
    elif task.get("repo_tree"):
        sections.append(f"Repository files (from the runtime):\n{task['repo_tree']}")
    sections.append(
        f"Use the tools to make the change and run the tests. Call {FINISH_TOOL} when done."
    )
    return "\n\n".join(sections)


def _result_text(entry: dict[str, Any]) -> str:
    """What the model sees about one executed or rejected call."""
    output = entry.get("output") or ""
    if entry["ok"]:
        return output or "(no output)"
    error = f"error: {entry.get('error') or 'the call failed'}"
    return f"{error}\n{output}" if output else error


def _add(a: Usage, b: Usage) -> Usage:
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        reasoning_tokens=a.reasoning_tokens + b.reasoning_tokens,
    )
