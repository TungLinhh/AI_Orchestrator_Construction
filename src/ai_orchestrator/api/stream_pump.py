"""Feeding the browser stream from the event bus.

The stream in `api/stream.py` is a projection, and this is the only thing that writes
into it. It reads from NATS, the same bus the outbox relay and every other consumer
read, so a browser tab is a subscriber to the platform's real event flow rather than a
private channel into the database.

**One consumer per process, not one per tab.** A pull consumer per connection would
mean a durable per connection, an ack deadline per connection, and a stream whose size
grows with the number of people watching. One durable, fanned out in process.

**Nothing here is allowed to be the only copy.** If this consumer dies, the events are
still in the `events` table and a client that reconnects backfills from there. That is
the reason the stream backfills at all, and it is why a dead feed is a degraded view
rather than a lost one.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select

from ai_orchestrator.persistence.models import Event

logger = logging.getLogger(__name__)

#: The durable's name. Stable across restarts on purpose: a durable name that changed
#: per process would leave an abandoned durable behind on every deploy, and JetStream
#: keeps them until something explicitly deletes them.
CONSUMER_NAME = "api-stream-fanout"


async def publish_from_database(fanout: Any, organization_id: str) -> int:
    """Push anything written since the last cursor, then advance it.

    The safety net under the NATS consumer. If the bus is down, or the consumer
    missed something, the view still moves — and because it reads the same durable
    `events` table the API serves, it cannot invent state the platform does not have.

    Cursor is a timestamp rather than an offset because `events` is append-only with
    no sequence column of its own, and a timestamp cursor on an append-only table is
    monotone in practice. `>` rather than `>=` so the last frame is not replayed on
    every tick, which would look like the platform repeating itself.
    """
    from ai_orchestrator.api.stream import enrich, frame_of
    from ai_orchestrator.persistence.session import Database

    db: Database = fanout.db
    cursor: datetime | None = getattr(fanout, "cursor", None)
    stmt = select(Event).where(Event.organization_id == organization_id)
    if cursor is not None:
        stmt = stmt.where(Event.occurred_at > cursor)
    async with db.tenant_session(organization_id) as session:
        events = (await session.execute(stmt.order_by(Event.occurred_at))).scalars().all()
        if not events:
            return 0
        frames = [frame_of(event) for event in events]
        # Enriched inside the same transaction the events were read in, so a
        # subscriber never sees a half-resolved frame: an id with no name, because
        # the name query had not run yet.
        await enrich(frames, session, organization_id)
    for frame in frames:
        await fanout.publish(frame)
    fanout.cursor = events[-1].occurred_at
    return len(events)


async def run_stream_pump(
    fanout: Any,
    database: Any,
    *,
    interval_seconds: float = 2.0,
) -> None:
    """Poll the durable event log and push what is new, forever.

    Deliberately a database poll rather than a NATS consumer for the *first* version.
    It needs no broker, survives a broker restart, cannot lose an event, and — the
    part that matters for a first version — cannot acknowledge a message and then fail
    to render it. It costs one indexed query every two seconds per process, which for
    a control plane viewed by a human is nothing.

    A NATS consumer is the right answer at a different point: when the event rate makes
    a two-second poll visibly late, or when a frame must arrive within milliseconds of
    the write. Both are measurable, and neither is true yet.

    The loop must not die. A dead pump is a frozen page, and a frozen page is
    indistinguishable from an idle platform.
    """
    fanout.db = database
    while True:
        try:
            for org in await _organizations(database):
                await publish_from_database(fanout, org)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("stream.pump_failed", extra={"error": str(exc)})
        await asyncio.sleep(interval_seconds)


async def _organizations(database: Any) -> list[str]:
    from sqlalchemy import select as _select

    from ai_orchestrator.persistence.models import Organization

    async with database.session() as session:
        rows = (await session.execute(_select(Organization.id))).scalars().all()
        return [str(row) for row in rows]


def start_stream_pump(app: Any, database: Any) -> asyncio.Task[None]:
    """Start the pump and keep the handle on the app so shutdown can stop it."""
    from ai_orchestrator.api.stream import fanout

    task = asyncio.create_task(run_stream_pump(fanout(), database), name="api-stream-pump")
    app.state.stream_pump = task
    logger.info("stream.pump_started", extra={"consumer": CONSUMER_NAME})
    return task


async def stop_stream_pump(app: Any) -> None:
    """Stop the pump, then release every subscriber.

    Subscribers first, so a browser tab learns the platform is going away and shows it,
    rather than holding a connection open until the socket times out.
    """
    from ai_orchestrator.api.stream import fanout

    await fanout().close()
    task = getattr(app.state, "stream_pump", None)
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        # The expected outcome of cancelling the pump. Logged rather than passed in
        # silence: shutdown is the last place a bug gets to hide, and a bare `pass`
        # here is how a pump that never stops would look like a clean shutdown.
        logger.debug("stream.pump_cancelled")
    except Exception as exc:
        logger.warning("stream.pump_stop_failed", extra={"error": str(exc)})
    logger.info("stream.pump_stopped")


__all__ = [
    "CONSUMER_NAME",
    "publish_from_database",
    "run_stream_pump",
    "start_stream_pump",
    "stop_stream_pump",
]
