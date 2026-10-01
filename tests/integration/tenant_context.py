"""Tenant-scoped test context.

Integration tests need a live database and a tenant to scope to. Rather than
each test wiring up an organization and a transaction by hand, they take the
`tenant` fixture, which yields both and guarantees cleanup.

The organization is created through the owner role (`organizations` has no RLS,
it is the tenant root) and the session that tests use is bound to it through the
same `tenant_session` path the application takes. That means a test exercising
RLS is exercising the real code path, not a special case.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from itertools import count

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.persistence.session import Database


@dataclass(slots=True)
class Tenant:
    """One organization plus a session bound to it."""

    organization_id: str
    session: AsyncSession
    slug: str
    db: Database = None  # type: ignore[assignment]

    async def commit(self) -> None:
        """Commit, re-open a transaction and re-bind the tenant.

        Needed by any test that requires a second connection to see the fixture's
        work. The tenant GUC is `SET LOCAL`, so it is transaction-scoped and has
        to be re-established after every commit.

        The session is also expired: `expire_on_commit=False` is right for the
        application, where one request owns one transaction, but a test that
        commits mid-test is deliberately simulating a *new* request and must
        re-read rather than trust a cached object.
        """
        await self.session.commit()
        await self.session.begin()
        await self.session.execute(
            text("SELECT set_config('app.current_tenant', :org, true)"),
            {"org": self.organization_id},
        )
        self.session.expire_all()

    async def run(self, fn):
        """Run a callable against this tenant's session.

        Commits at the end so a test that writes rows can read them back, and
        rolls back on failure so a failing test does not leave a half-written
        tenant behind for the next one.

        The commit goes through `commit()` rather than `session.commit()` on its
        own, and that indirection is load-bearing. The tenant binding is
        `set_config(..., true)` — transaction-local — so a bare commit ends the
        transaction and takes the binding with it. Every subsequent read in that
        session then runs with no tenant, which under RLS returns zero rows, and a
        test that wrote a row and read it back fails with `NoResultFound` and no
        explanation. That is not a theoretical sharp edge: it is what four of the
        tests calling this method were silently doing.
        """
        try:
            result = await fn(self.session)
            await self.commit()
            return result
        except BaseException:
            await self.session.rollback()
            raise


#: A per-process counter, so two tenants created in the same run cannot collide however
#: the slug is built.
#:
#: The slug used to be `{label}-{uuid4().hex[:8]}` — 32 random bits, which is fine in
#: isolation and wrong over time. The test database is **never truncated**: every test
#: leaks one organization, so it grows by roughly 700 rows per run and keeps every row
#: from every previous run. Ten runs puts seven thousand slugs in a 4-billion space,
#: and `uq_organizations_slug` then refuses a fixture setup somewhere in the middle of a
#: green suite with `Key (slug)=(tenant-7116ae6f) already exists` — reported against
#: whichever test happened to be running, which is nothing to do with slugs.
#:
#: A counter plus the pid makes a collision **structurally impossible** within a run and
#: across runs on the same second, and it makes the row readable: `tenant-12345-0042`
#: says which process and which test created it. `make reset-test-db` deals with the
#: unbounded growth, which is the other half.
_TENANT_SERIAL = count()


async def _create_organization(admin: Database, label: str) -> str:
    org_id = str(OrganizationId.create())
    slug = f"{label}-{os.getpid()}-{next(_TENANT_SERIAL):04d}"
    async with admin.session() as session:
        await session.execute(
            text(
                "INSERT INTO organizations (id, slug, name, status) "
                "VALUES (:id, :slug, :name, 'active')"
            ),
            {"id": org_id, "slug": slug, "name": f"Test {label} {slug}"},
        )
        await session.commit()
    return org_id


@pytest_asyncio.fixture
async def tenant(db: Database) -> AsyncIterator[Tenant]:
    """A fresh organization with a session bound to it.

    The transaction is started explicitly rather than through `tenant_session`,
    which wraps `session.begin()` in a context manager. A test that needs a
    second connection to observe its own writes has to commit mid-test, and a
    commit inside that context manager ends it permanently. Owning the
    transaction here keeps that possible; `Tenant.commit()` re-establishes the
    tenant binding afterwards.
    """
    admin = Database.from_settings(use_admin_role=True)
    org_id = await _create_organization(admin, "tenant")
    await admin.dispose()

    session = db.session_factory()
    await session.begin()
    await session.execute(
        text("SELECT set_config('app.current_tenant', :org, true)"),
        {"org": org_id},
    )
    try:
        yield Tenant(organization_id=org_id, session=session, slug="test-tenant", db=db)
    finally:
        await session.rollback()
        await session.close()
