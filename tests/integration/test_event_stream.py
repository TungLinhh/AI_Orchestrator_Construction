"""The live stream, and the two things it must never do.

**It must never be a source of truth.** Every frame is an `Event` row the platform
already wrote. If the stream invented state, the UI and the database would eventually
disagree, and there would be no way to tell which one was lying.

**It must never hand a browser an id where a person needed a name.** The first draft
of the view read `to_agent_name` and `child_task_id` from the event payload. Neither
field exists: a `delegation.accepted` event carries `parent_task_id`,
`source_agent_id`, `target_agent_id` and `depth`, and nothing else. So the first
version of this UI would have rendered two opaque ids per delegation — which is the
same class of mistake as reading an enum's spelling from memory, and the reason these
tests assert the *joined* result rather than the shape of the code.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.api.stream import (
    BACKFILL_LIMIT,
    EventFanout,
    backfill,
    enrich,
    frame_of,
)
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.persistence.models import Agent, Event, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

ORG_TITLE = "Board pack"
ORG_GOAL = "Prepare the quarterly board pack"


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _one_task(seeded, agent_name: str = "Executive Agent") -> tuple[str, str]:  # type: ignore[no-untyped-def]
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id, Agent.name == agent_name
            )
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    task = await tasks.create(
        title=ORG_TITLE, goal=ORG_GOAL, task_type="coordination", requester_type="human"
    )
    await tasks.assign(task.id, agent.id)
    await TaskExecutionService(
        seeded.session, seeded.organization_id, runtime=ScriptedRuntime()
    ).execute_task(task.id, agent_id=agent.id)
    await seeded.session.flush()
    return str(task.id), str(agent.id)


class TestTheStreamIsAProjection:
    async def test_every_frame_is_an_event_the_platform_already_wrote(self, seeded) -> None:
        """Not a shape check — an identity check.

        A frame that is not backed by a row is a frame the UI believes and the
        database does not, which is the one failure this whole design exists to make
        impossible.
        """
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        assert frames, "no frames for a task that ran"
        stored = {
            str(row)
            for row in (
                await seeded.session.execute(
                    select(Event.id).where(Event.organization_id == seeded.organization_id)
                )
            ).scalars()
        }
        for frame in frames:
            assert str(frame["id"]) in stored, f"frame {frame['id']} has no row in the events table"

    async def test_the_event_payload_is_passed_through_untouched(self, seeded) -> None:
        """Enrichment goes under `view`, never into `data`.

        Other consumers read `data`. Rewriting it for a browser would make the event
        log mean different things to different readers, and the log is the record.
        """
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        raw = {
            str(row.id): row.data
            for row in (
                await seeded.session.execute(
                    select(Event).where(Event.organization_id == seeded.organization_id)
                )
            ).scalars()
        }
        for frame in frames:
            assert frame["data"] == raw[str(frame["id"])], (
                f"{frame['type']}: the event payload was rewritten for display"
            )

    async def test_frames_come_back_oldest_first(self, seeded) -> None:
        """A sequence rendered newest-first on arrival looks like the platform is
        rewinding itself."""
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        times = [f["occurred_at"] for f in frames]
        assert times == sorted(times), "the replay ran backwards"


class TestTheViewBlockSaysWhoAndWhat:
    async def test_a_task_frame_carries_its_title(self, seeded) -> None:
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        created = [f for f in frames if f["type"] == "task.created"]
        assert created, "no task.created in the replay"
        assert created[0]["view"]["title"] == ORG_TITLE

    async def test_a_delegation_frame_resolves_both_agent_names(self, seeded) -> None:
        """The assertion the first UI would have failed.

        `delegation.accepted` carries `source_agent_id` and `target_agent_id` and no
        names. A view built from the event alone shows two opaque ids, and a
        delegation to Finance is indistinguishable from one to Legal.
        """
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        delegations = [f for f in frames if f["type"].startswith("delegation.")]
        if not delegations:
            pytest.skip("this run delegated nothing")
        view = delegations[0]["view"]
        assert view["from_agent"] and " " in view["from_agent"], (
            f"the delegating agent is not a name: {view.get('from_agent')!r}"
        )
        assert view["to_agent"] and " " in view["to_agent"], (
            f"the delegated-to agent is not a name: {view.get('to_agent')!r}"
        )
        assert not view["from_agent"].startswith("agt_"), "an id was passed off as a name"
        assert not view["to_agent"].startswith("agt_"), "an id was passed off as a name"

    async def test_the_parent_task_is_identified_even_though_the_subject_is_not_it(
        self, seeded
    ) -> None:
        """A delegation's subject is the *delegation*. Reading the task off the
        subject is the obvious implementation and it is wrong — it would attach every
        delegation to a node named after a delegation id."""
        await _one_task(seeded)
        frames = await backfill(seeded.session, seeded.organization_id, 50)
        for frame in frames:
            if not frame["type"].startswith("delegation."):
                continue
            view = frame.get("view", {})
            assert view.get("task_id", "").startswith("tsk_"), (
                f"{frame['type']} did not resolve the task it is about"
            )
            assert view["task_id"] != frame["subject"]

    async def test_an_unresolvable_id_is_left_as_the_id(self, seeded) -> None:
        """A card labelled with an id is honest about not knowing. A card with a blank
        field looks like the platform has nothing to say."""
        frames = [
            {
                "id": "evt_x",
                "type": "delegation.accepted",
                "subject": "del_x",
                "data": {"parent_task_id": "tsk_nope", "target_agent_id": "agt_nope"},
            }
        ]
        await enrich(frames, seeded.session, seeded.organization_id)
        view = frames[0]["view"]
        assert view["title"] == "tsk_nope"
        assert view["to_agent"] == "agt_nope"

    async def test_enrichment_never_invents_a_field(self, seeded) -> None:
        """A frame with no ids in it gets no `view` block at all, rather than a `view`
        full of empty strings — which a client would render as a row of blanks."""
        frames = [{"id": "evt_y", "type": "agent.created", "subject": "a", "data": {}}]
        await enrich(frames, seeded.session, seeded.organization_id)
        assert "view" not in frames[0]


class TestTheFanout:
    async def test_a_subscriber_receives_what_is_published(self) -> None:
        fan = EventFanout()
        async for queue in fan.subscribe():
            await fan.publish({"id": "1", "type": "task.created"})
            assert (await queue.get())["id"] == "1"
            break

    async def test_every_subscriber_gets_the_frame(self) -> None:
        """The whole reason for the fan-out: one reader, many tabs."""
        fan = EventFanout()
        async for a in fan.subscribe():
            async for b in fan.subscribe():
                await fan.publish({"id": "7"})
                assert (await a.get())["id"] == "7"
                assert (await b.get())["id"] == "7"
                break
            break

    async def test_a_slow_subscriber_drops_the_oldest_frame_not_the_newest(self) -> None:
        """An unbounded per-subscriber queue is a memory leak with a browser tab
        attached. And on a live view the newest frame is the one worth having."""
        fan = EventFanout(maxsize=2)
        async for queue in fan.subscribe():
            for i in range(5):
                await fan.publish({"id": str(i)})
            got = [queue.get_nowait()["id"] for _ in range(2)]
            assert got == ["3", "4"], f"kept {got} instead of the newest frames"
            break

    async def test_subscriber_count_tracks_reality(self) -> None:
        fan = EventFanout()
        assert fan.subscriber_count == 0
        async for _ in fan.subscribe():
            assert fan.subscriber_count == 1
        assert fan.subscriber_count == 0, "a closed connection stayed subscribed"

    async def test_close_releases_every_subscriber(self) -> None:
        """A shutdown that does not release subscribers leaves every open tab hanging
        until its socket times out."""
        fan = EventFanout()
        async for queue in fan.subscribe():
            await fan.close()
            assert queue.get_nowait() is not None, "no shutdown sentinel was sent"
            break


class TestTheFrameShape:
    def test_a_frame_is_json_serialisable(self, seeded) -> None:
        """The `Decimal` in a cost field must not take the stream down mid-frame."""
        from datetime import UTC, datetime

        row = Event(
            id="evt_z",
            organization_id="org_x",
            type="task.created",
            subject="tsk_x",
            source="test",
            data={"cost_usd": "1.25", "nested": {"n": 1}},
            occurred_at=datetime.now(UTC),
        )
        payload = json.dumps(frame_of(row), default=str)
        assert "evt_z" in payload

    def test_the_backfill_default_is_bounded(self) -> None:
        """A page load must not re-transmit a day of events."""
        assert 1 <= BACKFILL_LIMIT <= 500


async def test_stream_cursor_is_independent_for_each_tenant(tenant, other_tenant):
    from datetime import timedelta
    from types import SimpleNamespace

    from ai_orchestrator.api.stream_pump import publish_from_database
    from ai_orchestrator.domain.ids import EventId
    from ai_orchestrator.persistence.base import utcnow

    ids = []
    for context, age in ((tenant, 0), (other_tenant, 60)):
        event_id = str(EventId.create())
        ids.append(event_id)
        context.session.add(
            Event(
                id=event_id,
                organization_id=context.organization_id,
                type="approval.requested",
                source="control-plane",
                subject="review",
                data={},
                occurred_at=utcnow() - timedelta(seconds=age),
            )
        )
        await context.commit()
    frames = []

    async def publish(frame):
        frames.append(frame)

    fan = SimpleNamespace(db=tenant.db, publish=publish)
    assert await publish_from_database(fan, tenant.organization_id) == 1
    assert await publish_from_database(fan, other_tenant.organization_id) == 1
    assert {f["id"] for f in frames} == set(ids)
    assert await publish_from_database(fan, tenant.organization_id) == 0
    assert await publish_from_database(fan, other_tenant.organization_id) == 0
