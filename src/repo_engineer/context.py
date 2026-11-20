"""Context budgeting for a planner's conversation.

estimate_tokens is a deliberate estimate (characters / 4); it is cheap, needs
no tokenizer, and errs in a known direction for English and code. When a
conversation's estimate passes the budget, compact() replaces the oldest tool
results with one-line stubs. Stubs keep their call ids, so every provider
still accepts the conversation. The first message (the task and the repo
map) and the most recent messages are never compacted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from .models import AssistantMessage, Message, ToolResultMessage, UserMessage

CHARS_PER_TOKEN = 4
STUB_PREFIX = "[compacted:"


def estimate_tokens(messages: Sequence[Message]) -> int:
    """An estimate of the conversation's size in tokens (characters / 4)."""
    chars = 0
    for message in messages:
        if isinstance(message, (UserMessage, AssistantMessage)):
            chars += len(message.text)
        if isinstance(message, AssistantMessage):
            chars += sum(
                len(c.name) + len(c.raw_arguments or str(c.arguments)) for c in message.tool_calls
            )
        if isinstance(message, ToolResultMessage):
            chars += len(message.content)
    return chars // CHARS_PER_TOKEN


def compact(
    messages: Sequence[Message], budget_tokens: int, keep_recent: int = 6
) -> tuple[list[Message], int]:
    """Stub out the oldest tool results until the estimate fits the budget.

    Returns the (possibly unchanged) messages and how many results were
    stubbed. Results within the last keep_recent messages are kept whole; if
    the conversation still does not fit, it is returned as it is.
    """
    out = list(messages)
    if estimate_tokens(out) <= budget_tokens:
        return out, 0
    protected_from = max(1, len(out) - keep_recent)
    stubbed = 0
    for index in range(1, protected_from):
        message = out[index]
        if not isinstance(message, ToolResultMessage) or message.content.startswith(STUB_PREFIX):
            continue
        stub = f"{STUB_PREFIX} {len(message.content)} characters of {message.name} output removed to fit the context budget]"
        out[index] = replace(message, content=stub)
        stubbed += 1
        if estimate_tokens(out) <= budget_tokens:
            break
    if estimate_tokens(out) > budget_tokens and stubbed == 0:
        return list(messages), 0
    return out, stubbed
