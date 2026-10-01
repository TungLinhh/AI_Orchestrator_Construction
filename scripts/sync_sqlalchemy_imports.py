"""Reconcile a module's `from sqlalchemy import (...)` block with what it actually uses.

`python scripts/sync_sqlalchemy_imports.py`

## Why this is a script and not a habit

Migration `0020` through `0023` moved 135 foreign keys from a column-level
`ForeignKey("parent.id")` to a table-level
`ForeignKeyConstraint(["organization_id", column], [...])`. That is a change to *which*
SQLAlchemy names a table needs, and the import list is the thing that tracks it:

* a module whose last bare `ForeignKey` was removed has an unused `ForeignKey` import --
  harmless, and `ruff --fix` deletes it;
* a module that gained a `ForeignKeyConstraint` and never imported the name does not
  import at all, with `NameError: name 'ForeignKeyConstraint' is not defined` pointing
  at the constraint rather than at the import.

Twenty-eight of the second, across six modules. Reading each file to find the import
block and add one name is six edits; doing it by pattern is the mistake F133 was made of.

## The rule

The names in the block are exactly the names the module references, in the order ruff
sorts them. Adding a name that is not used and removing one that is would both be
edits-in-the-wrong-direction, so this only ever *syncs*, and it prints both directions so
a removal is as visible as an addition.

The `__init__` module is skipped: it re-exports deliberately, and a name it does not
reference is the point.
"""

from __future__ import annotations

import ast
import pathlib
import sys

MODULES = pathlib.Path("src/ai_orchestrator/persistence")

#: The names this script manages inside a `from sqlalchemy import (...)` block.
#:
#: `JSONB` is deliberately absent: this repository imports it from
#: `sqlalchemy.dialects.postgresql`, and `from sqlalchemy import JSONB` also works while
#: leaving two import paths for one name in the same file. The first version of this set
#: included it, and the script dutifully added a second route to a type that was already
#: imported one line below.
MANAGED = frozenset(
    {
        "BigInteger",
        "Boolean",
        "CheckConstraint",
        "Date",
        "DateTime",
        "ForeignKey",
        "ForeignKeyConstraint",
        "Index",
        "Integer",
        "Numeric",
        "PrimaryKeyConstraint",
        "String",
        "Text",
        "UniqueConstraint",
        "text",
    }
)


def _names_used(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    return used


def _import_block(tree: ast.Module) -> ast.ImportFrom | None:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
            return node
    return None


def sync(path: pathlib.Path) -> tuple[list[str], list[str]]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    block = _import_block(tree)
    if block is None:
        return [], []

    present = {alias.name for alias in block.names}
    used = _names_used(tree)
    # A name in the block but not in `MANAGED` came from a submodule import
    # (`sqlalchemy.dialects.postgresql`), which is a different statement.
    wanted = {name for name in MANAGED if name in used}
    if not present & MANAGED:
        return [], []

    added = sorted(wanted - present)
    removed = sorted((present & MANAGED) - wanted)
    if not added and not removed:
        return [], []

    # `wanted`, not `present & wanted`: the first version built the block from the
    # intersection, so it reported every name it had "added" and wrote a file identical to
    # the one it read. The mtime moved, which is why it looked like it worked.
    kept = sorted(wanted) + sorted(present - MANAGED)
    lines = source.split("\n")
    start, end = block.lineno - 1, (block.end_lineno or block.lineno)
    rendered = ["from sqlalchemy import ("] + [f"    {name}," for name in kept] + [")"]
    lines[start:end] = rendered
    path.write_text("\n".join(lines))
    return added, removed


def main(argv: list[str] | None = None) -> int:
    paths = [pathlib.Path(p) for p in (argv or sys.argv[1:])] or sorted(MODULES.glob("*.py"))
    for path in paths:
        if path.stem == "__init__":
            continue
        added, removed = sync(path)
        if added:
            print(f"  {path.name}: imported {', '.join(added)}")
        if removed:
            print(f"  {path.name}: dropped unused {', '.join(removed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
