"""A failed task must be **actionable**, and stranded executions must stop lying.

Two defects, both found by walking the CEO queue rather than by reading code:

1. **Stranded executions.** Eleven executions sat at `running` on tasks that had reached
   `failed`, the oldest from 27 September. `local_runner`'s double-run guard counts exactly
   those, so pressing Run answered *"1 execution(s) are already running for this task, so
   something else is working on it"* — and would have answered the same thing forever, about
   work nobody was doing. `reap_stranded_executions` closes them, on a condition that needs no
   lease and no clock: **a run cannot still be in flight on a task that has finished.**

2. **Failed tasks could not be retried.** `TASK_TRANSITIONS` maps every terminal status to
   `{}` and the comment says a retry is *"a new task (linked via task_dependencies)"*. That
   was the whole of the design; the retry was never built. So the platform could explain all
   58 failures and let nobody do anything about any of them.

The refusals are asserted by their sentences, per the rule the rest of this suite follows: a
test that only asserts the exception passes against a message saying "invalid input".

## One fixture, written once

The first version of this file had three hand-written task INSERTs. They drifted: one named a
column the others did not, one supplied a value for a column it never named, and one put the
same bind parameter in two columns of different types so asyncpg could not deduce a type for
it. All three were caught by the database, which is the right place for that — but **a
fixture written by hand three times is a fixture written wrongly at least once.**
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import text

from ai_orchestrator.application.task_reaper import reap_stranded_executions
from ai_orchestrator.application.task_retry import RETRY_MARKER, retry_failed
from ai_orchestrator.domain.errors import ConflictError
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)

_INSERT_TASK = """
INSERT INTO tasks (
    id, organization_id, title, goal, task_type, status, priority, input, constraints,
    fingerprint, dedup_key, requester_type, last_error, failure_category, created_at,
    updated_at)
VALUES (
    CAST(:id AS varchar(64)), CAST(:org AS varchar(64)), CAST(:title AS varchar(200)),
    CAST(:goal AS text), :type, :status, 'normal', '{}'::jsonb, '{}'::jsonb,
    CAST(:fingerprint AS varchar(64)), CAST(:dedup_key AS varchar(64)), 'human',
    :last_error, :category, :now, :now)
ON CONFLICT (id) DO NOTHING
"""

#: `executions.agent_id` is a **foreign key to `agents`**, so a made-up id is refused. That is
#: why the tests that need an execution take `seeded` and read a real agent id, rather than
#: inventing one and discovering the constraint.
_INSERT_EXECUTION = """
INSERT INTO executions (
    id, organization_id, task_id, agent_id, attempt, status, runtime_adapter, model_profile,
    started_at, created_at)
VALUES (
    CAST(:id AS varchar(64)), CAST(:org AS varchar(64)), CAST(:task AS varchar(64)),
    CAST(:agent AS varchar(64)), 1, 'running', 'test', 'test', :now, :now)
