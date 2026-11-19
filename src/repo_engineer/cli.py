"""The repo-engineer command line.

    repo-engineer run <repo> --task "..." [--provider gemini|openai|anthropic|local --model M]
    repo-engineer replay <trace.jsonl> [--repo <repo>]
    repo-engineer mcp-serve <workspace>
    repo-engineer bench [--live --provider P --model M --attempts 3]
    repo-engineer sandbox status|build

`run` never touches the repository it is given: the agent works in a copy,
and the verified change is written next to the audit trace as a patch. With
--apply that patch is applied to the repository, after a dry run, and only
when verification passed. Exit status: 0 verified, 1 not verified, 2 a
configuration error (nothing ran).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .agent import AgentRuntime, prepare_workspace
from .models import PROVIDERS, ConfigurationError, create_adapter
from .planner import ConfigurationError as PlannerConfigurationError
from .planner import LLMPlanner, Planner
from .sandbox import SANDBOX_MODES, DockerSandbox, SandboxError, select_sandbox
from .state import ToolCall
from .tools import Permission, Policy, ToolRegistry
from .trace import PriceTable, TraceWriter

DEFAULT_VERIFY_CMD = "python -m pytest -q"
DESCRIPTION = "A verified tool-using coding agent with a sandboxed runtime."
PERMISSION_NAMES = {
    "read": Permission.READ_ONLY,
    "mutate": Permission.MUTATING,
    "execute": Permission.EXECUTION,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ConfigurationError, PlannerConfigurationError, SandboxError, ValueError) as exc:
        print(f"repo-engineer: {exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repo-engineer", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version=f"repo-engineer {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run the agent on a copy of a repository")
    run.add_argument("repo", type=Path)
    run.add_argument("--task", required=True, help="what to change, in plain words")
    run.add_argument("--verify-cmd", default=DEFAULT_VERIFY_CMD)
    run.add_argument("--protect", nargs="*", default=[], help="paths the agent may not modify")
    _model_options(run)
    _sandbox_option(run)
    run.add_argument("--max-steps", type=int, default=40)
    run.add_argument("--max-replans", type=int, default=4)
    run.add_argument("--out", type=Path, default=Path("runs"))
    run.add_argument("--prices", type=Path, help="JSON price table for cost accounting")
    run.add_argument("--mcp-config", type=Path, help="JSON list of external MCP servers")
    run.add_argument("--apply", action="store_true", help="apply the verified patch to the repo")
    run.set_defaults(handler=cmd_run)

    replay = commands.add_parser("replay", help="re-execute a recorded run offline")
    replay.add_argument("trace", type=Path)
    replay.add_argument("--repo", type=Path, help="starting repository (benchmark tasks are found)")
    _sandbox_option(replay)
    replay.set_defaults(handler=cmd_replay)

    serve = commands.add_parser("mcp-serve", help="serve a workspace's tools over MCP (stdio)")
    serve.add_argument("workspace", type=Path)
    serve.add_argument("--protect", nargs="*", default=[])
    serve.add_argument("--deny", nargs="*", default=[], choices=sorted(PERMISSION_NAMES))
    _sandbox_option(serve)
    serve.set_defaults(handler=cmd_mcp_serve)

    bench = commands.add_parser("bench", help="run the benchmark")
    bench.add_argument("--results", type=Path, default=Path("results"))
    bench.add_argument("--live", action="store_true", help="use a model instead of the recipes")
    bench.add_argument("--attempts", type=int, default=3)
    bench.add_argument("--tasks", nargs="*", help="task ids (default: all)")
    _model_options(bench)
    _sandbox_option(bench)
    bench.set_defaults(handler=cmd_bench)

    sandbox = commands.add_parser("sandbox", help="inspect or build the Docker sandbox")
    sandbox.add_argument("action", choices=("status", "build"))
    sandbox.set_defaults(handler=cmd_sandbox)
    return parser


def _model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--planner", choices=("native", "json"), default="native")
    parser.add_argument("--provider", choices=PROVIDERS, default="gemini")
    parser.add_argument("--model", default=None, help="model id (required for live runs)")
    parser.add_argument("--base-url", default=None, help="endpoint for --provider local")
    parser.add_argument("--api-key-env", default=None, help="variable holding the API key")
    parser.add_argument("--context-budget", type=int, default=100_000)


def _sandbox_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sandbox", choices=SANDBOX_MODES, default="auto")


# ------------------------------------------------------------- run


def cmd_run(args: argparse.Namespace) -> int:
    repo: Path = args.repo.resolve()
    if not repo.is_dir():
        raise ValueError(f"{repo} is not a directory")
    planner = _planner(args)  # fails on missing credentials before anything is copied
    prices = PriceTable.load(args.prices) if args.prices else PriceTable.empty()
    if hasattr(planner, "prices"):
        planner.prices = prices  # type: ignore[attr-defined]
    servers = _mcp_servers(args.mcp_config) if args.mcp_config else []
    sandbox = select_sandbox(args.sandbox)
    out: Path = args.out.resolve()
    workspace = prepare_workspace(repo, out, _name(repo.name))
    run_id = workspace.name
    task = {
        "task_id": run_id,
        "description": args.task,
        "verify_cmd": args.verify_cmd,
        "protected_paths": list(args.protect),
    }
    registry = ToolRegistry(workspace, sandbox=sandbox)
    trace_path = out / f"{run_id}.trace.jsonl"
    with TraceWriter(trace_path) as writer:
        runtime = AgentRuntime(
            registry, planner, max_steps=args.max_steps, max_replans=args.max_replans, events=writer
        )
        if servers:
            from .mcp_tools import ExternalTools

            with ExternalTools(registry, servers):
                state = runtime.run_task(task)
        else:
            state = runtime.run_task(task)
    (out / f"{run_id}.state.json").write_text(
        json.dumps(state.to_dict(), indent=2), encoding="utf-8"
    )
    patch = registry.snapshot.diff()
    patch_path = out / f"{run_id}.patch"
    patch_path.write_text(
        "" if patch == "(no changes)" else patch + "\n", encoding="utf-8", newline="\n"
    )
    print(
        f"{state.state.value}: verified={state.verified} steps={state.steps_used} sandbox={sandbox.name}"
    )
    print(f"trace: {trace_path}\npatch: {patch_path}")
    if not state.verified:
        return 1
    if args.apply and patch != "(no changes)":
        _apply(repo, patch)
    return 0


def _planner(args: argparse.Namespace) -> Planner:
    if args.planner == "json":
        return LLMPlanner()
    if not args.model:
        raise ValueError("--model is required for a model-backed run")
    from .native_planner import NativeToolPlanner

    adapter = create_adapter(
        args.provider, args.model, base_url=args.base_url, api_key_env=args.api_key_env
    )
    return NativeToolPlanner(adapter, context_budget_tokens=args.context_budget)


def _apply(repo: Path, patch: str) -> None:
    """Apply the verified patch to the original repository, dry run first."""
    registry = ToolRegistry(repo)
    for dry_run in (True, False):
        observation = registry.execute(
            ToolCall("apply_patch", {"patch": patch, "dry_run": dry_run})
        )
        if not observation.ok:
            raise ValueError(f"the verified patch does not apply to {repo}: {observation.error}")
    print(f"applied the verified patch to {repo}")


def _mcp_servers(path: Path) -> list[Any]:
    from .mcp_tools import ExternalServer

    servers = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        servers.append(
            ExternalServer(
                name=entry["name"],
                command=tuple(entry["command"]),
                permission=Permission(entry.get("permission", "EXECUTION")),
                path_args={k: tuple(v) for k, v in entry.get("path_args", {}).items()},
                timeout_s=float(entry.get("timeout_s", 60.0)),
            )
        )
    return servers


def _name(raw: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in raw) or "repo"


# ------------------------------------------------------------- other commands


def cmd_replay(args: argparse.Namespace) -> int:
    from .replay import replay

    report = replay(args.trace, repo=args.repo, sandbox=select_sandbox(args.sandbox))
    if report.reproduced:
        print(f"reproduced: {report.compared} events match; final state {report.final_state}")
        return 0
    print(f"not reproduced: {report.first_difference}")
    return 1


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from .mcp_server import serve_stdio

    policy = {PERMISSION_NAMES[name]: Policy.DENY for name in args.deny}
    registry = ToolRegistry(args.workspace, policy=policy, sandbox=select_sandbox(args.sandbox))
    registry.protect(args.protect)
    serve_stdio(registry)
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    sandbox = select_sandbox(args.sandbox)
    if not args.live:
        from .benchmark.harness import main as scripted

        return scripted(args.results, sandbox=sandbox)
    from .benchmark.harness import load_tasks
    from .benchmark.live import run_live_benchmark

    if not args.model:
        raise ValueError("--model is required for a live benchmark")
    _planner(args)  # fail fast on missing credentials
    task_ids = args.tasks or [t["task_id"] for t in load_tasks()]
    path = run_live_benchmark(
        task_ids, args.attempts, args.results / "llm_runs", lambda: _planner(args), sandbox=sandbox
    )
    summary = json.loads(path.read_text(encoding="utf-8"))["summary"]
    print(json.dumps(summary, indent=2))
    print(f"wrote {path}")
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    docker = DockerSandbox()
    if args.action == "build":
        docker.require_available()
        docker.build_image()
        print(f"built {docker.image}")
        return 0
    available = docker.available()
    print(f"docker command: {' '.join(docker.docker_cmd)}")
    print(f"docker available: {available}")
    print(f"sandbox image: {docker.image}")
    print(f"auto selects: {'docker' if available else 'local'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
