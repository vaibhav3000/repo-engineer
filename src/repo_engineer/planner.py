"""Planners: propose actions for the agent runtime.

The planner is the "LLM" part of the agent; the runtime is everything else.
Three planners ship:

- ScriptedPlanner: replays fixed recipes that already contain each task's fix,
  for the reproducible benchmark. Zero API keys, zero network. It exercises the
  runtime machinery (validation, state machine, verification) with known-good
  steps; it solves nothing, and the jail is exercised by the attack evidence.
- LLMPlanner: a real planner backed by an OpenAI-compatible chat provider
  (default: Gemini). It sees the task, the tool schemas and the truncated
  execution history, and must answer with a JSON tool call. The runtime still
  validates, permission-checks and jails everything the model proposes, and
  completion still requires the runtime's own verification run: the model
  cannot claim its way to COMPLETE.
- RecordedPlanner: replays a previously captured LLM trajectory (used by tests
  so no network is needed to test the planner plumbing).
"""

from __future__ import annotations

import abc
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .llm_provider import OpenAICompatProvider, ProviderError
from .schema import ToolSchema
from .state import ToolCall

if TYPE_CHECKING:
    from .trace import EventSink

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
- run_tests {"cmd": str}  -> only "python -m pytest [flags] [test paths]"; flags limited to
  -q, -v, -x, -s, -l, -r<chars>, --tb=<style>, --maxfail=<n>, -k <expr>, -m <expr>;
  test paths must exist inside the workspace; the result starts with exit=<code>

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


@dataclass(frozen=True)
class PlannerUsage:
    """Cumulative provider accounting reported by model-backed planners."""

    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None  # only when a price table knows the model


class Planner(abc.ABC):
    """Proposes the next action given the task and execution history."""

    name: str = "planner"

    @abc.abstractmethod
    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        """Return the next ToolCall, or None to stop (the runtime then verifies)."""

    def prepare(  # noqa: B027
        self, tools: Sequence[ToolSchema], events: EventSink | None = None
    ) -> None:
        """Optional hook at the start of a run: the tools on offer and the audit sink."""

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:  # noqa: B027
        """Optional hook when the runtime asks the planner to replan."""

    def usage(self) -> PlannerUsage | None:
        """Cumulative model usage, or None for planners that call no model."""
        return None


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
        self.provider = OpenAICompatProvider(base_url=base_url, api_key_env=key_env, model=model)
        self.model = model
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.calls = 0
        self._replan_reason = ""

    def usage(self) -> PlannerUsage:
        return PlannerUsage(
            model=self.model,
            calls=self.calls,
            prompt_tokens=self.total_prompt_tokens,
            completion_tokens=self.total_completion_tokens,
        )

    def observe_replan(self, reason: str, history: list[dict[str, Any]]) -> None:
        """Remember why the runtime sent the planner back; shown in the next prompt."""
        self._replan_reason = reason

    def next_action(self, task: dict[str, Any], history: list[dict[str, Any]]) -> ToolCall | None:
        sections = [f"Task: {task.get('description', task.get('task_id', ''))}"]
        if task.get("repo_tree"):
            sections.append(f"Repository files (from the runtime):\n{task['repo_tree']}")
        if self._replan_reason:
            sections.append(
                "The runtime sent you back to planning because:\n" + self._replan_reason
            )
            self._replan_reason = ""
        sections.append(
            "Execution history (newest last; outputs truncated):\n"
            + json.dumps(history[-10:], indent=1, default=str)
        )
        sections.append(TOOL_CATALOG)
        user = "\n\n".join(sections)
        data, resp = self.provider.chat_json(SYSTEM_PROMPT, user)
        self.calls += 1
        self.total_prompt_tokens += resp.prompt_tokens
        self.total_completion_tokens += resp.completion_tokens
        # Only a literal JSON true ends the run; "false" or "yes" do not.
        if data.get("done") is True:
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

