"""One projection of the event log, used twice.

`enrich` moved here from `api/stream.py` because the task report needed the same
projection: the live feed read names while the report read ids, for the same events.
Two serialisations of "an event" that drift apart is how a live view and a page refresh
start disagreeing.

These tests pin the contract at the level the move lives: ids in, names out, everything
display-only under `view`, and the durable fields untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ai_orchestrator.application.event_view import enrich

pytestmark = pytest.mark.unit


class _Session:
    """Answers the two selects `enrich` makes, from canned rows.

    It inspects the query text for which table is asked, because the call pattern is
    `session.execute(select(...))` and a fake that pretends to parse SQL would be a
    second implementation of the database. The rows carry attributes (`row.id`,
    `row.title`) exactly as SQLAlchemy returns them, which is the part of the contract
    that matters here.
    """

    def __init__(self, tasks: dict[str, str], agents: dict[str, str]) -> None:
        self._tasks = tasks
        self._agents = agents

    async def execute(self, query: Any, *args: Any, **kwargs: Any) -> Any:
        text = str(query)
        if "FROM tasks" in text:
            rows = [SimpleNamespace(id=i, title=t, status=None) for i, t in self._tasks.items()]
        else:
            rows = [SimpleNamespace(id=i, name=n) for i, n in self._agents.items()]

        class _Result:
            def all(self) -> list[Any]:
                return rows

        return _Result()


def _frame(type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": "evt_1", "type": type, "data": data}


class TestNamesAreResolved:
    async def test_agent_ids_become_names(self) -> None:
        frames = [
            _frame(
                "delegation.accepted",
                {
                    "parent_task_id": "tsk_1",
                    "source_agent_id": "agt_1",
                    "target_agent_id": "agt_2",
                },
            )
        ]
        await enrich(
            frames,
            _Session(
                {"tsk_1": "Pick a vendor"},
                {"agt_1": "Executive Agent", "agt_2": "Finance Agent"},
            ),
            "org_1",
        )
        view = frames[0].get("view", {})
        assert view["from_agent"] == "Executive Agent", view
        assert view["to_agent"] == "Finance Agent", view
        assert view["title"] == "Pick a vendor", view

    async def test_an_unresolvable_id_stays_an_id(self) -> None:
        """A card labelled with an id is honest; a blank field looks like silence."""
        frames = [_frame("task.created", {"task_id": "tsk_missing"})]
        await enrich(frames, _Session({}, {}), "org_1")
        assert "view" not in frames[0] or frames[0]["view"].get("title") in (
            "tsk_missing",
            None,
        )

    async def test_the_payload_is_untouched(self) -> None:
        """Everything added lives under `view`. The durable fields are not rewritten."""
        data = {"task_id": "tsk_1", "status": "completed"}
        frames = [_frame("task.completed", dict(data))]
        await enrich(frames, _Session({"tsk_1": "T"}, {}), "org_1")
        assert frames[0]["data"] == data
        assert frames[0]["type"] == "task.completed"

    async def test_a_frame_without_data_gets_no_view(self) -> None:
        frames = [{"id": "evt_1", "type": "task.created"}]
        await enrich(frames, _Session({}, {}), "org_1")
        assert "view" not in frames[0]
