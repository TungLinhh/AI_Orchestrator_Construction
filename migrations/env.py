"""Alembic environment.

Migrations run synchronously. An async migration runner adds a failure mode
(reordering, partial application on cancellation) that buys nothing: schema
changes are rare and blocking is fine.

Two rules encoded here rather than in prose:

  * `include_schemas` is off. Everything lives in `public`.
  * The URL comes from the same `Settings` object as the application, so a
    migration can never be pointed at a different database by a stale
    environment variable.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config

# Make the src/ layout importable when alembic is invoked directly.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The construction domain lives in its own module. It is imported here, and not
# merely for its side effect, because a table only reaches `Base.metadata` when
# its module is imported — so without this line autogenerate would see a schema
# with the construction tables missing and propose to DROP every one of them.
# `noqa: F401` on the import is the point: the reference is unused by name.
# Every module that declares domain tables, imported for the side effect. A table
# only reaches `Base.metadata` when its module is imported, so a module missing
# here is a module whose tables autogenerate would propose to DROP.
import ai_orchestrator.persistence.agent_framework  # noqa: E402
import ai_orchestrator.persistence.commercial  # noqa: E402
import ai_orchestrator.persistence.construction  # noqa: E402
import ai_orchestrator.persistence.contracts  # noqa: E402
import ai_orchestrator.persistence.process  # noqa: E402
import ai_orchestrator.persistence.procurement  # noqa: E402
import ai_orchestrator.persistence.progress  # noqa: E402
import ai_orchestrator.persistence.supply  # noqa: E402, F401
from ai_orchestrator.config.settings import get_settings  # noqa: E402
from ai_orchestrator.persistence.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # `sqlalchemy.url` in alembic.ini is left empty on purpose: a checked-in
    # DSN with a password is a leak waiting to happen.
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    # `AO_MIGRATION_DB` names the database explicitly. The integration suite runs
    # against `ai_orchestrator_test`, and it read for a long time that
    # `make migrate` had prepared it — which is only true of whichever database the
    # settings happened to point at. So a migration could be applied to the
    # development schema and never exercised against the schema the tests actually
    # use, and the first sign of it was `UndefinedColumnError` in a test that had
    # passed a minute earlier.
    #
    # `sync_database_url` is used rather than the app DSN because the owner role is
    # needed for DDL, and the owner is exactly what `make migrate` has always used.
    override = os.environ.get("AO_MIGRATION_DB")
    settings = get_settings()
    if override:
        return settings.owner_dsn_for(override)
    return settings.sync_database_url


def run_migrations_offline() -> None:
    """Emit SQL without connecting. Used to review a migration before applying."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        # Autogenerate must see the same schema the app sees, including server
        # defaults; without these a rename looks like a drop+add and a changed
        # default is invisible.
        compare_type=True,
        compare_server_default=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(section, prefix="sqlalchemy.", future=True)
    try:
        async with connectable.connect() as connection:
            # The engine is asyncpg; Alembic's own API is synchronous, so the
            # whole migration runs inside one greenlet context. Streaming each
            # statement separately would break transactional DDL.
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    import asyncio

    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