# Unified diffs and test files used by the recipes below. Blank context lines
# are written as a single space, as diff tools emit them.
TASK_06_PATCH = (
    "--- a/accounts/store.py\n"
    "+++ b/accounts/store.py\n"
    "@@ -5,3 +5,3 @@\n"
    " \n"
    "-def get_user(user_id: int) -> str:\n"
    "+def fetch_user(user_id: int) -> str:\n"
    '     """The name of a user, or a KeyError for an unknown id."""\n'
    "--- a/accounts/api.py\n"
    "+++ b/accounts/api.py\n"
    "@@ -1,5 +1,5 @@\n"
    "-from accounts.store import get_user\n"
    "+from accounts.store import fetch_user\n"
    " \n"
    " \n"
    " def user_payload(user_id: int) -> dict[str, object]:\n"
    '-    return {"id": user_id, "name": get_user(user_id)}\n'
    '+    return {"id": user_id, "name": fetch_user(user_id)}\n'
    "--- a/accounts/cli.py\n"
    "+++ b/accounts/cli.py\n"
    "@@ -4,2 +4,2 @@\n"
    " def show(user_id: int) -> str:\n"
    '-    return f"user {user_id}: {store.get_user(user_id)}"\n'
    '+    return f"user {user_id}: {store.fetch_user(user_id)}"\n'
)
TASK_07_TESTS = (
    "import pytest\n\nfrom timeparse.duration import parse_duration\n\n\n"
    "def test_hours():\n    assert parse_duration('2h') == 7200\n\n\n"
    "def test_minutes():\n    assert parse_duration('15m') == 900\n\n\n"
    "def test_seconds():\n    assert parse_duration('45s') == 45\n\n\n"
    "def test_combined_units():\n    assert parse_duration('1h30m15s') == 5415\n\n\n"
    "@pytest.mark.parametrize('text', ['', 'abc', '1x', 'h1', '10'])\n"
    "def test_invalid_input(text):\n"
    "    with pytest.raises(ValueError):\n        parse_duration(text)\n"
)
TASK_08_PATCH = (
    "--- /dev/null\n"
    "+++ b/mixer/util.py\n"
    "@@ -0,0 +1,5 @@\n"
    '+"""Helpers shared by the mixer modules."""\n'
    "+\n"
    "+\n"
    "+def clamp(value: float, low: float, high: float) -> float:\n"
    "+    return max(low, min(high, value))\n"
    "--- a/mixer/color.py\n"
    "+++ b/mixer/color.py\n"
    "@@ -1,8 +1,6 @@\n"
    ' """RGB channels are integers from 0 to 255."""\n'
    " \n"
    "-\n"
    "-def clamp(value: float, low: float, high: float) -> float:\n"
    "-    return max(low, min(high, value))\n"
    "+from mixer.util import clamp\n"
    " \n"
    " \n"
    " def brighten(channel: int, amount: int) -> int:\n"
    "--- a/mixer/volume.py\n"
    "+++ b/mixer/volume.py\n"
    "@@ -1,8 +1,6 @@\n"
    ' """Volume is a float from 0.0 (silent) to 1.0 (full)."""\n'
    " \n"
    "-\n"
    "-def clamp(value: float, low: float, high: float) -> float:\n"
    "-    return max(low, min(high, value))\n"
    "+from mixer.util import clamp\n"
    " \n"
    " \n"
    " def louder(level: float, step: float = 0.1) -> float:\n"
)
TASK_09_PATCH = (
    "--- a/notify/messages.py\n"
    "+++ b/notify/messages.py\n"
    "@@ -2,4 +2,4 @@\n"
    " \n"
    " \n"
    "-def send(user: str, text: str) -> dict[str, str]:\n"
    '-    return {"to": user, "text": text}\n'
    '+def send(user: str, text: str, channel: str = "email") -> dict[str, str]:\n'
    '+    return {"to": user, "text": text, "channel": channel}\n'
    "--- a/notify/onboarding.py\n"
    "+++ b/notify/onboarding.py\n"
    "@@ -4,2 +4,2 @@\n"
    " def welcome(user: str) -> dict[str, str]:\n"
    '-    return messages.send(user, f"Welcome aboard, {user}!")\n'
    '+    return messages.send(user, f"Welcome aboard, {user}!", channel="sms")\n'
)
TASK_11_TESTS = (
    "import pytest\n\nfrom versions.semver import is_valid_semver\n\n\n"
    "@pytest.mark.parametrize('text', ['1.2.3', '0.0.0', '10.20.30', '1.0.0-alpha.1'])\n"
    "def test_valid(text):\n    assert is_valid_semver(text)\n\n\n"
    "@pytest.mark.parametrize(\n"
    "    'text', ['01.2.3', '1.02.3', '1.2', '1.2.3.4', 'v1.2.3', '', '1.2.3-']\n)\n"
    "def test_invalid(text):\n    assert not is_valid_semver(text)\n"
)
TASK_12_PATCH = (
    "--- a/app/views.py\n"
    "+++ b/app/views.py\n"
    "@@ -1 +1,2 @@\n"
    "-from textkit.old_strings import slugify, truncate\n"
    "+from textkit.old_strings import truncate\n"
    "+from textkit.text import slugify\n"
    "--- a/app/forms.py\n"
    "+++ b/app/forms.py\n"
    "@@ -1 +1 @@\n"
    "-from textkit.old_strings import slugify\n"
    "+from textkit.text import slugify\n"
)

