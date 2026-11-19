"""Anthropic messages with native tool use.

The tool call is forced and parallel tool use is off, so each reply carries
one call. anthropic 2.x removed the sampling parameters, so none are sent.
Tool results for one assistant turn travel together in the next user
message, as the API requires. An assistant turn this adapter produced is
sent back with its original content blocks, which keeps any thinking blocks
and their signatures intact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..schema import ToolSchema
from .base import (
    AssistantMessage,
    Message,
    ModelReply,
    ModelToolCall,
    ProviderError,
    ToolResultMessage,
    Usage,
    UserMessage,
    call_with_retries,
    connection_error,
    status_error,
)


class AnthropicAdapter:
    provider = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        client: Any,
        max_output_tokens: int = 4096,
        retries: int = 2,
        backoff_s: float = 4.0,
    ) -> None:
        self.model = model
        self.client = client
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.backoff_s = backoff_s

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_output_tokens,
            "system": system,
            "messages": _messages(messages),
            "tools": [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
        }
        response = call_with_retries(
            lambda: self.client.messages.create(**request),
            _provider_error,
            retries=self.retries,
            backoff_s=self.backoff_s,
        )
        return _reply(response, self.model)


def _messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Alternating user/assistant turns; consecutive user-side items are merged."""
    out: list[dict[str, Any]] = []

    def user_block(block: dict[str, Any]) -> None:
        if out and out[-1]["role"] == "user":
            out[-1]["content"].append(block)
        else:
            out.append({"role": "user", "content": [block]})

    for message in messages:
        if isinstance(message, UserMessage):
            user_block({"type": "text", "text": message.text})
        elif isinstance(message, ToolResultMessage):
            user_block(
                {
                    "type": "tool_result",
                    "tool_use_id": message.call_id,
                    "content": message.content,
                    "is_error": message.is_error,
                }
            )
        elif isinstance(message, AssistantMessage):
            if message.raw is not None:
                content = [_as_param(block) for block in message.raw]
            else:
                content = [{"type": "text", "text": message.text}] if message.text else []
                content += [
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments or {},
                    }
                    for call in message.tool_calls
                ]
            out.append({"role": "assistant", "content": content})
    return out


def _as_param(block: Any) -> dict[str, Any]:
    """A response content block as a request content block."""
    data = block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)
    data.pop("citations", None)
    data.pop("caller", None)
    return data


def _reply(response: Any, model: str) -> ModelReply:
    texts: list[str] = []
    calls: list[ModelToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            arguments = block.input if isinstance(block.input, dict) else None
            calls.append(ModelToolCall(block.id, block.name, arguments, raw_arguments=""))
    usage = response.usage
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return ModelReply(
        text="\n".join(texts),
        tool_calls=tuple(calls),
        stop_reason=response.stop_reason or "",
        usage=Usage(
            input_tokens=(usage.input_tokens or 0) + cache_read + cache_write,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
        model=response.model or model,
        raw=list(response.content),
    )


def _provider_error(exc: Exception) -> ProviderError | None:
    import anthropic

    if isinstance(exc, anthropic.APIStatusError):
        return status_error(exc.status_code, str(exc))
    if isinstance(exc, anthropic.APIConnectionError):
        return connection_error(exc)
    if isinstance(exc, anthropic.APIError):
        return ProviderError(f"{type(exc).__name__}: {exc}"[:300])
    return None
