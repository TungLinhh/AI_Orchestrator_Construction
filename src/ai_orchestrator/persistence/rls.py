"""Row-level security policies.

The policy is one predicate, applied uniformly:

    current_setting('app.current_tenant', true) = organization_id

with `FORCE ROW LEVEL SECURITY` so the table owner is filtered too, and with the
GUC compared as text. Three details are load-bearing:

`true` as the missing_ok argument
    An unset GUC yields '' rather than raising. That is what lets migrations,
    seeds and the login path run at all, since they legitimately have no
    tenant bound yet.

Empty GUC denies
    The predicate compares to `organization_id`, never to ''. So an unset GUC
    matches nothing rather than everything. The dangerous version of this design
    is `current_setting(...) = organization_id OR current_setting(...) = ''`,
    which silently disables isolation for every code path that forgot to set it.

Two documented escape hatches
    The owner role (used only by Alembic) bypasses RLS by being a superuser, and
    a dedicated `ao_app` role is created without BYPASSRLS. If the application
    ever connects as the owner, the protection is gone — `tests/integration/
    test_tenant_isolation.py` asserts it does not.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

#: Tables that are global rather than tenant-scoped. RLS is meaningless on them
#: and enabling it would only add a predicate that always passes.
#:
#: `alembic_version` joined this list when a whole-schema `verify_rls` check was
#: written and failed on it. It has no `organization_id` and never will — it
#: records which migration the database is at, for every tenant at once. The
#: previous handling was an `| {"alembic_version"}` in the assertion inside
#: `tests/integration/test_tenant_isolation.py`, which put the fact in one test
#: while the function that needed to know about it did not. Stated here instead,
#: so the exclusion lives in one place and every caller inherits it.
GLOBAL_TABLES: frozenset[str] = frozenset(
    {
        "organizations",
        "consumer_offsets",
        "alembic_version",
    }
)

#: Role the application connects as. Created by migration 0002.
APP_ROLE = "ao_app"


async def apply_rls(connection: AsyncConnection, table_names: list[str]) -> list[str]:
    """Enable and force RLS, and install the isolation policy, per table."""
    applied: list[str] = []
    for table in sorted(table_names):
        if table in GLOBAL_TABLES:
            continue
        await connection.execute(text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
        # Without FORCE, the table owner bypasses every policy. The migration
        # role is the owner, so FORCE is what makes the policy real.
        await connection.execute(text(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY'))
        await connection.execute(text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
        await connection.execute(
            text(
                f'CREATE POLICY tenant_isolation ON "{table}" '
                "USING (organization_id = current_setting('app.current_tenant', true)) "
                "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
            )
        )
        applied.append(table)
    return applied


async def verify_rls(connection: AsyncConnection) -> dict[str, object]:
    """Check that RLS is enabled and forced wherever it should be."""
    sql = text(
        """
        SELECT c.relname AS table_name,
               c.relrowsecurity AS rls_enabled,
               c.relforcerowsecurity AS rls_forced,
               EXISTS (SELECT 1 FROM pg_policies p
                       WHERE p.schemaname = n.nspname AND p.tablename = c.relname) AS has_policy
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY c.relname
        """
    )
    result = await connection.execute(sql)
    rows = result.mappings().all()
    unprotected = [
        r["table_name"]
        for r in rows
        if r["table_name"] not in GLOBAL_TABLES
        and not (r["rls_enabled"] and r["rls_forced"] and r["has_policy"])
    ]
    return {
        "total_tables": len(rows),
        "protected": len(rows)
        - len(unprotected)
        - len([r for r in rows if r["table_name"] in GLOBAL_TABLES]),
        "unprotected": unprotected,
        "ok": not unprotected,
    }


__all__ = ["APP_ROLE", "GLOBAL_TABLES", "apply_rls", "verify_rls"]
