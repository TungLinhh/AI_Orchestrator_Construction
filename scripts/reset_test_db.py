"""Truncate the test database back to empty.

`make test` is safe to run repeatedly, and the database it uses is not.

Every integration test creates one organization through the `tenant` fixture and
**never deletes it**. Nothing truncates between runs either, so `ai_orchestrator_test`
grows by roughly 700 rows per run and keeps every row from every run before it. That is
how `uq_organizations_slug` eventually refused a fixture setup with

    Key (slug)=(tenant-7116ae6f) already exists

reported against whichever test happened to be executing, which is nothing to do with
slugs. A test suite that is only re-runnable on a fresh database is a test suite you
cannot run twice, and one you run twice is how a schema change gets verified.

Two ways to fix that, and this is the second:

* make the identifiers collision-proof, which the `tenant` fixture now does with a
  pid-plus-counter slug — that removes the *symptom*;
* bound the database, which is this script.

**Every table in the schema**, not `organizations` with `CASCADE`. The first version
truncated `organizations` and cascaded, and it left **3518 projects and 6181 progress
readings** behind — `CASCADE` follows foreign keys, and the tenant-scoped construction
tables do not all have one to `organizations`. So the cascade was a guess about the
schema that happened to be wrong about 2 tables out of 100, and a stale `projects` row is
exactly what a test that hardcodes an id would collide with next.

The table list comes from `information_schema` rather than from a hand-kept list,
because a hand-kept list is a fourth thing that silently stops matching reality. And
`alembic_version` is excluded, because emptying the database must not un-migrate it.

Refuses to touch a database whose name does not end in `_test`, because the whole point
is that nobody runs this against development by accident. There is no `--force` for the
same reason: a flag that exists is a flag that gets used.

    python scripts/reset_test_db.py
    make reset-test-db && make test
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url

from ai_orchestrator.persistence.session import Database

#: Refuse anything that does not end in this. A guard that is a convention is not a
#: guard; this one reads the name and stops.
_REQUIRED_SUFFIX = "_test"


async def main() -> int:
    settings = Database.from_settings(use_admin_role=True)
    database_name = make_url(str(settings.engine.url)).database or ""
    if not database_name.endswith(_REQUIRED_SUFFIX):
        print(
            f"refusing to truncate {database_name!r}: it does not end in "
            f"{_REQUIRED_SUFFIX!r}. This script exists to empty a throwaway test "
            f"database, and the name is the only thing standing between it and "
            f"development.",
            file=sys.stderr,
        )
        return 2

    async with settings.engine.connect() as conn:
        before = (await conn.execute(text("SELECT count(*) FROM organizations"))).scalar()
        tables = [
            r[0]
            for r in (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                        "   AND table_type = 'BASE TABLE'"
                        "   AND table_name <> 'alembic_version'"
                        " ORDER BY table_name"
                    )
                )
            ).all()
        ]
        if not tables:
            print(f"{database_name} has no tables; nothing to do.", file=sys.stderr)
            return 1
        # One statement, one transaction, `RESTART IDENTITY` because a sequence advanced
        # by 700 runs is not a thing anybody wants to keep. `CASCADE` is still here so
        # that the order of the list is irrelevant.
        await conn.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(f'"{t}"' for t in tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
        await conn.commit()
        after = (await conn.execute(text("SELECT count(*) FROM organizations"))).scalar()
        left = (
            await conn.execute(text("SELECT coalesce(sum(n_live_tup), 0) FROM pg_stat_user_tables"))
        ).scalar()

    await settings.engine.dispose()
    print(f"  {database_name}: truncated {len(tables)} tables")
    print(f"  organizations {before} -> {after}, rows left across the schema: {left}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
