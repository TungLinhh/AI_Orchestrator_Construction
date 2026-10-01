"""Tenant isolation, proven against a live database.

These are the tests that decide whether the multi-tenant claim is real. They run
against PostgreSQL, not a mock, because the entire mechanism lives in the
database and a mock would only prove the mock works.

Three distinct attacks are covered:

  * an unbound session (no GUC set) must see nothing
  * a session bound to org A must not read org B's rows
  * a session bound to org A must not *write* into org B (the WITH CHECK clause)

The third is the one usually missed. A read-only leak is bad; an agent writing
a task into another tenant's organization is a data-integrity incident.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import OrganizationId, OrgUnitId, TaskId
from ai_orchestrator.persistence.rls import GLOBAL_TABLES, verify_rls
from ai_orchestrator.persistence.session import TENANT_GUC, Database

pytestmark = [pytest.mark.integration, pytest.mark.security]


async def _seed_org(db: Database, slug: str) -> str:
    """Create an organization.

    `organizations` has no RLS (it is the tenant root), so it is inserted through
    the owner role. The slug gets a unique suffix because the test schema
    persists between runs and `slug` is globally unique.
    """
    import uuid

    admin = Database.from_settings(use_admin_role=True)
    try:
        org_id = OrganizationId.create()
        async with admin.session() as session:
            await session.execute(
                text(
                    "INSERT INTO organizations (id, slug, name, status) "
                    "VALUES (:id, :slug, :name, 'active')"
                ),
                {
                    "id": str(org_id),
                    "slug": f"{slug}-{uuid.uuid4().hex[:8]}",
                    "name": f"Org {slug}",
                },
            )
            await session.commit()
    finally:
        await admin.dispose()
    return str(org_id)


async def _add_task(db: Database, org_id: str, title: str) -> str:
    task_id = str(TaskId.create())
    async with db.tenant_session(org_id) as session:
        await session.execute(
            text(
                "INSERT INTO tasks (id, organization_id, title, goal, fingerprint, dedup_key) "
                "VALUES (:id, :org, :title, :goal, :fp, :fp)"
            ),
            {
                "id": task_id,
                "org": org_id,
                "title": title,
                "goal": title,
                "fp": f"fp-{task_id}",
            },
        )
    return task_id


class TestRLSIsInstalled:
    async def test_every_tenant_table_is_protected(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            status = await verify_rls(conn)
        # `alembic_version` is Alembic's own bookkeeping table. It carries no
        # organization_id, so there is no column for a policy to compare and RLS
        # on it would be theatre.
        unprotected = set(status["unprotected"]) - {"alembic_version"}
        assert unprotected == set(), f"tables without RLS: {sorted(unprotected)}"
        assert status["total_tables"] > 40

    async def test_global_tables_are_the_only_ones_excluded(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            status = await verify_rls(conn)
        # `alembic_version` is Alembic's own bookkeeping table and carries no
        # organization_id, so it is expected to be unprotected. It used to be
        # named here as a literal; it is in `GLOBAL_TABLES` now, so this test
        # cannot pass while `verify_rls` still considers it unprotected.
        assert set(status["unprotected"]) <= set(GLOBAL_TABLES)


class TestUnboundSessionSeesNothing:
    async def test_no_tenant_guc_means_no_rows(self, db: Database) -> None:
        """A session that never set the GUC must not read tenant data.

        This is the default-deny property. The tempting policy is
        `... OR current_setting(...) = ''`, which returns everything to any
        code path that forgot to bind a tenant.
        """
        await _seed_org(db, "rls-unbound")
        async with db.session() as session:
            count = (await session.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        assert count == 0, "an unbound session must see no tenant rows"

    async def test_empty_tenant_guc_matches_nothing(self, db: Database) -> None:
        await _seed_org(db, "rls-empty")
        async with db.session() as session:
            await session.execute(text(f"SELECT set_config('{TENANT_GUC}', '', false)"))
            count = (await session.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        assert count == 0


class TestCrossTenantReadIsBlocked:
    async def test_agent_cannot_read_another_tenants_tasks(self, db: Database) -> None:
        org_a = await _seed_org(db, "rls-read-a")
        org_b = await _seed_org(db, "rls-read-b")
        await _add_task(db, org_a, "Org A private task")
        await _add_task(db, org_b, "Org B private task")

        async with db.tenant_session(org_a) as session:
            rows = (await session.execute(text("SELECT title FROM tasks"))).scalars().all()

        assert rows == ["Org A private task"], f"tenant A saw: {rows}"

    async def test_direct_lookup_by_known_id_still_denied(self, db: Database) -> None:
        """Knowing the row id must not bypass isolation."""
        org_a = await _seed_org(db, "rls-id-a")
        org_b = await _seed_org(db, "rls-id-b")
        b_task = await _add_task(db, org_b, "Org B secret")

        async with db.tenant_session(org_a) as session:
            found = (
                (
                    await session.execute(
                        text("SELECT title FROM tasks WHERE id = :id"), {"id": b_task}
                    )
                )
                .scalars()
                .all()
            )

        assert found == [], "guessing an id must not cross the tenant boundary"

    async def test_org_units_are_isolated_too(self, db: Database) -> None:
        org_a = await _seed_org(db, "rls-unit-a")
        org_b = await _seed_org(db, "rls-unit-b")
        for org, slug in ((org_a, "unit-a"), (org_b, "unit-b")):
            async with db.tenant_session(org) as session:
                await session.execute(
                    text(
                        "INSERT INTO organizational_units "
                        "(id, organization_id, name, slug, path) "
                        "VALUES (:id, :org, :n, :s, '/')"
                    ),
                    {"id": str(OrgUnitId.create()), "org": org, "n": slug, "s": slug},
                )
        async with db.tenant_session(org_a) as session:
            names = (
                (await session.execute(text("SELECT name FROM organizational_units")))
                .scalars()
                .all()
            )
        assert names == ["unit-a"]


class TestCrossTenantWriteIsBlocked:
    async def test_cannot_insert_a_row_into_another_tenant(self, db: Database) -> None:
        """The WITH CHECK clause. Read isolation without write isolation is half a
        control: an agent could still poison another org's task board."""
        from sqlalchemy.exc import DBAPIError

        org_a = await _seed_org(db, "rls-write-a")
        org_b = await _seed_org(db, "rls-write-b")

        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.tenant_session(org_a) as session:
                await session.execute(
                    text(
                        "INSERT INTO tasks "
                        "(id, organization_id, title, goal, fingerprint, dedup_key) "
                        "VALUES (:id, :org, 'injected', 'injected', :fp, :fp)"
                    ),
                    {
                        "id": str(TaskId.create()),
                        "org": org_b,
                        "fp": "fp-injected",
                    },
                )

        # And nothing landed.
        async with db.tenant_session(org_b) as session:
            count = (await session.execute(text("SELECT count(*) FROM tasks"))).scalar_one()
        assert count == 0

    async def test_cannot_update_another_tenants_row(self, db: Database) -> None:
        """An update to another tenant's row must be a no-op, not an error.

        This differs from the insert case, and the difference matters. RLS filters
        the row out of the UPDATE's scan, so the statement matches nothing and
        returns success with rowcount 0. PostgreSQL does not raise, because from
        its point of view the statement was valid — it just did not apply.

        The consequence for application code is the real trap: code that checks
        "did the UPDATE raise?" instead of "did it affect a row?" will believe a
        cross-tenant write succeeded. The assertion is therefore on the data
        being unchanged, not on an exception.
        """
        org_a = await _seed_org(db, "rls-upd-a")
        org_b = await _seed_org(db, "rls-upd-b")
        b_task = await _add_task(db, org_b, "Org B row")

        async with db.tenant_session(org_a) as session:
            result = await session.execute(
                text("UPDATE tasks SET title = 'hijacked' WHERE id = :id"),
                {"id": b_task},
            )
            affected = result.rowcount

        assert affected == 0, "a cross-tenant UPDATE must match no rows"

        async with db.tenant_session(org_b) as session:
            title = (
                await session.execute(
                    text("SELECT title FROM tasks WHERE id = :id"), {"id": b_task}
                )
            ).scalar_one()
        assert title == "Org B row", "another tenant's row must be unchanged"


