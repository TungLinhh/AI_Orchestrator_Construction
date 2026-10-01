"""Copy the dossier's reference catalogue into another schema, across databases.

`python scripts/seed_reference_catalogue.py --to ai_orchestrator_test`

## Why this exists

`sop_definitions`, `autonomy_policies` and `roles` are **tenant-scoped**, and the dossier's
twenty-eight SOPs, six autonomy policies and nine roles have only ever been seeded into
the development schema. Two consequences, both found by running things:

* `tests/integration/test_agent_register_seeded.py` skipped all ten of its tests, because a
  schema with no catalogue cannot run the eight agents — which is correct, and useless as
  a permanent state.
* `scripts/seed_agent_register.py` refused every agent with
  `no such SOP in sop_definitions`, having correctly read the catalogue from the schema it
  was configured for and found nothing.

## Why a separate script and not `--from-org` on the agent seeder

`--from-org` reads the source through `db.tenant_session`, and that binds to **one**
schema. Pointing it at a tenant in a different database reads as empty, and the twenty-odd
downstream "no such SOP" messages say nothing about the real cause — which cost a full
debugging round before the schema was named in the message.

This script opens a connection to each database and moves rows in Python, so crossing is
explicit and the failure says which database it could not reach.

## Rows copied, and rows deliberately not

Copied: `sop_definitions`, `autonomy_policies`, `roles`. Ids are **fresh**, so two schemas
holding the same catalogue do not share a row — and a change in the test schema is not a
change in development. `roles.parent_role_id` is re-pointed at the copy for the same
reason.

Not copied: anything business. A quotation, a contract, a progress reading belongs to an
organisation that earned it. This moves the dossier's *reference* data — the procedures
and the limits — and nothing else.

## Idempotent

Every insert is skipped when a row of the same natural key exists, and the script prints
what it wrote and what it found. Running it twice is a no-op, which is the only way a
schema-provisioning step is safe to put in a `make` target.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import asyncpg

#: `(table, natural key column, columns in order)`. The natural key is what makes the
#: insert idempotent, and it is the dossier's own key rather than a surrogate.
TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "roles",
        "name",
        (
            "name",
            "description",
            "authority_profile",
            "allowed_capabilities",
            "max_autonomy",
            "may_delegate_to_peers",
            "may_spawn_subagents",
            "max_delegation_depth",
            "is_system_role",
        ),
    ),
    (
        "sop_definitions",
        "code",
        (
            "code",
            "name_vi",
            "name_en",
            "doc_type",
            "block",
            "department",
            "owner_role_key",
            "related_gate_codes",
            "source",
            "source_actor",
        ),
    ),
    (
        "autonomy_policies",
        "action_class",
        (
            "action_class",
            "name_vi",
            "max_level",
            "is_hard_block",
            "rationale",
            "approved_by",
            "approved_at",
            "source",
            "source_actor",
        ),
    ),
)

#: The organization whose rows are the source. Resolved by asking which one holds a
#: catalogue, so the script needs no configuration and cannot copy the wrong tenant's.
SOURCE_HINT = "the organization holding the SOP catalogue"


async def _dsn_for(database: str) -> str:
    """A DSN `asyncpg.connect` accepts.

    The application's DSN carries the `postgresql+asyncpg` scheme, which SQLAlchemy
    understands and `asyncpg` rejects with *"scheme is expected to be either postgresql or
    postgres"*. The rewrite lives here rather than at the call sites so the two connections
    this script opens cannot be built differently from each other.
    """
    from ai_orchestrator.config.settings import get_settings

    dsn = get_settings().owner_dsn_for(database)
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _source_org(conn: asyncpg.Connection) -> str | None:
    row = await conn.fetchrow(
        "SELECT organization_id FROM sop_definitions "
        " GROUP BY organization_id HAVING count(*) > 0 ORDER BY 1 LIMIT 1"
    )
    return str(row["organization_id"]) if row else None


async def run(to_database: str, from_database: str) -> int:
    source = await asyncpg.connect(await _dsn_for(from_database))
    target = await asyncpg.connect(await _dsn_for(to_database))
    written = found = 0
    try:
        src_org = await _source_org(source)
        if src_org is None:
            print(
                f"  {from_database} holds no SOP catalogue, so there is nothing to copy. "
                f"Seed it first: this script copies, it does not create."
            )
            return 1
        print(f"  copying the reference catalogue from {from_database} ({src_org})")

        # Reuse the target schema's existing catalogue holder rather than creating
        # another. This is the idempotence: the first version inserted a **new
        # organization** on every run, so running it twice left two schemas-worth of a
        # catalogue, and the agent seeder's discovery then found two and refused with
        # "2 organizations hold at least 16 SOPs. Pass --from-org to say which" -- a
        # provisioning step that breaks the thing it provisions on its second run.
        needed = 16
        dst_org = await target.fetchval(
            "SELECT organization_id FROM sop_definitions "
            " GROUP BY organization_id HAVING count(*) >= $1 ORDER BY 1 LIMIT 1",
            needed,
        )
        if dst_org is None:
            dst_org = f"org_{await target.fetchval('SELECT gen_random_uuid()')}"
            await target.execute(
                "INSERT INTO organizations (id, slug, name, status) VALUES ($1, $2, $3, 'active')",
                dst_org,
                f"reference-{dst_org[4:12]}",
                f"Reference catalogue (from {src_org})",
            )
            print(f"    created the catalogue holder {dst_org}")
        else:
            dst_org = str(dst_org)
            print(f"    reusing the catalogue holder {dst_org}")

        id_map: dict[str, dict[str, str]] = {}
        for table, key, columns in TABLES:
            rows = await source.fetch(
                f"SELECT {', '.join(columns)} FROM {table} WHERE organization_id = $1 "
                f" ORDER BY {key}",
                src_org,
            )
            already = {
                str(r[key])
                for r in await target.fetch(
                    f"SELECT {key} FROM {table} WHERE organization_id = $1", dst_org
                )
            }
            id_map[table] = {}
            for row in rows:
                name = str(row[key])
                if name in already:
                    found += 1
                    continue
                new_id = f"{table[:3]}_{await target.fetchval('SELECT gen_random_uuid()')}"
                values = [new_id, dst_org, *[row[c] for c in columns]]
                placeholders = ", ".join(f"${i + 1}" for i in range(len(values)))
                await target.execute(
                    f"INSERT INTO {table} (id, organization_id, {', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    *values,
                )
                id_map[table][name] = new_id
                written += 1
            print(f"    {table}: {len(rows)} in source, {written} written so far")

        # Re-point the self-reference at the copies, not at the source rows.
        for row in await source.fetch(
            "SELECT name, parent_role_id FROM roles WHERE organization_id = $1", src_org
        ):
            parent = row["parent_role_id"]
            if parent is None:
                continue
            parent_name = await source.fetchval("SELECT name FROM roles WHERE id = $1", parent)
            mapped = id_map.get("roles", {}).get(str(parent_name))
            if mapped is None:
                continue
            await target.execute(
                "UPDATE roles SET parent_role_id = $1 WHERE id = $2",
                mapped,
                id_map["roles"][str(row["name"])],
            )
        print(f"  wrote {written}, already present {found}, into {to_database}")
        return 0
    finally:
        await source.close()
        await target.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="the schema to copy into")
    parser.add_argument(
        "--from",
        dest="from_database",
        default="ai_orchestrator",
        help=f"the schema to copy from; it holds {SOURCE_HINT}",
    )
    args = parser.parse_args()
    if args.to == args.from_database:
        print("  --to and --from are the same schema; nothing to do")
        return 0
    return asyncio.run(run(args.to, args.from_database))


if __name__ == "__main__":
    sys.exit(main())
