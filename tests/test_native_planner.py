"""NativeToolPlanner driven by a scripted model adapter through the real runtime."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from repo_engineer.agent import AgentRuntime
from repo_engineer.models import (
    Message,
    ModelReply,
    ModelToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from repo_engineer.native_planner import FINISH_TOOL, NativeToolPlanner
from repo_engineer.schema import ToolSchema
from repo_engineer.state import State
from repo_engineer.tools import ToolRegistry

TASK = {"task_id": "t", "description": "Make VALUE 2 and keep the tests green."}


def call(name: str, args: dict[str, Any] | None, call_id: str) -> ModelToolCall:
    return ModelToolCall(call_id, name, args, "")


def reply(*calls: ModelToolCall, text: str = "") -> ModelReply:
    return ModelReply(
        text, tuple(calls), "tool_use", Usage(input_tokens=100, output_tokens=10), "m"
    )


class ScriptedAdapter:
    """Replays replies in order and records every conversation it was sent."""

    provider = "scripted"
    model = "m"

    def __init__(self, *replies: ModelReply) -> None:
        self.replies = list(replies)
        self.sent: list[list[Message]] = []
        self.tools: list[list[ToolSchema]] = []

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply:
        self.sent.append(list(messages))
        self.tools.append(list(tools))
        return self.replies.pop(0)


def results(messages: Sequence[Message]) -> list[ToolResultMessage]:
    return [m for m in messages if isinstance(m, ToolResultMessage)]


def fix_workspace(workspace: Path) -> None:
    (workspace / "tests" / "test_app.py").write_text(
        "from app import VALUE\n\n\ndef test_value():\n    assert VALUE == 2\n", encoding="utf-8"
    )


def test_a_task_runs_to_completion_with_native_tool_calls(workspace: Path) -> None:
    fix_workspace(workspace)
    adapter = ScriptedAdapter(
        reply(call("read_file", {"path": "app.py"}, "c1")),
        reply(call("apply_edit", {"path": "app.py", "old_text": "1", "new_text": "2"}, "c2")),
        reply(call("run_tests", {"cmd": "python -m pytest -q tests/"}, "c3")),
        reply(call(FINISH_TOOL, {"summary": "done"}, "c4")),
    )
    planner = NativeToolPlanner(adapter)
    state = AgentRuntime(ToolRegistry(workspace), planner).run_task(TASK)
    assert state.state is State.COMPLETE and state.verified
    # A passing run_tests sends the runtime straight to VERIFY, so the model
    # is asked three times and the scripted finish is never needed.
    assert len(adapter.sent) == 3 and len(adapter.replies) == 1
    last = results(adapter.sent[-1])
    assert [r.call_id for r in last] == ["c1", "c2"]
    assert "VALUE = 1" in last[0].content and not last[0].is_error
    assert "edited app.py" in last[1].content
    usage = planner.usage()
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (3, 300, 30)


def test_the_model_is_offered_the_registry_tools_and_finish(workspace: Path) -> None:
    adapter = ScriptedAdapter(reply(call(FINISH_TOOL, {}, "c1")))
    registry = ToolRegistry(workspace)
    AgentRuntime(registry, NativeToolPlanner(adapter), max_replans=0).run_task(TASK)
    offered = {t.name for t in adapter.tools[0]}
    assert offered == {s.name for s in registry.tool_schemas()} | {FINISH_TOOL}


def test_the_first_message_carries_the_task_and_the_repo_listing(workspace: Path) -> None:
    adapter = ScriptedAdapter(reply(call(FINISH_TOOL, {}, "c1")))
    AgentRuntime(ToolRegistry(workspace), NativeToolPlanner(adapter), max_replans=0).run_task(TASK)
    first = adapter.sent[0][0]
    assert isinstance(first, UserMessage)
    assert TASK["description"] in first.text and "app.py" in first.text


def test_a_failed_verification_answers_the_finish_call(workspace: Path) -> None:
    fix_workspace(workspace)  # the suite fails until VALUE is 2
    adapter = ScriptedAdapter(
        reply(call(FINISH_TOOL, {}, "f1")),
        reply(call("apply_edit", {"path": "app.py", "old_text": "1", "new_text": "2"}, "c2")),
        reply(call(FINISH_TOOL, {}, "f2")),
    )
    state = AgentRuntime(ToolRegistry(workspace), NativeToolPlanner(adapter)).run_task(TASK)
    assert state.state is State.COMPLETE and state.replans == 1
    (answer,) = results(adapter.sent[1])
    assert answer.call_id == "f1" and answer.is_error
    assert "verification failed" in answer.content


def test_a_rejected_call_comes_back_as_an_error_result(workspace: Path) -> None:
    adapter = ScriptedAdapter(
        reply(call("read_file", {"path": "../secret.txt"}, "c1")),
        reply(call(FINISH_TOOL, {}, "c2")),
    )
    AgentRuntime(ToolRegistry(workspace), NativeToolPlanner(adapter), max_replans=0).run_task(TASK)
    (answer,) = results(adapter.sent[1])
    assert answer.is_error and "path escape rejected" in answer.content


def test_arguments_that_are_not_an_object_are_a_planner_error(workspace: Path) -> None:
    adapter = ScriptedAdapter(
        reply(call("read_file", None, "c1")),
        reply(call(FINISH_TOOL, {}, "c2")),
    )
    state = AgentRuntime(
        ToolRegistry(workspace), NativeToolPlanner(adapter), max_replans=0
    ).run_task(TASK)
    assert state.planner_errors == 1 and state.steps_used == 1  # only VERIFY counted
    (answer,) = results(adapter.sent[1])
    assert answer.call_id == "c1" and answer.is_error
    assert "not a JSON object" in answer.content


def test_two_calls_in_one_reply_run_in_order_before_the_model_is_asked_again(
    workspace: Path,
) -> None:
    adapter = ScriptedAdapter(
        reply(call("read_file", {"path": "app.py"}, "a"), call("list_tree", {}, "b")),
        reply(call(FINISH_TOOL, {}, "c")),
    )
    state = AgentRuntime(
        ToolRegistry(workspace), NativeToolPlanner(adapter), max_replans=0
    ).run_task(TASK)
    assert [a["tool"] for a in state.actions[1:3]] == ["read_file", "list_tree"]
    assert len(adapter.sent) == 2
    assert [r.call_id for r in results(adapter.sent[1])] == ["a", "b"]


def test_a_reply_without_a_tool_call_is_nudged_once_then_ends_planning(workspace: Path) -> None:
    adapter = ScriptedAdapter(reply(text="I think it is done."), reply(text="Really done."))
    state = AgentRuntime(
        ToolRegistry(workspace), NativeToolPlanner(adapter), max_replans=0
    ).run_task(TASK)
    assert len(adapter.sent) == 2
    nudge = adapter.sent[1][-1]
    assert isinstance(nudge, UserMessage) and FINISH_TOOL in nudge.text
    assert state.state is State.COMPLETE  # the untouched suite passes verification
