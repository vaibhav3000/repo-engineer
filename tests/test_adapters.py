"""Provider adapters, exercised through the real SDK clients with no network.

Each SDK client is built with an httpx2 client whose MockTransport answers
requests in-process, so request serialisation and response parsing run
exactly as they would against the provider; nothing leaves the machine.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from repo_engineer.models import (  # noqa: E402
    AssistantMessage,
    ModelToolCall,
    ProviderError,
    ToolResultMessage,
    UserMessage,
)
from repo_engineer.schema import ToolSchema  # noqa: E402

TOOLS = [
    ToolSchema(
        "read_file",
        "Read a file.",
        {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
]
HISTORY = [
    UserMessage("Fix the bug."),
    AssistantMessage(
        "", (ModelToolCall("call_1", "read_file", {"path": "a.py"}, '{"path": "a.py"}'),)
    ),
    ToolResultMessage("call_1", "read_file", "   1: x = 1", is_error=False),
]


class Recorder:
    """A MockTransport handler that replays queued responses and keeps the requests."""

    def __init__(self, *responses: tuple[int, dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.urls: list[str] = []

    def __call__(self, request: Any) -> Any:
        self.urls.append(str(request.url))
        self.requests.append(json.loads(request.content or b"{}"))
        status, body = self.responses.pop(0)
        return httpx2.Response(status, json=body)

    def http_client(self) -> Any:
        return httpx2.Client(transport=httpx2.MockTransport(self))


# ------------------------------------------------------------- OpenAI


def openai_reply(tool_args: str = '{"path": "b.py"}') -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_2",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": tool_args},
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 9,
            "total_tokens": 129,
            "prompt_tokens_details": {"cached_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 4},
        },
    }


def openai_adapter(recorder: Recorder, **kwargs: Any) -> Any:
    openai = pytest.importorskip("openai")
    from repo_engineer.models.openai_adapter import OpenAIAdapter

    client = openai.OpenAI(api_key="test-key", http_client=recorder.http_client(), max_retries=0)
    return OpenAIAdapter("gpt-test", client=client, backoff_s=0.0, **kwargs)


def test_openai_sends_native_tools_and_the_conversation() -> None:
    recorder = Recorder((200, openai_reply()))
    reply = openai_adapter(recorder).complete("Be precise.", HISTORY, TOOLS)
    (body,) = recorder.requests
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file.",
                "parameters": TOOLS[0].parameters,
            },
        }
    ]
    assert body["tool_choice"] == "required" and body["parallel_tool_calls"] is False
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert body["messages"][2]["tool_calls"][0]["id"] == "call_1"
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "   1: x = 1",
    }
    (call,) = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_2", "read_file", {"path": "b.py"})
    assert reply.usage.input_tokens == 120 and reply.usage.output_tokens == 9
    assert reply.usage.cache_read_tokens == 100 and reply.usage.reasoning_tokens == 4


def test_openai_arguments_that_are_not_json_are_flagged() -> None:
    recorder = Recorder((200, openai_reply("{not json")))
    (call,) = openai_adapter(recorder).complete("s", HISTORY, TOOLS).tool_calls
    assert call.arguments is None and call.raw_arguments == "{not json"


def test_local_servers_get_the_compatible_request() -> None:
    recorder = Recorder((200, openai_reply()))
    openai_adapter(recorder, provider="local", force_tool_choice=False).complete(
        "s", HISTORY, TOOLS
    )
    (body,) = recorder.requests
    assert "tool_choice" not in body and "parallel_tool_calls" not in body
    assert "max_tokens" in body and "max_completion_tokens" not in body


@pytest.mark.parametrize("status", [429, 500, 503])
def test_openai_retries_retryable_statuses(status: int) -> None:
    recorder = Recorder((status, {"error": {"message": "busy"}}), (200, openai_reply()))
    reply = openai_adapter(recorder).complete("s", HISTORY, TOOLS)
    assert len(recorder.requests) == 2 and reply.tool_calls


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_openai_does_not_retry_other_client_errors(status: int) -> None:
    recorder = Recorder((status, {"error": {"message": "no"}}), (200, openai_reply()))
    with pytest.raises(ProviderError) as info:
        openai_adapter(recorder).complete("s", HISTORY, TOOLS)
    assert info.value.status == status and not info.value.retryable
    assert len(recorder.requests) == 1


def test_retries_stop_after_the_budget() -> None:
    recorder = Recorder(*[(429, {"error": {"message": "busy"}})] * 3)
    with pytest.raises(ProviderError) as info:
        openai_adapter(recorder, retries=2).complete("s", HISTORY, TOOLS)
    assert info.value.retryable and len(recorder.requests) == 3


# ------------------------------------------------------------- Anthropic


def anthropic_reply() -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "messages-test-model",
        "content": [
            {"type": "text", "text": "Reading the other file."},
            {"type": "tool_use", "id": "toolu_2", "name": "read_file", "input": {"path": "b.py"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 20,
            "output_tokens": 7,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 5,
        },
    }


def anthropic_adapter(recorder: Recorder) -> Any:
    anthropic = pytest.importorskip("anthropic")
    from repo_engineer.models.anthropic_adapter import AnthropicAdapter

    client = anthropic.Anthropic(
        api_key="test-key", http_client=recorder.http_client(), max_retries=0
    )
    return AnthropicAdapter("messages-test-model", client=client, backoff_s=0.0)


def test_anthropic_sends_native_tools_and_the_conversation() -> None:
    recorder = Recorder((200, anthropic_reply()))
    reply = anthropic_adapter(recorder).complete("Be precise.", HISTORY, TOOLS)
    (body,) = recorder.requests
    assert body["system"] == "Be precise."
    assert body["tools"] == [
        {"name": "read_file", "description": "Read a file.", "input_schema": TOOLS[0].parameters}
    ]
    assert body["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert "temperature" not in body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"] == [
        {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}
    ]
    assert body["messages"][2]["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "   1: x = 1",
            "is_error": False,
        }
    ]
    (call,) = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("toolu_2", "read_file", {"path": "b.py"})
    assert reply.text == "Reading the other file."
    assert reply.usage.input_tokens == 125 and reply.usage.output_tokens == 7
    assert reply.usage.cache_read_tokens == 100 and reply.usage.cache_write_tokens == 5


def test_anthropic_overload_is_retried() -> None:
    recorder = Recorder(
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
        (200, anthropic_reply()),
    )
    assert anthropic_adapter(recorder).complete("s", HISTORY, TOOLS).tool_calls
    assert len(recorder.requests) == 2


def test_anthropic_reuses_its_own_assistant_content_verbatim() -> None:
    first = Recorder((200, anthropic_reply()))
    adapter = anthropic_adapter(first)
    reply = adapter.complete("s", [UserMessage("go")], TOOLS)
    second = Recorder((200, anthropic_reply()))
    adapter2 = anthropic_adapter(second)
    adapter2.complete(
        "s",
        [
            UserMessage("go"),
            reply.to_message(),
            ToolResultMessage("toolu_2", "read_file", "ok", is_error=False),
        ],
        TOOLS,
    )
    sent = second.requests[0]["messages"][1]["content"]
    assert sent[0] == {"type": "text", "text": "Reading the other file."}
    assert sent[1]["id"] == "toolu_2"


# ------------------------------------------------------------- Gemini


def gemini_reply() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "id": "fc_2",
                                "name": "read_file",
                                "args": {"path": "b.py"},
                            },
                            "thoughtSignature": "c2lnbmF0dXJl",
                        }
                    ],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 30,
            "candidatesTokenCount": 6,
            "thoughtsTokenCount": 10,
            "cachedContentTokenCount": 12,
            "totalTokenCount": 46,
        },
        "modelVersion": "gemini-test",
    }


def gemini_adapter(recorder: Recorder) -> Any:
    genai = pytest.importorskip("google.genai")
    from google.genai import types

    from repo_engineer.models.gemini_adapter import GeminiAdapter

    client = genai.Client(
        api_key="test-key", http_options=types.HttpOptions(httpx_client=recorder.http_client())
    )
    return GeminiAdapter("gemini-test", client=client, backoff_s=0.0)


def test_gemini_sends_declarations_and_forces_a_call() -> None:
    recorder = Recorder((200, gemini_reply()))
    reply = gemini_adapter(recorder).complete("Be precise.", HISTORY, TOOLS)
    (body,) = recorder.requests
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "read_file"
    # google-genai serialises this field in snake_case; the API accepts both spellings.
    assert declaration["parameters_json_schema"] == TOOLS[0].parameters
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "ANY"
    assert body["systemInstruction"]["parts"][0]["text"] == "Be precise."
    roles = [c["role"] for c in body["contents"]]
    assert roles == ["user", "model", "user"]
    assert body["contents"][1]["parts"][0]["functionCall"]["name"] == "read_file"
    response = body["contents"][2]["parts"][0]["functionResponse"]
    assert response["name"] == "read_file" and response["id"] == "call_1"
    (call,) = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("fc_2", "read_file", {"path": "b.py"})
    assert reply.usage.input_tokens == 30 and reply.usage.output_tokens == 16
    assert reply.usage.reasoning_tokens == 10 and reply.usage.cache_read_tokens == 12


def test_gemini_returns_its_thought_signature_on_the_next_turn() -> None:
    first = Recorder((200, gemini_reply()))
    reply = gemini_adapter(first).complete("s", [UserMessage("go")], TOOLS)
    second = Recorder((200, gemini_reply()))
    gemini_adapter(second).complete(
        "s",
        [
            UserMessage("go"),
            reply.to_message(),
            ToolResultMessage("fc_2", "read_file", "ok", False),
        ],
        TOOLS,
    )
    part = second.requests[0]["contents"][1]["parts"][0]
    assert part["thoughtSignature"] == "c2lnbmF0dXJl"


def test_gemini_rate_limit_is_retried() -> None:
    recorder = Recorder(
        (429, {"error": {"code": 429, "message": "slow down", "status": "RESOURCE_EXHAUSTED"}}),
        (200, gemini_reply()),
    )
    assert gemini_adapter(recorder).complete("s", HISTORY, TOOLS).tool_calls
    assert len(recorder.requests) == 2


def test_gemini_bad_request_is_not_retried() -> None:
    recorder = Recorder(
        (400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}),
    )
    with pytest.raises(ProviderError) as info:
        gemini_adapter(recorder).complete("s", HISTORY, TOOLS)
    assert info.value.status == 400 and len(recorder.requests) == 1


# ------------------------------------------------------------- factory


def test_the_factory_needs_the_key_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    from repo_engineer.models import ConfigurationError, create_adapter

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        create_adapter("openai", "gpt-test")


def test_the_factory_rejects_unknown_providers() -> None:
    from repo_engineer.models import ConfigurationError, create_adapter

    with pytest.raises(ConfigurationError, match="unknown provider"):
        create_adapter("acme", "m")
