"""Event pipeline, end to end: database outbox -> NATS -> durable consumer.

Acceptance scenario 6 is the reason this file exists: the same event delivered
three times must produce one logical effect. The test delivers it three times on
purpose, through a real JetStream consumer with a real PostgreSQL dedup table,
because a mock would only prove the mock deduplicates.

Also covered here:

  * a rolled-back task publishes nothing
  * the relay publishes what was committed, and marks it published
  * a failing handler redelivers, then dead-letters rather than spinning forever
  * the subject convention actually isolates tenants on the bus
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import EventType
from ai_orchestrator.events.bus import (
    DEAD_LETTER_SUBJECT,
    DurableConsumer,
    EventContext,
    HandlerResult,
    NatsTransport,
    PostgresDeduplicator,
)
from ai_orchestrator.events.envelope import (
    CloudEvent,
    parse_subject,
    subject_for,
)
from ai_orchestrator.events.relay import OutboxRelay
from ai_orchestrator.persistence.models import OutboxEvent
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import configure_logging

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

SUFFIX = "pytest"


@pytest_asyncio.fixture
async def transport() -> NatsTransport:
    configure_logging(get_settings())
    bus = NatsTransport()
    await bus.connect()
    if not bus.connected:
        pytest.skip("nats is not running; start it with `make dev-nats`")
    yield bus
    await bus.close()


class TestSubjectConvention:
    def test_org_is_in_the_subject(self) -> None:
        """A consumer binds to one tenant with a wildcard and never sees
        another's traffic."""
        subject = subject_for("org_123", EventType.TASK_CREATED.value)
        assert subject == "ao.org_123.task.created"
        org, rest = parse_subject(subject)
        assert org == "org_123"
        assert rest == "task.created"

    def test_malformed_subject_is_reported_not_raised(self) -> None:
        org, rest = parse_subject("something.else")
        assert org is None
        assert rest == "something.else"

    def test_two_orgs_never_collide(self) -> None:
        a = subject_for("org_a", "task.created")
        b = subject_for("org_b", "task.created")
        assert a != b

    def test_envelope_round_trips(self) -> None:
        event = CloudEvent(
            id="evt_1",
            type="task.created",
            source="control-plane/control-plane",
            subject="tsk_1",
            organization_id="org_1",
            data={"a": 1},
        )
        restored = CloudEvent.from_json(event.to_json())
        assert restored.id == event.id
        assert restored.data == {"a": 1}
        assert restored.specversion == "1.0"


