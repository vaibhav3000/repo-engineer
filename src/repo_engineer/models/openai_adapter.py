"""OpenAI chat completions with native tool calling; also local OpenAI-compatible servers.

provider="openai" talks to OpenAI with a forced tool call and parallel calls
off. provider="local" talks to an OpenAI-compatible server (Ollama, vLLM,
llama.cpp) through base_url and sends only what such servers commonly accept:
max_tokens instead of max_completion_tokens, and tool_choice only when asked.
The SDK's own retries are off; call_with_retries applies the shared policy.
"""

from __future__ import annotations

import json
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


class OpenAIAdapter:
    """chat.completions with tools, for OpenAI and OpenAI-compatible local servers."""

    def __init__(
        self,
        model: str,
        *,
        client: Any,
        provider: str = "openai",
        temperature: float | None = None,
        max_output_tokens: int = 4096,
        force_tool_choice: bool = True,
        retries: int = 2,
        backoff_s: float = 4.0,
    ) -> None:
        self.model = model
        self.client = client
        self.provider = provider
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.force_tool_choice = force_tool_choice
        self.retries = retries
        self.backoff_s = backoff_s

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *_messages(messages)],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ],
        }
        if self.force_tool_choice:
            request["tool_choice"] = "required"
            request["parallel_tool_calls"] = False
        if self.provider == "local":
            request["max_tokens"] = self.max_output_tokens
        else:
            request["max_completion_tokens"] = self.max_output_tokens
        if self.temperature is not None:
            request["temperature"] = self.temperature
        response = call_with_retries(
            lambda: self.client.chat.completions.create(**request),
            _provider_error,
            retries=self.retries,
            backoff_s=self.backoff_s,
        )
        return _reply(response, self.model)


def _messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, UserMessage):
            out.append({"role": "user", "content": message.text})
        elif isinstance(message, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant", "content": message.text or None}
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": call.raw_arguments},
                    }
                    for call in message.tool_calls
                ]
            out.append(entry)
        elif isinstance(message, ToolResultMessage):
            out.append(
                {"role": "tool", "tool_call_id": message.call_id, "content": message.content}
            )
    return out


def _reply(response: Any, model: str) -> ModelReply:
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError) as exc:
        raise ProviderError(f"malformed provider reply: {exc}") from exc
    message = choice.message
    calls = tuple(
        ModelToolCall(
            id=call.id,
            name=call.function.name,
            arguments=_json_object(call.function.arguments),
            raw_arguments=call.function.arguments or "",
        )
        for call in (message.tool_calls or [])
        if getattr(call, "function", None) is not None
    )
    usage = response.usage
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    completion_details = getattr(usage, "completion_tokens_details", None)
    return ModelReply(
        text=message.content or "",
        tool_calls=calls,
        stop_reason=choice.finish_reason or "",
        usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
            reasoning_tokens=getattr(completion_details, "reasoning_tokens", 0) or 0,
        ),
        model=response.model or model,
    )


def _json_object(text: str | None) -> dict[str, Any] | None:
    """The arguments as a dict, or None when they are not a JSON object."""
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _provider_error(exc: Exception) -> ProviderError | None:
    import openai

    if isinstance(exc, openai.APIStatusError):
        return status_error(exc.status_code, exc.message)
    if isinstance(exc, openai.APIConnectionError):
        return connection_error(exc)
    if isinstance(exc, openai.APIError):
        return ProviderError(f"{type(exc).__name__}: {exc}"[:300])
    return None
