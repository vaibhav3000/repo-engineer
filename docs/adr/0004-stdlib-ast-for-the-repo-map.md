# Build the symbol index with the stdlib ast module, not tree-sitter

The repo map and `find_symbol` index Python sources with `ast`. tree-sitter
would tolerate syntax errors and cover other languages, but it adds two
compiled dependencies whose grammar ABI has to match the binding, and this
project's benchmark and target repositories are Python. `ast` gives exact
CPython semantics with no install. A file that does not parse is still listed
in the map, without symbols, so a syntax error the agent introduced is visible
rather than silently dropped. If non-Python repositories become a goal,
tree-sitter belongs behind the same index interface as an optional extra.
