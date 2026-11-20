"""Provider-neutral conversation types, the adapter interface and the retry policy.

A ModelAdapter turns a system prompt, a list of messages and the tools on
offer into one provider request, and the provider's reply back into text,
tool calls with ids, a stop reason and normalised usage. Planners only ever
see these types.

Retries: every adapter switches its SDK's own retries off and maps provider
errors onto ProviderError(status, retryable); call_with_retries then applies
one policy everywhere (docs/adr/0003).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from ..schema import ToolSchema

# Rate limits, overload and transient server errors; 408 and 409 are the
# request-timeout and conflict statuses providers ask clients to retry.
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})


class ConfigurationError(RuntimeError):
    """The planner or provider is misconfigured (unknown provider, no key)."""


class ProviderError(RuntimeError):
    """A provider call failed. status is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class ModelToolCall:
    """One tool call a model asked for. arguments is None when they were not a JSON object."""

    id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str = ""


@dataclass(frozen=True)
class Usage:
    """Token counts for one model call, normalised across providers.

    input_tokens counts every prompt token, cached ones included;
    output_tokens counts every generated token, reasoning included.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ModelToolCall, ...] = ()
    # The provider's own representation of this turn, when the adapter that
    # produced it must send it back verbatim (Gemini thought signatures,
    # Anthropic thinking blocks). Never serialised into traces.
    raw: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class ToolResultMessage:
    call_id: str
    name: str
    content: str
    is_error: bool = False


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class ModelReply:
    text: str
    tool_calls: tuple[ModelToolCall, ...]
    stop_reason: str
    usage: Usage
    model: str
    raw: Any = field(default=None, compare=False, repr=False)

    def to_message(self) -> AssistantMessage:
        """This reply as the assistant turn of a conversation."""
        return AssistantMessage(self.text, self.tool_calls, raw=self.raw)


class ModelAdapter(Protocol):
    """One provider behind a provider-neutral interface."""

    provider: str
    model: str

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply: ...


T = TypeVar("T")


def call_with_retries(
    call: Callable[[], T],
    to_provider_error: Callable[[Exception], ProviderError | None],
    *,
    retries: int = 2,
    backoff_s: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run call, retrying only errors that map to a retryable ProviderError.

    to_provider_error maps an SDK exception to a ProviderError, or returns
    None for exceptions that are not provider errors (those propagate as is).
    Backoff grows linearly: backoff_s, 2 * backoff_s, ...
    """
    for attempt in range(retries + 1):
        try:
            return call()
        except Exception as exc:
            error = to_provider_error(exc)
            if error is None:
                raise
            if not error.retryable or attempt == retries:
                raise error from exc
            sleep(backoff_s * (attempt + 1))
    raise AssertionError("unreachable: the loop returns or raises")


def status_error(status: int, message: str) -> ProviderError:
    """A ProviderError for an HTTP status, retryable when the status is."""
    return ProviderError(
        f"HTTP {status}: {message[:300]}", status=status, retryable=status in RETRYABLE_STATUS
    )


def connection_error(exc: Exception) -> ProviderError:
    """Connection failures and timeouts are retryable."""
    return ProviderError(f"{type(exc).__name__}: {exc}"[:300], retryable=True)