ON CONFLICT (id) DO NOTHING
"""


def _suffix(value: str) -> str:
    """A suffix unique per title, so each fixture gets its own row and its own dedup key.

    `hash()` is salted per process, which is acceptable and worth being explicit about: the
    ids are only ever used inside the run that made them.
    """
    return f"{abs(hash(value)) % 10**10:010d}"


async def _task(
    tenant: Tenant,
    *,
    title: str,
    status: str = "failed",
    last_error: str | None = "the agent exceeded its turn budget and was stopped",
    category: str | None = "budget_exhausted",
) -> str:
    suffix = _suffix(title)
    task_id = f"tsk_{tenant.organization_id[-8:]}_{suffix}"
    await tenant.session.execute(
        text(_INSERT_TASK),
        {
            "id": task_id,
            "org": tenant.organization_id,
            "title": title,
            "goal": title,
            "type": "coordination" if status == "failed" else "execution",
            "status": status,
            "fingerprint": f"fp{suffix}",
            "dedup_key": f"fp{suffix}",
            "last_error": last_error,
            "category": category,
            "now": NOW,
        },
    )
    await tenant.commit()
    return task_id


async def _running_execution(tenant: Tenant, *, task_id: str, tag: str) -> str:
    agent_id = str(
        (
            await tenant.session.execute(
                text(
                    "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                    "ORDER BY id LIMIT 1"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar_one()
    )
    execution_id = f"exe_{tenant.organization_id[-8:]}_{tag}"
    await tenant.session.execute(
        text(_INSERT_EXECUTION),
        {
            "id": execution_id,
            "org": tenant.organization_id,
            "task": task_id,
            "agent": agent_id,
            "now": NOW,
        },
    )
    await tenant.commit()
    return execution_id


@pytest_asyncio.fixture
async def seeded(tenant: Tenant) -> Tenant:
    """A tenant with agents, because `executions.agent_id` is a foreign key to one.

    Defined here rather than reached for across files: it is two lines, and a shared fixture
    in a `conftest.py` for one test file's convenience is a fixture every other test file then
    depends on without knowing it.
    """
    from sqlalchemy import select

    from ai_orchestrator.persistence.models import Organization
    from ai_orchestrator.seed import seed

    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


class TestTheStrandedExecutionSweep:
    async def test_it_closes_a_run_on_a_task_that_finished(self, seeded: Tenant) -> None:
        task_id = await _task(seeded, title="Stranded, on a failed task")
        await _running_execution(seeded, task_id=task_id, tag="s1")

        closed = await reap_stranded_executions(
            seeded.session, organization_id=seeded.organization_id, now=NOW
        )
        await seeded.commit()
        assert task_id in closed

        row = (
            (
                await seeded.session.execute(
                    text(
                        "SELECT status, error_message FROM executions "
                        "WHERE task_id = CAST(:t AS varchar(64))"
                    ),
                    {"t": task_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "failed", "the execution must not still claim to be running"
        # The message has to say *why*, in terms a reader can act on. A bare "failed" tells an
        # operator nothing about a run that vanished.
        assert "terminal state" in row["error_message"]

    async def test_it_leaves_a_run_on_a_live_task_alone(self, seeded: Tenant) -> None:
        """**The assertion that keeps this from being a blunt instrument.**

        A timeout-based sweep would close this one eventually, and closing a run that is
        genuinely working is the expensive mistake: 67,000 tokens spent and then thrown away.
        So a task still `running` keeps its `running` execution, whatever the age.
        """
        task_id = await _task(
            seeded, title="Live work", status="running", last_error=None, category=None
        )
        await _running_execution(seeded, task_id=task_id, tag="s2")

        closed = await reap_stranded_executions(
            seeded.session, organization_id=seeded.organization_id, now=NOW
        )
        await seeded.commit()
        assert task_id not in closed
        status = (
            await seeded.session.execute(
                text("SELECT status FROM executions WHERE task_id = CAST(:t AS varchar(64))"),
                {"t": task_id},
            )
        ).scalar_one()
        assert status == "running", "a run in flight must survive the sweep"

    async def test_running_it_twice_changes_nothing(self, seeded: Tenant) -> None:
        """What makes it safe in `make page` rather than a cron somebody forgets."""
        task_id = await _task(seeded, title="Swept twice")
        await _running_execution(seeded, task_id=task_id, tag="s3")

        first = await reap_stranded_executions(
            seeded.session, organization_id=seeded.organization_id, now=NOW
        )
        await seeded.commit()
        second = await reap_stranded_executions(
            seeded.session, organization_id=seeded.organization_id, now=NOW
        )
        await seeded.commit()
        assert len(first) == 1
        assert second == [], "idempotent by construction, not by bookkeeping"

    async def test_a_naive_clock_is_refused(self, tenant: Tenant) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await reap_stranded_executions(
                tenant.session,
                organization_id=tenant.organization_id,
                now=dt.datetime(2026, 9, 28, 12, 0),
            )


class TestRetryingFailedWork:
    async def test_a_retry_carries_the_work_and_not_the_history(self, tenant: Tenant) -> None:
        """The design in one test: the *work* moves, the *attempt* stays put.

        Reusing the row would make the audit trail lie about what was attempted, which is why
        `TASK_TRANSITIONS` closes every terminal state. This is the other half of that
        decision — a new row that says where it came from.
        """
        original = await _task(tenant, title="Retry carries the goal")
        out = await retry_failed(
            tenant.session, organization_id=tenant.organization_id, task_id=original, now=NOW
        )
        await tenant.commit()

        assert out["retried_from"] == original
        assert RETRY_MARKER in out["title"], (
            "a queue showing two identical titles gives an operator nothing to tell them apart"
        )
        fresh = (
            (
                await tenant.session.execute(
                    text(
                        "SELECT goal, status, owner_agent_id, last_error FROM tasks "
                        "WHERE id = CAST(:i AS varchar(64))"
                    ),
                    {"i": out["task_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert fresh["goal"] == "Retry carries the goal", "the work must be carried over"
        assert fresh["status"] == "created"
        assert fresh["owner_agent_id"] is None, (
            "a retry is unassigned until somebody gives it an owner — it is not a rerun wearing "
            "a new id"
        )
        assert fresh["last_error"] is None, "and it must not inherit a failure it did not have"

    async def test_the_original_still_holds_its_failure(self, tenant: Tenant) -> None:
        """The whole reason a retry is a new task. If this changes, the retry is a lie."""
        original = await _task(tenant, title="The failure stays")
        await retry_failed(
            tenant.session, organization_id=tenant.organization_id, task_id=original, now=NOW
        )
        await tenant.commit()
        after = (
            (
                await tenant.session.execute(
                    text("SELECT status, last_error FROM tasks WHERE id = CAST(:i AS varchar(64))"),
                    {"i": original},
                )
            )
            .mappings()
            .one()
        )
        assert after["status"] == "failed", "the old row must not be reopened"
        assert "turn budget" in after["last_error"], "and must keep saying what went wrong"

    async def test_the_link_survives_so_the_trail_is_queryable(self, tenant: Tenant) -> None:
        original = await _task(tenant, title="The link is written")
        out = await retry_failed(
            tenant.session, organization_id=tenant.organization_id, task_id=original, now=NOW
        )
        await tenant.commit()
        link = (
            (
                await tenant.session.execute(
                    text(
                        "SELECT depends_on_task_id, dependency_type FROM task_dependencies "
                        "WHERE task_id = CAST(:i AS varchar(64))"
                    ),
                    {"i": out["task_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert link["depends_on_task_id"] == original
        assert link["dependency_type"] == "finish_to_start"

    async def test_the_link_does_not_block_the_retry(self, tenant: Tenant) -> None:
        """It reads like a deadlock and is not: the failed task is terminal, so satisfied.

        Worth a test because the reasoning is easy to get the other way round — a guard that
        assumed "depends on" meant "wait for" would leave every retry permanently stuck, and
        the symptom would be a retry that never runs and says nothing about why.
        """
        original = await _task(tenant, title="The link does not block")
        out = await retry_failed(
            tenant.session, organization_id=tenant.organization_id, task_id=original, now=NOW
        )
        await tenant.commit()
        blocking = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM task_dependencies d "
                    "JOIN tasks t ON t.id = d.depends_on_task_id "
                    "  AND t.organization_id = d.organization_id "
                    "WHERE d.task_id = CAST(:t AS varchar(64)) AND t.status NOT IN "
                    "  ('completed', 'failed', 'cancelled', 'blocked')"
                ),
                {"t": out["task_id"]},
            )
        ).scalar_one()
        assert blocking == 0, "a retry of a terminal task must be runnable immediately"

    async def test_work_that_has_not_failed_is_refused(self, tenant: Tenant) -> None:
        """Copying a task that is merely unfinished would manufacture work.

        And the refusal says which, because "cannot retry" with no reason is the answer that
        made this a dead end in the first place.
        """
        live = await _task(
            tenant, title="Not failed", status="assigned", last_error=None, category=None
        )
        with pytest.raises(ConflictError) as caught:
            await retry_failed(
                tenant.session, organization_id=tenant.organization_id, task_id=live, now=NOW
            )
        assert "only failed work is retried" in str(caught.value)

    async def test_a_naive_clock_is_refused(self, tenant: Tenant) -> None:
        original = await _task(tenant, title="Naive clock")
        with pytest.raises(ValueError, match="timezone-aware"):
            await retry_failed(
                tenant.session,
                organization_id=tenant.organization_id,
                task_id=original,
                now=dt.datetime(2026, 9, 28, 12, 0),
            )
