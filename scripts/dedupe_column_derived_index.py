"""Drop an `Index` from `__table_args__` that a column already creates.

`python scripts/dedupe_column_derived_index.py`

## What it removes, and why it is not a bug

A column declared `unique=True` or `index=True` makes SQLAlchemy create an index for it,
named by the naming convention. `audit_logs.sequence` is declared
`unique=True`, so the metadata already holds an index called `ix_audit_logs_sequence` —
and the schema has one too, under the same name.

`rebuild_model_table_args.py` then emits `Index("ix_audit_logs_sequence", "sequence",
unique=True)` inside `__table_args__`, because it reads the schema and the schema says the
index exists. Both are correct on their own and together they are one index declared
twice, which `test_no_table_declares_one_index_name_twice` reports.

**The `__table_args__` one is dropped**, for two reasons. The column-level declaration is
the one that carries the intent — `unique=True` next to the column says what the column
*is*, while an `Index` in `__table_args__` says the same thing a screen away. And a
migration generated from a model that declares both would try to create an object that
exists.

This has to run *after* the rebuild, and it is the only step in the pipeline that imports
the models — which is why it is a separate file rather than a phase of the rebuild. A
script that repairs the models cannot import them while they are still broken.

## The criterion

An `Index` in `__table_args__` is dropped when the same class has a column whose
`unique=True` or `index=True` and whose convention name matches. Matching on the column
list alone would drop a legitimate second index over the same columns; matching on the name
is what makes it the *same* index.
"""

from __future__ import annotations

import ast
import pathlib
import sys

MODELS = pathlib.Path("src/ai_orchestrator/persistence")

#: The convention's index prefixes, tried in order. A column-level `unique=True` uses
#: `ix_`; a `UniqueConstraint` uses `uq_`.
PREFIXES = ("ix_", "uq_", "ck_")


def _column_index_names(node: ast.ClassDef, table: str) -> set[str]:
    """The index names the class's columns create for themselves."""
    names: set[str] = set()
    for stmt in node.body:
        if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call)):
            continue
        call = stmt.value
        if not (isinstance(call.func, ast.Name) and call.func.id == "mapped_column"):
            continue
        flagged = False
        for keyword in call.keywords:
            if keyword.arg in ("unique", "index") and isinstance(keyword.value, ast.Constant):
                flagged = bool(keyword.value.value)
        if not flagged:
            continue
        column = stmt.target.id if isinstance(stmt.target, ast.Name) else None
        if not column:
            continue
        for prefix in PREFIXES:
            names.add(f"{prefix}{table}_{column}")
    return names


def dedupe(path: pathlib.Path) -> list[str]:
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    lines = source.split("\n")
    kill: list[tuple[int, int]] = []
    report: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        tables = [
            b.value.value
            for b in node.body
            if isinstance(b, ast.Assign)
            and any(getattr(t, "id", None) == "__tablename__" for t in b.targets)
            and isinstance(b.value, ast.Constant)
        ]
        if not tables:
            continue
        table = tables[0]
        derived = _column_index_names(node, table)
        if not derived:
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
            for element in elements:
                if not (
                    isinstance(element, ast.Call)
                    and isinstance(element.func, ast.Name)
                    and element.func.id == "Index"
                    and element.args
                    and isinstance(element.args[0], ast.Constant)
                ):
                    continue
                name = str(element.args[0].value)
                if name in derived:
                    kill.append((element.lineno, element.end_lineno or element.lineno))
                    report.append(f"{table}: {name} (a column already creates it)")

    for start, end in sorted(kill, reverse=True):
        del lines[start - 1 : end]
    if kill:
        path.write_text("\n".join(lines))
    return report


def main() -> int:
    total = 0
    for path in sorted(MODELS.glob("*.py")):
        if path.stem == "__init__":
            continue
        for item in dedupe(path):
            print(f"  {path.name}: dropped {item}")
            total += 1
    print(f"  {total} column-derived index(es) left to the column")
    return 0


if __name__ == "__main__":
    sys.exit(main())