class TestOutboxWritesWithTheStateChange:
    async def test_task_creation_enqueues_an_event(self, tenant) -> None:
        repo = TaskRepository(tenant.session, tenant.organization_id)
        task = await repo.create(title="Enqueue me", goal="enqueue me please")
        await tenant.session.flush()
        count = await tenant.session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(
                OutboxEvent.organization_id == tenant.organization_id,
                OutboxEvent.subject == task.id,
            )
        )
        assert count == 1

    async def test_rollback_leaves_no_event(self, tenant) -> None:
        """The event is in the same transaction as the state change, so an
        uncommitted task announces nothing."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        before = await tenant.session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.organization_id == tenant.organization_id)
        )
        savepoint = await tenant.session.begin_nested()
        await repo.create(title="Doomed", goal="doomed task")
        await tenant.session.flush()
        await savepoint.rollback()
        after = await tenant.session.scalar(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.organization_id == tenant.organization_id)
        )
        assert after == before


class TestRelayAndBus:
    async def test_relay_publishes_and_marks_published(
        self, tenant, db: Database, transport: NatsTransport
    ) -> None:
        """A committed state change reaches the bus and stops being pending."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="Publish me", goal="publish this to the bus")
        await tenant.commit()

        received: list[CloudEvent] = []

        async def handler(event: CloudEvent, _ctx: EventContext) -> HandlerResult:
            received.append(event)
            return HandlerResult(ok=True)

        consumer = DurableConsumer(
            transport,
            name=f"{SUFFIX}-relay-{tenant.organization_id[-6:]}",
            handler=handler,
            filter_subject=f"ao.{tenant.organization_id}.>",
        )
        await consumer.start()
        try:
            relay = OutboxRelay(db, transport)
            published = await relay.run_once()
            assert published >= 1

            for _ in range(50):
                if received:
                    break
                await asyncio.sleep(0.1)
            assert received, "the consumer received nothing"
            assert any(r.type == EventType.TASK_CREATED.value for r in received)

            assert await relay.pending_count(tenant.organization_id) == 0, (
                "this tenant's events were published and delivered but the outbox "
                "still holds one as pending"
            )
        finally:
            await consumer.stop()

    async def test_duplicate_delivery_is_processed_once(
        self, tenant, db: Database, transport: NatsTransport
    ) -> None:
        """Acceptance scenario 6.

        The same event is published three times. A real JetStream consumer
        receives all three. The dedup table is what makes the effect happen once:
        the bus guarantees at-least-once, and the consumer is responsible for
        turning that into exactly-once.
        """
        processed: list[str] = []
        gate = asyncio.Event()

        async def handler(event: CloudEvent, _ctx: EventContext) -> HandlerResult:
            # The dedup check runs before the handler, so reaching the handler
            # more than once means deduplication failed.
            processed.append(event.id)
            if not gate.is_set():
                gate.set()
            return HandlerResult(ok=True)

        name = f"{SUFFIX}-dedup-{tenant.organization_id[-6:]}"
        consumer = DurableConsumer(
            transport,
            name=name,
            handler=handler,
            filter_subject=f"ao.{tenant.organization_id}.dedup-test.>",
            # The real store, not a stand-in. It opens its own tenant-bound
            # session per call: a JetStream consumer serves every tenant from one
            # process, so the envelope is the only authority on which
            # organisation a dedup row belongs to. Injecting a private attribute
            # to replace it would have hidden exactly the bug this test exists to
            # catch.
            dedup=PostgresDeduplicator(db, name),
        )
        await consumer.start()

        try:
            event = CloudEvent(
                id=f"evt_dup_{tenant.organization_id[-8:]}",
                type="dedup-test.created",
                source="control-plane/control-plane",
                subject=subject_for(tenant.organization_id, "dedup-test.created"),
                organization_id=tenant.organization_id,
                data={"x": 1},
            )
            for _ in range(3):
                await transport.publish(event)

            for _ in range(60):
                if processed:
                    break
                await asyncio.sleep(0.1)
            # Give any duplicate a chance to arrive before asserting.
            await asyncio.sleep(0.5)

            assert processed == [event.id], f"expected one logical effect, got {len(processed)}"
            assert consumer.stats.duplicates >= 2, (
                f"expected the duplicates to be recognised, stats={consumer.stats}"
            )
        finally:
            await consumer.stop()

    async def test_failing_handler_dead_letters(self, tenant, transport: NatsTransport) -> None:
        """A handler that always fails must not spin the queue forever."""
        attempts: list[str] = []

        async def handler(event: CloudEvent, _ctx: EventContext) -> HandlerResult:
            attempts.append(event.id)
            return HandlerResult(ok=False, retryable=True, reason="dependency down")

        dead_letters: list[dict] = []
        bus = transport._nc

        async def watch_dead_letter(msg) -> None:
            dead_letters.append(json.loads(msg.data))

        sub = await bus.subscribe(DEAD_LETTER_SUBJECT, cb=watch_dead_letter)

        consumer = DurableConsumer(
            transport,
            name=f"{SUFFIX}-dlq-{tenant.organization_id[-6:]}",
            handler=handler,
            filter_subject=f"ao.{tenant.organization_id}.dlq-test.>",
            max_deliveries=2,
        )
        await consumer.start()
        try:
            await transport.publish(
                CloudEvent(
                    id=f"evt_dlq_{tenant.organization_id[-8:]}",
                    type="dlq-test.failed",
                    source="control-plane/control-plane",
                    subject=subject_for(tenant.organization_id, "dlq-test.failed"),
                    organization_id=tenant.organization_id,
                    data={},
                )
            )
            for _ in range(80):
                if dead_letters:
                    break
                await asyncio.sleep(0.1)

            assert dead_letters, "the poisoned message was never quarantined"
            assert "dependency down" in dead_letters[0]["reason"]
            assert dead_letters[0]["original_subject"].endswith("dlq-test.failed")
        finally:
            await consumer.stop()
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    async def test_non_retryable_failure_terminates_immediately(
        self, tenant, transport: NatsTransport
    ) -> None:
        """A validation failure will never succeed on retry, so redelivering it
        is pure waste."""

        async def handler(_event: CloudEvent, _ctx: EventContext) -> HandlerResult:
            return HandlerResult(ok=False, retryable=False, reason="malformed payload")

        consumer = DurableConsumer(
            transport,
            name=f"{SUFFIX}-terminal-{tenant.organization_id[-6:]}",
            handler=handler,
            filter_subject=f"ao.{tenant.organization_id}.terminal-test.>",
            max_deliveries=10,
        )
        await consumer.start()
        try:
            await transport.publish(
                CloudEvent(
                    id=f"evt_terminal_{tenant.organization_id[-8:]}",
                    type="terminal-test.created",
                    source="control-plane/control-plane",
                    subject=subject_for(tenant.organization_id, "terminal-test.created"),
                    organization_id=tenant.organization_id,
                    data={},
                )
            )
            for _ in range(40):
                if consumer.stats.dead_lettered:
                    break
                await asyncio.sleep(0.1)
            assert consumer.stats.dead_lettered == 1
            assert consumer.stats.received == 1, "a terminal failure was redelivered"
        finally:
            await consumer.stop()


