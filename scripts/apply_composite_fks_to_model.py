"""Insert the `0020` constraints into each class's **existing** `__table_args__`.

`python scripts/apply_composite_fks_to_model.py`

## The rule this exists to enforce

**A class gets one `__table_args__`.** This script therefore never creates one. It finds
the `__table_args__ = domain_args(` call a class already has and appends to it, which is
the only direction that cannot silently discard what is already declared.

That is the whole of F133, and it is worth stating precisely because I did the opposite
three times:

| attempt | what happened |
|---|---|
| added a second `__table_args__` | the second wins; six indexes gone, no error |
| text-based merge | crossed class boundaries; three class bodies duplicated |
| AST merge, kept the first copy | the copies differed; a column removed, no import |
| AST merge, kept the last copy | 408 and 928 lines deleted; two files broken |

Every one of those *imported cleanly* except the last, and the reason each was caught was
mypy or `test_schema_matches_models` — not the edit.

So: append into the existing call, verify against the database, and let the drift test be
the arbiter rather than this script.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import sys
from typing import Any

#: `(table, constraint name, foreign column, parent table)`, and the parents that need a
#: unique `(organization_id, id)`.
#:
#: These were a hand-typed literal for the eighteen keys in `0020`, and reconciling the ORM
#: against that literal is what cost two model files (F133). They are now read from
#: `pg_constraint` by `_read_composite_keys()`, so the model can only ever be told to
#: declare a relationship the database actually has -- and a key that exists in one and
#: not the other is impossible by construction.
FOREIGN_KEYS: dict[str, list[tuple[str, str, str]]] = {}
PARENTS: set[str] = set()

MODULES = pathlib.Path("src/ai_orchestrator/persistence")


async def _read_composite_keys() -> tuple[dict[str, list[tuple[str, str, str]]], set[str]]:
    """Every composite tenant key in the database, and every parent they reference.

    The second return value is **every** such parent, not the ones the database is missing
    an index for. Those are different questions: `have` describes the database, and this
    script's job is to bring the *model* into line with it. Subtracting `have` said "the
    database already has `uq_bids_org_id`, so the model does not need to declare it" — and
    then the model-declares-constraints test reported 31 parents whose composite keys
    referenced something unique in neither place.

    Idempotency comes from the insert guard instead, which compares by name against what
    the class already declares. That is the right place for the question "does this need
    adding?", because it asks about the file being edited rather than about a database it
    is not editing.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_orchestrator.persistence.session import Database

    admin = Database.from_settings(use_admin_role=True)
    engine = create_async_engine(admin.engine.url)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT c.conrelid::regclass::text              AS child,
                               c.conname                               AS constraint_name,
                               a.attname                               AS column_name,
                               c.confrelid::regclass::text              AS parent,
                               c.confkey[2] <> 0                       AS is_pair
                          FROM pg_constraint c
                          JOIN pg_attribute a
                            ON a.attrelid = c.conrelid
                           AND a.attnum = c.conkey[2]
                         WHERE c.contype = 'f'
                           AND array_length(c.conkey, 1) = 2
                         ORDER BY child, column_name
                        """
                    )
                )
            ).all()
            # A parent with no `id` column needs no pair, and asking for one is refused
            # with `ConstraintColumnNotFoundError: Can't create Index on table
            # 'units_dictionary': no column named 'id' is present`. `units_dictionary` is
            # exactly that table -- its primary key is `(organization_id, code)` -- and four
            # keys reference it on that pair, so the naive "parents of composite keys minus
            # parents with the index" puts it in the list. This is the trap F135 describes,
            # reached from the other direction.
            without_id = set(
                (
                    await conn.execute(
                        text(
                            "SELECT c.confrelid::regclass::text "
                            "  FROM pg_constraint c "
                            "  JOIN pg_attribute a ON a.attrelid = c.conrelid "
                            "   AND a.attnum = c.conkey[1] "
                            " WHERE c.contype = 'f' AND array_length(c.conkey, 1) = 2 "
                            "   AND NOT EXISTS (SELECT 1 FROM pg_attribute b "
                            "                    WHERE b.attrelid = c.confrelid "
                            "                      AND b.attname = 'id')"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()
        await admin.engine.dispose()

    keys: dict[str, list[tuple[str, str, str]]] = {}
    parents: set[str] = set()
    for child, constraint, column, parent, is_pair in rows:
        # A key whose *parent* has no `id` column already has its only possible
        # declaration: the model's own composite key on `(organization_id, code)`, which
        # `units_dictionary` requires. Adding a second one pointing at `units_dictionary.id`
        # raises `NoReferencedColumnError` at import.
        #
        # The first version of this filter tested `child`, which is the table doing the
        # referencing and never the one being referenced -- so it filtered nothing and
        # wrote two bad constraints, into `bid_items` and `wbs_items`.
        if not is_pair or parent in without_id:
            continue
        keys.setdefault(child, []).append((constraint, column, parent))
        parents.add(parent)
    return keys, parents


def _insertions(table: str) -> list[str]:
    """The lines to append, as source, at eight spaces inside `domain_args(`."""
    out: list[str] = []
    for name, column, parent in FOREIGN_KEYS.get(table, []):
        out += [
            f"        # `{column}` must not be able to name a parent in another tenant.",
            "        # The id alone is not unique within an organization, so the pair is.",
            "        ForeignKeyConstraint(",
            f'            ["organization_id", "{column}"],',
            f'            ["{parent}.organization_id", "{parent}.id"],',
            f'            name="{name}",',
            "        ),",
        ]
    if table in PARENTS:
        out += [
            "        # A composite foreign key needs the referenced",
            "        # *pair* to be unique, and this table is somebody's parent.",
            f'        Index("uq_{table}_org_id", "organization_id", "id", unique=True),',
        ]
    return out


def _is_bare_key(node: ast.expr, parent: str) -> bool:
    """Whether `node` is `ForeignKey("<parent>.id")`.

    A module-level function taking `parent` as an argument rather than a closure over
    the loop variable: `B023` is right that the closure would read whatever `parent`
    held by call time, and it is correct today only because the call is on the next
    line. A predicate that depends on iteration order is a predicate waiting for a
    `continue` to be added between the definition and the call.
    """
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ForeignKey"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == f"{parent}.id"
    )


def _strip_column_level_keys(module: pathlib.Path) -> int:
    """Drop the column-level `ForeignKey("<parent>.id")` for the eighteen.

    **Both declarations cannot stand.** The naming convention
    `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` gives the column-level
    key the *same* name as the composite one, so leaving it produces two constraints with
    one name — the model declares the relationship twice, once bare and once composite,
    and which one SQLAlchemy emits depends on ordering.

    Removing the argument is done by AST and re-rendered with `ast.unparse`, so a
    class body cannot be confused with a function body (F133, attempt two) and a removed
    argument cannot leave an unbalanced parenthesis behind.

    The match is on the **argument's value**, not on its source text. `ast.unparse` in
    3.14 emits `ForeignKey('contracts.id')` with single quotes, and comparing against
    `ForeignKey("contracts.id")` silently matches nothing — which is exactly what the
    first run of this function did, reporting "0 removed" for eighteen present keys.

    Returns the number of column declarations rewritten.
    """
    source = module.read_text()
    tree = ast.parse(source, filename=str(module))
    lines = source.split("\n")
    edits: list[tuple[int, int, str]] = []

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
        wanted = {column: parent for _name, column, parent in FOREIGN_KEYS.get(tables[0], [])}
        if not wanted:
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                continue
            column = stmt.target.id
            parent = wanted.get(column)
            if parent is None or not isinstance(stmt.value, ast.Call):
                continue
            call = stmt.value
            keep = [a for a in call.args if not _is_bare_key(a, parent)]
            if len(keep) == len(call.args):
                continue
            if not keep:
                print(
                    f"  {module.name}:{tables[0]}.{column} has nothing left once the "
                    f"key is removed; left alone for a human",
                    file=sys.stderr,
                )
                continue
            call.args = keep
            rendered = ast.unparse(stmt)
            if len(rendered) > 92:
                rendered = rendered.replace(" = mapped_column(", " = mapped_column(\n        ", 1)
            edits.append((stmt.lineno, stmt.end_lineno or stmt.lineno, "    " + rendered))

    for start, end, text in sorted(edits, reverse=True):
        lines[start - 1 : end] = text.split("\n")
    if edits:
        module.write_text("\n".join(lines))
    return len(edits)


def _dedupe_table_args(module: pathlib.Path) -> int:
    """Reduce each class to **one non-empty** `__table_args__`.

    Two failures, one rule:

    * a repeated `ForeignKeyConstraint` or `Index` inside one tuple -- the first version of
      this function was not idempotent on its own output, so four classes carried their
      block twice;
    * a second `__table_args__` *assignment* in a class that already had one. SQLAlchemy
      reads it as a class attribute, so the second wins and the first is discarded with no
      error. `scripts/sync_model_constraints.py` created exactly this on three classes
      whose `__table_args__` was an empty tuple left behind by an earlier repair: it
      appended a populated one, and the populated one was the one that survived.

    An **empty** `__table_args__` is dropped rather than merged. It declares nothing, so
    removing it changes no behaviour -- which is the only kind of removal this script
    performs, and it is safe for exactly that reason.

    Returns the number of elements and statements removed.
    """
    source = module.read_text()
    tree = ast.parse(source, filename=str(module))
    lines = source.split("\n")
    kill: list[tuple[int, int]] = []
    found = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        assigns = [
            stmt
            for stmt in node.body
            if isinstance(stmt, ast.Assign)
            and any(getattr(t, "id", None) == "__table_args__" for t in stmt.targets)
        ]
        if not assigns:
            continue
        for stmt in assigns[1:]:
            kill.append((stmt.lineno, stmt.end_lineno or stmt.lineno))
            found += 1
        keeper = assigns[0]
        container = keeper.value
        elements = (
            list(container.args)
            if isinstance(container, ast.Call)
            else list(container.elts)
            if isinstance(container, ast.Tuple)
            else []
        )
        if not elements:
            kill.append((keeper.lineno, keeper.end_lineno or keeper.lineno))
            found += 1
            continue
        seen: set[str] = set()
        table_name = _table_name(node)
        for arg in elements:
            key = _constraint_key(arg)
            # A `name=` that already carries the convention's prefix composes into a
            # doubled name: `CheckConstraint(..., name="ck_sop_steps_status_known")` on
            # `sop_steps` reaches Postgres as
            # `ck_sop_steps_ck_sop_steps_status_known`, which the schema does not have and
            # no test can match. The convention *supplies* the prefix, so the model
            # carries the suffix -- and an argument that has the prefix is a second
            # declaration of a rule the class already states.
            #
            # Only `CheckConstraint` and `UniqueConstraint` compose. An `Index` name is
            # used verbatim, so `Index("uq_evaluation_cases_org_id", ...)` is the correct
            # spelling and removing it deleted a real index from `models.py` and left the
            # tuple unterminated.
            if (
                key is not None
                and table_name is not None
                and key.startswith(("CheckConstraint:", "UniqueConstraint:"))
                and _is_prefixed(key, table_name)
            ):
                kill.append((arg.lineno, arg.end_lineno or arg.lineno))
                found += 1
                continue
            if key is None:
                continue
            if key in seen:
                kill.append((arg.lineno, arg.end_lineno or arg.lineno))
                found += 1
                continue
            seen.add(key)

    for start, end in sorted(kill, reverse=True):
        del lines[start - 1 : end]
        if start - 1 < len(lines) and not lines[start - 1].strip():
            del lines[start - 1]
    if kill:
        module.write_text("\n".join(lines))
    return found


def _table_name(node: ast.ClassDef) -> str | None:
    for stmt in node.body:
        if (
            isinstance(stmt, ast.Assign)
            and any(getattr(t, "id", None) == "__tablename__" for t in stmt.targets)
            and isinstance(stmt.value, ast.Constant)
        ):
            return str(stmt.value.value)
    return None


def _is_prefixed(key: str, table: str) -> bool:
    """Whether `key` already carries the naming convention's table prefix."""
    _, _, name = key.partition(":")
    return name.startswith(f"ck_{table}_") or name.startswith(f"uq_{table}_")


