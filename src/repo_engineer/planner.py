"""Planners: propose actions for the agent runtime.

The planner is the "LLM" part of the agent; the runtime is everything else.
Three planners ship:

- ScriptedPlanner: deterministic, recipe-based policy used by the reproducible
  benchmark. Zero API keys, zero network. It exercises the runtime machinery
  (validation, jail, state machine, verification) with known-good steps.
- LLMPlanner: a real planner backed by an OpenAI-compatible chat provider
  (default: Gemini). It sees the task, the tool schemas and the truncated
  execution history, and must answer with a JSON tool call. The runtime still
  validates, permission-checks and jails everything the model proposes, and
  completion still requires a green test suite - the model cannot claim its
  way to COMPLETE.
- RecordedPlanner: replays a previously captured LLM trajectory (used by tests
  so no network is needed to test the planner plumbing).
"""

from __future__ import annotations

import abc
import json
import os
from typing import Any

from .llm_provider import OpenAICompatProvider, ProviderError
from .state import ToolCall

# Mirrors ToolRegistry's default specs; the runtime remains the authority and
# rejects anything the registry would reject.
TOOL_CATALOG = """You may call exactly one tool per turn, as JSON: {"tool": "<name>", "args": {...}}

Tools:
- list_tree {}  -> bounded file listing
- search {"pattern": str, "glob": str (optional, default "**/*.py")}  -> regex matches as file:line:text
- read_file {"path": str, "start_line": int (opt), "end_line": int (opt)}  -> numbered lines
- apply_edit {"path": str, "old_text": str, "new_text": str}  -> replaces EXACTLY ONE occurrence; 0 or 2+ matches is an error
- write_file {"path": str, "content": str}  -> creates a NEW file; refuses to overwrite
- git_diff {}  -> current changes
- run_tests {"cmd": str}  -> only "python -m pytest ..." or "python -m unittest ..." allowed

Rules:
- Paths are relative to the workspace root. The runtime rejects anything else.
- When you believe the task is done, reply {"done": true} - the runtime will
  then run the verification suite; done is only accepted if tests pass.
- Reply with ONLY the JSON object, no prose."""

SYSTEM_PROMPT = (
    "You are an autonomous software engineering agent. You modify a repository "
    "through validated tool calls to complete the given task. Be precise and "
    "minimal: locate the relevant code, make the smallest correct change, and "
    "verify with the test suite."
)


class ConfigurationError(RuntimeError):
    pass


class Planner(abc.ABC):
    """Proposes the next action given the task and execution history."""

    name: str = "planner"

    @abc.abstractmethod
    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        """Return the next ToolCall, or None to stop (the runtime then verifies)."""

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:
        """Optional hook when the runtime asks the planner to replan."""


class ScriptedPlanner(Planner):
    """Deterministic recipe planner keyed by task['task_id'].

    Each recipe is a fixed list of ToolCalls. This is the reproducible
    benchmark mode: no API keys, no network, byte-stable results. It measures
    the runtime, not model intelligence.
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


DEFAULT_PROVIDER_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_PROVIDER_KEY_ENV = "GEMINI_API_KEY"
DEFAULT_PROVIDER_MODEL = "gemini-2.5-flash"


class LLMPlanner(Planner):
    """Real planner backed by an OpenAI-compatible chat provider.

    Configuration (environment variables):
      REPO_ENGINEER_LLM_PROVIDER  "gemini" (default) or "custom"
      REPO_ENGINEER_LLM_MODEL     model name (default: gemini-2.5-flash)
      REPO_ENGINEER_LLM_BASE_URL  required when provider is "custom"
      REPO_ENGINEER_LLM_KEY_ENV   name of the env var holding the API key
                                  (default GEMINI_API_KEY; the key itself is
                                  never configured here, only read at call time)

    The planner proposes; it does not decide. Every proposal passes through
    ToolRegistry validation, the permission policy and the workspace jail, and
    the runtime only accepts completion on a green verification suite.
    """

    name = "llm"

    def __init__(self) -> None:
        provider = os.environ.get("REPO_ENGINEER_LLM_PROVIDER", "gemini").lower()
        model = os.environ.get("REPO_ENGINEER_LLM_MODEL", DEFAULT_PROVIDER_MODEL)
        key_env = os.environ.get("REPO_ENGINEER_LLM_KEY_ENV", DEFAULT_PROVIDER_KEY_ENV)
        if provider == "gemini":
            base_url = DEFAULT_PROVIDER_BASE_URL
        elif provider == "custom":
            base_url = os.environ.get("REPO_ENGINEER_LLM_BASE_URL", "")
            if not base_url:
                raise ConfigurationError(
                    "REPO_ENGINEER_LLM_BASE_URL is required when provider is 'custom'"
                )
        else:
            raise ConfigurationError(f"unknown provider {provider!r}")
        if not os.environ.get(key_env, "").strip():
            raise ConfigurationError(
                f"environment variable {key_env} is not set; the LLM planner has "
                "no credentials. Set it or use the deterministic ScriptedPlanner."
            )
        self.provider = OpenAICompatProvider(
            base_url=base_url, api_key_env=key_env, model=model
        )
        self.model = model
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.calls = 0

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        user = (
            f"Task: {task.get('description', task.get('task_id', ''))}\n\n"
            f"Execution history (newest last; outputs truncated):\n"
            f"{json.dumps(history[-10:], indent=1, default=str)}\n\n"
            f"{TOOL_CATALOG}"
        )
        data, resp = self.provider.chat_json(SYSTEM_PROMPT, user)
        self.calls += 1
        self.total_prompt_tokens += resp.prompt_tokens
        self.total_completion_tokens += resp.completion_tokens
        if data.get("done"):
            return None
        tool = data.get("tool")
        args = data.get("args", {})
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise ProviderError(f"planner returned malformed action: {data!r}")
        return ToolCall(tool, args)


class RecordedPlanner(Planner):
    """Replays captured actions; deterministic test double for LLMPlanner."""

    name = "recorded"

    def __init__(self, actions: list[ToolCall | None]) -> None:
        self.actions = list(actions)
        self._i = 0

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        if self._i >= len(self.actions):
            return None
        action = self.actions[self._i]
        self._i += 1
        return action


# ---------------------------------------------------------------------------
# Deterministic-benchmark recipes. Each step is executed and validated by the
# runtime; the final step leaves the workspace to be verified by tests.
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
