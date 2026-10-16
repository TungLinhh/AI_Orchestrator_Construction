"""The installed JetStream client can raise the built-in idle timeout too."""

import asyncio

import pytest
from nats.errors import TimeoutError as NatsTimeout

from ai_orchestrator.events.bus import DurableConsumer, HandlerResult, NatsTransport


@pytest.mark.parametrize("timeout_error", [TimeoutError, NatsTimeout])
async def test_idle_fetch_timeout_does_not_kill_consumer(timeout_error, monkeypatch):
    async def handler(event, context):
        return HandlerResult(ok=True)

    consumer = DurableConsumer(NatsTransport(), name="idle-contract", handler=handler)
    message = object()

    class Subscription:
        calls = 0

        async def fetch(self, batch, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise timeout_type("idle window")
            return [message]

    timeout_type = timeout_error
    subscription = Subscription()
    consumer._sub = subscription
    received = []

    async def handle(msg):
        received.append(msg)
        consumer._stopping.set()

    monkeypatch.setattr(consumer, "_handle", handle)
    await consumer._run()
    assert subscription.calls == 2 and received == [message]
    assert consumer.stats.last_error is None


async def test_fatal_consumer_error_has_type_even_when_message_is_empty():
    async def handler(event, context):
        return HandlerResult(ok=True)

    consumer = DurableConsumer(NatsTransport(), name="fatal-contract", handler=handler)

    class Subscription:
        async def fetch(self, *args, **kwargs):
            raise ValueError()

    consumer._sub = Subscription()
    await consumer._run()
    assert consumer.stats.last_error and "ValueError" in consumer.stats.last_error


async def test_cancelled_consumer_propagates_cancellation():
    async def handler(event, context):
        return HandlerResult(ok=True)

    consumer = DurableConsumer(NatsTransport(), name="cancel-contract", handler=handler)

    class Subscription:
        async def fetch(self, *args, **kwargs):
            raise asyncio.CancelledError()

    consumer._sub = Subscription()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(consumer._run(), timeout=1)
