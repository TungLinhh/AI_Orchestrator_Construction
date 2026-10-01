"""NATS JetStream transport.

The event bus is a delivery mechanism, not a system of record. Three properties
follow from that and shape this module:

  * durable consumers. A projection that misses an event is permanently wrong,
    so consumers are JetStream durable with explicit ack, and a failure means
    nack with redelivery rather than a dropped message.
  * at-least-once delivery, exactly-once effect. The bus redelivers by design.
    Deduplication is the consumer's job, keyed on the CloudEvent id, and it is
    implemented in PostgreSQL rather than in memory so it survives a restart.
  * a poisoned message must not block the queue. A message that fails
    repeatedly is moved to a dead-letter subject with its reason attached, rather
    than redelivered forever or silently dropped.

Subjects follow `ao.<org_id>.<event-type>`, so a consumer can bind to one
tenant with a wildcard and never see another's traffic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import nats
from nats.aio.msg import Msg
from nats.errors import TimeoutError as NatsTimeout
from nats.js import JetStreamContext
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import BadRequestError
from nats.js.errors import NotFoundError as JsNotFoundError

from ai_orchestrator.domain.errors import DependencyFailure
from ai_orchestrator.events.envelope import (
    STREAM_EVENTS,
    CloudEvent,
    subject_for,
)
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Redelivery attempts before a message is treated as poisoned. Five is enough to
#: ride out a dependency restart and short enough that a genuinely broken handler
#: does not hold the queue for minutes.
MAX_DELIVERIES = 5

#: Dead-letter subject. Separate from the stream so a failed message is
#: inspectable without being redelivered.
DEAD_LETTER_SUBJECT = "ao.deadletter"

#: Messages requested per fetch. Bounded so a burst does not pin a large batch
#: of unacked messages while a slow handler works through it.
FETCH_BATCH = 20

#: How long one fetch waits before returning empty. Long enough that an idle
#: consumer is not spinning, short enough that `stop()` is responsive.
FETCH_TIMEOUT_S = 1.0


class NatsTransport:
    """Owns the connection and the stream."""

    def __init__(
        self,
        *,
        url: str = "nats://127.0.0.1:4222",
        stream_name: str = STREAM_EVENTS,
        max_age_hours: int = 72,
        max_msgs: int = 1_000_000,
    ) -> None:
        self._url = url
        self._stream_name = stream_name
        self._max_age_hours = max_age_hours
        self._max_msgs = max_msgs
        self._nc: Any = None
        self._js: JetStreamContext | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, *, timeout_s: float = 10.0) -> None:
        """Connect and ensure the stream exists.

        A missing event bus is a degraded control plane, not a fatal one: task
        creation still works and the outbox accumulates, so the events publish
        when the bus returns. Failing startup outright would turn a broker outage
        into a full outage.
        """
        if self._connected:
            return
        try:
            # The *initial* connection is bounded; reconnection is not.
            #
            # `max_reconnect_attempts=-1` applies from the first attempt, so
            # pointing this at a port with nothing on it does not fail — it
            # retries every second forever. That turns "the broker is down" into
            # a hang at startup, which is the opposite of the degradation this
            # method exists to provide. `wait_for` gives the first attempt a
            # deadline; once connected, the client keeps retrying in the
            # background, which is the behaviour we want.
            self._nc = await asyncio.wait_for(
                nats.connect(
                    servers=[self._url],
                    connect_timeout=timeout_s,
                    max_reconnect_attempts=-1,  # keep trying; the bus usually returns
                    reconnect_time_wait=1,
                    name="ai-orchestrator",
                ),
                timeout=timeout_s,
            )
        except (TimeoutError, Exception) as exc:
            logger.warning("nats.unavailable", url=self._url, error=type(exc).__name__)
            self._connected = False
            return

        self._js = self._nc.jetstream()
        await self._ensure_stream()
        self._connected = True
        logger.info("nats.connected", url=self._url, stream=self._stream_name)

    async def _ensure_stream(self) -> None:
        """Make sure the event stream exists, and prove it does.

        `ao.deadletter` is deliberately *not* listed as a second subject. It
        already matches `ao.>`, and a subject list that overlaps itself is
        rejected by the server (`subject "ao.>" overlaps with "ao.deadletter"`).
        The dead letter is inside the stream for free, which is where a
        quarantined message belongs: durable and inspectable.
        """
        assert self._js is not None
        try:
            await self._js.find_stream_name_by_subject("ao.>")
            return
        except JsNotFoundError, BadRequestError:
            pass
        try:
            await self._js.add_stream(
                StreamConfig(
                    name=self._stream_name,
                    subjects=["ao.>"],
                    retention=RetentionPolicy.LIMITS,
                    storage=StorageType.FILE,
                    max_age=self._max_age_hours * 3600,
                    max_msgs=self._max_msgs,
                    # Replicas 1: this is a single-node development stack. A
                    # production deployment sets 3 and accepts the cost.
                    num_replicas=1,
                )
            )
            logger.info("nats.stream_created", stream=self._stream_name)
        except Exception as exc:
            # A concurrent starter may have created it, so a failure here is not
            # necessarily fatal — which is why this verifies rather than raising.
            logger.info("nats.stream_ensure", stream=self._stream_name, note=type(exc).__name__)

        # Verified, not assumed. Reporting "connected" with no stream behind it
        # is the worst outcome available: every consumer then fails to subscribe
        # with a `NotFoundError` that points at the consumer rather than the
        # cause, and the outbox grows without anyone noticing. A health check
        # that reports healthy while broken is worse than no health check.
        try:
            found = await self._js.find_stream_name_by_subject("ao.>")
        except Exception as exc:
            msg = f"nats stream {self._stream_name!r} is missing after create: {exc}"
            raise DependencyFailure(msg, details={"stream": self._stream_name}) from exc
        if found != self._stream_name:
            msg = (
                f"subjects ao.> are served by stream {found!r}, not "
                f"{self._stream_name!r}; publishing would go to the wrong stream"
            )
            raise DependencyFailure(msg, details={"stream": self._stream_name})

    async def publish(self, event: CloudEvent) -> None:
        """Publish one event.

        Raises when the bus is unavailable. The caller is the outbox relay, which
        leaves the row unpublished so it is retried — the alternative is a task
        whose state changed and whose event was lost.
        """
        if not self._connected or self._js is None:
            msg = "nats is not connected"
            raise ConnectionError(msg)
        subject = (
            event.subject
            if event.subject.startswith("ao.")
            else subject_for(event.organization_id, event.type)
        )
        try:
            await self._js.publish(subject, event.to_json().encode("utf-8"))
        except Exception as exc:
            msg = f"nats publish failed on {subject}: {type(exc).__name__}"
            raise ConnectionError(msg) from exc

    async def close(self) -> None:
        if self._nc is not None:
            # Closing must not raise: it runs from a shutdown path that has
            # already decided to stop, and a failure here has nothing left to
            # report to.
            with contextlib.suppress(Exception):
                await self._nc.close()
        self._connected = False
        self._js = None
        self._nc = None


@dataclass(slots=True)
class HandlerResult:
    """What a consumer handler decided."""

    ok: bool
    #: When false, the message is retried.
    retryable: bool = True
    reason: str = ""
    #: Set when the handler determined the message was already applied.
    duplicate: bool = False


@dataclass(slots=True)
class ConsumerStats:
    received: int = 0
    processed: int = 0
    duplicates: int = 0
    failed: int = 0
    dead_lettered: int = 0
    last_error: str | None = None


ConsumerHandler = Callable[[CloudEvent, "EventContext"], Awaitable[HandlerResult]]


@dataclass(slots=True)
class EventContext:
    """Metadata a handler may need and must not be trusted for."""

    subject: str
    sequence: int
    delivery_count: int
    redelivered: bool
    raw: Msg | None = None


class DurableConsumer:
    """A JetStream durable consumer with dedup and a dead-letter path."""

    def __init__(
        self,
        transport: NatsTransport,
        *,
        name: str,
        handler: ConsumerHandler,
        filter_subject: str = "ao.>",
        dedup: Any = None,
        max_deliveries: int = MAX_DELIVERIES,
    ) -> None:
        self._transport = transport
        self._name = name
        self._handler = handler
        self._filter_subject = filter_subject
        self._dedup = dedup
        self._max_deliveries = max_deliveries
        self._sub: Any = None
        self._task: asyncio.Task[Any] | None = None
        self.stats = ConsumerStats()
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if not self._transport.connected:
            logger.warning("consumer.not_started", consumer=self._name, reason="nats not connected")
            return
        js = self._transport._nc.jetstream()
        try:
            self._sub = await js.pull_subscribe(
                self._filter_subject,
                durable=self._name,
                stream=STREAM_EVENTS,
                config=ConsumerConfig(
                    durable_name=self._name,
                    # Ack explicitly. An auto-acked message is lost if the process
                    # dies between receiving and processing it.
                    ack_policy=AckPolicy.EXPLICIT,
                    max_deliver=self._max_deliveries,
                    # Long enough for a legitimate slow handler, short enough to
                    # reclaim from a worker that vanished.
                    ack_wait=60,
                    max_ack_pending=256,
                ),
            )
        except Exception as exc:
            logger.warning(
                "consumer.subscribe_failed", consumer=self._name, error=type(exc).__name__
            )
            return

        self._task = asyncio.create_task(self._run(), name=f"consumer-{self._name}")
        logger.info("consumer.started", consumer=self._name, subject=self._filter_subject)

    async def _run(self) -> None:
        """Fetch and handle until stopped.

        `fetch()`, not an async iterator over `sub.messages`. A *pull* consumer
        is the right kind here — it is the only kind that can be given an ack
        deadline and a delivery count, and a pull consumer is driven by fetching,
        not by iteration. `PullSubscription` has no `messages` attribute, so the
        iterator form raised `AttributeError` on the first pass and the consumer
        silently consumed nothing while looking perfectly healthy.
        """
        try:
            while not self._stopping.is_set():
                try:
                    messages = await self._sub.fetch(FETCH_BATCH, timeout=FETCH_TIMEOUT_S)
                except asyncio.CancelledError, NatsTimeout:
                    # No messages in this window is the normal idle case, not an
                    # error; the loop simply asks again.
                    if self._stopping.is_set():
                        return
                    continue
                for msg in messages:
                    if self._stopping.is_set():
                        return
                    await self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The consumer loop must not die silently. A dead consumer is a
            # projection that quietly stops updating.
            logger.error("consumer.loop_failed", consumer=self._name, error=str(exc))
            self.stats.last_error = str(exc)

    async def _handle(self, msg: Msg) -> None:
        self.stats.received += 1
        try:
            event = CloudEvent.from_json(msg.data)
        except Exception as exc:
            # Unparseable is never going to parse. Dead-letter immediately rather
            # than redelivering a message that will fail identically forever.
            await self._dead_letter(msg, f"unparseable envelope: {type(exc).__name__}")
            await msg.term()
            return

        context = EventContext(
            subject=msg.subject,
            sequence=int(msg.metadata.sequence.stream) if msg.metadata else 0,
            delivery_count=int(msg.metadata.num_delivered) if msg.metadata else 1,
            redelivered=bool(msg.metadata.num_delivered > 1) if msg.metadata else False,
            raw=msg,
        )

        # Deduplication before the handler. The bus redelivers by design, so this
        # is what turns at-least-once delivery into an exactly-once effect.
        if self._dedup is not None:
            try:
                already = await self._dedup.seen(event.id, event.organization_id)
            except Exception as exc:
                logger.warning("dedup.check_failed", consumer=self._name, error=str(exc))
                already = False
            if already:
                self.stats.duplicates += 1
                await msg.ack()
                return

        try:
            result = await self._handler(event, context)
        except Exception as exc:
            result = HandlerResult(ok=False, retryable=True, reason=f"{type(exc).__name__}: {exc}")

        if result.ok:
            self.stats.processed += 1
            if self._dedup is not None and not result.duplicate:
                try:
                    await self._dedup.record(event.id, event.type, event.organization_id)
                except Exception as exc:
                    # The side effect happened but the dedup record did not, so a
                    # redelivery would repeat it. Surface it: this is the one
                    # failure that can duplicate a side effect, and it must be
                    # visible rather than logged and forgotten.
                    logger.error(
                        "dedup.record_failed",
                        consumer=self._name,
                        event_id=event.id,
                        error=str(exc),
                    )
            await msg.ack()
            return

        self.stats.failed += 1
        self.stats.last_error = result.reason
        if not result.retryable or context.delivery_count >= self._max_deliveries:
            await self._dead_letter(msg, f"{result.reason} (deliveries={context.delivery_count})")
            await msg.term()
            return
        # nak with a delay so a dependency that is down does not spin the queue.
        await msg.nak(delay=5)

    async def _dead_letter(self, msg: Msg, reason: str) -> None:
        self.stats.dead_lettered += 1
        logger.error(
            "consumer.dead_letter", consumer=self._name, subject=msg.subject, reason=reason
        )
        if not self._transport.connected:
            return
        try:
            envelope = {
                "original_subject": msg.subject,
                "reason": reason,
                "payload": msg.data.decode("utf-8", errors="replace")[:10_000],
            }
            await self._transport._nc.publish(
                DEAD_LETTER_SUBJECT, json.dumps(envelope).encode("utf-8")
            )
        except Exception as exc:
            logger.error("consumer.dead_letter_publish_failed", error=str(exc))

    async def stop(self) -> None:
        self._stopping.set()
        if self._sub is not None:
            with contextlib.suppress(Exception):
                await self._sub.unsubscribe()
        if self._task is not None:
            self._task.cancel()
            # `CancelledError` is a `BaseException`, so suppressing `Exception`
            # alone would let a cancelled task escape as an error.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None
        self._sub = None


class PostgresDeduplicator:
    """Event-id dedup, persisted in the tenant's own database.

    In-memory dedup would be lost on restart, and a consumer that restarts with
    an empty set re-applies every event it had already handled. A restart during
    an incident is exactly when that matters most.

    The tenant comes from the event envelope on every call. A JetStream consumer
    serves every tenant from one process, so there is no ambient tenant to read —
    and guessing one would be exactly the bug this class is here to avoid: the
    dedup row would be written under the wrong `organization_id`, and the
    uniqueness check would then miss the very redelivery it exists to catch.

    Each call opens its own tenant-bound session. That means the dedup record is
    *not* in the same transaction as the side effect. The alternative — sharing
    the handler's session — is only available to a consumer that owns the
    session, and this class does not. The ordering below is the mitigation: the
    record is written only after the handler succeeded, so a crash in between
    repeats the effect rather than skipping it. Repeating is recoverable;
    skipping is not.
    """

    def __init__(self, database: Any, consumer_name: str) -> None:
        self._db = database
        self._consumer = consumer_name

    async def seen(self, event_id: str, organization_id: str) -> bool:
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import IdempotencyRecord

        async with self._db.tenant_session(organization_id) as session:
            result = await session.execute(
                select(IdempotencyRecord.id).where(
                    IdempotencyRecord.idempotency_key == event_id,
                    IdempotencyRecord.operation == f"event:{self._consumer}",
                )
            )
            return result.scalar_one_or_none() is not None

    async def record(self, event_id: str, event_type: str, organization_id: str) -> None:
        """Persist the claim. The tenant is set explicitly, not left to default.

        `organization_id` is the column the row-level-security policy tests, so an
        INSERT that omits it writes NULL and is refused by `WITH CHECK` — the
        isolation working, not a nuisance to route around.
        """
        from sqlalchemy import text

        # ON CONFLICT DO NOTHING: two concurrent deliveries of the same event are
        # an expected race, not an error.
        # `tenant_session` already opens and commits the transaction — it has to,
        # because `SET LOCAL` is transaction-scoped. A second `session.begin()`
        # here raises "a transaction is already begun".
        async with self._db.tenant_session(organization_id) as session:
            await session.execute(
                text(
                    """
                    INSERT INTO idempotency_records
                        (id, organization_id, idempotency_key, operation,
                         request_hash, resource_id)
                    VALUES (gen_random_uuid()::text, :org, :key, :op, '', :event_type)
                    ON CONFLICT (idempotency_key, operation) DO NOTHING
                    """
                ),
                {
                    "org": organization_id,
                    "key": event_id,
                    "op": f"event:{self._consumer}",
                    "event_type": event_type,
                },
            )


__all__ = [
    "DEAD_LETTER_SUBJECT",
    "MAX_DELIVERIES",
    "ConsumerHandler",
    "ConsumerStats",
    "DurableConsumer",
    "EventContext",
    "HandlerResult",
    "NatsTransport",
    "PostgresDeduplicator",
]
