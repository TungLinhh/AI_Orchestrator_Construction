"""Merge duplicate `__table_args__` in one class, by AST.

`python scripts/merge_table_args.py src/ai_orchestrator/persistence/procurement.py`

**Why this exists.** SQLAlchemy reads `__table_args__` as a class attribute, so a second
assignment in the same class body silently wins and the first is discarded. Migration
`0020` needed composite foreign keys on nine tables, and adding them as a second
`__table_args__` per class is exactly how a reader loses the table's existing indexes --
and how this repository lost six of them, once, during a text-based edit.

The rule this enforces is small and worth having mechanically:

    **a class gets one `__table_args__`.**

The transform is AST-based rather than text-based because a text edit cannot tell a
class body from a function body, and the failure mode of guessing is a constraint
attached to the wrong class -- which imports cleanly, migrates cleanly, and enforces
nothing.

Every merge is reported with the table it touched, so the output is reviewable and a
silent merge is not possible.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


class _Merger(ast.NodeTransformer):
    def __init__(self) -> None:
        self.merged: list[tuple[str, int, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        self.generic_visit(node)
        targets: list[ast.Assign] = []
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                getattr(t, "id", None) == "__table_args__" for t in stmt.targets
            ):
                targets.append(stmt)
        if len(targets) < 2:
            return node

        first = targets[0]
        elements: list[ast.expr] = []
        for assign in targets:
            if not isinstance(assign.value, ast.Tuple):
                # A non-tuple is not something to merge; leave it alone and say so.
                print(
                    f"  {node.name}: __table_args__ is not a tuple; left as the first is",
                    file=sys.stderr,
                )
                return node
            elements.extend(assign.value.elts)

        # Deduplicate by source text: a merged table whose two blocks overlapped would
        # otherwise declare the same constraint twice and Postgres would refuse it.
        seen: set[str] = set()
        unique: list[ast.expr] = []
        for element in elements:
            try:
                key = ast.unparse(element)
            except Exception:
                key = repr(element)
            if key in seen:
                continue
            seen.add(key)
            unique.append(element)

        merged = ast.Assign(
            targets=[ast.Name(id="__table_args__", ctx=ast.Store())],
            value=ast.Tuple(elts=unique, ctx=ast.Load()),
        )
        ast.copy_location(merged, first)
        ast.fix_missing_locations(merged)

        out: list[ast.stmt] = []
        placed = False
        for stmt in node.body:
            if stmt in targets:
                if not placed:
                    out.append(merged)
                    placed = True
                continue
            out.append(stmt)
        node.body = out
        self.merged.append((node.name, len(targets), len(unique)))
        return node


def main(argv: list[str] | None = None) -> int:
    paths = [Path(p) for p in (argv or sys.argv[1:])]
    if not paths:
        print(__doc__.split("\n")[0], file=sys.stderr)
        return 2
    for path in paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        merger = _Merger()
        new_tree = merger.visit(tree)
        ast.fix_missing_locations(new_tree)
        if not merger.merged:
            print(f"  {path.name}: nothing to merge")
            continue
        for name, before, after in merger.merged:
            print(f"  {path.name}: {name}: {before} __table_args__ -> 1, {after} constraints")
        path.write_text(ast.unparse(new_tree) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