RECIPES: dict[str, list[ToolCall]] = {
    # task_01: off-by-one loop in benchmark/tasks/task_01_fix_off_by_one/shop/cart.py
    "task_01_fix_off_by_one": [
        ToolCall("search", {"pattern": "range\\(len\\(items\\) - 1\\)"}),
        ToolCall("read_file", {"path": "shop/cart.py"}),
        ToolCall(
            "apply_edit",
            {
                "path": "shop/cart.py",
                "old_text": "for i in range(len(items) - 1):",
                "new_text": "for i in range(len(items)):",
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_02: wrong constant in benchmark/tasks/task_02_failing_test/billing/invoice.py
    "task_02_failing_test": [
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
        ToolCall("search", {"pattern": "TAX_RATE"}),
        ToolCall(
            "apply_edit",
            {
                "path": "billing/invoice.py",
                "old_text": "TAX_RATE = 0.20",
                "new_text": "TAX_RATE = 0.25",
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_03: function has no test coverage; add a test file for it
    "task_03_add_missing_test": [
        ToolCall("search", {"pattern": "def slugify"}),
        ToolCall("read_file", {"path": "web/slug.py"}),
        ToolCall(
            "write_file",
            {
                "path": "tests/test_slug.py",
                "content": (
                    "from web.slug import slugify\n\n\n"
                    "def test_basic():\n"
                    '    assert slugify("Hello World") == "hello-world"\n\n'
                    "def test_strips_punctuation():\n"
                    '    assert slugify("A B & C!") == "a-b-c"\n\n'
                    "def test_collapses_spaces():\n"
                    '    assert slugify("  many   spaces ") == "many-spaces"\n'
                ),
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_04: rename compute_total -> calculate_total across call sites
    "task_04_constrained_refactor": [
        ToolCall("search", {"pattern": "compute_total"}),
        ToolCall(
            "apply_edit",
            {
                "path": "orders/pricing.py",
                "old_text": "def compute_total(",
                "new_text": "def calculate_total(",
            },
        ),
        ToolCall("search", {"pattern": "compute_total"}),
        ToolCall(
            "apply_edit",
            {
                "path": "orders/service.py",
                "old_text": "from orders.pricing import compute_total",
                "new_text": "from orders.pricing import calculate_total",
            },
        ),
        ToolCall(
            "apply_edit",
            {
                "path": "orders/service.py",
                "old_text": "compute_total(",
                "new_text": "calculate_total(",
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_05: the report is wrong, but the bug is in the money helper it uses
    "task_05_cross_file_units": [
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
        ToolCall("find_symbol", {"name": "to_cents"}),
        ToolCall("read_file", {"path": "inventory/money.py"}),
        ToolCall(
            "apply_edit",
            {
                "path": "inventory/money.py",
                "old_text": "int(round(amount * 10))",
                "new_text": "int(round(amount * 100))",
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_06: rename across three modules in one checked, all-or-nothing patch
    "task_06_rename_across_package": [
        ToolCall("find_symbol", {"name": "get_user"}),
        ToolCall("apply_patch", {"patch": TASK_06_PATCH, "dry_run": True}),
        ToolCall("apply_patch", {"patch": TASK_06_PATCH}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_07: add tests strong enough to kill the seeded mutants
    "task_07_add_test_duration": [
        ToolCall("read_file", {"path": "timeparse/duration.py"}),
        ToolCall("write_file", {"path": "tests/test_duration.py", "content": TASK_07_TESTS}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_08: extract the duplicated helper into a new module
    "task_08_extract_helper": [
        ToolCall("repo_map", {}),
        ToolCall("apply_patch", {"patch": TASK_08_PATCH}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_09: a new parameter and the caller that must use it
    "task_09_signature_change": [
        ToolCall("find_symbol", {"name": "send"}),
        ToolCall("apply_patch", {"patch": TASK_09_PATCH}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_10: a mutable default argument shared by every instance
    "task_10_mutable_default": [
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
        ToolCall("read_file", {"path": "plugins/registry.py"}),
        ToolCall(
            "apply_edit",
            {
                "path": "plugins/registry.py",
                "old_text": "    def __init__(self, enabled=[]):\n        self.enabled = enabled",
                "new_text": (
                    "    def __init__(self, enabled=None):\n"
                    "        self.enabled = [] if enabled is None else enabled"
                ),
            },
        ),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_11: add tests strong enough to kill the seeded mutants
    "task_11_add_test_semver": [
        ToolCall("read_file", {"path": "versions/semver.py"}),
        ToolCall("write_file", {"path": "tests/test_semver.py", "content": TASK_11_TESTS}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
    # task_12: stale imports after a function moved between modules. The
    # recipe searches instead of running the broken suite first: a collection
    # error prints interpreter frames that differ between sandboxes and
    # platforms, which a golden file cannot hold.
    "task_12_import_after_move": [
        ToolCall("search", {"pattern": "old_strings import .*slugify"}),
        ToolCall("apply_patch", {"patch": TASK_12_PATCH}),
        ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}),
    ],
}
