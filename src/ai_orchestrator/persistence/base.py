"""SQLAlchemy base and shared column conventions.

Two conventions used everywhere, so they are stated once:

  * primary keys are text holding a prefixed ULID (see `domain/ids.py`), never a
    database sequence. A sequence leaks row counts and makes cross-service id
    generation require a round trip; a sortable ULID gives sortable natural keys
    for free and lets any component mint an id offline.
  * `organization_id` is NOT NULL on every org-scoped table and leads every
    composite index. Tenant isolation is then an index property rather than
    something a query author has to remember.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, MetaData, Numeric, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Explicit constraint naming so Alembic can autogenerate a drop for everything,
# and so a failed migration names a constraint an operator can actually look up.
NAMING_CONVENTION: dict[str, str] = {
    # Index names are built from the table, not from the first column: an index
    # on three columns would otherwise be named after whichever one happens to be
    # first, and renaming a column would rename the index with it.
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Every money column uses this. 18 digits, 6 decimal places: enough for
#: per-call cost in USD, and NUMERIC rather than float so comparisons are exact.
MONEY = Numeric(18, 6)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> dt.datetime:
    """Timezone-aware UTC. The only clock read in the persistence layer."""
    return dt.datetime.now(dt.UTC)


def created_at_col() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


def updated_at_col() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


__all__ = [
    "JSONB",
    "MONEY",
    "NAMING_CONVENTION",
    "Base",
    "created_at_col",
    "updated_at_col",
    "utcnow",
]
