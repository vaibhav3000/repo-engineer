"""OpenAI-compatible provider: retries, reply parsing and the done signal.

A local HTTP server stands in for the provider; nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from repo_engineer.llm_provider import LLMResponse, OpenAICompatProvider, ProviderError
from repo_engineer.planner import LLMPlanner
from repo_engineer.state import ToolCall


class FakeProvider:
    """Answers every request with a fixed status code and counts the requests."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                outer.requests += 1
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.dumps(
                    {
                        "choices": [{"message": {"content": '{"done": true}'}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    }
                    if outer.status == 200
                    else {"error": "nope"}
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


@pytest.fixture
def fake_server(request: pytest.FixtureRequest) -> Iterator[FakeProvider]:
    fake = FakeProvider(request.param)
    thread = threading.Thread(target=fake.server.serve_forever, daemon=True)
    thread.start()
    yield fake
    fake.server.shutdown()


def client(url: str, monkeypatch: pytest.MonkeyPatch) -> OpenAICompatProvider:
    monkeypatch.setenv("FAKE_PROVIDER_KEY", "dummy-value-for-tests")
    return OpenAICompatProvider(
        base_url=url, api_key_env="FAKE_PROVIDER_KEY", model="m", backoff_s=0.01
    )


@pytest.mark.parametrize("fake_server", [400, 401, 403, 404], indirect=True)
def test_non_retryable_status_is_not_retried(
    fake_server: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProviderError, match=f"HTTP {fake_server.status}"):
        client(fake_server.url, monkeypatch).chat("s", "u", retries=2)
    assert fake_server.requests == 1


@pytest.mark.parametrize("fake_server", [429, 503], indirect=True)
def test_retryable_status_is_retried(
    fake_server: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProviderError):
        client(fake_server.url, monkeypatch).chat("s", "u", retries=2)
    assert fake_server.requests == 3


@pytest.mark.parametrize("fake_server", [200], indirect=True)
def test_successful_reply_is_parsed(
    fake_server: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, resp = client(fake_server.url, monkeypatch).chat_json("s", "u")
    assert data == {"done": True} and resp.prompt_tokens == 1


def reply(content: str) -> LLMResponse:
    return LLMResponse(
        content=content, prompt_tokens=1, completion_tokens=1, model="m", finish_reason="stop"
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            'Calling {"tool": "read_file", "args": {"path": "a.py"}} then {"done": true}',
            {"tool": "read_file", "args": {"path": "a.py"}},
        ),
        (
            'I think {this} is fine. {"tool": "list_tree", "args": {}}',
            {"tool": "list_tree", "args": {}},
        ),
        ('```json\n{"done": true}\n```', {"done": True}),
    ],
)
def test_first_json_object_in_prose_is_extracted(
    content: str, expected: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = OpenAICompatProvider(base_url="http://127.0.0.1:9", api_key_env="UNUSED", model="m")
    monkeypatch.setattr(provider, "chat", lambda system, user, retries=2: reply(content))
    data, _ = provider.chat_json("s", "u")
    assert data == expected


@pytest.fixture
def llm_planner(monkeypatch: pytest.MonkeyPatch) -> LLMPlanner:
    for var in ("REPO_ENGINEER_LLM_PROVIDER", "REPO_ENGINEER_LLM_KEY_ENV"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-value-for-tests")
    return LLMPlanner()


def answer(planner: LLMPlanner, data: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(planner.provider, "chat_json", lambda system, user: (data, reply("")))
    return planner.next_action({"task_id": "t"}, [])


def test_only_a_literal_true_done_stops(
    llm_planner: LLMPlanner, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = answer(
        llm_planner, {"done": "false", "tool": "read_file", "args": {"path": "a.py"}}, monkeypatch
    )
    assert action == ToolCall("read_file", {"path": "a.py"})
    assert answer(llm_planner, {"done": True}, monkeypatch) is None


def test_a_non_boolean_done_without_a_tool_is_malformed(
    llm_planner: LLMPlanner, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ProviderError):
        answer(llm_planner, {"done": "yes"}, monkeypatch)