class TestRelayResilience:
    async def test_relay_keeps_rows_when_the_bus_is_down(self, tenant, db: Database) -> None:
        """State changed and the bus is unreachable. The row stays pending, so the
        event is delivered when the bus returns rather than lost."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="Offline", goal="offline event")
        await tenant.commit()

        offline = NatsTransport(url="nats://127.0.0.1:4299", stream_name="AO_EVENTS_TEST")
        await offline.connect(timeout_s=1.0)
        assert not offline.connected

        relay = OutboxRelay(db, offline)
        assert await relay.run_once() == 0
        assert await relay.pending_count(tenant.organization_id) >= 1, (
            "an undelivered event was dropped"
        )

        health = await relay.health()
        assert health["connected"] is False
        assert health["pending"] >= 1
        await offline.close()

    async def test_quarantine_after_repeated_failure(
        self, tenant, db: Database, transport: NatsTransport
    ) -> None:
        """A relay that retries one poisoned row forever stops relaying
        everything behind it."""
        repo = TaskRepository(tenant.session, tenant.organization_id)
        await repo.create(title="Poison", goal="poison the relay")
        await tenant.commit()

        class _AlwaysFails:
            connected = True

            async def publish(self, _event) -> None:
                msg = "broker refused the message"
                raise ConnectionError(msg)

        relay = OutboxRelay(db, _AlwaysFails(), max_attempts=3)  # type: ignore[arg-type]
        for _ in range(4):
            await relay.run_once()

        health = await relay.health()
        assert health["quarantined"] >= 1, "the poisoned row was never quarantined"
        # Scoped to this tenant, not the whole database. The relay drains every
        # tenant, so `health()` is a platform-wide number and asserting it is zero
        # would only pass on an empty database — a test that measures something
        # other than what it claims.
        assert await relay.pending_count(tenant.organization_id) == 0, (
            "this tenant still has unpublished events after quarantine"
        )
        assert relay.stats.quarantined >= 1
