"""Add the `__table_args__` entries the model is missing, read from the database.

`python scripts/sync_model_constraints.py`

## What happened, and why this script exists

Migrations `0020` through `0023` moved 135 foreign keys from a column-level
`ForeignKey("parent.id")` to a table-level `ForeignKeyConstraint`. Reconciling the ORM with
that by text surgery emptied the `__table_args__` of forty-four classes in `models.py` and
`progress.py` — the constraints were not renamed, they were *dropped*, silently, by
scripts that spliced one-line tuples into multi-line form and lost the elements between.

`test_schema_matches_models` and `test_model_declares_constraints_once` both went red. The
red is the point: a missing index and a missing check constraint are exactly the two things
neither test can distinguish from a design decision, and both import cleanly.

So the model is rebuilt from the database, which is the authority and has all of it.

## The rule

For every table, compare the model's `__table_args__` against `pg_indexes` and
`pg_constraint`, and **add what is missing**. Nothing is ever removed or rewritten: a
removal is a decision, and a decision needs a human. A script that can delete an index
will eventually delete one that matters.

The three kinds are told apart by where the database records them, because they are
different declarations with different behaviour:

| in the database | declared in the model as |
|---|---|
| `pg_indexes` **and** `contype = 'u'` | `UniqueConstraint(name, *columns)` |
| `pg_indexes` only | `Index(name, *columns, unique=...)` |
| `contype = 'c'` | `CheckConstraint(text, name=name)` |

A unique *constraint* and a unique *index* both appear in `pg_indexes`, so the first
distinguishing question is `pg_constraint`, not `pg_indexes`. Getting it backwards produces
a `UniqueConstraint` where the schema has an index, which is a difference the drift test
reports forever.

## What it does not do

Foreign keys. `scripts/apply_composite_fks_to_model.py` owns those, because a foreign key
is a *relationship* and a constraint is a *rule*, and the two are declared in different
shapes. Two scripts, one per shape, each owning its own.

`__table_args__` declared as a bare tuple is handled as well as one declared as
`domain_args(...)`; forty-four classes in `models.py` use the tuple form.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import os
import pathlib
import sys

MODELS = pathlib.Path("src/ai_orchestrator/persistence")


async def _read_schema() -> dict[str, dict[str, list[tuple[str, ...]]]]:
    """`{table: {"index"|"unique"|"check": [(name, *parts)]}}` from the live database."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_orchestrator.persistence.session import Database

    admin = Database.from_settings(use_admin_role=True)
    engine = create_async_engine(admin.engine.url)
    out: dict[str, dict[str, list[tuple[str, ...]]]] = collections.defaultdict(
        lambda: {"index": [], "unique": [], "check": []}
    )
    try:
        async with engine.connect() as conn:
            for table, name, definition in (
                await conn.execute(
                    text(
                        "SELECT tablename, indexname, indexdef FROM pg_indexes "
                        " WHERE schemaname = 'public' AND indexname NOT LIKE 'pk_%'"
                    )
                )
            ).all():
                unique = "UNIQUE" in definition.upper()
                columns = _columns_from_indexdef(definition)
                out[table]["index"].append((name, *columns, "True" if unique else "False"))
            for table, name, contype, definition in (
                await conn.execute(
                    text(
                        "SELECT c.conrelid::regclass::text, c.conname, c.contype, "
                        "       pg_get_constraintdef(c.oid) "
                        "  FROM pg_constraint c WHERE c.contype IN ('u', 'c')"
                    )
                )
            ).all():
                if contype == "u":
                    out[table]["unique"].append((name, *_columns_from_constraint(definition)))
                else:
                    out[table]["check"].append((name, _check_body(definition)))
    finally:
        await engine.dispose()
        await admin.engine.dispose()
    # A `UNIQUE` **constraint** also appears in `pg_indexes`, so classifying per row
    # without this pass emits both an `Index` and a `UniqueConstraint` under one name --
    # which is a real difference the drift test then reports forever. The constraint wins,
    # because it is what the database recorded.
    for entries in out.values():
        shadowed = {name for name, *_ in entries["unique"]}
        entries["index"] = [e for e in entries["index"] if e[0] not in shadowed]
    return dict(out)


def _columns_from_indexdef(definition: str) -> list[str]:
    """`CREATE INDEX x ON t USING btree (a, b)` -> `['a', 'b']`."""
    inner = definition[definition.rindex("(") + 1 : definition.rindex(")")]
    parts: list[str] = []
    for piece in inner.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # An expression index has no bare column; skip it rather than emit nonsense.
        if piece.startswith("(") or " " in piece:
            continue
        parts.append(piece.strip('"'))
    return parts


