"""Fixtures for the integration suite.

Only fixtures that **every** integration test module wants live here. The `client`
fixture is the one: three modules need an HTTP client against the real app, and each
growing its own is how two of them ended up sending an `Authorization` header that
`HTTPBearer` rejects before it reads the value. F123.

It lives here rather than in a shared helper module because that is what pytest is for:
a fixture is discovered by name, so every test can take a `client` parameter without
importing anything, and `ruff` does not read three dozen `client: Any` parameters as a
redefinition of an imported name.

The lifespan is deliberately **not** run. `create_app`'s lifespan health-checks the
database and asserts the application connects as a least-privilege role, which is right
for a process about to serve traffic and wrong for a test that already holds a session;
running it would also need `asgi_lifespan`, which is not a dependency. Everything the
handlers actually touch -- middleware, routing, authentication, the tenant binding, the
dependency chain -- is real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest_asyncio
from sqlalchemy import text

from ai_orchestrator.persistence.session import Database

#: Invented here, used only between the test process and the app it builds. Never a
#: real credential and never read from the environment.
TEST_SECRET = "test-only-service-secret-not-a-real-credential"


@pytest_asyncio.fixture
async def client(tenant: Any, monkeypatch: Any) -> AsyncIterator[Any]:
    """An HTTP client against the real app, in-process."""
    import httpx

    from ai_orchestrator.api.app import create_app
    from ai_orchestrator.config.settings import reset_settings_cache

    monkeypatch.setenv("AO_INTERNAL_SERVICE_SECRET", TEST_SECRET)
    reset_settings_cache()

    app = create_app()
    app.state.db = tenant.db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control-plane.test"
    ) as http:
        yield http


#: A second organization with a session bound to it, for cross-tenant tests.
#:
#: Two tenants are what a cross-tenant test needs, and the second is the same idea as the
#: first. A row written through this session is invisible to `tenant`'s session under RLS,
#: which is what makes it a *real* cross-tenant parent rather than a plausible-looking id --
#: and a plausible-looking id is refused by any foreign key, composite or not, so a test
#: built on one proves only that foreign keys exist.
@pytest_asyncio.fixture
async def other_tenant(db: Any) -> AsyncIterator[Any]:
    from tests.integration.tenant_context import Tenant, _create_organization

    admin = Database.from_settings(use_admin_role=True)
    org_id = await _create_organization(admin, "other")
    await admin.dispose()

    session = db.session_factory()
    await session.begin()
    await session.execute(
        text("SELECT set_config('app.current_tenant', :org, true)"), {"org": org_id}
    )
    try:
        yield Tenant(organization_id=org_id, session=session, slug="other", db=db)
    finally:
        await session.rollback()
        await session.close()
