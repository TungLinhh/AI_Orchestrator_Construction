"""Restore column-level `ForeignKey` declarations the model is missing, from the database.

`python scripts/restore_column_foreign_keys.py`

## Why this reads the database instead of taking a list

F133 cost two model files because the reconciliation was driven by a hand-typed list of
constraint names. A typo in a name is invisible: the column keeps a valid type, the module
imports, and the *absence* of a constraint is the only symptom, and an absent constraint
never raises.

So this script asks the database instead. The database is the authority — it already has
every constraint, applied by migration, checked by `test_schema_matches_models` — and the
model is the thing being reconciled. **The script can only ever add a declaration the
database already has.** It cannot invent a relationship, and it cannot remove one, so a
mistake in this file is a no-op rather than a silent deletion.

## The rule it enforces

For every **single-column** foreign key in the database that is not to `organization_id`:

* if the model's table has a table-level `ForeignKeyConstraint` covering that column, the
  relationship is already declared -- composite, and correct. Leave it.
* if the model's column has no `ForeignKey` at all, add `ForeignKey("<parent>.id")`.

The first branch is the important one. Migration `0020` made eighteen of these composite,
and re-adding a bare key on top of a composite one gives the relationship two
declarations under one name, because the naming convention
`fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` cannot tell them apart.

## What it does not touch

Multi-column foreign keys. Those are the `0020` work and they belong in `__table_args__`,
next to the indexes the pair needs; a column-level `ForeignKey` cannot express them.

Every insertion is reported with the table, the column and the parent, because a silent
repair is how six indexes went missing once.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sys

MODELS = pathlib.Path("src/ai_orchestrator/persistence")


def _read_single_column_foreign_keys() -> list[tuple[str, str, str, str]]:
    """`(child table, column, parent table, constraint name)` for every bare key."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_orchestrator.persistence.session import Database

    async def read() -> list[tuple[str, str, str, str]]:
        admin = Database.from_settings(use_admin_role=True)
        engine = create_async_engine(admin.engine.url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT c.conrelid::regclass::text          AS child,
                                   a.attname                          AS column_name,
                                   c.confrelid::regclass::text         AS parent,
                                   c.conname                          AS constraint_name
                              FROM pg_constraint c
                              JOIN pg_attribute a
                                ON a.attrelid = c.conrelid
                               AND a.attnum = c.conkey[1]
                             WHERE c.contype = 'f'
                               AND array_length(c.conkey, 1) = 1
                               AND a.attname <> 'organization_id'
                             ORDER BY child, column_name
                            """
                        )
                    )
                ).all()
        finally:
            await engine.dispose()
            await admin.engine.dispose()
        return [tuple(r) for r in rows]  # type: ignore[misc]

    return asyncio.run(read())


def _already_declared(table, column: str, parent: str) -> bool:
    """Whether the model declares the relationship for `column`, bare or composite.

    The test is deliberately narrow: a foreign key on this table that references `parent`
    **and** carries `column` as one of its local columns. The first version asked only
    "does any key on this table reference the parent", which is true for a *different*
    column's relationship -- `rfqs` references both `contracts` and `projects`, so a
    per-table test would have skipped `rfqs.contract_id` and reported the work as done.
    """
    for fk in table.foreign_key_constraints:
        if column not in fk.columns:
            continue
        if any(
            element.column.table.name == parent
            for element in fk.elements
            if element.column.table is not None
        ):
            return True
    return False


def _load_models() -> None:
    """Import every model module, so `Base.metadata` is complete.

    `Base` alone registers nothing. Without this the script sees nine tables and reports
    the other hundred as "not in the ORM", which reads like a database problem and is
    actually an import-order one.
    """
    import importlib

    for module in sorted(MODELS.glob("*.py")):
        if module.stem == "__init__":
            continue
        importlib.import_module(f"ai_orchestrator.persistence.{module.stem}")


def main() -> int:
    from ai_orchestrator.persistence.base import Base

    _load_models()
    keys = _read_single_column_foreign_keys()
    print(f"  the database has {len(keys)} single-column foreign keys")

    by_module: dict[pathlib.Path, list[tuple[str, str, str]]] = {}
    for child, column, parent, _name in keys:
        table = Base.metadata.tables.get(child)
        if table is None:
            print(f"  {child}: not in the ORM; skipped", file=sys.stderr)
            continue
        if _already_declared(table, column, parent):
            continue
        # Find the module that declares the table.
        for module in sorted(MODELS.glob("*.py")):
            if f'__tablename__ = "{child}"' in module.read_text():
                by_module.setdefault(module, []).append((column, parent))
                break
        else:
            print(f"  {child}: declared nowhere; skipped", file=sys.stderr)

    total = 0
    for module, entries in sorted(by_module.items()):
        source = module.read_text()
        tree = ast.parse(source, filename=str(module))
        lines = source.split("\n")
        edits: list[tuple[int, int, str]] = []
        # Several tables in one module share a column name -- `po_item_id` is on both
        # `material_reconciliations` and `receipt_items` -- so the report has to name the
        # table. The first version printed `po_item_id -> po_items.id` twice with no way
        # to tell the tables apart, which is how a bare key survived a composite one and
        # I could not say which of the two it belonged to.
        wanted: dict[str, list[tuple[str, str]]] = {}
        for child, column, parent in entries:
            wanted.setdefault(column, []).append((child, parent))

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
            for stmt in node.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                candidates = wanted.get(stmt.target.id, [])
                entry = next((c for c in candidates if c[0] == table), None)
                if entry is None or not isinstance(stmt.value, ast.Call):
                    continue
                if not stmt.value.args:
                    continue
                _child, parent = entry
                call = stmt.value
                call.args.insert(
                    1,
                    ast.Call(
                        func=ast.Name(id="ForeignKey", ctx=ast.Load()),
                        args=[ast.Constant(value=f"{parent}.id")],
                        keywords=[],
                    ),
                )
                rendered = ast.unparse(stmt)
                if len(rendered) > 92:
                    rendered = rendered.replace(
                        " = mapped_column(", " = mapped_column(\n        ", 1
                    )
                edits.append((stmt.lineno, stmt.end_lineno or stmt.lineno, "    " + rendered))
                total += 1

        for start, end, text_lines in sorted(edits, reverse=True):
            lines[start - 1 : end] = text_lines.split("\n")
        if edits:
            module.write_text("\n".join(lines))
            for child, column, parent in entries:
                print(f"  {module.name}: {child}.{column} -> {parent}.id restored")
    print(f"  {total} column-level key(s) restored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
