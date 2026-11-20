"""Context budgeting: size estimates and compaction of old tool results."""

from __future__ import annotations

from repo_engineer.context import compact, estimate_tokens
from repo_engineer.models import AssistantMessage, ModelToolCall, ToolResultMessage, UserMessage


def conversation(results: int, size: int) -> list:
    messages: list = [UserMessage("task " * 50)]
    for i in range(results):
        call = ModelToolCall(f"c{i}", "read_file", {"path": f"f{i}.py"}, "")
        messages.append(AssistantMessage("", (call,)))
        messages.append(ToolResultMessage(f"c{i}", "read_file", "x" * size))
    return messages


def test_estimate_is_characters_over_four() -> None:
    assert estimate_tokens([UserMessage("x" * 400)]) == 100


def test_under_budget_nothing_changes() -> None:
    messages = conversation(3, 100)
    assert compact(messages, budget_tokens=10_000, keep_recent=2) == (messages, 0)


def test_oldest_results_are_stubbed_first_and_ids_survive() -> None:
    messages = conversation(6, 4000)
    compacted, count = compact(messages, budget_tokens=3000, keep_recent=4)
    assert count > 0 and estimate_tokens(compacted) <= 3000
    results = [m for m in compacted if isinstance(m, ToolResultMessage)]
    assert [r.call_id for r in results] == [f"c{i}" for i in range(6)]
    assert results[0].content.startswith("[compacted:")
    assert results[-1].content == "x" * 4000  # inside the protected recent window
    assert compacted[0] == messages[0]  # the task is never compacted


def test_compaction_stops_when_only_protected_messages_remain() -> None:
    messages = conversation(2, 40_000)
    compacted, count = compact(messages, budget_tokens=100, keep_recent=4)
    assert count == 0 and compacted == messages
