"""LLMPlanner prompt construction, with the provider replaced by a local fake."""

from __future__ import annotations

from typing import Any

import pytest

from repo_engineer.llm_provider import LLMResponse
from repo_engineer.planner import LLMPlanner


@pytest.fixture
def planner(monkeypatch: pytest.MonkeyPatch) -> tuple[LLMPlanner, list[str]]:
    for var in ("REPO_ENGINEER_LLM_PROVIDER", "REPO_ENGINEER_LLM_KEY_ENV"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-value-for-tests")
    llm = LLMPlanner()
    prompts: list[str] = []

    def fake_chat_json(system: str, user: str) -> tuple[dict[str, Any], LLMResponse]:
        prompts.append(user)
        reply = LLMResponse(
            content="", prompt_tokens=10, completion_tokens=2, model="m", finish_reason="stop"
        )
        return {"done": True}, reply

    monkeypatch.setattr(llm.provider, "chat_json", fake_chat_json)
    return llm, prompts


def test_prompt_includes_repository_tree_and_replan_reason(
    planner: tuple[LLMPlanner, list[str]],
) -> None:
    llm, prompts = planner
    task = {"task_id": "t", "description": "fix it", "repo_tree": "       shop/cart.py"}
    llm.observe_replan("verification failed: the suite exited with status 1", [])
    llm.next_action(task, [])
    assert "shop/cart.py" in prompts[0]
    assert "verification failed: the suite exited with status 1" in prompts[0]
    llm.next_action(task, [])
    assert "verification failed" not in prompts[1]


def test_usage_counts_every_call(planner: tuple[LLMPlanner, list[str]]) -> None:
    llm, _ = planner
    llm.next_action({"task_id": "t"}, [])
    llm.next_action({"task_id": "t"}, [])
    usage = llm.usage()
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (2, 20, 4)