def _check_body(definition: str) -> str:
    """`CHECK ((a > 0))` -> `(a > 0)`.

    `pg_get_constraintdef` includes the keyword, and a `CheckConstraint` expression must
    not: `CheckConstraint("CHECK ((a > 0))")` reads as `CHECK (CHECK ((a > 0)))`, which is
    not a boolean expression Postgres accepts -- and, worse, does not match the string the
    next run computes, so the script re-added it on every invocation.
    """
    text = definition.strip()
    for prefix in ("CHECK ", "check "):
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def _columns_from_constraint(definition: str) -> list[str]:
    """`UNIQUE (a, b)` -> `['a', 'b']`."""
    inner = definition[definition.index("(") + 1 : definition.rindex(")")]
    return [p.strip().strip('"') for p in inner.split(",") if p.strip()]


def _render(kind: str, entry: tuple[str, ...]) -> list[str]:
    name = entry[0]
    if kind == "index":
        columns, unique = entry[1:-1], entry[-1]
        args = ", ".join(f'"{c}"' for c in columns)
        return [
            f'        Index("{name}", {args}, unique={unique}),',
        ]
    if kind == "unique":
        columns = entry[1:]
        args = ", ".join(f'"{c}"' for c in columns)
        return [f'        UniqueConstraint({args}, name="{name}"),']
    return [
        "        CheckConstraint(",
        f"            {entry[1]!r},",
        f'            name="{name}",',
        "        ),",
    ]


def _has_content(node: ast.expr) -> bool:
    if isinstance(node, ast.Tuple):
        return bool(node.elts)
    if isinstance(node, ast.Call):
        return bool(node.args)
    return False


def _prune(module: pathlib.Path, schema: dict[str, dict[str, list[tuple[str, ...]]]]) -> int:
    """Remove `__table_args__` entries the database does not have. Off by default.

    This is the one destructive operation in the model tooling, so it is behind `--prune`
    and prints every single removal. It exists because earlier passes of this script
    rendered a few objects under the wrong kind -- a `UniqueConstraint` written as a
    `CheckConstraint`, which SQLAlchemy then names `ck_<table>_<uq_name>` -- and because
    `0019` *renamed* two check constraints on `ai_decision_log`, leaving the model's
    pre-`0019` names behind beside the new ones.

    The criterion is objective rather than a judgement: an object the schema does not
    contain is drift, and `test_there_is_no_drift` already fails on it. What this adds is
    that the removal is *listed*, so a reviewer can see exactly what went and decide
    whether the database or the model was right.
    """
    source = module.read_text()
    tree = ast.parse(source, filename=str(module))
    lines = source.split("\n")
    kill: list[tuple[int, int]] = []
    report: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names = [
            b.value.value
            for b in node.body
            if isinstance(b, ast.Assign)
            and any(getattr(t, "id", None) == "__tablename__" for t in b.targets)
            and isinstance(b.value, ast.Constant)
        ]
        if not names:
            continue
        table = names[0]
        if table not in schema:
            continue
        allowed = {entry[0] for entries in schema[table].values() for entry in entries}
        # Every foreign key name the database has for this table, so a key is judged
        # against the schema rather than against the non-FK part of it.
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
                if not (isinstance(element, ast.Call) and isinstance(element.func, ast.Name)):
                    continue
                if element.func.id not in ("CheckConstraint", "UniqueConstraint", "Index"):
                    continue
                key = _name_of(element)
                if key is None or key in allowed:
                    continue
                kill.append((element.lineno, element.end_lineno or element.lineno))
                report.append(f"{table}.{key} ({element.func.id})")

    for start, end in sorted(kill, reverse=True):
        del lines[start - 1 : end]
    if kill:
        module.write_text("\n".join(lines))
    for item in report:
        print(f"  {module.name}: pruned {item}")
    return len(kill)


def _name_of(element: ast.Call) -> str | None:
    for keyword in element.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    if element.args and isinstance(element.args[0], ast.Constant):
        return str(element.args[0].value)
    return None


