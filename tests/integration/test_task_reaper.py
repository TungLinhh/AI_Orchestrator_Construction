"""The reaper, against a live database.

The rules are tested in `tests/unit/test_reaper_rules.py`. These tests exist for the
three things a unit test cannot see, and each of them is a way the sweeper could be
harmful rather than merely wrong:

* **It changes rows.** A module that reads cannot corrupt anything; one that writes
  can. Every assertion here is about the state of a task *after* the sweep.
* **The database refuses independently of the domain.** `assess_task` never touches a
  completed task, and the `UPDATE` also says `AND status = 'running'`. Two layers, and
  `TestTheDatabaseRefusesWithoutTheDomain` turns the domain off entirely to prove the
  second one is load-bearing rather than decorative.
* **The race is real.** A verdict is decided from a row that has since had time to
  change, and the write re-states the condition. `TestTheRaceGuardFires` injects a
  worker renewing the lease between the read and the write and asserts the reaper
  reports `contended` rather than claiming a reclaim that never happened.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import event, text

import ai_orchestrator.application.task_reaper as reaper_module
from ai_orchestrator.application.task_reaper import ReapReport, reap
from ai_orchestrator.domain.enums import TaskStatus
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.reaper import ReapPolicy
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.UTC)


def _ago(**over: dt.timedelta) -> dt.datetime:
    return NOW - dt.timedelta(**over)


async def _task(
    tenant: Tenant,
    status: TaskStatus,
    *,
    updated: dt.datetime | None = None,
    lease: dt.datetime | None = None,
    attempts: int = 0,
    failure: str | None = None,
) -> str:
    """One task in one state. A test states only what it varies."""
    # A ULID, like every other row in the system. The first version derived the id
    # from `hash()` of the arguments, which is a collision waiting to happen: the
    # modulo is a 10-digit space, two tests in a class can hash alike, and a
    # collision surfaces as `UniqueViolationError: pk_tasks` in whichever test lost
    # the race -- an error that names neither the fixture nor the two rows that met.
    task_id = f"tsk_{new_ulid()}"
    await tenant.session.execute(
        text(
            "INSERT INTO tasks (id, organization_id, title, goal, fingerprint, "
            "dedup_key, status, attempt_count, updated_at, created_at) "
            "VALUES (:id, :org, 'reap test', 'reap test', :fp, :fp, "
            "CAST(:status AS varchar(128)), :attempts, CAST(:upd AS timestamptz), "
            "CAST(:upd AS timestamptz))"
        ),
        {
            "id": task_id,
            "org": tenant.organization_id,
            "fp": f"fp-{task_id}",
            "status": status.value,
            "attempts": attempts,
            "upd": updated or _ago(hours=1),
        },
    )
    if lease is not None:
        await tenant.session.execute(
            text("UPDATE tasks SET lease_expires_at = CAST(:lease AS timestamptz) WHERE id = :id"),
            {"id": task_id, "lease": lease},
        )
    if failure is not None:
        await tenant.session.execute(
            text("UPDATE tasks SET failure_category = :f WHERE id = :id"),
            {"id": task_id, "f": failure},
        )
    return task_id


async def _row(tenant: Tenant, task_id: str) -> dict[str, object]:
    result = await tenant.session.execute(
        text(
            "SELECT status, lease_expires_at, attempt_count, failure_category "
            "FROM tasks WHERE id = :id"
        ),
        {"id": task_id},
    )
    return dict(result.mappings().one())


async def _audits(tenant: Tenant) -> list[dict[str, object]]:
    result = await tenant.session.execute(
        text(
            "SELECT action, task_id, outcome, policy_decision, policy_reason "
            "FROM audit_logs ORDER BY created_at, sequence"
        )
    )
    return [dict(r) for r in result.mappings().all()]


async def _sweep(tenant: Tenant, **over: object) -> ReapReport:
    """Sweep at the standard clock, overridable for the tests that need another one."""
    kwargs: dict[str, object] = {"organization_id": tenant.organization_id, "now": NOW}
    kwargs.update(over)
    return await tenant.run(lambda s: reap(s, **kwargs))  # type: ignore[arg-type]


class TestWhatTheSweepActuallyChanges:
    async def test_an_expired_lease_puts_the_task_back_in_the_queue(self, tenant: Tenant) -> None:
        """The one automatic action, verified as a row and not a verdict.

        `lease_expires_at` is cleared as well as the status changed. Leaving a stale
        lease on a queued task would mean the next sweep of that same row finds a
        `running` task with a dead lease that is no longer running, and the report
        would describe a reclaim that never happened.
        """
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        report = await _sweep(tenant)

        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.QUEUED.value
        assert row["lease_expires_at"] is None
        assert task_id in report.reclaimed
        assert report.contended == ()

    async def test_a_live_lease_is_left_alone(self, tenant: Tenant) -> None:
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=-1)
        )
        report = await _sweep(tenant)

        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.RUNNING.value
        assert report.reclaimed == ()
        assert report.surfaced == ()

    async def test_a_retryable_failure_is_requeued(self, tenant: Tenant) -> None:
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(hours=1),
            attempts=1,
            failure="timeout",
        )
        report = await _sweep(tenant)

        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.QUEUED.value
        assert task_id in report.requeued
        assert row["attempt_count"] == 1, "the reaper does not spend the retry budget"

    async def test_the_retry_budget_is_not_spent_by_the_reaper(self, tenant: Tenant) -> None:
        """`attempt_count` belongs to the execution path, not to the sweeper.

        The reaper makes a task eligible to run again. Charging the crashed attempt
        against the ceiling would mean a reaper that ran often enough would exhaust
        the budget of tasks it never even tried, and the count in the column would
        describe reaper sweeps rather than attempts.
        """
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(hours=1),
            attempts=2,
            failure="timeout",
        )
        await _sweep(tenant)
        assert (await _row(tenant, task_id))["attempt_count"] == 2

    async def test_the_failure_category_survives_the_requeue(self, tenant: Tenant) -> None:
        """It is the evidence for why the task failed.

        And it matters for the reaper's own next pass: a task requeued and failed
        again with an empty category would be refused a retry, because absence of a
        reason is not a reason to retry.
        """
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(hours=1),
            attempts=1,
            failure="rate_limited",
        )
        await _sweep(tenant)
        assert (await _row(tenant, task_id))["failure_category"] == "rate_limited"


class TestWhatTheSweepRefusesToChange:
    """The principle, as rows rather than as verdicts."""

    @pytest.mark.parametrize(
        "status",
        [TaskStatus.WAITING_FOR_APPROVAL, TaskStatus.WAITING_FOR_INPUT, TaskStatus.BLOCKED],
    )
    async def test_a_task_waiting_on_a_person_is_never_touched(
        self, tenant: Tenant, status: TaskStatus
    ) -> None:
        """Not expired, not requeued, not closed. Reported and left alone.

        A year old, and still left alone.
        """
        task_id = await _task(tenant, status, updated=_ago(days=365))
        report = await _sweep(tenant)

        assert (await _row(tenant, task_id))["status"] == status.value
        assert task_id in {v.task_id for v in report.surfaced}
        assert report.actioned == 0
        assert await _audits(tenant) == [], (
            "surfacing is not an action; a finding that wrote an audit row would "
            "make the trail claim forty decisions the system never took"
        )

    @pytest.mark.parametrize(
        "status",
        [TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.EXPIRED],
    )
    async def test_a_terminal_task_is_never_touched(
        self, tenant: Tenant, status: TaskStatus
    ) -> None:
        task_id = await _task(tenant, status, updated=_ago(days=365))
        report = await _sweep(tenant)

        assert (await _row(tenant, task_id))["status"] == status.value
        assert report.findings == ()

    async def test_a_permanent_failure_is_reported_and_not_retried(self, tenant: Tenant) -> None:
        """Retrying would spend money to fail the same way."""
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(days=3),
            attempts=1,
            failure="policy_violation",
        )
        report = await _sweep(tenant)

        assert (await _row(tenant, task_id))["status"] == TaskStatus.FAILED.value
        assert report.requeued == ()
        assert task_id in {v.task_id for v in report.surfaced}

    async def test_an_exhausted_failure_is_reported_and_not_retried(self, tenant: Tenant) -> None:
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(days=3),
            attempts=3,
            failure="timeout",
        )
        report = await _sweep(tenant)

        assert (await _row(tenant, task_id))["status"] == TaskStatus.FAILED.value
        assert report.requeued == ()
        assert task_id in {v.task_id for v in report.surfaced}


class TestTheDatabaseRefusesWithoutTheDomain:
    """Defence in depth, with the domain switched off.

    `assess_task` will never hand the sweeper a completed task, so the
    `AND status = 'running'` in the reclaim is unreachable through the normal path.
    That is exactly why it needs its own test: an unreachable guard that nobody has
    exercised is a guard that will be dropped by the next person tidying the SQL, and
    the day after that a refactor or a second caller reaches `failed` or `completed`
    through this statement.
    """

    async def test_reclaiming_a_completed_task_changes_nothing(self, tenant: Tenant) -> None:
        task_id = await _task(tenant, TaskStatus.COMPLETED, updated=_ago(days=365))
        took = await tenant.run(
            lambda s: reaper_module._act(
                reaper_module._RECLAIM, s, tenant.organization_id, task_id, NOW
            )
        )
        assert took is False
        assert (await _row(tenant, task_id))["status"] == TaskStatus.COMPLETED.value

    async def test_reclaiming_a_live_lease_changes_nothing(self, tenant: Tenant) -> None:
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=-1)
        )
        took = await tenant.run(
            lambda s: reaper_module._act(
                reaper_module._RECLAIM, s, tenant.organization_id, task_id, NOW
            )
        )
        assert took is False
        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.RUNNING.value
        assert row["lease_expires_at"] is not None, "the lease must survive a refused reclaim"

    async def test_reclaiming_an_expired_lease_does_work(self, tenant: Tenant) -> None:
        """So the previous two are testing the guard and not a broken statement."""
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        took = await tenant.run(
            lambda s: reaper_module._act(
                reaper_module._RECLAIM, s, tenant.organization_id, task_id, NOW
            )
        )
        assert took is True
        assert (await _row(tenant, task_id))["status"] == TaskStatus.QUEUED.value

    async def test_one_tenants_reaper_cannot_reclaim_anothers_task(self, tenant: Tenant) -> None:
        """RLS, and the explicit `organization_id` in the statement.

        The test role is `BYPASSRLS` (see `tests/conftest.py`), so this is not
        proving RLS works — it is proving the `WHERE organization_id = :o` clause
        does, which is what protects production where the role is *not* privileged.
        """
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        took = await tenant.run(
            lambda s: reaper_module._act(
                reaper_module._RECLAIM,
                s,
                "org_someone_else",
                task_id,
                NOW,
            )
        )
        assert took is False
        assert (await _row(tenant, task_id))["status"] == TaskStatus.RUNNING.value


class TestTheRaceGuardFires:
    async def test_a_worker_that_renews_the_lease_beats_the_reaper(self, tenant: Tenant) -> None:
        """The one case that would otherwise be a lie in the report.

        The candidate query has already returned its rows by the time the first
        verdict is asked for, so a worker renewing the lease *immediately before the
        reclaim executes* is precisely "a live worker renewed it between the read and
        the write". The reaper's verdict was correct when made and is wrong by the
        time it is acted on, and the `AND lease_expires_at <= :now` in the UPDATE is
        what stops it acting.

        The hook is a `before_cursor_execute` listener rather than something inside
        `assess_task`, because a reaper is not supposed to be re-entrant and
        monkeypatching the domain call to mutate the database would be testing a
        program that cannot exist. The listener fires inside the greenlet already
        spawned for the UPDATE, so the competing statement is ordinary synchronous
        driver work on the same connection -- no deadlock, no second connection, and
        the reaper's copy of the row is simply stale.

        The result is `contended`, not `reclaimed`: the report must not claim a
        reclaim the database refused.
        """
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        renewed = await _competing_worker(
            tenant, task_id, before="UPDATE tasks", marker="lease_expires_at IS NOT NULL"
        )

        report = await _sweep(tenant)

        assert renewed, "the competing worker never ran, so nothing was proved"
        assert report.contended == (task_id,)
        assert report.reclaimed == ()
        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.RUNNING.value, (
            "the reaper must not queue a task a worker is holding"
        )
        assert row["lease_expires_at"] is not None

        # And the refusal is written down. "I could not take this" is information;
        # a silently dropped attempt is not.
        audits = await _audits(tenant)
        assert len(audits) == 1
        assert audits[0]["outcome"] == "contended"
        assert audits[0]["policy_decision"] == "lease_expired"


class _CompetingWorker:
    """A live worker that renews a lease, fired at a chosen statement.

    Held rather than inlined because `event.listens_for` registers a callback and
    nothing holds a reference to it afterwards -- without this the listener is
    garbage collected mid-sweep and the test passes for the wrong reason.
    """

    def __init__(self, fired: list[bool]) -> None:
        self._fired = fired

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        # `$1`/`$2`, not `%s`: this is asyncpg's own cursor, not SQLAlchemy's
        # pyformat layer, and it does not translate the placeholders for us.
        cursor.execute(
            "UPDATE tasks SET lease_expires_at = $1 WHERE id = $2",
            (NOW + dt.timedelta(hours=1), self._task_id),
        )
        self._fired.append(True)


async def _competing_worker(
    tenant: Tenant, task_id: str, *, before: str, marker: str
) -> list[bool]:
    """Register the listener and return the list it appends to when it fires.

    The `marker` narrows which statement the worker races. Scoping it to the
    reclaim's own `AND lease_expires_at IS NOT NULL` is what keeps the worker from
    also firing on the test's own setup UPDATEs, where renewing a lease that is
    already what we want would prove nothing.
    """
    fired: list[bool] = []
    worker = _CompetingWorker(fired)
    worker._task_id = task_id  # type: ignore[attr-defined]

    def guard(conn, cursor, statement, parameters, context, executemany):
        if before in statement and marker in statement:
            worker(conn, cursor, statement, parameters, context, executemany)

    event.listen(tenant.session.sync_session.get_bind(), "before_cursor_execute", guard)
    return fired


class TestTheSweepIsIdempotent:
    async def test_a_second_sweep_changes_nothing(self, tenant: Tenant) -> None:
        """Because both actions land on `queued`, which the rules ignore.

        Worth testing rather than reasoning about: a sweeper that runs on a timer
        will run again in five minutes regardless, and if the second pass acted a
        second time the report would show a task reclaimed N times.
        """
        expired = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        failed = await _task(
            tenant, TaskStatus.FAILED, updated=_ago(hours=1), attempts=1, failure="timeout"
        )
        waiting = await _task(tenant, TaskStatus.WAITING_FOR_APPROVAL, updated=_ago(days=30))

        first = await _sweep(tenant)
        assert set(first.reclaimed) | set(first.requeued) == {expired, failed}

        second = await _sweep(tenant)
        assert second.reclaimed == ()
        assert second.requeued == ()
        assert second.actioned == 0
        assert {v.task_id for v in second.surfaced} == {waiting}, (
            "the waiting task is still overdue -- surfacing it again is correct, "
            "and it is the one thing a repeat sweep should keep saying"
        )

    async def test_the_audit_trail_does_not_grow_on_a_repeat_sweep(self, tenant: Tenant) -> None:
        await _task(tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2))
        await _sweep(tenant)
        after_first = len(await _audits(tenant))
        await _sweep(tenant)
        assert len(await _audits(tenant)) == after_first == 1


class TestWhatTheSweepLooksAt:
    async def test_a_tenant_only_sees_its_own_tasks(self, tenant: Tenant) -> None:
        """The candidate query is scoped, and the test role would not have stopped it.

        `BYPASSRLS` in `tests/conftest.py` exists so tenant-isolation tests do not
        pass vacuously. That makes the explicit `organization_id` in the query the
        only thing standing between a sweep and another tenant's overdue approvals,
        so it gets a test of its own.
        """
        await tenant.run(lambda s: reap(s, organization_id="org_someone_else", now=NOW))
        assert await _audits(tenant) == []

    async def test_recent_rows_are_not_even_loaded(self, tenant: Tenant) -> None:
        """The query is a load guard, and the domain still decides everything.

        A retryable failure a minute old is a genuine finding under the policy, but
        it is only a finding on a sweep that runs more often than the default. The
        query must not pre-empt that judgement, so the floor is derived from the
        policy and a test proves a tighter policy sees more candidates.
        """
        task_id = await _task(
            tenant, TaskStatus.FAILED, updated=_ago(hours=1), attempts=1, failure="timeout"
        )
        assert (await _sweep(tenant)).requeued == (task_id,)

    async def test_a_tighter_policy_finds_more(self, tenant: Tenant) -> None:
        """Proves the threshold is load-bearing and the query follows it.

        A failure 30 seconds old is not a finding under the default five-minute
        delay. Under a one-second delay it is. If the SQL filtered at a hardcoded
        48 hours this test would fail, which is the point: a second copy of a
        threshold is a second thing to forget to change.
        """
        task_id = await _task(
            tenant,
            TaskStatus.FAILED,
            updated=NOW - dt.timedelta(seconds=30),
            attempts=1,
            failure="timeout",
        )
        assert (await _sweep(tenant)).requeued == ()
        tight = await _sweep(tenant, policy=ReapPolicy(requeue_after=dt.timedelta(seconds=1)))
        assert tight.requeued == (task_id,)

    async def test_the_limit_bounds_one_sweep_without_losing_the_rest(self, tenant: Tenant) -> None:
        """A tenant with a million tasks gets a slice, and the next sweep continues.

        `ORDER BY updated_at` is what makes the walk stable. Without it a limit is a
        lottery, and the tasks that never get picked are the recent ones, which are
        the ones nobody is watching anyway -- so it fails quietly.
        """
        for i in range(5):
            await _task(
                tenant,
                TaskStatus.FAILED,
                updated=_ago(hours=1) - dt.timedelta(minutes=i),
                attempts=1,
                failure="timeout",
            )
        first = await _sweep(tenant, limit=2)
        assert len(first.requeued) == 2
        rest = await _sweep(tenant, limit=10)
        assert len(rest.requeued) == 3, "the remaining three are not skipped"


class TestTheClockItRefuses:
    async def test_a_naive_now_is_refused_with_a_reason(self, tenant: Tenant) -> None:
        """The error names the column type and the fix, not a missing built-in method.

        Without this guard the failure arrives as `TypeError: can't subtract
        offset-naive and offset-aware datetimes` from inside `_age`, which is four
        frames below the caller that passed the bad value and mentions neither the
        `timestamptz` column nor the fact that the caller has to supply an aware
        clock.
        """
        with pytest.raises(ValueError) as caught:
            await tenant.run(
                lambda s: reap(
                    s,
                    organization_id=tenant.organization_id,
                    now=dt.datetime(2026, 9, 27, 12, 0),
                )
            )
        message = str(caught.value)
        assert "timezone-aware" in message
        assert "timestamptz" in message
        assert "lease" in message, "the reason to care is the two-hour lease margin"

    async def test_a_naive_now_is_refused_before_anything_is_written(self, tenant: Tenant) -> None:
        """The guard is a precondition, not a cleanup on the way out.

        A sweep that read its candidates, found an expired lease, and *then* noticed
        the clock was naive would have a report to reconcile and a transaction to
        unwind. Refusing first means a refused sweep did nothing at all.
        """
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        # Called on the session directly rather than through `tenant.run`: that
        # helper rolls the whole transaction back when the callable raises, which
        # would take the fixture's own INSERT with it and leave nothing to assert
        # about. The refused sweep must leave the *task* alone, so the task has to
        # still be there afterwards.
        with pytest.raises(ValueError):
            await reap(
                tenant.session,
                organization_id=tenant.organization_id,
                now=dt.datetime(2026, 9, 27, 12, 0),
            )

        row = await _row(tenant, task_id)
        assert row["status"] == TaskStatus.RUNNING.value
        assert row["lease_expires_at"] is not None, "the lease must survive a refusal"
        assert await _audits(tenant) == []

    async def test_an_aware_now_in_another_offset_is_fine(self, tenant: Tenant) -> None:
        """Non-UTC is not the problem; naive is.

        The rows come back normalised to UTC by the driver, so a caller on
        `Asia/Ho_Chi_Minh` and a caller on UTC must reach the same verdicts. If this
        failed it would mean the sweep was sensitive to the scheduler's locale
        rather than to the clock.
        """
        task_id = await _task(
            tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2)
        )
        report = await tenant.run(
            lambda s: reap(
                s,
                organization_id=tenant.organization_id,
                now=NOW.astimezone(dt.timezone(dt.timedelta(hours=7))),
            )
        )
        assert report.reclaimed == (task_id,)


class TestTheReportIsHonest:
    async def test_surfaced_and_actioned_are_separate_numbers(self, tenant: Tenant) -> None:
        """A sweep reporting "4" is ambiguous between those two things.

        They call for completely different responses: one needs a person, the other
        means the system is keeping up on its own.
        """
        await _task(tenant, TaskStatus.RUNNING, updated=_ago(hours=3), lease=_ago(hours=2))
        await _task(tenant, TaskStatus.WAITING_FOR_APPROVAL, updated=_ago(days=30))
        await _task(tenant, TaskStatus.BLOCKED, updated=_ago(days=10))

        got = (await _sweep(tenant)).as_dict()
        assert got["surfaced"] == 2
        assert got["actioned"] == 1
        assert got["total"] == 3
        assert got["candidates"] >= 3

    async def test_an_empty_sweep_reports_nothing_rather_than_zero_everything(
        self, tenant: Tenant
    ) -> None:
        """A clean sweep is a result, and it should be a small one."""
        got = (await _sweep(tenant)).as_dict()
        assert got["candidates"] == 0
        assert got["total"] == 0
        assert got["actioned"] == 0
        assert got["surfaced"] == 0

    async def test_the_audit_row_says_why(self, tenant: Tenant) -> None:
        """`action=task.reclaim` alone is not a record of anything.

        Six weeks from now the only useful question about a requeue is what went
        wrong and how long it had been true, and both are in the row.
        """
        await _task(
            tenant,
            TaskStatus.FAILED,
            updated=_ago(hours=2),
            attempts=1,
            failure="timeout",
        )
        await _sweep(tenant)

        audits = await _audits(tenant)
        assert len(audits) == 1
        row = audits[0]
        assert row["action"] == "task.requeue"
        assert row["outcome"] == "requeued"
        assert row["policy_decision"] == "retryable_failure"
        assert "timeout" in str(row["policy_reason"])
        assert str(row["policy_reason"]).startswith("failed with timeout")
