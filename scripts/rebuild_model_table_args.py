"""Rebuild every class's `__table_args__` from the database. Destructive by design.

`python scripts/rebuild_model_table_args.py`

## Why this exists, and why it is destructive

Migrations `0020` through `0023` moved 135 foreign keys from a column-level
`ForeignKey("parent.id")` to a table-level `ForeignKeyConstraint`. Reconciling the ORM with
that by appending produced duplicated blocks — `Approval.__table_args__` ended up with its
entire contents five times over — because the append guard compared *names* while the
naming convention composes them, so an existing constraint was never recognised as
present (F133, and the long tail after it).

Appending cannot repair a duplicated block. The state is only reachable by replacing the
declaration, so this script replaces it.

**What is lost, stated plainly:** the hand-written comments that explained *why* a check
constraint exists. They are prose in a Python file, not schema, and the database does not
store them. The rules survive exactly; their justifications do not. That is a real cost and
the reason this is a separate script with a name that says "rebuild" rather than a flag on
one that says "sync".

## What the rebuilt declaration contains

For every table, from `pg_indexes`, `pg_constraint` and `pg_attribute`:

* every foreign key that is `(organization_id, <column>)`, as a `ForeignKeyConstraint`
  with its existing name — so the name does not churn;
* `UNIQUE (organization_id, id)` on any table that is a composite key's parent;
* every index, unique constraint and check constraint the table has;
* and for a construction table, wrapped in `domain_args(...)` so the provenance rules
  cannot be left off.

**Constraint names are written as the suffix, not the full name.** The convention composes
`ck_<table>_<suffix>`, so writing the database's full name yields
`ck_<table>_ck_<table>_<suffix>`, which exists in neither. The hand-written constraints in
this repository already used the suffix form; that is the convention working as intended.

## The one thing it will not do

Touch a column. This rebuilds constraints only. A column is a different kind of decision
and none of the churn affected them — `Base.metadata` reports the same 550 constraints and
the same columns as the database after every pass.
"""

from __future__ import annotations

import ast
import asyncio
import collections
import pathlib
import sys

MODELS = pathlib.Path("src/ai_orchestrator/persistence")


