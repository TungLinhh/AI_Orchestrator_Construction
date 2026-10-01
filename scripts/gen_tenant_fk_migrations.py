"""Emit the composite-tenant foreign key migrations from the live schema.

`python scripts/gen_tenant_fk_migrations.py --plan`
`python scripts/gen_tenant_fk_migrations.py --write`

## Why the schema is the input and not a list in this file

Migration `0020` listed its eighteen keys as a Python constant, and reconciling the ORM
against that list is what cost two model files (F133). A hand-typed list of 117 names has
117 chances to transpose a column, and a transposed `ForeignKeyConstraint` **does not
fail loudly**: it references a column that exists, the migration succeeds, and the schema
enforces something other than what the code says.

So the schema is the input. This script reads `pg_constraint`, groups the keys into
tranches, and writes a migration whose every name it also prints. A name in the output
that is not in the database cannot happen; a name in the database that is missing from the
output can, and the count is printed for exactly that reason.

## How the tranches are cut

By **parent**, not by child. Each tranche then owns a disjoint set of parents, which
means:

* the `UNIQUE (organization_id, id)` it adds and the keys that need it are in the same
  file, so no tranche depends on another;
* `downgrade` is the exact reverse within the file, and the two cannot be reordered into
  something invalid, because a composite key's parent index is created before the key and
  dropped after it;
* a tranche is reviewable on its own — the table in the docstring is the whole change.

The cut is by domain because a cut by child would scatter one parent's key across three
files, and then no single file's `downgrade` would be independently runnable.

`organizations` is excluded, and it is worth saying why: it is the tenant root and has no
`organization_id` column of its own, so there is no pair to reference. A composite key to
it would be a key on one column wearing a composite key's name.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib

#: Which parent belongs to which tranche. A parent appears exactly once, and a tranche's
#: key set is every bare key pointing at one of its parents — so the sets are disjoint by
#: construction and the assertion below checks it rather than trusting it.
TRANCHES: dict[str, tuple[str, ...]] = {
    "0021_orchestration": (
        # The execution core. Everything an agent is, and everything it is given.
        "agents",
        "tasks",
        "executions",
        "delegations",
        "subagent_runs",
        "agent_definitions",
        "agent_relationships",
        "agent_skill_bindings",
        "agent_tool_bindings",
        "skills",
        "skill_versions",
        "tools",
        "tool_versions",
        "tool_policies",
        "mcp_servers",
        "model_profiles",
        "policies",
        "policy_versions",
        "a2a_agents",
        "a2a_endpoints",
        "organizational_units",
        "roles",
        "users",
        "memory_items",
        "memory_chunks",
        "messages",
        "events",
        "outbox_events",
        "workflow_runs",
        "task_dependencies",
        "idempotency_records",
    ),
    "0022_governance": (
        # Who decided, on whose authority, and what it cost.
        "approvals",
        "budgets",
        "budget_ledger",
        "documents",
        "clients",
        "client_contacts",
        "evaluation_cases",
        "evaluation_runs",
        "connectors",
        "credentials_metadata",
    ),
    "0023_construction": (
        # The built world: the commercial chain, the gates, the procedures.
        "contracts",
        "suppliers",
        "projects",
        "tenders",
        "tender_documents",
        "tender_requirements",
        "bids",
        "opportunities",
        "contract_claims",
        "contract_variations",
        "gate_definitions",
        "gate_criteria",
        "gate_instances",
        "gate_criterion_evaluations",
        "gate_decisions",
        "gate_conditions",
        "sop_definitions",
        "sop_versions",
        "sop_steps",
        "sop_forms",
        "sop_raci",
        "material_categories",
        "zones",
        "wbs",
        "wbs_items",
        "po_items",
    ),
}

#: A parent may appear in exactly one tranche, because its unique index would otherwise
#: be created twice and Postgres refuses that with a duplicate-object error naming an
#: index. The first draft of this file listed `tools` in both `0021` and `0022` and the
#: assertion below caught it before a migration was written -- which is the whole reason
#: the assertion is here rather than a comment.

MIGRATIONS = pathlib.Path("migrations/versions")


async def _read_schema() -> tuple[list[tuple[str, str, str, str]], set[str]]:
    """`(child, column, parent, constraint name)` for every bare key, and the parents
    that already carry `UNIQUE (organization_id, id)`."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from ai_orchestrator.persistence.session import Database

    async def read() -> tuple[list[tuple[str, str, str, str]], set[str]]:
        admin = Database.from_settings(use_admin_role=True)
        engine = create_async_engine(admin.engine.url)
        try:
            async with engine.connect() as conn:
                keys = (
                    await conn.execute(
                        text(
                            """
                            SELECT c.conrelid::regclass::text AS child,
                                   a.attname                AS column_name,
                                   c.confrelid::regclass::text AS parent,
                                   c.conname                AS constraint_name
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
                have_pair = set(
                    (
                        await conn.execute(
                            text(
                                "SELECT tablename FROM pg_indexes "
                                " WHERE indexname LIKE 'uq%org%id' "
                                "   AND indexdef LIKE '%(organization_id, id)%'"
                            )
                        )
                    ).scalars()
                )
        finally:
            await engine.dispose()
            await admin.engine.dispose()
        return [tuple(r) for r in keys], have_pair  # type: ignore[misc]

    return await read()


async def build_tranches() -> tuple[dict[str, dict[str, list[tuple[str, str, str, str]]]], int]:
    keys, have_pair = await _read_schema()
    seen_parents: dict[str, str] = {}
    duplicates: list[str] = []
    for name, parents in TRANCHES.items():
        for parent in parents:
            if parent in seen_parents:
                duplicates.append(f"{parent} in {seen_parents[parent]} and {name}")
            seen_parents[parent] = name
    if duplicates:
        raise SystemExit(
            "A parent is in two tranches, so its unique index would be created twice: "
            + "; ".join(duplicates)
        )

    # A key belongs to the tranche that owns its *parent*. A key whose parent is in no
    # tranche is reported, never silently dropped.
    out: dict[str, dict[str, list[tuple[str, str, str, str]]]] = {}
    orphaned: list[str] = []
    for child, column, parent, constraint in keys:
        tranche = seen_parents.get(parent)
        if tranche is None:
            orphaned.append(f"{child}.{column} -> {parent}")
            continue
        out.setdefault(tranche, {"keys": [], "parents": []})["keys"].append(
            (child, column, parent, constraint)
        )
    for name, parents in TRANCHES.items():
        bucket = out.setdefault(name, {"keys": [], "parents": []})
        bucket["parents"] = [p for p in parents if p not in have_pair]
    if orphaned:
        raise SystemExit(
            f"{len(orphaned)} keys point at a parent in no tranche, and would be dropped "
            f"silently: {orphaned[:8]}"
        )
    return out, len(keys)


def _migration_source(
    name: str,
    parents: list[str],
    keys: list[tuple[str, str, str, str]],
    previous: str,
) -> str:
    rows = "\n".join(
        f"| `{child}` | `{column}` | `{parent}` |" for child, column, parent, _ in keys
    )
    parent_list = "\n".join(f'    "{p}",' for p in parents)
    fk_list = "\n".join(
        f'    ("{child}", "{constraint}", "{column}", "{parent}"),'
        for child, column, parent, constraint in keys
    )
    title = name.split("_", 1)[1].replace("_", " ")
    body = f'''"""A row in the {title} tranche may not name a parent in another tenant.

Revision ID: {name.split("_")[0]}
Revises: {previous}

## What this is

Migration `0020` did this for the supply chain. The remaining
{len(keys)} single-column foreign keys get the same treatment: `(organization_id, <column>)`
referencing `(organization_id, id)`.

RLS already prevents a cross-tenant **read** -- it is `FORCE`d on every tenant-scoped
table and the application role is not `BYPASSRLS`. What it does not do is check a foreign
key, so a write could name a parent in another tenant and succeed. The write is the only
place the hole showed, which is why every read-path test passed before this existed.

## The parent index comes first, and that is not a detail

A composite foreign key needs the referenced **pair** to be unique. `{keys[0][2]}.id`
being unique on its own is not that statement, so `UNIQUE (organization_id, id)` has to
exist on all {len(parents)} parents before the first key can be added. The order is
therefore forced: indexes, then keys, and `downgrade` is the exact reverse.

## The constraint names do not change

A composite key keeps its bare name and gains a leading `organization_id` column. The
convention `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` reads
`column_0_name`, which on a composite key is `organization_id` for all {len(keys)} of them,
so following it would collapse every name to `fk_<table>_organization_id_<parent>` and
lose which relationship each one is. Each is therefore created with an explicit name, the
way `0016` and `0020` bypassed the convention for the same reason.

## The {len(keys)} keys

| child | column | parent |
|---|---|---|
{rows}

## Generated, not typed

`scripts/gen_tenant_fk_migrations.py` reads these from `pg_constraint` and prints the
count, because a hand-typed list of {len(keys)} names has {len(keys)} chances to transpose
a column -- and a transposed constraint does not fail loudly, it enforces the wrong thing
(F133).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "{name.split("_")[0]}"
down_revision = "{previous}"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The parents that need `UNIQUE (organization_id, id)` before any composite key can
#: reference them.
PARENTS: tuple[str, ...] = (
{parent_list}
)

#: `(child table, constraint name, foreign column, parent table)`. The name is reused
#: verbatim so nothing that quotes it breaks.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
{fk_list}
)


def upgrade() -> None:
    for parent in PARENTS:
        op.create_index(f"uq_{{parent}}_org_id", parent, ["organization_id", "id"], unique=True)
    for child, name, column, parent in FOREIGN_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(
            name, child, parent, ["organization_id", column], ["organization_id", "id"]
        )


def downgrade() -> None:
    """The exact reverse, and the order is forced: keys off before the indexes they need,
    because dropping an index a live constraint depends on is refused by Postgres with a
    message that does not name the constraint."""
    for child, name, column, parent in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(name, child, parent, [column], ["id"])
    for parent in reversed(PARENTS):
        op.drop_index(f"uq_{{parent}}_org_id", table_name=parent)
'''
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="write the migration files")
    args = parser.parse_args()

    tranches, total_keys = asyncio.run(build_tranches())
    previous = "0020"
    for name in sorted(TRANCHES):
        bucket = tranches.get(name, {"keys": [], "parents": []})
        keys = sorted(bucket["keys"])
        parents = sorted(bucket["parents"])
        print(f"  {name}: {len(keys)} keys, {len(parents)} parents needing the pair")
        if args.write:
            source = _migration_source(name, parents, keys, previous)
            path = MIGRATIONS / f"{name}_tenant_foreign_keys.py"
            path.write_text(source)
            print(f"    wrote {path} ({len(source.splitlines())} lines)")
        previous = name.split("_")[0]
    covered = sum(len(b["keys"]) for b in tranches.values())
    print(f"  {covered} of {total_keys} bare keys are covered")
    if covered != total_keys:
        raise SystemExit("a key was dropped; the tranches do not partition the schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
