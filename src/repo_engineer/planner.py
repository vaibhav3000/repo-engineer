"""Planners: propose actions for the agent runtime.

The planner is the "LLM" part of the agent; the runtime is everything else.
Two planners ship:

- ScriptedPlanner: a deterministic, recipe-based policy used by the benchmark
  so runs are reproducible with zero API keys. It demonstrates the runtime
  machinery (validation, jail, state machine, verification), not intelligence.
- LLMPlanner: the intended production planner. It is a thin stub that reads
  provider/model configuration from environment variables and raises a clear
  ConfigurationError when unset — no network calls happen anywhere in tests.
"""

from __future__ import annotations

import abc
import json
import os
from typing import Any

from .state import ToolCall


class ConfigurationError(RuntimeError):
    pass


class Planner(abc.ABC):
    """Proposes the next action(s) given the task and execution history."""

    name: str = "planner"

    @abc.abstractmethod
    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        """Return the next ToolCall, or None to stop (the runtime then verifies)."""

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:
        """Optional hook when the runtime asks the planner to replan."""


class ScriptedPlanner(Planner):
    """Deterministic recipe planner keyed by task['task_id'].

    Each recipe is a list of ToolCall templates; special steps reference
    facts discovered at runtime (e.g. files found by search). This is honest
    scripted automation: the recipes encode WHERE a fix goes for the fixed
    benchmark tasks, while the runtime still validates and verifies everything.
    """

    name = "scripted"

    def __init__(self) -> None:
        self._cursor = 0

    def reset(self) -> None:
        self._cursor = 0

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        recipe = RECIPES.get(task["task_id"])
        if recipe is None:
            raise KeyError(f"ScriptedPlanner has no recipe for task '{task['task_id']}'")
        self._cursor = min(len(history), len(recipe))
        if self._cursor >= len(recipe):
            return None  # recipe exhausted: runtime proceeds to VERIFY
        return recipe[self._cursor]


class LLMPlanner(Planner):
    """Planner stub for a real LLM backend (configured via environment)."""

    name = "llm"

    def __init__(self) -> None:
        provider = os.environ.get("REPO_ENGINEER_LLM_PROVIDER")
        model = os.environ.get("REPO_ENGINEER_LLM_MODEL")
        if not provider or not model:
            raise ConfigurationError(
                "LLMPlanner requires REPO_ENGINEER_LLM_PROVIDER and "
                "REPO_ENGINEER_LLM_MODEL environment variables (e.g. "
                "'openai' + 'gpt-4o-mini'); no network calls are made by tests."
            )
        self.provider = provider
        self.model = model

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        raise ConfigurationError(
            "LLMPlanner backend integration is intentionally left as a documented "
            "extension point; the benchmark uses ScriptedPlanner for reproducibility."
        )


# ---------------------------------------------------------------------------
# Benchmark recipes. Each step is executed and validated by the runtime; the
# final step of every recipe leaves the workspace to be verified by tests.
# ---------------------------------------------------------------------------

RECIPES: dict[str, list[ToolCall]] = {
    # task_01: off-by-one loop in benchmark/tasks/task_01_fix_off_by_one/shop/cart.py
    "task_01_fix_off_by_one": [
        ToolCall("search", {"pattern": "range\\(len\\(items\\) - 1\\)"}),
        ToolCall("read_file", {"path": "shop/cart.py"}),
        ToolCall("apply_edit", {
            "path": "shop/cart.py",
            "old_text": "for i in range(len(items) - 1):",
            "new_text": "for i in range(len(items)):",
        }),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_02: wrong constant in benchmark/tasks/task_02_failing_test/billing/invoice.py
    "task_02_failing_test": [
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
        ToolCall("search", {"pattern": "TAX_RATE"}),
        ToolCall("apply_edit", {
            "path": "billing/invoice.py",
            "old_text": "TAX_RATE = 0.20",
            "new_text": "TAX_RATE = 0.25",
        }),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_03: function has no test coverage; add a test file for it
    "task_03_add_missing_test": [
        ToolCall("search", {"pattern": "def slugify"}),
        ToolCall("read_file", {"path": "web/slug.py"}),
        ToolCall("write_file", {
            "path": "tests/test_slug.py",
            "content": (
                "from web.slug import slugify\n\n\n"
                "def test_basic():\n"
                "    assert slugify(\"Hello World\") == \"hello-world\"\n\n"
                "def test_strips_punctuation():\n"
                "    assert slugify(\"A B & C!\") == \"a-b-c\"\n\n"
                "def test_collapses_spaces():\n"
                "    assert slugify(\"  many   spaces \") == \"many-spaces\"\n"
            ),
        }),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_04: rename compute_total -> calculate_total across call sites
    "task_04_constrained_refactor": [
        ToolCall("search", {"pattern": "compute_total"}),
        ToolCall("apply_edit", {
            "path": "orders/pricing.py",
            "old_text": "def compute_total(",
            "new_text": "def calculate_total(",
        }),
        ToolCall("search", {"pattern": "compute_total"}),
        ToolCall("apply_edit", {
            "path": "orders/service.py",
            "old_text": "from orders.pricing import compute_total",
            "new_text": "from orders.pricing import calculate_total",
        }),
        ToolCall("apply_edit", {
            "path": "orders/service.py",
            "old_text": "compute_total(",
            "new_text": "calculate_total(",
        }),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
}
