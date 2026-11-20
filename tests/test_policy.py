"""Permission policy: partial policies, approvers that fail, and fail-closed defaults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import error_text

from repo_engineer.state import ToolCall
from repo_engineer.tools import Permission, Policy, ToolRegistry, ToolSpec

EDIT = ToolCall("apply_edit", {"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"})


def test_partial_policy_keeps_defaults_for_other_classes(workspace: Path) -> None:
    """Restricting one permission class must not crash calls of the others."""
    reg = ToolRegistry(workspace, policy={Permission.EXECUTION: Policy.DENY})
    obs = reg.execute(ToolCall("read_file", {"path": "app.py"}))
    assert obs.ok, obs
    obs = reg.execute(ToolCall("run_tests", {"cmd": "python -m pytest tests/ -q"}))
    assert obs.rejected and "policy denies EXECUTION" in error_text(obs)


def test_approver_that_raises_is_a_denial(workspace: Path) -> None:
    def approver(spec: ToolSpec, args: dict[str, Any]) -> bool:
        raise RuntimeError("approval service unavailable")

    reg = ToolRegistry(workspace, policy={Permission.MUTATING: Policy.ASK}, ask_callback=approver)
    obs = reg.execute(EDIT)
    assert not obs.ok and obs.rejected
    assert "approval" in error_text(obs)
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_unrecognised_policy_value_fails_closed(workspace: Path) -> None:
    reg = ToolRegistry(workspace)
    reg.policy[Permission.MUTATING] = "SOMETIMES"  # type: ignore[assignment]
    obs = reg.execute(EDIT)
    assert obs.rejected and not obs.ok
    assert (workspace / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