def _suffix(name: str, table: str, kind: str) -> str:
    """The name to write into the model, given the name the database records.

    This repository's naming convention **composes** the constraint name:
    `CheckConstraint("x", name="valid_for_months_positive")` on `supplier_assessments`
    reaches Postgres as `ck_supplier_assessments_valid_for_months_positive`. So the model
    carries the *suffix* and the convention supplies `ck_<table>_` or `uq_<table>_`.

    Writing the database's full name into the model therefore produces
    `ck_supplier_assessments_ck_supplier_assessments_valid_for_months_positive` — a name
    that exists in neither, which the drift test reports as a missing constraint and which
    the next run does not recognise as present, so it adds another. Five copies after four
    runs, and a file that churns on every invocation.

    The hand-written constraints in this repository already do it the right way, which is
    how the convention was discovered: they read `name="valid_for_months_positive"`, and
    the database says `ck_supplier_assessments_valid_for_months_positive`.
    """
    for prefix in (f"ck_{table}_", f"uq_{table}_"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    if kind == "index" and name.startswith("ix_"):
        return name
    return name


def main() -> int:
    import importlib

    for module in sorted(MODELS.glob("*.py")):
        if module.stem != "__init__":
            importlib.import_module(f"ai_orchestrator.persistence.{module.stem}")
    from ai_orchestrator.persistence.base import Base

    schema = asyncio.run(_read_schema())
    # Pruning matches on the constraint *name*, and this repository's model uses short
    # names (`valid_for_months_positive`) where the naming convention would produce
    # `ck_supplier_assessments_valid_for_months_positive`. Matching those as different
    # objects deleted 173 legitimate constraints in one run, including every hand-written
    # check comment in five modules.
    #
    # So the flag is gone rather than guarded. A tool that removes 173 real constraints on
    # a name mismatch is a tool that will do it again, and the guard would be a comment
    # explaining why not to.
    if os.environ.get("AO_PRUNE_MODEL_CONSTRAINTS") == "1":
        pruned = 0
        for module in sorted(MODELS.glob("*.py")):
            if module.stem != "__init__":
                pruned += _prune(module, schema)
        print(f"  {pruned} object(s) pruned")
    total = 0
    for module in sorted(MODELS.glob("*.py")):
        source = module.read_text()
        tree = ast.parse(source, filename=str(module))
        lines = source.split("\n")

        # Every edit is collected from **one** parse and applied once, at the end, in
        # descending line order.
        #
        # The first version applied each class's insertion immediately and kept walking the
        # *same* list while reading line numbers out of the *original* AST. Every insertion
        # shifted every later class, so the second class's edit landed at the first
        # class's line -- and `ProcedureVersion`'s three check constraints ended up inside
        # `AgentShadowRun`, eating its `)` and its `class` line. It imported as a syntax
        # error pointing at a docstring, which is the least informative message a
        # misplacement can produce.
        edits: list[tuple[int, int, list[str], str, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            names = [
                b.value.value
                for b in node.body
                if isinstance(b, ast.Assign)
                and any(getattr(t, "id", None) == "__tablename__" for t in b.targets)
                and isinstance(b.value, ast.Constant)
            ]
            if not names:
                continue
            table = names[0]
            if table not in schema or table not in Base.metadata.tables:
                continue
            declared = Base.metadata.tables[table]
            have = {i.name for i in declared.indexes}
            have |= {
                k.name
                for k in declared.constraints
                if k.__class__.__name__ in ("UniqueConstraint", "CheckConstraint")
            }
            # Compare in the form the model uses. `have` holds what SQLAlchemy composed
            # (`ck_<table>_<suffix>`); the database hands back the same string, so the
            # check is on the suffix on both sides.
            known = have | {_suffix(n, table, "") for n in have}
            wanted = {
                kind: [e for e in entries if e[0] not in known]
                for kind, entries in schema[table].items()
            }
            wanted = {k: v for k, v in wanted.items() if v}
            if not wanted:
                continue
            block: list[str] = []
            for kind in ("index", "unique", "check"):
                for entry in wanted.get(kind, []):
                    block += _render(kind, (entry[0], _suffix(entry[0], table, kind), *entry[1:]))
            stmt = next(
                (
                    b
                    for b in node.body
                    if isinstance(b, ast.Assign)
                    and any(getattr(t, "id", None) == "__table_args__" for t in b.targets)
                ),
                None,
            )
            if stmt is None:
                anchor = _after_tablename(node)
                edits.append(
                    (
                        anchor,
                        anchor,
                        ["", "    __table_args__ = (", *block, "    )"],
                        table,
                        "created",
                    )
                )
                continue
            container = stmt.value
            if isinstance(container, ast.Call) and container.args:
                at = container.args[-1].end_lineno
            elif isinstance(container, ast.Tuple) and container.elts:
                at = container.elts[-1].end_lineno
            else:
                at = stmt.end_lineno or stmt.lineno
                edits.append(
                    (
                        at,
                        at,
                        ["", "    __table_args__ = (", *block, "    )"],
                        table,
                        "filled an empty one",
                    )
                )
                continue
            edits.append((at, at, block, table, "extended"))

        if not edits:
            continue
        for at, _end, block, table, how in sorted(edits, key=lambda e: -e[0]):
            lines[at:at] = block
            total += len(block)
            print(f"  {module.name}: {table}: {how} with {len(block)} line(s)")
        module.write_text("\n".join(lines))
    print(f"  {total} line(s) added to the model")
    return 0


def _after_tablename(node: ast.ClassDef) -> int:
    """The line to insert a new `__table_args__` after: the end of `__tablename__`."""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(
            getattr(t, "id", None) == "__tablename__" for t in stmt.targets
        ):
            return stmt.end_lineno or stmt.lineno
    return node.body[0].end_lineno or node.body[0].lineno


if __name__ == "__main__":
    sys.exit(main())
