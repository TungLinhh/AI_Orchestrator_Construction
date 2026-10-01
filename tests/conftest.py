"""Shared fixtures.

Two rules for tests in this repository:

  * a test that needs a database says so with `@pytest.mark.integration` and
    fails loudly rather than skipping silently, so "green" never quietly means
    "the database was never touched";
  * tests never depend on pre-existing database state. Each test creates the
    tenants it needs, with unique slugs.

Run order note: integration tests need `make migrate` to have been run. `make
test` does that for you.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from ai_orchestrator.config.settings import Environment, Settings, reset_settings_cache
from ai_orchestrator.persistence.session import Database


@pytest.fixture(scope="session", autouse=True)
def _deterministic_settings() -> None:
    """Tests must never spend money or reach a shared broker.

    The fake model provider is a deterministic scripted implementation: the full
    pipeline runs, with zero network calls and zero cost. See
    `ai_orchestrator.models.fake`.
    """
    os.environ["AO_ENVIRONMENT"] = Environment.TEST.value
    os.environ["AO_MODEL_PROVIDER_DEFAULT"] = "fake"
    os.environ["AO_EMBEDDING_PROVIDER_DEFAULT"] = "hash"
    os.environ["AO_LOG_JSON"] = "true"
    # Integration tests must never touch the development database. Pointing them
    # at their own schema is what makes `make test` safe to run while a dev
    # stack is up on the same machine.
    os.environ["AO_POSTGRES_DB"] = "ai_orchestrator_test"
    os.environ["AO_TEST_DB_NAME"] = "ai_orchestrator_test"
    reset_settings_cache()


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    """A `Database` using the least-privilege application role.

    Deliberately *not* the owner role. Connecting as the owner would give the
    tests BYPASSRLS, and every tenant-isolation test would pass vacuously — the
    policies would be in the schema and doing nothing.
    """
    from ai_orchestrator.persistence.session import Database

    database = Database.from_settings()
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def admin_db() -> AsyncIterator[Database]:
    """A `Database` using the owner role.

    Only for asserting on grants and catalogue state, where the point is the
    privilege configuration rather than the data.
    """
    from ai_orchestrator.persistence.session import Database

    database = Database.from_settings(use_admin_role=True)
    try:
        yield database
    finally:
        await database.dispose()


@pytest_asyncio.fixture
async def test_settings() -> Settings:
    return Settings()


# Re-exported so every integration test can `from tests.conftest import tenant`
# without each one re-importing the fixture machinery.
from tests.integration.tenant_context import Tenant, tenant  # noqa: E402, F401
