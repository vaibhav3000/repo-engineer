"""A symbol index and a ranked repository map built with the stdlib ast module.

The index records every class, function and method (qualified name, kind,
line range, signature, first docstring line) and every place a defined
name is used (imports, calls, attribute access). The repo map lists files,
most-referenced first, with their symbols, cut to a character budget. A
file that does not parse is listed with the line of its syntax error rather
than dropped, so a syntax error the agent introduced stays visible.

Only Python is indexed; tree-sitter was considered and left out
(docs/adr/0004).
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Symbol:
    qualname: str  # "Cart.add" for a method, "total" for a function
    kind: str  # "class", "function" or "method"
    path: str
    line: int
    end_line: int
    signature: str
    doc: str  # first line of the docstring, or ""

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class Reference:
    name: str
    path: str
    line: int


class SymbolIndex:
    """Definitions and references across a set of Python sources."""

    def __init__(
        self,
        symbols: list[Symbol],
        references: list[Reference],
        unparsed: dict[str, int],
        files: list[str],
    ) -> None:
        self.symbols = symbols
        self.references = references
        self.unparsed = unparsed  # path -> line of the syntax error
        self.files = files

    @classmethod
    def build(cls, sources: Mapping[str, str]) -> SymbolIndex:
        """Index sources given as {workspace-relative POSIX path: text}."""
        symbols: list[Symbol] = []
        trees: dict[str, ast.Module] = {}
        unparsed: dict[str, int] = {}
        for path in sorted(sources):
            try:
                tree = ast.parse(sources[path], filename=path)
                found = _definitions(tree, path)
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                # RecursionError: an expression nested deeper than the parser
                # (or ast.unparse) can follow; such a file is listed, not fatal.
                unparsed[path] = getattr(exc, "lineno", None) or 0
                continue
            trees[path] = tree
            symbols.extend(found)
        defined = {s.name for s in symbols}
        references = [
            ref for path, tree in trees.items() for ref in _references(tree, path, defined)
        ]
        return cls(symbols, references, unparsed, sorted(sources))

    def find(self, name: str) -> tuple[list[Symbol], list[Reference]]:
        """Definitions matching a plain or qualified name, and references to it."""
        definitions = [s for s in self.symbols if name in (s.qualname, s.name)]
        short = name.rsplit(".", 1)[-1]
        definition_lines = {(s.path, s.line) for s in definitions}
        references = sorted(
            (
                r
                for r in self.references
                if r.name == short and (r.path, r.line) not in definition_lines
            ),
            key=lambda r: (r.path, r.line),
        )
        return definitions, references

    def render_map(self, budget_chars: int) -> str:
        """Files ranked by how often other files use their symbols, within the budget."""
        owner = {}
        for symbol in self.symbols:
            owner.setdefault(symbol.name, set()).add(symbol.path)
        score: Counter[str] = Counter()
        for ref in self.references:
            for path in owner.get(ref.name, ()):
                if path != ref.path:
                    score[path] += 1
        ranked = sorted(self.files, key=lambda p: (-score[p], p))
        by_file: dict[str, list[Symbol]] = {}
        for symbol in self.symbols:
            by_file.setdefault(symbol.path, []).append(symbol)
        lines: list[str] = []
        used = 0
        for count, path in enumerate(ranked):
            rows = _file_block(path, by_file.get(path, []), self.unparsed.get(path))
            fitted = 0
            for row in rows:
                if used + len(row) + 1 > budget_chars and (lines or fitted):
                    break
                lines.append(row)
                used += len(row) + 1
                fitted += 1
            if fitted < len(rows):
                if fitted:
                    lines.append(f"    ... {len(rows) - fitted} more symbols")
                lines.append(f"... {len(ranked) - count - (1 if fitted else 0)} more files")
                break
        return "\n".join(lines) if lines else "(no Python files)"


def _file_block(path: str, symbols: list[Symbol], syntax_error_line: int | None) -> list[str]:
    if syntax_error_line is not None:
        where = (
            f"syntax error at line {syntax_error_line}"
            if syntax_error_line
            else "too deeply nested"
        )
        return [f"{path}  (does not parse: {where})"]
    rows = [path]
    for symbol in sorted(symbols, key=lambda s: s.line):
        indent = "    " if symbol.kind == "method" else "  "
        keyword = "class" if symbol.kind == "class" else "def"
        doc = f"  # {symbol.doc}" if symbol.doc else ""
        rows.append(f"{indent}{keyword} {symbol.signature}  L{symbol.line}-{symbol.end_line}{doc}")
    return rows


def _definitions(tree: ast.Module, path: str) -> list[Symbol]:
    found: list[Symbol] = []

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                signature = f"{node.name}({bases})" if bases else node.name
                found.append(_symbol(node, prefix + node.name, "class", path, signature))
                visit(node.body, f"{prefix}{node.name}.", True)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "function"
                found.append(_symbol(node, prefix + node.name, kind, path, _signature(node)))

    visit(tree.body, "", False)
    return found


def _symbol(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    qualname: str,
    kind: str,
    path: str,
    signature: str,
) -> Symbol:
    doc = (ast.get_docstring(node) or "").strip().splitlines()
    return Symbol(
        qualname=qualname,
        kind=kind,
        path=path,
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        signature=signature,
        doc=doc[0] if doc else "",
    )


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({ast.unparse(node.args)}){returns}"


def _references(tree: ast.Module, path: str, defined: set[str]) -> list[Reference]:
    found: list[Reference] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in defined:
            found.append(Reference(node.id, path, node.lineno))
        elif isinstance(node, ast.Attribute) and node.attr in defined:
            found.append(Reference(node.attr, path, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            found.extend(
                Reference(alias.name, path, node.lineno)
                for alias in node.names
                if alias.name in defined
            )
    return sorted(set(found), key=lambda r: (r.path, r.line, r.name))