async def _read() -> dict[str, dict[str, list]]:
    """Everything the database says about every table's constraints."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_orchestrator.persistence.session import Database

    admin = Database.from_settings(use_admin_role=True)
    engine = create_async_engine(admin.engine.url)
    out: dict[str, dict[str, list]] = collections.defaultdict(
        lambda: {"fk": [], "index": [], "unique": [], "check": [], "pk": []}
    )
    try:
        async with engine.connect() as conn:
            for table, name, column, parent, target in (
                await conn.execute(
                    text(
                        """
                        SELECT c.conrelid::regclass::text  AS child,
                               c.conname                   AS constraint_name,
                               b.attname                   AS local_column,
                               c.confrelid::regclass::text  AS parent,
                               d.attname                   AS target_column
                          FROM pg_constraint c
                          JOIN pg_attribute b
                            ON b.attrelid = c.conrelid AND b.attnum = c.conkey[2]
                          JOIN pg_attribute d
                            ON d.attrelid = c.confrelid AND d.attnum = c.confkey[2]
                         WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 2
                         ORDER BY 1, 3
                        """
                    )
                )
            ).all():
                # `target` is read, not assumed. `units_dictionary` has no `id` column --
                # its key is `(organization_id, code)` -- so hard-coding `id` as the
                # second referenced column produced
                # `["units_dictionary.organization_id", "units_dictionary.id"]` and an
                # import-time `NoReferencedColumnError` pointing at `bid_items`, four
                # tables away from the table that was wrong.
                out[table]["fk"].append((name, column, parent, target))
            for table, name, contype, definition in (
                await conn.execute(
                    text(
                        # `contype::text`, and the cast is load-bearing. `pg_constraint.contype`
                        # is of type `"char"`, and asyncpg returns `"char"` as **bytes** -- so
                        # `contype == "p"` in Python is `b'p' == 'p'`, which is False, and
                        # every row fell through to the `else` branch. The result was a
                        # `CheckConstraint('PRIMARY KEY (organization_id, code)')` and a
                        # `CheckConstraint` for every unique constraint, on 101 tables,
                        # with no error anywhere: the code ran, produced plausible source,
                        # and the schema it read was not the schema it queried.
                        "SELECT c.conrelid::regclass::text, c.conname, "
                        "       c.contype::text, "
                        "       pg_get_constraintdef(c.oid) "
                        "  FROM pg_constraint c WHERE c.contype IN ('u', 'c', 'p')"
                    )
                )
            ).all():
                if contype == "p":
                    inner = definition[definition.index("(") + 1 : definition.rindex(")")]
                    out[table]["pk"] = [
                        part.strip().strip('"') for part in inner.split(",") if part.strip()
                    ]
                elif contype == "u":
                    inner = definition[definition.index("(") + 1 : definition.rindex(")")]
                    cols = [p.strip().strip('"') for p in inner.split(",") if p.strip()]
                    out[table]["unique"].append((name, cols))
                else:
                    body = definition.strip()
                    out[table]["check"].append(
                        (name, body[6:] if body.upper().startswith("CHECK ") else body)
                    )
            for table, _name, definition in (
                await conn.execute(
                    text(
                        "SELECT tablename, indexname, indexdef FROM pg_indexes "
                        " WHERE schemaname = 'public' AND indexname NOT LIKE 'pk_%'"
                    )
                )
            ).all():
                out[table]["index"].append(_parse_index(definition))
    finally:
        await engine.dispose()
        await admin.engine.dispose()
    # A unique *constraint* is also in pg_indexes; keep the constraint, drop the index.
    for entries in out.values():
        shadowed = {name for name, _cols in entries["unique"]}
        entries["index"] = [e for e in entries["index"] if e[0] not in shadowed]
    return dict(out)


def _provenance_suffixes() -> set[str]:
    """The constraint names `domain_args(...)` already supplies.

    Asked, not listed. A hand-written list here would be a second place to update when
    `provenance_checks()` gains a third rule, and the failure would be silent: the
    duplicate lands in one `__table_args__` and `alembic check` tolerates it.
    """
    from ai_orchestrator.persistence.construction import provenance_checks

    return {str(c.name) for c in provenance_checks() if c.name}


def _is_provenance(name: str, table: str) -> bool:
    """Whether `name` is a provenance rule this table already gets from `domain_args`.

    `name` is a name **read out of Postgres**, so it is already composed:
    `ck_clients_source_known`. The prefix is stripped before the comparison, and
    stripped against *this table's* name rather than a regex, so a constraint on
    `sop_steps` called `ck_clients_source_known` -- which would be a different rule that
    happens to end the same way -- is not mistaken for the provenance one.
    """
    prefix = f"ck_{table}_"
    suffix = name[len(prefix) :] if name.startswith(prefix) else name
    return suffix in _provenance_suffixes()


def _wrap_string(value: str, indent: str, width: int = 92) -> list[str]:
    """Render `value` as implicitly-concatenated string literals, one per line.

    A check constraint's expression arrives from Postgres as one long normalised string --
    `(((status)::text = ANY ((ARRAY['shadow'::character varying, 'active'::character
    varying, ...])))` is 179 characters. It has to be written as a literal, and a literal
    that long is `E501`.

    Splitting on `, ` rather than on width alone matters: a split mid-token would change
    the value, and a check constraint whose text differs from the schema's is a
    constraint the next `alembic check` wants to change. The chunks concatenate back to
    exactly the input, which the test asserts by comparing the model's rendered source
    against `pg_get_constraintdef`.
    """
    if len(value) + len(indent) <= width:
        return [f"{indent}{value!r},"]
    chunks: list[str] = []
    remaining = value
    while len(remaining) + len(indent) > width:
        cut = remaining.rfind(", ", 0, width - len(indent))
        if cut == -1:
            cut = width - len(indent)
        else:
            cut += 2
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    chunks.append(remaining)
    return [f"{indent}{chunk!r}" for chunk in chunks[:-1]] + [f"{indent}{chunks[-1]!r},"]


def _parse_index(definition: str) -> tuple:
    """`pg_indexes.indexdef` -> `(name, columns, unique, where, using, ops)`.

    A plain btree index is the easy case. Two are not, and both exist in this schema:

    * a **partial** index carries `WHERE <predicate>` after the column list. Six do, and
      the predicate *is* the constraint -- `uq_procedures_one_active` means nothing
      without `WHERE status = 'active'`. Dropping it would create a unique index over
      every row and refuse the second version of every procedure.
    * an **opclass** index reads `USING hnsw (embedding vector_cosine_ops)`. The column is
      `embedding` and `vector_cosine_ops` is the operator class; both are needed, and
      `vector_cosine_ops` is not a column.

    `pg_indexes` carries all of it as text, so the parse is mechanical -- but it is a
    parse, and an index silently rebuilt without its predicate is a *stricter* schema that
    refuses legitimate writes. That is why the unparsed case is reported rather than
    guessed at.
    """
    # The name is the token *after* `INDEX`, not a fixed position: `CREATE INDEX x` and
    # `CREATE UNIQUE INDEX x` put it at different offsets, and taking position 2 yielded
    # the literal string "INDEX" for every unique index in the schema.
    tokens = definition.split()
    name = tokens[tokens.index("INDEX") + 1]
    unique = "UNIQUE" in definition.upper()
    method = ""
    if " USING " in definition:
        method = definition.split(" USING ", 1)[1].split(" ", 1)[0]
    where = ""
    if " WHERE " in definition:
        where = definition.split(" WHERE ", 1)[1].strip()
    # The column list is the first parenthesised group after the method.
    tail = definition.split("(", 1)[1] if "(" in definition else ""
    depth, columns_raw = 1, []
    for ch in tail:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            columns_raw.append(ch)
    inner = "".join(columns_raw)
    columns: list[str] = []
    ops: list[str] = []
    for part in inner.split(","):
        part = part.strip().strip('"')
        if not part:
            continue
        pieces = part.split()
        columns.append(pieces[0])
        ops.append(pieces[1] if len(pieces) > 1 else "")
    return (name, columns, unique, where, method, ops)


def _suffix(name: str, table: str, kind: str) -> str:
    """The name to write into the model, given the name the database records.

    **The convention composes for check constraints and not for unique ones**, which is
    the whole of this function:

    * `CheckConstraint("x", name="valid_for_months_positive")` on `supplier_assessments`
      reaches Postgres as `ck_supplier_assessments_valid_for_months_positive`, so the
      model carries the *suffix* and the convention supplies `ck_<table>_`. Writing the
      database's full name yields `ck_supplier_assessments_ck_supplier_assessments_...`,
      a name in neither.
    * `UniqueConstraint(..., name="uq_agent_defs_name_version")` keeps the name it is
      given, because the convention token it would compose from is a *column*, not a
      constraint name. Stripping the prefix there gives the model `agent_defs_name_version`
      and the drift test reports a paired `remove_constraint` / `add_constraint` for the
      same object on every run.

    The two kinds are told apart by the first argument, so the stripping is per-kind rather
    than uniform. An earlier version stripped for both and produced 56 tables whose
    constraints existed in neither the model nor the schema.
    """
    if kind != "check":
        return name
    prefix = f"ck_{table}_"
    return name[len(prefix) :] if name.startswith(prefix) else name


async def main() -> int:
    # The models are NOT imported here. This script exists to repair them, and importing
    # a model that does not yet parse raises before the first line of repair -- a script
    # that cannot fix the thing it is for. Everything it needs comes from the schema.
    schema = await _read()

    total = 0
    skipped: list[str] = []
    # Synchronous filesystem work inside an async function: the file *editing* has
    # no async form, and wrapping each read and write in a thread would cost more
    # than it buys for a script that runs once, by hand, on 9 files.
    for module in sorted(MODELS.glob("*.py")):  # noqa: ASYNC240
        source = module.read_text()
        tree = ast.parse(source, filename=str(module))
        lines = source.split("\n")
        edits: list[tuple[int, int, list[str]]] = []
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
            if table not in schema or table == "alembic_version":
                continue
            entries = schema[table]
            if not any(entries[k] for k in ("fk", "index", "unique", "check", "pk")):
                continue
            body: list[str] = []
            # The primary key first: a table whose key is composite *declares* it here
            # rather than with `primary_key=True`, and replacing `__table_args__` without
            # it left `units_dictionary` with no key at all -- which is an import-time
            # `could not assemble any primary key columns`, on a table that had been
            # correct since `0013`.
            if len(entries["pk"]) > 1:
                body.append(
                    "        PrimaryKeyConstraint("
                    + ", ".join(f'"{c}"' for c in entries["pk"])
                    + "),"
                )
            for name, column, parent, target in entries["fk"]:
                body += [
                    "        ForeignKeyConstraint(",
                    f'            ["organization_id", "{column}"],',
                    f'            ["{parent}.organization_id", "{parent}.{target}"],',
                    f'            name="{name}",',
                    "        ),",
                ]
            for name, cols, unique, where, method, ops in entries["index"]:
                if not cols:
                    skipped.append(f"{table}.{name}")
                    continue
                lines_out = ["        Index(", f'            "{name}",']
                for column in cols:
                    lines_out.append(f'            "{column}",')
                if unique:
                    lines_out.append("            unique=True,")
                if method and method != "btree":
                    lines_out.append(f'            postgresql_using="{method}",')
                emitted = [o for o in ops if o]
                if emitted:
                    listed = ", ".join(f'"{o}"' for o in emitted)
                    lines_out.append(f"            postgresql_ops=[{listed}],")
                if where:
                    lines_out.append("            postgresql_where=text(")
                    lines_out += _wrap_string(where, "                ")
                    lines_out.append("            ),")
                lines_out.append("        ),")
                body += lines_out
            for name, cols in entries["unique"]:
                args = ", ".join(f'"{c}"' for c in cols)
                one_line = (
                    f'        UniqueConstraint({args}, name="{_suffix(name, table, "unique")}"),'
                )
                if len(one_line) <= 100:
                    body.append(one_line)
                    continue
                body.append("        UniqueConstraint(")
                body += [f'            "{c}",' for c in cols]
                body.append(f'            name="{_suffix(name, table, "unique")}",')
                body.append("        ),")
            for name, expression in entries["check"]:
                if _is_provenance(name, table):
                    # `domain_args(...)` prepends `provenance_checks()`, so a construction
                    # table gets `source_known` and `agent_source_needs_proposal` from
                    # there. Emitting them from the schema as well put the **same rule in
                    # one `__table_args__` twice**, on all 75 construction tables --
                    # found by `test_every_check_constraint_is_uniquely_named_per_table`,
                    # which is the fourth-occurrence instrument for the naming convention
                    # finally catching something the drift test tolerated.
                    #
                    # The comparison is on the **suffix**, because `name` here is the
                    # *composed* name read out of the database
                    # (`ck_clients_source_known`) while `provenance_checks()` hands back
                    # the bare `source_known`. Comparing the two directly never matches,
                    # so the first version of this skip did nothing and the duplicates
                    # survived it -- which is why the rebuild has to strip the prefix
                    # rather than trust that the two sides are spelled alike.
                    continue
                body.append("        CheckConstraint(")
                body += _wrap_string(expression, "            ")
                body += [
                    f'            name="{_suffix(name, table, "check")}",',
                    "        ),",
                ]
            if not body:
                continue
            stmt = next(
                (
                    b
                    for b in node.body
                    if isinstance(b, ast.Assign)
                    and any(getattr(t, "id", None) == "__table_args__" for t in b.targets)
                ),
                None,
            )
            uses_domain_args = any(
                "domain_args" in ast.unparse(b.value)
                for b in node.body
                if isinstance(b, ast.Assign)
                and any(getattr(t, "id", None) == "__table_args__" for t in b.targets)
            )
            opener = (
                "    __table_args__ = domain_args("
                if uses_domain_args
                else "    __table_args__ = ("
            )
            rendered = [opener, *body, "    )"]
            if stmt is None:
                anchor = _after_tablename(node)
                edits.append((anchor, anchor, ["", *rendered]))
            else:
                edits.append((stmt.lineno, stmt.end_lineno or stmt.lineno, rendered))
            total += 1
        if not edits:
            continue
        for start, end, payload in sorted(edits, reverse=True):
            lines[start - 1 : end] = payload
        module.write_text("\n".join(lines))
        print(f"  {module.name}: {len(edits)} __table_args__ rebuilt")
    stripped = 0
    for module in sorted(MODELS.glob("*.py")):  # noqa: ASYNC240 -- see above
        if module.stem != "__init__":
            stripped += _strip_column_level_keys(module, schema)
    print(f"  {stripped} column-level key(s) removed; the composite key replaces them")
    print(f"  {total} declaration(s) rebuilt from the database")
    if skipped:
        print(f"  {len(skipped)} partial/expression index(es) NOT reproduced: {skipped}")
    return 0


def _strip_column_level_keys(module: pathlib.Path, schema: dict) -> int:
    """Remove `ForeignKey("<parent>.id")` from a column that has a composite key.

    A composite key and a bare key under one name is two declarations of one
    relationship, and the naming convention cannot tell them apart -- both come out as
    `fk_po_items_purchase_order_id_purchase_orders`. The bare one is the leftover from
    before `0020`; it lives in a `mapped_column(...)` and not in `__table_args__`, so the
    rebuild above cannot reach it.

    This lives here rather than in a separate script because it is part of the same
    statement: **the model is rebuilt from the schema**, and a column-level key that
    contradicts the schema is part of what is being rebuilt.
    """
    source = module.read_text()
    tree = ast.parse(source, filename=str(module))
    lines = source.split("\n")
    edits: list[tuple[int, int, str]] = []
    composite: dict[tuple[str, str], str] = {}
    for table, entries in schema.items():
        for _name, column, parent, target in entries["fk"]:
            composite[(table, column)] = f"{parent}.{target}"

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
            parent = composite.get((table, stmt.target.id))
            if parent is None or not isinstance(stmt.value, ast.Call):
                continue
            call = stmt.value
            keep = [
                a
                for a in call.args
                if not (
                    isinstance(a, ast.Call)
                    and isinstance(a.func, ast.Name)
                    and a.func.id == "ForeignKey"
                    and a.args
                    and isinstance(a.args[0], ast.Constant)
                    and a.args[0].value == f"{parent}.id"
                )
            ]
            if len(keep) == len(call.args) or not keep:
                continue
            call.args = keep
            rendered = "    " + ast.unparse(stmt)
            if len(rendered) > 92:
                rendered = rendered.replace(" = mapped_column(", " = mapped_column(\n        ", 1)
            edits.append((stmt.lineno, stmt.end_lineno or stmt.lineno, rendered))

    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1 : end] = text.split("\n")
    if edits:
        module.write_text("\n".join(lines))
    return len(edits)


def _after_tablename(node: ast.ClassDef) -> int:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(
            getattr(t, "id", None) == "__tablename__" for t in stmt.targets
        ):
            return stmt.end_lineno or stmt.lineno
    return node.body[0].end_lineno or node.body[0].lineno


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
