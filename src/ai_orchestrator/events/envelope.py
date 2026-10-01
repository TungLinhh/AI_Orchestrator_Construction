"""CloudEvents envelope and NATS subject naming.

Two contracts that must never drift, because consumers on both sides depend on
them:

the envelope shape
    CloudEvents 1.0. Anything that can read CloudEvents can read our events
    without a schema negotiation step, and a producer that adds a field cannot
    break a consumer that ignores it.

the subject convention
    `ao.<org_id>.<aggregate>.<event-type>`. The organisation id is in the
    subject so a consumer can subscribe to one tenant with a wildcard without
    filtering every message, and so two tenants can never write to each other's
    stream partition by accident.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Root of every subject. Namespaced so the platform's traffic is identifiable
#: on a shared NATS cluster.
SUBJECT_ROOT = "ao"

#: Streams.
STREAM_EVENTS = "AO_EVENTS"
#: Durable consumers.
CONSUMER_PROJECTION = "ao-projection"
CONSUMER_WORKFLOW_TRIGGER = "ao-workflow-trigger"
CONSUMER_AUDIT = "ao-audit"
CONSUMER_METRICS = "ao-metrics"

#: Event schema version. Bump when a field changes meaning, never when one is
#: added: adding a field is backward compatible, changing one is not.
EVENT_SCHEMA_VERSION = 1


class CloudEvent(BaseModel):
    """A CloudEvents 1.0 envelope.

    `id` is the deduplication key. A consumer that sees the same id twice must
    treat the second delivery as a no-op, which is what makes at-least-once
    delivery safe to build on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    specversion: str = "1.0"
    id: str
    type: str
    source: str
    subject: str
    time: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    # Extension attribute. Standard CloudEvents has no tenant concept, and the
    # platform cannot put a vendor concept in `subject` without breaking the
    # convention other consumers rely on.
    organization_id: str
    trace_id: str | None = None
    schema_version: int = EVENT_SCHEMA_VERSION
    actor_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str | bytes) -> CloudEvent:
        return cls.model_validate_json(raw)


def subject_for(organization_id: str, event_type: str) -> str:
    """`ao.<org_id>.<event-type>`.

    The event type already contains the aggregate as its first segment
    (`task.created`), so there is no separate aggregate segment to add.
    """
    safe_type = event_type.replace(".", ".")
    return f"{SUBJECT_ROOT}.{organization_id}.{safe_type}"


def task_subject(organization_id: str, task_id: str) -> str:
    """Per-entity subject, for consumers that follow one task.

    Used by the UI projection and the debug timeline, which both need every event
    for one task and would otherwise have to filter the whole stream.
    """
    return f"{SUBJECT_ROOT}.{organization_id}.task.{task_id}"


def agent_subject(organization_id: str, agent_id: str) -> str:
    return f"{SUBJECT_ROOT}.{organization_id}.agent.{agent_id}"


def parse_subject(subject: str) -> tuple[str | None, str]:
    """Split a subject into (organization_id, remainder).

    Returns `(None, subject)` when the subject does not follow the convention, so
    a malformed subject is ignored rather than crashing a consumer.
    """
    parts = subject.split(".")
    if len(parts) < 3 or parts[0] != SUBJECT_ROOT:
        return None, subject
    return parts[1], ".".join(parts[2:])


@dataclass(slots=True)
class EventEnvelope:
    """What a consumer receives, with the JetStream metadata attached."""

    event: CloudEvent
    subject: str
    sequence: int | None = None
    stream: str | None = None
    consumer: str | None = None
    redelivered: bool = False
    # Set by the consumer to skip persisting the side effect. The message is still
    # acked, because a duplicate is not a failure.
    duplicate: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class DeadLetter:
    """A record of an event that could not be processed.

    Poison messages are not dropped and are not retried forever. They are moved
    somewhere a human will look, with the reason, because the alternative is a
    silently degraded pipeline that nobody notices for weeks.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason


__all__ = [
    "CONSUMER_AUDIT",
    "CONSUMER_METRICS",
    "CONSUMER_PROJECTION",
    "CONSUMER_WORKFLOW_TRIGGER",
    "EVENT_SCHEMA_VERSION",
    "STREAM_EVENTS",
    "SUBJECT_ROOT",
    "CloudEvent",
    "DeadLetter",
    "EventEnvelope",
    "agent_subject",
    "parse_subject",
    "subject_for",
    "task_subject",
]
