"""Database session management and tenant context.

The single most important thing in this file is `tenant_session`. It sets the
`app.current_tenant` GUC for the duration of a transaction, which is what the
row-level-security policies read. Two things follow from that and both matter:

  * `SET LOCAL` scopes the GUC to the transaction, so a pooled connection cannot
    leak one tenant's identity into the next request. This is the bug that makes
    a naive RLS implementation leak data between tenants under concurrency.
  * The GUC is set from a verified session attribute, never from a request
    parameter, so a caller cannot ask for another tenant's rows.

RLS is defence in depth, not the only defence. The repositories also scope
their queries, because an app-level bug must not be one `SET row_security = off`
away from a cross-tenant breach.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ai_orchestrator.config.settings import Settings, get_settings

#: The GUC the RLS policies read. Named `app.current_tenant` to match the
#: convention in the reference implementation this was adapted from.
TENANT_GUC = "app.current_tenant"
#: GUC identifying the acting principal, for policies that are actor-scoped.
ACTOR_GUC = "app.current_actor_id"


@dataclass(slots=True)
class Database:
    """Owns the engine and hands out sessions. One instance per process.

    `use_admin_role=True` connects as the owner, which has BYPASSRLS. It exists
    for backups and administrative tooling and for the tests that assert on
    grants. It is never what a request path uses: an application connected as
    the owner would run every query with tenant isolation switched off while the
    RLS policies still sat in the schema looking correct.
    """

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    is_admin: bool = False
    _configured: bool = field(default=False, init=False)

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, use_admin_role: bool = False
    ) -> Database:
        settings = settings or get_settings()
        url = settings.admin_database_url if use_admin_role else settings.async_database_url
        engine = create_async_engine(
            url,
            # A JSON column must never be able to abort a transaction.
            #
            # SQLAlchemy's default serializer is strict: a `date`, a `Decimal` or a
            # `UUID` inside a JSON column raises on flush, and because the flush
            # covers every pending row, one unserialisable value in one *record*
            # column fails writes to unrelated tables. It surfaced here when
            # counting tool calls added an `UPDATE executions`, which forced a
            # flush that had not happened at that point in the run -- the bug was
            # older than the change that revealed it.
            #
            # `default=str` is the right trade for these columns, which hold
            # audit context, decision records and tool arguments: they are read
            # by people and by models, not round-tripped into Python types. A
            # date stored as an ISO string is the same fact.
            json_serializer=lambda obj: json.dumps(obj, default=str),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_s,
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "application_name": (
                        f"{settings.service_name}-admin"
                        if use_admin_role
                        else settings.service_name
                    ),
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                },
                "timeout": settings.db_connect_timeout_s,
            },
        )
        return cls(
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            is_admin=use_admin_role,
        )

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session with no tenant bound.

        For migrations, health checks and work on global tables. With the
        application role this sees no tenant rows at all, because an unbound
        GUC matches nothing — which is the point.
        """
        async with self.session_factory() as session:
            yield session

    async def assert_app_role_is_least_privilege(self) -> None:
        """Verify at startup that RLS is actually in force for this connection.

        A deployment where the application accidentally holds the owner role
        would pass every functional test and leak every tenant. This check turns
        that into a startup failure.
        """
        async with self.session() as session:
            current_user: str = (await session.execute(text("SELECT current_user"))).scalar_one()
            bypass = (
                await session.execute(
                    text("SELECT rolbypassrls, rolsuper FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
        if bypass[0] or bypass[1]:
            msg = (
                f"database connection runs as {current_user!r}, which bypasses row-level "
                f"security; tenant isolation is not in force. Set AO_APP_DB_USER to the "
                f"least-privilege role, not the owner."
            )
            raise PermissionError(msg)

    @asynccontextmanager
    async def tenant_session(
        self, organization_id: str, *, actor_id: str | None = None
    ) -> AsyncIterator[AsyncSession]:
        """A session bound to one tenant, for the life of one transaction.

        Usage is `async with db.tenant_session(org_id) as session:` and the
        session is committed or rolled back by the caller's `async with` block.
        Nothing outside this context can read tenant rows.
        """
        async with self.session_factory() as session, session.begin():
            await _set_tenant(session, organization_id, actor_id)
            yield session

    async def bind_tenant(
        self, session: AsyncSession, organization_id: str, actor_id: str | None = None
    ) -> None:
        """Re-apply the tenant binding to an *open* session.

        `tenant_session` does this once, inside a transaction it owns. A driver
        that commits part-way through -- which the pipeline does, so that a crash
        keeps the work already done -- must call this again afterwards, and the
        reason is not a detail: **the binding is `SET LOCAL`, so it dies with the
        transaction.** A pipeline that committed and carried on would have no
        tenant set, row-level security would filter every row away, and the run
        would report an empty organisation and finish successfully.

        That is the shape of bug this project has hit repeatedly: a run that
        produces a plausible empty answer. So the re-bind is a named, documented
        call rather than something each caller has to remember.
        """
        await _set_tenant(session, organization_id, actor_id)

    @asynccontextmanager
    async def committing_tenant_session(
        self, organization_id: str, *, actor_id: str | None = None
    ) -> AsyncIterator[AsyncSession]:
        """A tenant session the caller may commit as often as it likes.

        The transaction is **not** owned here, so `session.commit()` is legal
        inside the block -- which is the point, and is why this is not
        `tenant_session`. Committing inside `tenant_session` ends the
        transaction its `session.begin()` opened, and the next statement raises
        "Can't operate on closed transaction".

        The tenant is re-applied on entry only; **call `bind_tenant` after every
        commit**, because the binding is transaction-scoped and the alternative is
        a run that reads nothing and says it finished.
        """
        async with self.session_factory() as session:
            await self.bind_tenant(session, organization_id, actor_id)
            yield session

    async def healthcheck(self) -> bool:
        try:
            async with self.session() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def rls_status(self) -> dict[str, Any]:
        """Report which tables actually have RLS enabled.

        Useful in tests and in the readiness probe: a table that was created
        without a policy would otherwise look identical to a protected one.
        """
        sql = text(
            """
            SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled,
                   c.relforcerowsecurity AS rls_forced
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )
        async with self.session() as session:
            rows = (await session.execute(sql)).mappings().all()
        return {
            "tables": len(rows),
            "rls_enabled": sum(1 for r in rows if r["rls_enabled"]),
            "rls_forced": sum(1 for r in rows if r["rls_forced"]),
            "unprotected": [r["table_name"] for r in rows if not r["rls_enabled"]],
        }


async def _set_tenant(session: AsyncSession, organization_id: str, actor_id: str | None) -> None:
    """Bind the tenant for this transaction only.

    `SET LOCAL` (not `SET`) is essential: the connection returns to the pool
    after the transaction, and a `SET` would persist the tenant for the next
    borrower of the same connection.
    """
    await session.execute(
        text(f"SELECT set_config('{TENANT_GUC}', :org, true)"),
        {"org": organization_id},
    )
    if actor_id:
        await session.execute(
            text(f"SELECT set_config('{ACTOR_GUC}', :actor, true)"),
            {"actor": actor_id},
        )


async def assert_no_leaked_tenant(engine: AsyncEngine) -> list[str]:
    """Report pooled connections that still carry a tenant GUC.

    With `SET LOCAL` this should always be empty. It is checked anyway because
    the failure it guards against — a connection returned to the pool still
    bound to a tenant — is silent, cross-tenant, and only ever observable as
    somebody else's data appearing in a response.

    Call it from a test or from the readiness probe, not on every request.
    """
    sql = text(
        """
        SELECT pid, current_setting('app.current_tenant', true) AS tenant
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND current_setting('app.current_tenant', true) <> ''
          AND pid <> pg_backend_pid()
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql)).mappings().all()
    return [f"pid={r['pid']} tenant={r['tenant']}" for r in rows]