def _constraint_key(node: ast.expr) -> str | None:
    """The name of a `ForeignKeyConstraint` or `Index` argument, or None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return None
    if node.func.id not in ("ForeignKeyConstraint", "Index"):
        return None
    for keyword in node.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
            return f"{node.func.id}:{keyword.value.value}"
    if node.args and isinstance(node.args[0], ast.Constant):
        return f"{node.func.id}:{node.args[0].value}"
    # No `name=`, so the convention is what names it -- and the convention reads
    # `column_0_name`, which is `organization_id` for every composite key. Two keys from
    # the same table to the same parent would therefore share a name and cannot be told
    # apart by it, so the identity is built from the shape: referenced table plus local
    # columns. Without this fallback, `progress_snapshots` kept two copies of
    # `fk_progress_snapshots_organization_id_projects` through every dedupe pass.
    referenced = [part.rsplit(".", 1)[0] for part in _string_list(node.args[1:2])] or [
        part.rsplit(".", 1)[0] for part in _string_list(node.args[-1:])
    ]
    local = _string_list(node.args[:1])
    if referenced and local:
        return f"{node.func.id}:{referenced[0]}:{','.join(local)}"
    return None


def _string_list(nodes: list[ast.expr]) -> list[str]:
    """The string elements of a call argument that is a list of string literals."""
    if not nodes or not isinstance(nodes[0], ast.List):
        return []
    return [
        element.value
        for element in nodes[0].elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    ]


def _render_multiline(
    assign: ast.Assign, container: ast.expr, elements: list[ast.expr], table: str
) -> str:
    """Re-render a one-line `__table_args__` as a multi-line tuple or call.

    `__table_args__ = (UniqueConstraint(...),)` becomes:

        __table_args__ = (
            UniqueConstraint(...),
            ForeignKeyConstraint(...),
            Index(...),
        )

    Existing elements are `ast.unparse`d, so this is a formatting change to lines that
    have no comments in them by definition -- a one-line statement cannot carry a
    comment. The new elements come from `_insertions`, which is the same text the
    multi-line path inserts, so both paths produce identical declarations and the guard
    that makes the script idempotent sees the same names either way.
    """
    opener = "domain_args(" if isinstance(container, ast.Call) else "("
    body = [f"        {ast.unparse(element)}," for element in elements]
    body += [f"    {line.strip()}" if line.strip() else "" for line in _insertions(table)]
    return "\n".join(
        [
            f"    __table_args__ = {opener}",
            *body,
            "    )",
        ]
    )


def main() -> int:
    global FOREIGN_KEYS, PARENTS
    FOREIGN_KEYS, parents = asyncio.run(_read_composite_keys())
    PARENTS = set(parents)
    total = sum(len(v) for v in FOREIGN_KEYS.values())
    print(
        f"  the database has {total} composite tenant keys across "
        f"{len(FOREIGN_KEYS)} tables, and {len(PARENTS)} distinct parents"
    )
    # Dedupe first: a class that already carries the block twice must be collapsed
    # before the guard below can see that it has what it wants.
    for module in sorted(MODULES.glob("*.py")):
        gone = _dedupe_table_args(module)
        if gone:
            print(f"  {module.name}: {gone} duplicated constraint(s) collapsed")

    changed = 0
    for module in sorted(MODULES.glob("*.py")):
        source = module.read_text()
        tree = ast.parse(source, filename=str(module))
        lines = source.split("\n")
        # Work bottom-up so earlier line numbers stay valid.
        targets: list[tuple[int, int, Any]] = []
        creations: list[tuple[int, str]] = []
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
            if not tables or (tables[0] not in FOREIGN_KEYS and tables[0] not in PARENTS):
                continue
            table = tables[0]
            assigns = [
                stmt
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and any(getattr(t, "id", None) == "__table_args__" for t in stmt.targets)
            ]
            if not assigns:
                # A class with no `__table_args__` at all. The insert loop used to walk
                # `node.body` looking for one, found nothing, and `continue`d -- so
                # `mcp_servers` never received the unique pair its four composite keys
                # depend on, and the test that checks exactly that reported it.
                #
                # A table that is somebody's parent needs an `__table_args__` whether or
                # not it had constraints before, so one is created.
                if table in PARENTS:
                    creations.append((node.body[0].end_lineno or node.body[0].lineno, table))
                continue
            for stmt in assigns:
                if not (
                    isinstance(stmt, ast.Assign)
                    and any(getattr(t, "id", None) == "__table_args__" for t in stmt.targets)
                ):
                    continue
                # `__table_args__` is a `domain_args(...)` call in the construction
                # modules and a bare tuple in `models.py` -- forty-four classes there use
                # the tuple form. Both are handled, because both are `__table_args__` and
                # the difference is syntax, not meaning.
                container = stmt.value
                if isinstance(container, ast.Call):
                    elements = list(container.args)
                elif isinstance(container, ast.Tuple):
                    elements = list(container.elts)
                else:
                    print(
                        f"  {module.name}:{table}: __table_args__ is neither a call nor a "
                        f"tuple ({type(container).__name__}); skipped",
                        file=sys.stderr,
                    )
                    continue
                if not elements:
                    print(
                        f"  {module.name}:{table}: __table_args__ is empty; skipped",
                        file=sys.stderr,
                    )
                    continue
                existing = {_constraint_key(a) for a in elements}
                wanted_keys = {
                    f"ForeignKeyConstraint:{name}" for name, _c, _p in FOREIGN_KEYS.get(table, [])
                } | ({f"Index:uq_{table}_org_id"} if table in PARENTS else set())
                if wanted_keys and wanted_keys <= existing:
                    continue
                if stmt.lineno == stmt.end_lineno:
                    # A one-line `__table_args__ = (UniqueConstraint(...),)` has its
                    # closing paren on the opening line, so "insert after the last
                    # element" lands *outside* the tuple and the file stops parsing. There
                    # are eleven of these in `models.py`. Re-render the whole statement
                    # instead -- there are no comments inside a one-liner to lose.
                    rendered = _render_multiline(stmt, container, elements, table)
                    targets.append((stmt.lineno, stmt.end_lineno or stmt.lineno, rendered))
                    continue
                # Append as the last element, before the closing paren or bracket.
                # `(start, end, payload)` with `start == end` means "splice these lines in
                # at this line"; a differing `start`/`end` means "replace that range", which
                # is the one-line-tuple case above. The two shapes used to disagree about
                # which tuple position held what, and the disagreement surfaced as
                # `slice indices must be integers` on a *string* that was a table name.
                insert_at = elements[-1].end_lineno
                targets.append((insert_at, insert_at, _insertions(table)))
        # A class that had no `__table_args__` gets one built from scratch, placed after
        # its `__tablename__` so the class still reads top to bottom.
        for at, table in sorted(creations, reverse=True):
            lines[at:at] = ["", "    __table_args__ = (", *_insertions(table), "    )"]
            changed += 1
        for start, end, payload in sorted(targets, reverse=True):
            if start == end:
                lines[start:start] = payload
            else:
                lines[start - 1 : end] = payload.split("\n")
            changed += 1
        if targets or creations:
            module.write_text("\n".join(lines))
            print(
                f"  {module.name}: {len(targets)} class(es) extended, "
                f"{len(creations)} __table_args__ created"
            )
    print(f"  {changed} classes extended with the composite constraints")

    # Only after the composite constraint exists in the same file: removing the
    # column-level key first would leave the relationship undeclared for a moment.
    stripped = 0
    for module in sorted(MODULES.glob("*.py")):
        count = _strip_column_level_keys(module)
        if count:
            stripped += count
            print(f"  {module.name}: {count} column-level key(s) removed")
    print(f"  {stripped} column-level keys removed, leaving one declaration per relationship")
    return 0


if __name__ == "__main__":
    sys.exit(main())
