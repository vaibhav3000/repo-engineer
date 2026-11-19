"""The ast symbol index, the ranked repo map and the repo_map / find_symbol tools."""

from __future__ import annotations

from pathlib import Path

from repo_engineer.repo_map import SymbolIndex
from repo_engineer.state import ToolCall
from repo_engineer.tools import ToolRegistry

SOURCES = {
    "shop/cart.py": (
        '"""Carts."""\n\n\nclass Cart:\n    """A shopping cart."""\n\n'
        "    def __init__(self):\n        self.items = []\n\n"
        "    def add(self, item: str, qty: int = 1) -> None:\n        self.items.append(item)\n\n\n"
        'def total(items: list[int]) -> int:\n    """Sum the prices.\n\n    More text."""\n'
        "    return sum(items)\n"
    ),
    "shop/checkout.py": (
        "from shop.cart import Cart, total\n\n\n"
        "async def checkout(cart: Cart) -> int:\n    return total([1, 2])\n"
    ),
    "tests/test_cart.py": "from shop.cart import total\n\n\ndef test_total():\n    assert total([1]) == 1\n",
    "broken.py": "def oops(:\n    pass\n",
}


def test_definitions_have_kinds_lines_and_signatures() -> None:
    index = SymbolIndex.build(SOURCES)
    by_name = {s.qualname: s for s in index.symbols}
    assert by_name["Cart"].kind == "class" and by_name["Cart"].line == 4
    assert by_name["Cart.add"].kind == "method"
    # ast.unparse writes annotated defaults without spaces around "=".
    assert by_name["Cart.add"].signature == "add(self, item: str, qty: int=1) -> None"
    assert by_name["total"].doc == "Sum the prices."
    assert (by_name["total"].line, by_name["total"].end_line) == (14, 18)
    assert by_name["checkout"].signature.startswith("async checkout(")


def test_files_that_do_not_parse_are_reported_not_dropped() -> None:
    index = SymbolIndex.build(SOURCES)
    assert index.unparsed == {"broken.py": 1}
    assert "broken.py" in index.render_map(10_000)


def test_references_are_found_across_files() -> None:
    definitions, references = SymbolIndex.build(SOURCES).find("total")
    assert [d.path for d in definitions] == ["shop/cart.py"]
    places = {(r.path, r.line) for r in references}
    assert ("shop/checkout.py", 1) in places and ("shop/checkout.py", 5) in places
    assert ("tests/test_cart.py", 5) in places


def test_qualified_names_find_methods() -> None:
    definitions, _ = SymbolIndex.build(SOURCES).find("Cart.add")
    assert [(d.qualname, d.line) for d in definitions] == [("Cart.add", 10)]


def test_the_map_ranks_referenced_files_first_and_respects_the_budget() -> None:
    text = SymbolIndex.build(SOURCES).render_map(10_000)
    assert text.index("shop/cart.py") < text.index("shop/checkout.py")
    assert "def total(items: list[int]) -> int" in text
    short = SymbolIndex.build(SOURCES).render_map(80)
    assert len(short) <= 80 + 40 and "more files" in short


def test_the_tools_work_in_a_registry(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    for rel, text in SOURCES.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(text, encoding="utf-8")
    registry = ToolRegistry(ws)
    repo_map = registry.execute(ToolCall("repo_map", {}))
    assert repo_map.ok and "class Cart" in repo_map.output_excerpt
    found = registry.execute(ToolCall("find_symbol", {"name": "total"}))
    assert found.ok
    assert "definition shop/cart.py:14-18" in found.output_excerpt
    assert "reference shop/checkout.py:5" in found.output_excerpt
    missing = registry.execute(ToolCall("find_symbol", {"name": "nope"}))
    assert missing.ok and "no definition" in missing.output_excerpt


def test_the_map_skips_links(tmp_path: Path) -> None:
    from conftest import make_dir_link

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("def secret():\n    pass\n", encoding="utf-8")
    make_dir_link(ws / "vendor", outside)
    text = ToolRegistry(ws).execute(ToolCall("repo_map", {})).output_excerpt
    assert "a.py" in text and "secret" not in text


def test_a_file_too_deep_for_the_parser_is_listed_not_fatal() -> None:
    """Found by the Step 2 review: RecursionError escaped and broke the whole index."""
    deep = "x = " + " + ".join(['"ab"'] * 6000) + "\n"
    index = SymbolIndex.build({**SOURCES, "generated.py": deep})
    assert "generated.py" in index.unparsed
    assert "total" in {s.qualname for s in index.symbols}
    assert "generated.py" in index.render_map(10_000)


def test_a_run_starts_even_if_the_repo_map_fails(workspace: Path) -> None:
    from repo_engineer.agent import AgentRuntime
    from repo_engineer.planner import RecordedPlanner

    (workspace / "generated.py").write_text(
        "x = " + " + ".join(['"ab"'] * 6000) + "\n", encoding="utf-8"
    )
    state = AgentRuntime(ToolRegistry(workspace), RecordedPlanner([None])).run_task(
        {"task_id": "t"}
    )
    assert state.state.value == "COMPLETE"
