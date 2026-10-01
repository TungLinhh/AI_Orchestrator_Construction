"""Outbox relay.

The pattern that makes "state changed" and "event published" one atomic thing,
which they cannot be without it:

    BEGIN
      update task
      insert outbox_event
    COMMIT
      -- then, separately
      relay publishes to NATS

Publishing inside the business transaction would hold a row lock across a
network call and would emit events for transactions that then roll back. Skipping
the outbox would leave committed state nobody knows about. This is the third
option.

Two properties the implementation depends on:

  * `FOR UPDATE SKIP LOCKED` when claiming rows, so several relay instances can
    run without any of them publishing the same row twice.
  * a dead-letter after a bounded number of attempts. A relay that retries
    forever on one poisoned row stops relaying everything behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, select, update

from ai_orchestrator.events.bus import NatsTransport
from ai_orchestrator.events.envelope import CloudEvent
from ai_orchestrator.persistence.models import OutboxEvent
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Attempts before an event is quarantined. Retrying a malformed payload forever
#: blocks every event queued behind it.
MAX_PUBLISH_ATTEMPTS = 5

#: Rows claimed per pass. Bounded so one relay cannot hold a huge transaction
#: open across the network.
BATCH_SIZE = 50


@dataclass(slots=True)
class RelayStats:
    published: int = 0
    failed: int = 0
    quarantined: int = 0
    cycles: int = 0
    last_error: str | None = None


@dataclass(slots=True)
class PendingEvent:
    id: str
    event_type: str
    subject: str
    nats_subject: str
    payload: dict[str, Any]
    attempts: int


class OutboxRelay:
    """Publishes committed outbox rows to NATS."""

    def __init__(
        self,
        database: Any,
        transport: NatsTransport,
        *,
        batch_size: int = BATCH_SIZE,
        max_attempts: int = MAX_PUBLISH_ATTEMPTS,
    ) -> None:
        # A `Database`, not a bare session factory: the relay has to open a
        # tenant-bound session per organisation, and that binding is the
        # database's job, not the relay's.
        self._db = database
        self._transport = transport
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self.stats = RelayStats()
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        # Round-robin cursor, so one tenant cannot monopolise the loop.
        self._cursor = 0

    async def run_once(self) -> int:
        """Claim a batch, publish it, mark it. Returns how many were published."""
        if not self._transport.connected:
            self.stats.last_error = "nats is not connected"
            return 0

        tenants = await self._tenants()
        if not tenants:
            return 0

        # Start where the last pass stopped. Without this, one tenant with a
        # permanent backlog is drained first on every pass and the rest wait.
        self._cursor = (self._cursor + 1) % len(tenants)
        ordered = tenants[self._cursor :] + tenants[: self._cursor]

        total = 0
        for organization_id in ordered:
            total += await self._drain_tenant(organization_id)
        self.stats.cycles += 1
        return total

    async def _tenants(self) -> list[str]:
        """Every organisation that exists.

        `organizations` is the one table without row-level security, by design:
        it is the tenant root, and a platform process needs to be able to
        enumerate tenants. Nothing else is readable without a binding.
        """
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Organization

        async with self._db.session() as session:
            result = await session.execute(select(Organization.id).order_by(Organization.id))
            return [row[0] for row in result.all()]

    async def _drain_tenant(self, organization_id: str) -> int:
        """Publish one tenant's pending events. Never crosses a tenant boundary."""
        claimed = await self._claim_batch(organization_id)
        if not claimed:
            return 0

        published_ids: list[str] = []
        for event in claimed:
            try:
                await self._transport.publish(_as_cloud_event(event.payload))
            except Exception as exc:
                self.stats.failed += 1
                self.stats.last_error = str(exc)
                await self._mark_failed(organization_id, event, str(exc))
                continue
            self.stats.published += 1
            published_ids.append(event.id)

        if published_ids:
            await self._mark_published(organization_id, published_ids)
        return len(published_ids)

    async def start(self, *, interval_s: float = 0.5) -> None:
        """Run the relay on a loop until stopped."""
        self._stopping.clear()

        async def _loop() -> None:
            while not self._stopping.is_set():
                try:
                    published = await self.run_once()
                except Exception as exc:
                    # A relay that dies stops every event the platform emits.
                    # Log and keep going; the rows stay unpublished.
                    self.stats.last_error = str(exc)
                    logger.error("outbox.cycle_failed", error=str(exc))
                    published = 0
                # A short sleep only when idle. Draining a backlog as fast as the
                # bus allows is the right behaviour, and a fixed sleep would make
                # recovery time proportional to backlog size.
                if published == 0:
                    # The timeout is the poll interval, not a failure.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=interval_s)

        self._task = asyncio.create_task(_loop(), name="outbox-relay")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _claim_batch(self, organization_id: str) -> list[PendingEvent]:
        """Claim unpublished rows with `FOR UPDATE SKIP LOCKED`.

        Two relays can then run against one table without coordinating: each gets
        a disjoint set of rows. Without SKIP LOCKED the second relay would block
        on the first's locks, and with a plain `FOR UPDATE` the second would
        re-publish rows the first had already sent.

        Written as a typed `select()` rather than `text()`. A raw text query
        discards the column types SQLAlchemy knows about, so `payload` (jsonb)
        arrives as a JSON *string* and deserialising it fails — the relay then
        marks every row failed for a reason that has nothing to do with the bus.
        """
        async with self._db.tenant_session(organization_id) as session:
            result = await session.execute(
                select(
                    OutboxEvent.id,
                    OutboxEvent.event_type,
                    OutboxEvent.subject,
                    OutboxEvent.nats_subject,
                    OutboxEvent.payload,
                    OutboxEvent.publish_attempts,
                )
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.dead_lettered_at.is_(None),
                )
                .order_by(OutboxEvent.created_at)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = result.all()
        return [
            PendingEvent(
                id=r.id,
                event_type=r.event_type,
                subject=r.subject,
                nats_subject=r.nats_subject,
                payload=r.payload,
                attempts=r.publish_attempts,
            )
            for r in rows
        ]

    async def _mark_published(self, organization_id: str, event_ids: Sequence[str]) -> None:
        async with self._db.tenant_session(organization_id) as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id.in_(list(event_ids)))
                .values(published_at=func.now(), publish_attempts=OutboxEvent.publish_attempts + 1)
            )

    async def _mark_failed(self, organization_id: str, event: PendingEvent, error: str) -> None:
        attempts = event.attempts + 1
        quarantined = attempts >= self._max_attempts
        if quarantined:
            self.stats.quarantined += 1
            logger.error(
                "outbox.quarantined",
                event_id=event.id,
                event_type=event.event_type,
                attempts=attempts,
                error=error[:200],
            )
        async with self._db.tenant_session(organization_id) as session:
            await session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == event.id)
                .values(
                    publish_attempts=attempts,
                    last_error=error[:2000],
                    **({"dead_lettered_at": func.now()} if quarantined else {}),
                )
            )

    async def pending_count(self, organization_id: str | None = None) -> int:
        """Pending events, for one tenant or across all of them.

        The all-tenant form sums per tenant rather than running one unbound
        query, because an unbound query sees nothing — that is the isolation
        working, and reporting "0 pending" from it would be a false all-clear on
        exactly the metric the runbook says to watch.
        """
        if organization_id is not None:
            async with self._db.tenant_session(organization_id) as session:
                return await self._pending_in(session)
        total = 0
        for org in await self._tenants():
            total += await self.pending_count(org)
        return total

    @staticmethod
    async def _pending_in(session: Any) -> int:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    and_(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                    )
                )
            )
            or 0
        )

    async def health(self) -> dict[str, object]:
        """Relay health, for the operations dashboard.

        A growing pending count is the signal that matters: it means state is
        changing and nobody downstream is hearing about it.
        """
        tenants = await self._tenants()
        pending = 0
        quarantined = 0
        oldest: Any = None
        for org in tenants:
            async with self._db.tenant_session(org) as session:
                pending += int(
                    await session.scalar(
                        select(func.count())
                        .select_from(OutboxEvent)
                        .where(OutboxEvent.published_at.is_(None))
                    )
                    or 0
                )
                quarantined += int(
                    await session.scalar(
                        select(func.count())
                        .select_from(OutboxEvent)
                        .where(OutboxEvent.dead_lettered_at.is_not(None))
                    )
                    or 0
                )
                candidate = await session.scalar(
                    select(func.min(OutboxEvent.created_at)).where(
                        OutboxEvent.published_at.is_(None)
                    )
                )
                if candidate is not None and (oldest is None or candidate < oldest):
                    oldest = candidate
        return {
            "pending": pending,
            "quarantined": quarantined,
            "published_total": self.stats.published,
            "failed_total": self.stats.failed,
            "oldest_pending_at": oldest.isoformat() if oldest else None,
            "connected": self._transport.connected,
        }


def _as_cloud_event(payload: dict[str, Any]) -> CloudEvent:
    return CloudEvent.model_validate(payload)


__all__ = ["MAX_PUBLISH_ATTEMPTS", "OutboxRelay", "PendingEvent", "RelayStats"]
