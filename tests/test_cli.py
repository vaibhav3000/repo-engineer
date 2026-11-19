"""The repo-engineer command line: run, replay, mcp-serve, bench and sandbox."""

from __future__ import annotations

import json
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from repo_engineer import cli
from repo_engineer.benchmark.harness import TASK_DIR
from repo_engineer.models import Message, ModelReply, ModelToolCall, Usage
from repo_engineer.schema import ToolSchema

FIX = {
    "path": "shop/cart.py",
    "old_text": "for i in range(len(items) - 1):",
    "new_text": "for i in range(len(items)):",
}


class ScriptedAdapter:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.replies = [
            ModelReply(
                "", (ModelToolCall("c1", "apply_edit", FIX, ""),), "tool_use", Usage(10, 2), "m"
            ),
            ModelReply("", (ModelToolCall("c2", "finish", {}, ""),), "tool_use", Usage(10, 2), "m"),
        ]

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[ToolSchema]
    ) -> ModelReply:
        return self.replies.pop(0)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "cart-repo"
    shutil.copytree(TASK_DIR / "task_01_fix_off_by_one", target)
    (target / "task.json").unlink()
    return target


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: ScriptedAdapter())


def run_args(repo: Path, out: Path, *extra: str) -> list[str]:
    return [
        "run",
        str(repo),
        "--task",
        "The cart total skips the last item; fix it.",
        "--provider",
        "openai",
        "--model",
        "scripted-model",
        "--sandbox",
        "local",
        "--protect",
        "tests/",
        "--verify-cmd",
        "python -m pytest -q tests/",
        "--out",
        str(out),
        *extra,
    ]


def test_run_writes_a_trace_a_state_and_the_verified_patch(
    repo: Path, tmp_path: Path, scripted: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert cli.main(run_args(repo, out)) == 0
    (trace,) = out.glob("*.trace.jsonl")
    kinds = [json.loads(line)["kind"] for line in trace.read_text(encoding="utf-8").splitlines()]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end" and "model_call" in kinds
    (state,) = out.glob("*.state.json")
    assert json.loads(state.read_text(encoding="utf-8"))["verified"] is True
    (patch,) = out.glob("*.patch")
    assert "+    for i in range(len(items)):" in patch.read_text(encoding="utf-8")
    # The original repository is untouched without --apply.
    assert "len(items) - 1" in (repo / "shop" / "cart.py").read_text(encoding="utf-8")
    assert "COMPLETE" in capsys.readouterr().out


def test_run_apply_writes_the_patch_into_the_repository(
    repo: Path, tmp_path: Path, scripted: None
) -> None:
    assert cli.main(run_args(repo, tmp_path / "out", "--apply")) == 0
    assert "range(len(items)):" in (repo / "shop" / "cart.py").read_text(encoding="utf-8")


def test_run_without_credentials_stops_before_doing_anything(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli.main(run_args(repo, tmp_path / "out")) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_replay_reproduces_a_cli_run(
    repo: Path, tmp_path: Path, scripted: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    cli.main(run_args(repo, out))
    (trace,) = out.glob("*.trace.jsonl")
    capsys.readouterr()
    assert cli.main(["replay", str(trace), "--repo", str(repo), "--sandbox", "local"]) == 0
    assert "reproduced" in capsys.readouterr().out


def test_bench_regenerates_the_scripted_results(tmp_path: Path) -> None:
    results = tmp_path / "results"
    assert cli.main(["bench", "--sandbox", "local", "--results", str(results)]) == 0
    data = json.loads((results / "benchmark_results.json").read_text(encoding="utf-8"))
    assert data["summary"]["n_success"] == data["summary"]["n_tasks"]


def test_sandbox_status_names_the_backend(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sandbox", "status"]) == 0
    assert "auto selects:" in capsys.readouterr().out


def test_mcp_serve_speaks_mcp_over_stdio(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    import anyio
    from mcp import Client, StdioServerParameters

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "repo_engineer", "mcp-serve", str(workspace), "--sandbox", "local"],
    )

    async def main() -> Any:
        async with Client(params) as client:
            tools = await client.list_tools()
            result = await client.call_tool("read_file", {"path": "a.py"})
            return tools, result

    tools, result = anyio.run(main)
    assert "apply_patch" in {t.name for t in tools.tools}
    assert not result.is_error and "x = 1" in result.content[0].text


def test_a_live_bench_without_credentials_stops_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    code = cli.main(["bench", "--live", "--model", "m", "--results", str(tmp_path)])
    assert code == 2 and "GEMINI_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "llm_runs").exists()


def test_a_custom_sandbox_image_reaches_the_docker_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from repo_engineer.sandbox import DockerSandbox

    monkeypatch.setattr(DockerSandbox, "available", lambda self: True)
    args = cli.build_parser().parse_args(
        ["replay", "t.jsonl", "--sandbox", "docker", "--sandbox-image", "team/tests:1"]
    )
    sandbox = cli.sandbox_from(args)
    assert isinstance(sandbox, DockerSandbox) and sandbox.image == "team/tests:1"


def test_apply_is_refused_when_a_change_could_not_be_compared(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Found by the Step 2 review: an unreadable file used to become a deletion."""
    big = {"path": "notes.txt", "content": "\u00e9" * 600_000}  # 1.2 MB of UTF-8
    adapter = ScriptedAdapter()
    adapter.replies.insert(
        0,
        ModelReply("", (ModelToolCall("c0", "write_file", big, ""),), "tool_use", Usage(1, 1), "m"),
    )
    monkeypatch.setattr(cli, "create_adapter", lambda *args, **kwargs: adapter)
    before = sorted(p.name for p in repo.rglob("*"))
    assert cli.main(run_args(repo, tmp_path / "out", "--apply")) == 2
    assert "could not be compared" in capsys.readouterr().err
    assert sorted(p.name for p in repo.rglob("*")) == before
    assert "len(items) - 1" in (repo / "shop" / "cart.py").read_text(encoding="utf-8")


def test_apply_honours_protected_paths(repo: Path) -> None:
    patch = (
        "--- a/tests/test_cart.py\n+++ b/tests/test_cart.py\n"
        "@@ -1 +1 @@\n-"
        + (repo / "tests" / "test_cart.py").read_text(encoding="utf-8").splitlines()[0]
        + "\n+import os\n"
    )
    with pytest.raises(ValueError, match="protected"):
        cli._apply(repo, patch, ["tests/"])
