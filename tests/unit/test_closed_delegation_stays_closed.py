"""A structural ceiling refuses once per run, not once per attempt.

Measured on a real procurement run: the chief was refused the same ceiling 358
times and ended `budget_error` — the whole request budget spent rediscovering a
ceiling that was never going to move. The tool result already said
`delegation_closed: true` with a `next_step`, but nothing in the loop honored
it: every re-ask went back through the executor, minted no work, and cost a
turn each.

So the closure remembers: the first refusal goes through the executor so the
reason is real; every later call in the same run is answered locally, with no
new child task and no new executor work.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.application.task_execution import TaskExecutionService

pytestmark = pytest.mark.unit


class _CeilingHitOnce:
    """A service whose executor refuses everything with a closed ceiling."""

    def __init__(self) -> None:
        self.executor_calls = 0

    async def _delegate_once(self, *, context, agent_name, objective):  # type: ignore[no-untyped-def]
        self.executor_calls += 1
        return None, "fan-out 16 reached the limit of 16", True


class TestAClosedCeilingRefusesOnce:
    async def test_the_second_ask_never_reaches_the_executor(self) -> None:
        stub = _CeilingHitOnce()
        delegate = TaskExecutionService._make_delegate(stub, None)  # type: ignore[arg-type]

        first = await delegate(agent_name="Back Office Agent", objective="run the tender")
        second = await delegate(agent_name="Front Office Agent", objective="run it instead")

        assert stub.executor_calls == 1, (
            f"the closed ceiling was re-asked {stub.executor_calls} times in one run"
        )
        assert first.ok is False and second.ok is False
        assert first.output["delegation_closed"] is True
        assert second.output["delegation_closed"] is True
        assert second.output["child_task_ids"] == []
        assert "Stop delegating" in second.output["next_step"]

    async def test_an_open_refusal_does_not_close_the_run(self) -> None:
        """A non-structural refusal (unknown name, duplicate) must not latch:
        the model is told to adjust and try once more, and that path still works."""

        class _OpenRefusal(_CeilingHitOnce):
            async def _delegate_once(self, *, context, agent_name, objective):  # type: ignore[no-untyped-def]
                self.executor_calls += 1
                return None, "no such agent", False

        stub = _OpenRefusal()
        delegate = TaskExecutionService._make_delegate(stub, None)  # type: ignore[arg-type]

        await delegate(agent_name="Nobody", objective="x")
        second = await delegate(agent_name="Nobody", objective="y")

        assert stub.executor_calls == 2
        assert second.output["delegation_closed"] is False
