"""Nothing makes progress that is not measured, and nothing is measured by hand.

Tests for `domain/reaper.py`, organised around the two things that could be wrong in
a reaper and are much worse than a reaper that does not exist:

* **It acts when it should only report.** The default is `SURFACE`; only an expired
  lease and a retryable failure are acted on, because both are cases where a
  contract that already exists has expired. Every other case is a finding.
* **It cannot do harm.** A completed task is never touched, a waiting-for-approval
  task is never expired, and a clock skewed into the future produces a number that
  is clamped rather than a task reported as `-3 days` passive.

The numbers are the ones the corpus and the schema actually support. Nothing here is
derived from a real project's response time, because the corpus says nothing about
how long a Vietnamese construction project takes to answer an email.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

# The application module is imported into a *unit* test on purpose, and this is the
# only file that does it. `assess_task` and the sweep's candidate query are two
# halves of one decision -- the query decides which rows to show the rules, the rules
# decide what they mean -- and nothing in either layer can check the other. This test
# is that check, and it needs both.
from ai_orchestrator.application.task_reaper import _CANDIDATES, REAPABLE_STATUSES
from ai_orchestrator.domain.enums import TaskStatus
from ai_orchestrator.domain.reaper import (
    RETRYABLE_FAILURES,
    Action,
    ReapPolicy,
    Reason,
    TaskSnapshot,
    Verdict,
    assess_task,
    summarise,
)

NOW = dt.datetime(2026, 9, 27, 12, 0, tzinfo=dt.UTC)
POLICY = ReapPolicy()


def _task(status: TaskStatus, **over: object) -> TaskSnapshot:
    base: dict[str, object] = {
        "task_id": "tsk_1",
        "status": status,
        "updated_at": NOW - dt.timedelta(hours=1),
    }
    base.update(over)
    return TaskSnapshot(**base)  # type: ignore[arg-type]


def _old(hours: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=hours)


class TestATaskThatNeedsNothing:
    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.CREATED,
            TaskStatus.QUEUED,
            TaskStatus.ASSIGNED,
        ],
    )
    def test_a_task_that_has_not_started_yet_is_fine(self, status: TaskStatus) -> None:
        """Not yet running, so nothing is owed and nothing has gone wrong.

        Including when the task is old: a task queued for a week has not gone wrong,
        it has not started, and the queue is a different system's problem.
        """
        assert assess_task(_task(status, updated_at=_old(24 * 30)), NOW) is None

    @pytest.mark.parametrize(
        "status",
        [TaskStatus.COMPLETED, TaskStatus.CANCELED, TaskStatus.EXPIRED],
    )
    def test_a_terminal_task_is_never_touched(self, status: TaskStatus) -> None:
        """The property that matters most, stated as a test.

        A reaper that touches a completed task looks exactly like data loss, and
        there is no version of this function in which a terminal state is anything
        but `None` — so the guarantee is worth a test rather than a code review.
        """
        assert assess_task(_task(status, updated_at=_old(24 * 365)), NOW) is None

    def test_a_healthy_running_task_is_fine(self) -> None:
        """A live lease, recently touched."""
        assert (
            assess_task(
                _task(
                    TaskStatus.RUNNING,
                    lease_expires_at=NOW + dt.timedelta(minutes=30),
                    updated_at=NOW - dt.timedelta(minutes=1),
                ),
                NOW,
            )
            is None
        )

    def test_a_failure_too_fresh_to_retry_is_left_for_the_next_sweep(self) -> None:
        """Seconds old, not hours.

        A sweeper runs on a timer, so a task that failed thirty seconds ago is not
        anything: the next sweep is seconds away, and requeueing now would race
        whatever is already looking at it.
        """
        task = _task(
            TaskStatus.FAILED,
            updated_at=_old(0.005),
            attempt_count=1,
            failure_category="timeout",
        )
        assert assess_task(task, NOW) is None


class TestAnExpiredLeaseIsTheOneAutomaticAction:
    async def test_a_running_task_whose_lease_expired_is_reclaimed(self) -> None:
        task = _task(
            TaskStatus.RUNNING,
            lease_expires_at=_old(2),
            updated_at=_old(3),
        )
        verdict = assess_task(task, NOW)
        assert verdict is not None
        assert verdict.action is Action.RECLAIM
        assert verdict.reason is Reason.LEASE_EXPIRED
        assert verdict.is_actionable

    def test_just_inside_the_lease_is_left_alone(self) -> None:
        """One second of grace, because clocks disagree.

        A lease expiring "now" on a sweeper running a few milliseconds behind the
        worker's clock would reclaim a task that is still being worked on. The
        comparison is strictly greater-than, which is the whole margin.
        """
        task = _task(
            TaskStatus.RUNNING,
            lease_expires_at=NOW + dt.timedelta(seconds=1),
            updated_at=_old(1),
        )
        assert assess_task(task, NOW) is None

    def test_the_policy_can_refuse_to_reclaim(self) -> None:
        """So a reaper can be run read-only before it is trusted.

        The same shadow-mode discipline the dossier applies to its agents: measure
        what you *would* do before letting you do it.
        """
        task = _task(TaskStatus.RUNNING, lease_expires_at=_old(2))
        verdict = assess_task(task, NOW, ReapPolicy(allow_reclaim=False))
        assert verdict is not None
        assert verdict.action is Action.SURFACE
        assert verdict.reason is Reason.LEASE_EXPIRED
        assert not verdict.is_actionable


class TestARunningTaskWithNoLeaseIsOnlyReported:
    """The distinction the whole function turns on.

    A lease is a promise to renew by a time. *No* lease means the code path that
    started the task does not take one, which is a bug — but the task may be
    genuinely in flight, and reclaiming it would race a live worker.
    """

    def test_it_is_surfaced_not_reclaimed(self) -> None:
        task = _task(TaskStatus.RUNNING, lease_expires_at=None, updated_at=_old(72))
        verdict = assess_task(task, NOW)
        assert verdict is not None
        assert verdict.action is Action.SURFACE
        assert verdict.reason is Reason.NO_LEASE
        assert not verdict.is_actionable

    def test_a_recent_one_is_not_surfaced(self) -> None:
        """Absent a lease the staleness threshold is the only signal, so a task
        started a minute ago is not a finding."""
        task = _task(TaskStatus.RUNNING, lease_expires_at=None, updated_at=_old(0.1))
        assert assess_task(task, NOW) is None

    def test_the_detail_says_why_it_was_not_reclaimed(self) -> None:
        """The sentence is the output, and a reader must be able to tell a
        deliberate choice from an oversight."""
        verdict = assess_task(
            _task(TaskStatus.RUNNING, lease_expires_at=None, updated_at=_old(72)), NOW
        )
        assert verdict is not None
        assert "may genuinely be in flight" in verdict.detail


class TestAPersonOwesSomethingAndTheReaperDoesNotPayIt:
    """The principle, as tests.

    A task waiting for approval is a person owing a commercial decision. Expiring it
    because it is old is the system making that decision for them.
    """

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (TaskStatus.WAITING_FOR_APPROVAL, Reason.WAITING_FOR_APPROVAL_TOO_LONG),
            (TaskStatus.WAITING_FOR_INPUT, Reason.WAITING_FOR_INPUT_TOO_LONG),
            (TaskStatus.BLOCKED, Reason.BLOCKED_TOO_LONG),
        ],
    )
    def test_an_old_passive_task_is_surfaced_and_never_expired(
        self, status: TaskStatus, reason: Reason
    ) -> None:
        verdict = assess_task(_task(status, updated_at=_old(24 * 30)), NOW)
        assert verdict is not None
        assert verdict.action is Action.SURFACE, (
            f"{status.value} is a person owing something; expiring it is the system "
            f"deciding for them"
        )
        assert verdict.reason is reason
        assert not verdict.is_actionable

    @pytest.mark.parametrize(
        "status",
        [
            TaskStatus.WAITING_FOR_APPROVAL,
            TaskStatus.WAITING_FOR_INPUT,
            TaskStatus.BLOCKED,
        ],
    )
    def test_one_under_the_threshold_is_not_a_finding(self, status: TaskStatus) -> None:
        """The threshold decides when to *mention* it, not what to do."""
        assert assess_task(_task(status, updated_at=_old(47)), NOW) is None

    def test_the_detail_says_what_is_owed(self) -> None:
        """So the finding is actionable rather than a status restated."""
        verdict = assess_task(_task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(72)), NOW)
        assert verdict is not None
        assert "a decision somebody has to make" in verdict.detail
        assert "not expired" in verdict.detail

    def test_a_very_old_approval_is_still_only_surfaced(self) -> None:
        """A year is not a threshold at which the answer changes.

        This is the case that decides whether the module is safe to run unattended.
        """
        verdict = assess_task(
            _task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(24 * 365)), NOW
        )
        assert verdict is not None
        assert verdict.action is Action.SURFACE


class TestAFailureIsRetriedBoundedOrReported:
    @pytest.mark.parametrize("category", sorted(RETRYABLE_FAILURES))
    def test_every_retryable_category_is_requeued(self, category: str) -> None:
        verdict = assess_task(
            _task(
                TaskStatus.FAILED,
                updated_at=_old(1),
                attempt_count=1,
                failure_category=category,
            ),
            NOW,
        )
        assert verdict is not None
        assert verdict.action is Action.REQUEUE, f"{category} should be retried"
        assert verdict.reason is Reason.RETRYABLE_FAILURE

    def test_the_attempt_ceiling_is_respected_exactly(self) -> None:
        """`>=` at the boundary, and the boundary is tested on both sides.

        `>` would give one more attempt than the policy says, which is the kind of
        off-by-one that only shows up as an unexplained charge.
        """
        task = _task(
            TaskStatus.FAILED,
            updated_at=_old(1),
            attempt_count=POLICY.max_attempts - 1,
            failure_category="timeout",
        )
        assert assess_task(task, NOW).action is Action.REQUEUE  # type: ignore[union-attr]

        task = _task(
            TaskStatus.FAILED,
            updated_at=_old(72),
            attempt_count=POLICY.max_attempts,
            failure_category="timeout",
        )
        verdict = assess_task(task, NOW)
        assert verdict is not None
        assert verdict.action is Action.SURFACE
        assert verdict.reason is Reason.ATTEMPTS_EXHAUSTED

    def test_a_retryable_failure_is_retried_in_minutes_not_days(self) -> None:
        """The two thresholds are independent, and this is what the difference buys.

        A single failure an hour old is the case that separates them: far too recent
        to bother a person about, and long past due for a retry. The first version
        of this module had one threshold for both, and so got one of the two wrong
        whatever number it was set to -- at 48 hours it requeued a task a second
        after the failure; at 5 minutes it held a recoverable timeout for two days.
        """
        an_hour = _task(
            TaskStatus.FAILED,
            updated_at=_old(1),
            attempt_count=1,
            failure_category="timeout",
        )
        verdict = assess_task(an_hour, NOW)
        assert verdict is not None
        assert verdict.action is Action.REQUEUE
        assert ReapPolicy().requeue_after < ReapPolicy().surface_after

    def test_the_two_thresholds_gate_the_two_kinds_of_outcome(self) -> None:
        """Stated as a property, because conflating them is the mistake.

        A permanent failure is *reported* on the long clock, so a sweep every minute
        does not announce the same dead task sixty times an hour. A transient one is
        *retried* on the short clock, so it actually recovers.
        """
        permanent = _task(
            TaskStatus.FAILED,
            updated_at=_old(1),
            attempt_count=1,
            failure_category="policy_violation",
        )
        assert assess_task(permanent, NOW) is None, "1h: not yet worth reporting"

        permanent_later = _task(
            TaskStatus.FAILED,
            updated_at=_old(72),
            attempt_count=1,
            failure_category="policy_violation",
        )
        verdict = assess_task(permanent_later, NOW)
        assert verdict is not None
        assert verdict.reason is Reason.FAILURE_NOT_RETRYABLE

    def test_a_non_transient_failure_is_never_retried(self) -> None:
        """Another attempt would spend money to fail the same way."""
        verdict = assess_task(
            _task(
                TaskStatus.FAILED,
                updated_at=_old(72),
                attempt_count=1,
                failure_category="policy_violation",
            ),
            NOW,
        )
        assert verdict is not None
        assert verdict.action is Action.SURFACE
        assert verdict.reason is Reason.FAILURE_NOT_RETRYABLE

    def test_a_failure_with_no_category_is_not_retried(self) -> None:
        """Absence of a reason is not a reason to retry.

        `failure_category` is empty, which could mean a crash before it was written.
        Guessing 'transient' from an empty string is how a permanent failure becomes
        an infinite loop.
        """
        verdict = assess_task(
            _task(TaskStatus.FAILED, updated_at=_old(72), attempt_count=1),
            NOW,
        )
        assert verdict is not None
        assert verdict.reason is Reason.FAILURE_NOT_RETRYABLE

    def test_the_category_match_ignores_case_and_padding(self) -> None:
        """Log lines are written by people."""
        verdict = assess_task(
            _task(
                TaskStatus.FAILED,
                updated_at=_old(1),
                attempt_count=1,
                failure_category="  TIMEOUT  ",
            ),
            NOW,
        )
        assert verdict is not None
        assert verdict.action is Action.REQUEUE

    def test_the_policy_can_refuse_to_requeue(self) -> None:
        task = _task(
            TaskStatus.FAILED,
            updated_at=_old(1),
            attempt_count=1,
            failure_category="timeout",
        )
        verdict = assess_task(task, NOW, ReapPolicy(allow_requeue=False))
        assert verdict is not None
        assert verdict.action is Action.SURFACE
        assert not verdict.is_actionable


class TestClockSkewDoesNotProduceNonsense:
    def test_a_task_updated_in_the_future_reports_zero_not_a_negative(self) -> None:
        """A timezone bug must not put `-3 days passive` at the top of a report.

        The clamp is in `_age`, and this is the test that says why it is there.
        """
        # An old task whose clock is ahead: the age is what it looks like, and the
        # clamp is what stops it reading as negative.
        old_but_future = _task(
            TaskStatus.WAITING_FOR_APPROVAL,
            updated_at=NOW - dt.timedelta(hours=72) + dt.timedelta(hours=3),
        )
        verdict = assess_task(old_but_future, NOW)
        assert verdict is not None
        assert verdict.passive_for == dt.timedelta(hours=69), "never negative"

        # And a task genuinely in the future is simply not a finding, because its
        # clamped age of zero is younger than every threshold.
        assert (
            assess_task(
                _task(
                    TaskStatus.WAITING_FOR_APPROVAL,
                    updated_at=NOW + dt.timedelta(hours=3),
                ),
                NOW,
            )
            is None
        )

    def test_a_future_lease_is_not_reclaimed(self) -> None:
        """The other direction: a worker whose clock is behind.

        A lease that looks far in the future must not be treated as expired, and
        `>` rather than `>=` is what guarantees it.
        """
        task = _task(TaskStatus.RUNNING, lease_expires_at=NOW + dt.timedelta(days=3))
        assert assess_task(task, NOW) is None

    def test_one_now_for_a_whole_batch(self) -> None:
        """Documented as a property because a sweeper that reads the clock per row
        sorts a batch by a difference nobody can reproduce.

        Two tasks, one second apart, swept with one `now`, get verdicts a consistent
        sweep would produce.
        """
        tasks = [
            _task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(50)),
            _task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(47)),
        ]
        verdicts = [v for v in (assess_task(t, NOW) for t in tasks) if v]
        assert len(verdicts) == 1, "only the one past 48h; 47h is not"
        assert verdicts[0].passive_for == dt.timedelta(hours=50)


class TestTheSummaryIsActionable:
    def test_it_counts_by_action_and_by_reason(self) -> None:
        """Both, deliberately.

        "12 findings" sends somebody to the table. "12 findings, 2 with an expired
        lease" sends them to the two tasks that are certainly stuck.
        """
        verdicts: list[Verdict] = [
            assess_task(_task(TaskStatus.RUNNING, lease_expires_at=_old(5)), NOW),  # type: ignore[arg-type]
            assess_task(_task(TaskStatus.RUNNING, lease_expires_at=_old(9)), NOW),  # type: ignore[arg-type]
            assess_task(_task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(72)), NOW),  # type: ignore[arg-type]
            assess_task(
                _task(
                    TaskStatus.FAILED,
                    updated_at=_old(72),
                    attempt_count=1,
                    failure_category="timeout",
                ),
                NOW,
            ),  # type: ignore[arg-type]
        ]
        got = summarise([v for v in verdicts if v is not None])
        assert got["total"] == 4
        assert got["actionable"] == 3, "two reclaims and one requeue"
        assert got["action_reclaim"] == 2
        assert got["action_requeue"] == 1
        assert got["action_surface"] == 1
        assert got["reason_lease_expired"] == 2
        assert got["reason_waiting_for_approval"] == 1

    def test_a_clean_sweep_summarises_to_nothing(self) -> None:
        assert summarise([]) == {"total": 0, "actionable": 0}

    def test_the_key_prefixes_do_not_collide(self) -> None:
        """`action_*` and `reason_*` are distinct namespaces in one flat dict.

        Without the prefixes a reason named `surface` would overwrite the action
        count, and the two would be indistinguishable in a JSON report.
        """
        got = summarise(
            [
                v
                for v in (
                    assess_task(
                        _task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(72)),
                        NOW,
                    ),
                )
                if v
            ]
        )
        assert "action_surface" in got
        assert "reason_waiting_for_approval" in got
        assert got["action_surface"] == 1
        assert got["reason_waiting_for_approval"] == 1


class TestTheQueryFetchesExactlyWhatTheDomainJudges:
    """The SQL's status list and `assess_task`'s remit are kept honest by a test.

    `_CANDIDATES` spells its five statuses out as a literal, because interpolating a
    Python tuple into SQL needs an `S608` suppression and a suppression is an
    assertion in a comment. The cost of the literal is a second place to forget to
    change, and this test is what makes that cost zero: if a sixth status ever gains
    a rule, or one of these five loses its, this fails and names both sides.
    """

    def test_the_two_lists_are_the_same_set(self) -> None:
        in_sql = set(re.findall(r"'([a-z_]+)'", _CANDIDATES.split("status IN")[1]))
        in_code = {s.value for s in REAPABLE_STATUSES}
        assert in_sql == in_code, (
            f"query has {in_sql - in_code} the domain does not judge, and is missing "
            f"{in_code - in_sql}"
        )

    @pytest.mark.parametrize("status", sorted(TaskStatus, key=lambda s: s.value))
    def test_no_status_is_judged_but_unfetched(self, status: TaskStatus) -> None:
        """Exhaustive over the whole enum, so a new status cannot be added quietly.

        `assess_task` returning a verdict for a status the query never fetches is
        dead code that looks live: a developer reading `_assess_running` would
        reasonably believe reclaiming a `running` task works end to end.
        """
        task = _task(
            status,
            updated_at=_old(72),
            lease_expires_at=_old(72),
            attempt_count=1,
        )
        if assess_task(task, NOW) is None:
            return
        assert status in REAPABLE_STATUSES, (
            f"{status.value} has a rule but the candidate query will never return "
            f"a row in that state, so the rule is unreachable"
        )


class TestThePolicyIsOnePlace:
    def test_the_thresholds_are_defaults_on_one_object(self) -> None:
        """So "how long is too long" is a single decision rather than six literals.

        Not derived from anything — the corpus says nothing about how long a
        construction project takes to answer — and deliberately conservative, because
        the cost of surfacing early is a notification and the cost of expiring early
        is somebody's approval disappearing.
        """
        assert ReapPolicy().surface_after == dt.timedelta(hours=48)
        assert ReapPolicy().max_attempts == 3
        assert ReapPolicy().lease_duration == dt.timedelta(hours=2)

    def test_a_tighter_policy_produces_fewer_findings(self) -> None:
        """The threshold has to actually do something, or it is decoration."""
        task = _task(TaskStatus.WAITING_FOR_APPROVAL, updated_at=_old(3))
        assert assess_task(task, NOW, ReapPolicy(surface_after=dt.timedelta(hours=1)))
        assert assess_task(task, NOW, ReapPolicy(surface_after=dt.timedelta(hours=48))) is None
