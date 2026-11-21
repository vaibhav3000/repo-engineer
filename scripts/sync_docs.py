"""Write every published number from the committed results, or check that they match.

    python scripts/sync_docs.py          # rewrite the generated parts
    python scripts/sync_docs.py --check  # exit 1 if any generated part is stale

The README, the design FAQ, the technical report and the demo site quote
numbers (benchmark outcomes, jail-suite counts, the recorded live run, the
tool count, the state machine). None of them is typed by hand: this script
reads results/*.json (never writing them), the tool registry and the state
table, and renders

- the blocks between `<!-- generated:NAME -->` and `<!-- /generated:NAME -->`
  in README.md and docs/design_faq.md;
- reports/generated/numbers.tex (macros) and the table bodies next to it,
  which the report pulls in with \\input.

The demo site renders its numbers from data.js, which demo-site/build.py
writes from the same results. The test suite runs `--check`, so a stale
number fails the build. Line endings are ignored when comparing, because a
Windows checkout may hold CRLF copies of LF files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from repo_engineer.state import TRANSITIONS, State
from repo_engineer.tools import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
GENERATED_TEX = ROOT / "reports" / "generated"

# Short labels for the benchmark tables; a task without a label fails the run.
TASK_LABELS = {
    "task_01_fix_off_by_one": "fix an off-by-one loop",
    "task_02_failing_test": "repair a failing test",
    "task_03_add_missing_test": "add missing tests (mutants)",
    "task_04_constrained_refactor": "constrained rename",
    "task_05_cross_file_units": "bug behind another module",
    "task_06_rename_across_package": "rename across a package (one patch)",
    "task_07_add_test_duration": "add tests for a parser (mutants)",
    "task_08_extract_helper": "extract a shared helper",
    "task_09_signature_change": "signature change and its callers",
    "task_10_mutable_default": "shared mutable default",
    "task_11_add_test_semver": "add tests for a validator (mutants)",
    "task_12_import_after_move": "stale imports after a move",
}

# How the jail-suite tables group attacks. The evidence's own categories are
# kept except for the two that mix concerns; an unknown category or an
# unmapped residual id fails the run, so no attack can drop out of a table.
GROUP_OF_CATEGORY = {
    "path escape": "path escape",
    "command injection": "shell commands",
    "malformed call": "malformed calls",
    "test command": "test command abuse",
    "path alias": "path aliases",
    "git metadata": "git metadata",
    "link": "links",
    "verification": "verification tampering",
    "resource": "resource limits",
    "credentials": "credentials",
    "sandbox": "agent-written test code",
}
GROUP_OF_ID = {"A39": "agent-written test code", "A46": "report forging"}
GROUP_ORDER = [
    "path escape",
    "shell commands",
    "malformed calls",
    "test command abuse",
    "path aliases",
    "git metadata",
    "links",
    "verification tampering",
    "resource limits",
    "credentials",
    "agent-written test code",
    "report forging",
]
OUTCOMES = ("rejected", "contained", "residual", "breached")
BACKENDS = {"local": "jail_attack_evidence.json", "docker": "jail_attack_evidence_docker.json"}


class StaleError(RuntimeError):
    pass


# ------------------------------------------------------------- facts


def load(name: str) -> Any:
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def group_of(attack: dict[str, Any]) -> str:
    if attack["id"] in GROUP_OF_ID:
        return GROUP_OF_ID[attack["id"]]
    category = attack["category"]
    if category == "residual" or category not in GROUP_OF_CATEGORY:
        raise StaleError(f"attack {attack['id']} ({category!r}) has no table group; add it")
    return GROUP_OF_CATEGORY[category]


def facts() -> dict[str, Any]:
    bench = load("benchmark_results.json")
    for task in bench["tasks"]:
        if task["task_id"] not in TASK_LABELS:
            raise StaleError(f"benchmark task {task['task_id']} has no label; add it")
    jail = {backend: load(name) for backend, name in BACKENDS.items()}
    groups: dict[str, dict[str, Counter[str]]] = {}
    for backend, evidence in jail.items():
        for attack in evidence["attacks"]:
            groups.setdefault(group_of(attack), {}).setdefault(backend, Counter())[
                attack["outcome"]
            ] += 1
    with tempfile.TemporaryDirectory() as empty:
        tools = sorted(ToolRegistry(empty).specs)
    return {
        "bench": bench,
        "jail": jail,
        "groups": groups,
        "live": load("llm_benchmark_results.json"),
        "tools": tools,
    }


# ------------------------------------------------------------- markdown blocks


def md_bench_table(f: dict[str, Any]) -> str:
    rows = [
        "| task | verified | steps | replans | failed tool calls |",
        "|---|---|---|---|---|",
    ]
    for t in f["bench"]["tasks"]:
        rows.append(
            f"| {TASK_LABELS[t['task_id']]} | {'yes' if t['success'] else 'no'} | {t['steps']} "
            f"| {t['replans']} | {t['failed_tool_calls']} |"
        )
    s = f["bench"]["summary"]
    steps = sum(t["steps"] for t in f["bench"]["tasks"])
    rows.append(
        f"| **total** | **{s['n_success']}/{s['n_tasks']}** | **{steps}** "
        f"| **{s['total_replans']}** | **{s['total_failed_tool_calls']}** |"
    )
    return "\n".join(rows)


def md_jail_summary(f: dict[str, Any]) -> str:
    rows = [
        "| sandbox | attacks | rejected | contained | residual | breached |",
        "|---|---|---|---|---|---|",
    ]
    for backend, evidence in f["jail"].items():
        s = evidence["summary"]
        rows.append(
            f"| {backend} | {s['total']} | {s['rejected']} | {s['contained']} "
            f"| {s['residual']} | {s['breached']} |"
        )
    return "\n".join(rows)


def md_jail_groups(f: dict[str, Any]) -> str:
    rows = [
        "| category | local (rejected / contained / residual) | docker (rejected / contained / residual) |",
        "|---|---|---|",
    ]
    for group in GROUP_ORDER:
        cells = []
        for backend in BACKENDS:
            c = f["groups"].get(group, {}).get(backend, Counter())
            cells.append(
                f"{c['rejected']} / {c['contained']} / {c['residual']}" if c else "not run"
            )
        rows.append(f"| {group} | {cells[0]} | {cells[1]} |")
    return "\n".join(rows)


def md_residuals(f: dict[str, Any]) -> str:
    lines = []
    for backend, evidence in f["jail"].items():
        names = [
            f"{a['id']} {a['description']}"
            for a in evidence["attacks"]
            if a["outcome"] == "residual"
        ]
        lines.append(f"- **{backend}**: " + "; ".join(names) + ".")
    return "\n".join(lines)


def md_live_table(f: dict[str, Any]) -> str:
    live = f["live"]
    rows = [
        "| task | verified | steps | model calls | failed tool calls | wall time (s) |",
        "|---|---|---|---|---|---|",
    ]
    for t in live["tasks"]:
        rows.append(
            f"| {TASK_LABELS[t['task_id']]} | {'yes' if t['success'] else 'no'} | {t['steps']} "
            f"| {t['llm_calls']} | {t['failed_tool_calls']} | {t['wall_time_s']} |"
        )
    return "\n".join(rows)


def md_live_line(f: dict[str, Any]) -> str:
    live = f["live"]
    s = live["summary"]
    return (
        f"Recorded on {live['date'].split(' ')[0]} with `{live['provider']['model']}` and the earlier "
        f"prompt-parsed JSON planner: {s['n_success']}/{s['n_tasks']} tasks verified, "
        f"{s['total_llm_calls']} model calls, {s['total_prompt_tokens']} prompt and "
        f"{s['total_completion_tokens']} completion tokens, one attempt per task."
    )


def md_bench_line(f: dict[str, Any]) -> str:
    s = f["bench"]["summary"]
    return (
        f"Scripted mode: {s['n_success']}/{s['n_tasks']} tasks verified, {s['total_replans']} replans, "
        f"{s['total_failed_tool_calls']} failed tool calls."
    )


def md_tools(f: dict[str, Any]) -> str:
    names = ", ".join(f"`{n}`" for n in f["tools"])
    return f"The registry has {len(f['tools'])} built-in tools: {names}."


def md_jail_line(f: dict[str, Any]) -> str:
    parts = []
    for backend, evidence in f["jail"].items():
        s = evidence["summary"]
        parts.append(f"{s['total']} against the {backend} sandbox")
    return "Recorded adversarial calls: " + " and ".join(parts) + "."


def md_walkthrough(f: dict[str, Any]) -> str:
    """The README's 30-second tour of the hero image (docs/figures/hero_sketch.png).

    Each bullet names the image's boxes exactly as drawn. The counts come from
    results/, so they match the badges the image carries.
    """
    bench = f["bench"]["summary"]
    docker = f["jail"]["docker"]["summary"]
    residuals = [a["id"] for a in f["jail"]["docker"]["attacks"] if a["outcome"] == "residual"]
    if residuals != ["A46"]:
        raise StaleError(
            f"the walkthrough names A46 as the Docker residual, the evidence has {residuals}"
        )
    return "\n".join(
        [
            "- **Model → JSON Schema → Permission gate → Path jail.** The model, through"
            " native tool calls or MCP, only proposes. The runtime requires a known tool with"
            " valid arguments, applies AUTO, ASK or DENY per permission class, and keeps"
            " declared path arguments in the workspace. Anything refused lands in **Rejected"
            " calls return to the planner as observations**.",
            "- **Execution: Host tools, Docker sandbox, or Local fallback.** File tools (read,"
            " search, edit, `apply_patch`) run in the host process. `run_tests` and external"
            " MCP servers run in the Docker sandbox (no network, read-only root, capabilities"
            " dropped, non-root) or, without Docker, the Local fallback (rlimits or a Windows"
            f" Job Object, no network isolation). Against Docker: {docker['total']} attacks,"
            f" {docker['breached']} breaches, {docker['residual']} residual.",
            "- **Observation → State machine, 9 states.** Each result returns as an"
            " Observation (ok, exit code, redacted output, one JSONL trace event). INIT and"
            " ANALYZE_REPO lead into the PLAN, ACT, OBSERVE loop; failing tests or a failed"
            " verification go to REPLAN, which returns to PLAN while replans are left; a spent"
            " step budget, repeated planner errors or no replans left end in FAILED.",
            "- **VERIFY → COMPLETE, and the A46 note.** COMPLETE only follows VERIFY, which"
            " restores the protected paths and pytest control files, reruns the suite in the"
            " sandbox and needs exit status 0 plus a passing JUnit report"
            f" ({bench['n_success']}/{bench['n_tasks']} multi-file tasks with the scripted"
            " planner). Residual A46"
            " ([ADR 0007](docs/adr/0007-report-forging-is-outside-the-sandbox.md)): code under"
            " test could still forge a passing report; protected tests, mutants and review"
            " narrow it.",
        ]
    )


# How each transition is drawn in reports/figures/fsm.tex (node names are the
# state names). Every edge of state.TRANSITIONS needs exactly one route, so the
# figure cannot drift from the code. Order matters: failure drops come first,
# so the one crossing (REPLAN -> PLAN over ACT -> FAILED) is drawn on top with
# a white halo.
FSM_ROUTES: dict[tuple[State, State], tuple[str, str]] = {
    (State.INIT, State.FAILED): ("", "(INIT) -- (INIT |- FAILED.north)"),
    (State.ANALYZE_REPO, State.FAILED): ("", "(ANALYZE_REPO) -- (ANALYZE_REPO |- FAILED.north)"),
    (State.PLAN, State.FAILED): (
        "",
        "([xshift=-6mm]PLAN.south) -- ([xshift=-6mm]PLAN.south |- FAILED.north)",
    ),
    (State.ACT, State.FAILED): ("", "(ACT) -- (ACT |- FAILED.north)"),
    (State.REPLAN, State.FAILED): (
        "",
        "(REPLAN) -- node[lab, right] {no replans left} (REPLAN |- FAILED.north)",
    ),
    (State.VERIFY, State.FAILED): (
        "",
        "([xshift=3mm]VERIFY.south) -- ([xshift=3mm]VERIFY.south |- FAILED.north)",
    ),
    (State.INIT, State.ANALYZE_REPO): ("", "(INIT) -- (ANALYZE_REPO)"),
    (State.ANALYZE_REPO, State.PLAN): ("", "(ANALYZE_REPO) -- (PLAN)"),
    (State.PLAN, State.ACT): ("", "(PLAN) -- (ACT)"),
    (State.ACT, State.OBSERVE): ("", "(ACT) -- (OBSERVE)"),
    (State.OBSERVE, State.VERIFY): ("", "(OBSERVE) -- node[lab, above] {tests passed} (VERIFY)"),
    (State.VERIFY, State.COMPLETE): ("", "(VERIFY) -- node[lab, above] {verified} (COMPLETE)"),
    (State.OBSERVE, State.PLAN): (
        "",
        "(OBSERVE.north) .. controls +(-0.2,0.85) and +(0.2,0.85) .. "
        "node[lab, above] {next step} (PLAN.north east)",
    ),
    (State.PLAN, State.VERIFY): (
        "",
        "(PLAN.north west) .. controls +(0.4,1.9) and +(-0.4,1.9) .. "
        "node[lab, above] {planner calls finish} (VERIFY.north)",
    ),
    (State.OBSERVE, State.REPLAN): ("", "(OBSERVE) -- node[lab, left] {tests failed} (REPLAN)"),
    (State.VERIFY, State.REPLAN): (
        "",
        "([xshift=-3mm]VERIFY.south) |- node[lab, pos=0.3, left] {verification failed} "
        "([yshift=-1.5mm]REPLAN.east)",
    ),
    (State.REPLAN, State.PLAN): (
        ", halo",
        "(REPLAN.west) -| node[lab, pos=0.3, below] {replans left} (PLAN.south)",
    ),
}


def tex_fsm_edges(_f: dict[str, Any]) -> str:
    """One TikZ \\draw per transition in state.TRANSITIONS, in FSM_ROUTES order."""
    wanted = {(a, b) for a, targets in TRANSITIONS.items() for b in targets}
    missing = sorted(f"{a.value} -> {b.value}" for a, b in wanted - FSM_ROUTES.keys())
    extra = sorted(f"{a.value} -> {b.value}" for a, b in FSM_ROUTES.keys() - wanted)
    if missing or extra:
        raise StaleError(
            "the state figure does not match state.TRANSITIONS: "
            + (f"undrawn {', '.join(missing)}" if missing else "")
            + (f" not a transition {', '.join(extra)}" if extra else "")
        )
    lines = ["% Generated by scripts/sync_docs.py from state.TRANSITIONS; do not edit by hand."]
    for (a, b), (options, path) in FSM_ROUTES.items():
        lines.append(f"\\draw[edge{options}] {path}; % edge {a.value} -> {b.value}")
    return "\n".join(lines) + "\n"


MD_BLOCKS: dict[str, Callable[[dict[str, Any]], str]] = {
    "bench-table": md_bench_table,
    "bench-line": md_bench_line,
    "jail-summary": md_jail_summary,
    "jail-groups": md_jail_groups,
    "jail-residuals": md_residuals,
    "jail-line": md_jail_line,
    "live-table": md_live_table,
    "live-line": md_live_line,
    "tools": md_tools,
    "walkthrough": md_walkthrough,
}
MD_FILES = [ROOT / "README.md", ROOT / "docs" / "design_faq.md"]
BLOCK = re.compile(r"(<!-- generated:([a-z-]+) -->\n)(.*?)(<!-- /generated:\2 -->)", re.DOTALL)


def render_markdown(text: str, f: dict[str, Any], path: Path) -> str:
    def fill(match: re.Match[str]) -> str:
        name = match.group(2)
        if name not in MD_BLOCKS:
            raise StaleError(f"{path.name}: unknown generated block {name!r}")
        return f"{match.group(1)}{MD_BLOCKS[name](f)}\n{match.group(4)}"

    return BLOCK.sub(fill, text)


# ------------------------------------------------------------- LaTeX


def tex_numbers(f: dict[str, Any]) -> str:
    s = f["bench"]["summary"]
    live = f["live"]["summary"]
    jl, jd = f["jail"]["local"]["summary"], f["jail"]["docker"]["summary"]
    values = {
        "BenchTasks": s["n_tasks"],
        "BenchVerified": s["n_success"],
        "BenchReplans": s["total_replans"],
        "BenchFailedCalls": s["total_failed_tool_calls"],
        "BenchSteps": sum(t["steps"] for t in f["bench"]["tasks"]),
        "LiveTasks": live["n_tasks"],
        "LiveVerified": live["n_success"],
        "LiveCalls": live["total_llm_calls"],
        "LiveFailedCalls": live["total_failed_tool_calls"],
        "LivePlannerErrors": sum(t["planner_errors"] for t in f["live"]["tasks"]),
        "JailLocalTotal": jl["total"],
        "JailLocalRejected": jl["rejected"],
        "JailLocalContained": jl["contained"],
        "JailLocalResidual": jl["residual"],
        "JailLocalBreached": jl["breached"],
        "JailDockerTotal": jd["total"],
        "JailDockerRejected": jd["rejected"],
        "JailDockerContained": jd["contained"],
        "JailDockerResidual": jd["residual"],
        "JailDockerBreached": jd["breached"],
        "ToolCount": len(f["tools"]),
        # A break after each underscore keeps the justified column free of
        # underfull lines; code names never hyphenate.
        "ToolList": ", ".join(
            "\\texttt{" + name.replace("_", "\\_\\allowbreak{}") + "}" for name in f["tools"]
        ),
    }
    lines = ["% Generated by scripts/sync_docs.py from results/; do not edit by hand."]
    lines += [f"\\newcommand{{\\{name}}}{{{value}}}" for name, value in values.items()]
    return "\n".join(lines) + "\n"


def _tabular(source: str, columns: str, header: str, rows: list[str]) -> str:
    """A whole booktabs tabular: \\input inside a tabular would break \\bottomrule."""
    lines = [
        f"% Generated by scripts/sync_docs.py from {source}; do not edit by hand.",
        f"\\begin{{tabular}}{{@{{}}{columns}@{{}}}}",
        "\\toprule",
        f"{header} \\\\",
        "\\midrule",
        *rows,
        "\\bottomrule",
        "\\end{tabular}",
    ]
    return "\n".join(lines) + "\n"


def tex_bench_table(f: dict[str, Any]) -> str:
    rows: list[str] = []
    for t in f["bench"]["tasks"]:
        rows.append(
            f"{TASK_LABELS[t['task_id']]} & {'yes' if t['success'] else 'no'} & {t['steps']} "
            f"& {t['replans']} \\\\"
        )
    s = f["bench"]["summary"]
    steps = sum(t["steps"] for t in f["bench"]["tasks"])
    rows += [
        "\\midrule",
        f"Total & {s['n_success']}/{s['n_tasks']} & {steps} & {s['total_replans']} \\\\",
    ]
    return _tabular(
        "results/benchmark_results.json", "lrrr", "Task & Verified & Steps & Replans", rows
    )


def tex_live_table(f: dict[str, Any]) -> str:
    rows: list[str] = []
    tasks = f["live"]["tasks"]
    for t in tasks:
        rows.append(
            f"{TASK_LABELS[t['task_id']]} & {'yes' if t['success'] else 'no'} & {t['steps']} "
            f"& {t['llm_calls']} & {t['wall_time_s']} \\\\"
        )
    s = f["live"]["summary"]
    wall = round(sum(t["wall_time_s"] for t in tasks), 1)
    steps = sum(t["steps"] for t in tasks)
    rows += [
        "\\midrule",
        f"Total & {s['n_success']}/{s['n_tasks']} & {steps} & {s['total_llm_calls']} & {wall} \\\\",
    ]
    return _tabular(
        "results/llm_benchmark_results.json",
        "lrrrr",
        "Task & Verified & Steps & LLM calls & Wall (s)",
        rows,
    )


def tex_attack_table(f: dict[str, Any]) -> str:
    rows: list[str] = []
    for group in GROUP_ORDER:
        cells = []
        for backend in BACKENDS:
            c = f["groups"].get(group, {}).get(backend, Counter())
            cells.append(f"{c['rejected']}/{c['contained']}/{c['residual']}" if c else "n/a")
        rows.append(f"{group} & {cells[0]} & {cells[1]} \\\\")
    totals = []
    for backend in BACKENDS:
        s = f["jail"][backend]["summary"]
        totals.append(f"{s['rejected']}/{s['contained']}/{s['residual']} ({s['total']})")
    rows += ["\\midrule", f"Total & {totals[0]} & {totals[1]} \\\\"]
    return _tabular("results/jail_attack_evidence*.json", "lcc", "Category & Local & Docker", rows)


TEX_FILES: dict[str, Callable[[dict[str, Any]], str]] = {
    "numbers.tex": tex_numbers,
    "bench_table.tex": tex_bench_table,
    "live_table.tex": tex_live_table,
    "attack_table.tex": tex_attack_table,
    "fsm_edges.tex": tex_fsm_edges,
}


# ------------------------------------------------------------- driver


def outputs(f: dict[str, Any]) -> dict[Path, str]:
    out: dict[Path, str] = {}
    for path in MD_FILES:
        current = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        out[path] = render_markdown(current, f, path)
    for name, render in TEX_FILES.items():
        out[GENERATED_TEX / name] = render(f)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="fail if anything is stale")
    args = parser.parse_args(argv)
    try:
        wanted = outputs(facts())
    except StaleError as exc:
        print(f"sync_docs: {exc}", file=sys.stderr)
        return 1
    stale = []
    for path, text in wanted.items():
        have = path.read_text(encoding="utf-8").replace("\r\n", "\n") if path.exists() else None
        if have == text:
            continue
        stale.append(path.relative_to(ROOT).as_posix())
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
    if args.check and stale:
        print("stale (run python scripts/sync_docs.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    print(("updated: " + ", ".join(stale)) if stale else "all generated numbers are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
