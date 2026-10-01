"""Drop an `Index` from `__table_args__` when a `UniqueConstraint` of the same name is
already declared on that table.

`python scripts/dedupe_index_against_unique.py`

## Why this is needed

A `UNIQUE` constraint in Postgres is backed by an index of the same name, so it appears
in **both** `pg_constraint` (`contype = 'u'`) and `pg_indexes`. A script that reads one
source and classifies per row emits an `Index` *and* a `UniqueConstraint` under one name.

That is a real difference, not a cosmetic one: the model then declares the uniqueness
twice, `test_no_index_and_constraint_share_a_name` fails, and — worse — a migration
generated from the model would try to create an object that already exists.

So: when both are present, the **constraint** is kept. It is what the database recorded,
it enforces the same rule, and it carries the name in `pg_constraint` where a migration
looks for it.

## Why a separate script and not a phase of the one that adds them

`scripts/sync_model_constraints.py` only ever *adds*, on the principle that a removal is a
decision and a decision needs a human. This one removes, so it is kept where that
principle is visible rather than buried as an exception inside the adder — and because a
removal deserves its own name in the log.
"""

from __future__ import annotations

import ast
import pathlib
import sys

MODELS = pathlib.Path("src/ai_orchestrator/persistence")


def _index_name(node: ast.expr) -> str | None:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id != "Index" or not node.args:
        return None
    first = node.args[0]
    return first.value if isinstance(first, ast.Constant) else None


def _unique_name(node: ast.expr) -> str | None:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id != "UniqueConstraint":
        return None
    for keyword in node.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def dedupe(path: pathlib.Path) -> list[tuple[str, str]]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    lines = source.split("\n")
    kill: list[tuple[int, int]] = []
    report: list[tuple[str, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not (
                isinstance(stmt, ast.Assign)
                and any(getattr(t, "id", None) == "__table_args__" for t in stmt.targets)
            ):
                continue
            container = stmt.value
            elements = (
                list(container.args)
                if isinstance(container, ast.Call)
                else list(container.elts)
                if isinstance(container, ast.Tuple)
                else []
            )
            uniques = {n for n in (_unique_name(e) for e in elements) if n}
            for element in elements:
                name = _index_name(element)
                if name and name in uniques:
                    kill.append((element.lineno, element.end_lineno or element.lineno))
                    report.append((node.name, name))

    for start, end in sorted(kill, reverse=True):
        del lines[start - 1 : end]
    if kill:
        path.write_text("\n".join(lines))
    return report


def main() -> int:
    for path in sorted(MODELS.glob("*.py")):
        if path.stem == "__init__":
            continue
        for cls, name in dedupe(path):
            print(f"  {path.name}: {cls}: dropped Index({name!r}), a UniqueConstraint has it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