class TestPooledConnectionSafety:
    async def test_tenant_does_not_leak_between_transactions(self, db: Database) -> None:
        """`SET LOCAL` must scope the GUC to the transaction.

        Without it, a pooled connection keeps the previous tenant's GUC and the
        next borrower reads the previous tenant's data. This is the failure mode
        that makes RLS implementations quietly wrong under concurrency.
        """
        from ai_orchestrator.persistence.session import assert_no_leaked_tenant

        org_a = await _seed_org(db, "rls-pool-a")
        org_b = await _seed_org(db, "rls-pool-b")
        await _add_task(db, org_a, "task A")
        await _add_task(db, org_b, "task B")

        # Interleave transactions on different tenants using the same pool.
        for org, expected in ((org_a, "task A"), (org_b, "task B"), (org_a, "task A")):
            async with db.tenant_session(org) as session:
                titles = (await session.execute(text("SELECT title FROM tasks"))).scalars().all()
                assert titles == [expected], f"tenant {org} saw {titles}"

        leaked = await assert_no_leaked_tenant(db.engine)
        assert leaked == [], f"connections returned to the pool still bound: {leaked}"


class TestAuditLedgerIsAppendOnly:
    async def test_application_role_cannot_delete_audit_rows(self, admin_db: Database) -> None:
        """Grants are asserted against the catalogue.

        A test connected as the owner would pass trivially, because the owner
        holds every privilege by definition.
        """
        async with admin_db.session() as session:
            privilege = (
                (
                    await session.execute(
                        text(
                            "SELECT privilege_type FROM information_schema.role_table_grants "
                            "WHERE grantee = 'ao_app' AND table_name = 'audit_logs' "
                            "AND privilege_type IN ('UPDATE','DELETE')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert privilege == [], f"ao_app must not hold {privilege} on audit_logs"

    async def test_application_role_cannot_bypass_rls(self, admin_db: Database) -> None:
        async with admin_db.session() as session:
            bypass = (
                await session.execute(
                    text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'ao_app'")
                )
            ).one()
        assert bypass[0] is False, "ao_app must not have BYPASSRLS"
        assert bypass[1] is False, "ao_app must not be a superuser"
