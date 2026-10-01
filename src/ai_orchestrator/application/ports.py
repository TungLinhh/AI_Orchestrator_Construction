"""The one capability the read layer needs from a database, and nothing else.

Every function in `application/*.py` that *reads* takes a `SqlRunner` rather than a
concrete `AsyncConnection`, and the reason is `api/deps.py`: the API hands handlers an
`AsyncSession`, because a request is a unit of work and the session is what commits it.
`AsyncSession` is not an `AsyncConnection` and mypy is right to refuse the substitution.

The tempting fix is to widen the annotation to `AsyncSession | AsyncConnection` and move
on. That is worse than it looks: it says "these functions want a database object", which
is not a fact anyone can check, and it spreads a two-way union into eight modules until
nobody knows which half a given function is safe with.

The fact is narrower and worth stating: **a read needs to execute a statement and get
mappings back.** `AsyncConnection` satisfies that, `AsyncSession` satisfies that, and so
does a test double. So that is the type.

Writes keep their concrete `AsyncConnection`, because they need more than this — several
use `begin_nested()` savepoints so a refused row does not poison the caller's
transaction, and a savepoint is a connection-level operation the Protocol does not
claim. Widening those would be a lie with a type annotation on it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncConnection


@runtime_checkable
class SqlRunner(Protocol):
    """Anything that can execute a statement and hand back the rows.

    Deliberately narrow: one method. The return type is `Any` on purpose -- SQLAlchemy's
    `Result` is a large, version-specific generic, and every caller narrows it with
    `.mappings()` on the next line regardless. Typing it as `CursorResult` looked more
    precise and made the Protocol match **nothing**: `AsyncSession.execute` returns
    `Result`, and a Protocol that a real object does not satisfy is a lie the type
    checker is right to reject.
    """

    async def execute(
        self, statement: Any, parameters: Any = None, *args: Any, **kwargs: Any
    ) -> Any: ...


#: What the application layer receives from the API and from tests. Named so that a
#: signature reads as an intent rather than as an implementation detail.
ReadConnection = SqlRunner

__all__ = ["AsyncConnection", "ReadConnection", "SqlRunner"]
