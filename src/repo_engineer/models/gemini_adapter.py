"""Gemini through google-genai with explicit function declarations.

Automatic function calling is disabled (the runtime executes every call) and
the calling mode is ANY, so each reply is a function call. Declarations carry
the registry's JSON Schema unchanged (parameters_json_schema). A model turn
this adapter produced is sent back as the original Content, which keeps the
thought signatures Gemini requires on the next request. Function responses
travel in a user turn, merged with any text that follows them.
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


class GeminiAdapter:
    provider = "gemini"

    def __init__(
        self,
        model: str,
        *,
        client: Any,
        temperature: float | None = 0.0,
        max_output_tokens: int = 4096,
        retries: int = 2,
        backoff_s: float = 4.0,
    ) -> None:
        self.model = model
        self.client = client
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self.backoff_s = backoff_s

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply:
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.ANY
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents = _contents(messages)
        response = call_with_retries(
            lambda: self.client.models.generate_content(
                model=self.model, contents=contents, config=config
            ),
            _provider_error,
            retries=self.retries,
            backoff_s=self.backoff_s,
        )
        return _reply(response, self.model)


def _contents(messages: Sequence[Message]) -> list[Any]:
    from google.genai import types

    out: list[Any] = []

    def user_part(part: Any) -> None:
        if out and out[-1].role == "user":
            out[-1].parts.append(part)
        else:
            out.append(types.Content(role="user", parts=[part]))

    for message in messages:
        if isinstance(message, UserMessage):
            user_part(types.Part(text=message.text))
        elif isinstance(message, ToolResultMessage):
            key = "error" if message.is_error else "output"
            user_part(
                types.Part(
                    function_response=types.FunctionResponse(
                        id=message.call_id, name=message.name, response={key: message.content}
                    )
                )
            )
        elif isinstance(message, AssistantMessage):
            if message.raw is not None:
                out.append(message.raw)
                continue
            parts = [types.Part(text=message.text)] if message.text else []
            parts += [
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=call.arguments or {}
                    )
                )
                for call in message.tool_calls
            ]
            out.append(types.Content(role="model", parts=parts))
    return out


def _reply(response: Any, model: str) -> ModelReply:
    candidates = response.candidates or []
    if not candidates or candidates[0].content is None:
        raise ProviderError("malformed provider reply: no candidate content")
    content = candidates[0].content
    texts: list[str] = []
    calls: list[ModelToolCall] = []
    for index, part in enumerate(content.parts or []):
        if part.function_call is not None:
            call = part.function_call
            args = call.args if isinstance(call.args, dict) else None
            calls.append(
                ModelToolCall(call.id or f"gemini-call-{index}", call.name or "", args, "")
            )
        elif part.text and not part.thought:
            texts.append(part.text)
    meta = response.usage_metadata
    candidates_tokens = getattr(meta, "candidates_token_count", 0) or 0
    thoughts = getattr(meta, "thoughts_token_count", 0) or 0
    finish = candidates[0].finish_reason
    return ModelReply(
        text="\n".join(texts),
        tool_calls=tuple(calls),
        stop_reason=str(getattr(finish, "value", finish) or ""),
        usage=Usage(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=candidates_tokens + thoughts,
            cache_read_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            reasoning_tokens=thoughts,
        ),
        model=getattr(response, "model_version", None) or model,
        raw=content,
    )


def _provider_error(exc: Exception) -> ProviderError | None:
    import httpx2
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        return status_error(exc.code, str(exc.message or exc))
    if isinstance(exc, (httpx2.TransportError, TimeoutError, ConnectionError)):
        return connection_error(exc)
    return None
