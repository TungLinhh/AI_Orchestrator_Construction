"""The queue must not offer an action the platform will refuse.

Measured, on a tenant a person was looking at: two `hr.open_headcount` requests,
a day old, each rendering an **Approve** button, each answering

    Could not approve: approval apr_01m3pt… expired at 2026-09-29T15:46:06

on click. Two faults, and only one of them was the button:

* `PendingApproval` carried no `expires_at`, so the queue **could not tell** a row
  that can still be decided from one that cannot;
* `ApprovalService.expire_stale` had never been called by anything, so the rows
  were still `pending` in the table as well as on the page.

Both are asserted here. The second is the one that will come back: an approval is
a deadline, and a deadline nobody owns is a row that lies forever.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.role_views import hitl_inbox
from ai_orchestrator.approvals.service import ApprovalService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.models import Approval, Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    # **No commit.** The tenant binding is `SET LOCAL`, so a commit ends it and
    # the next insert is rejected by row-level security -- which is precisely the
    # trap `Database.committing_tenant_session` was added for. This file's reads
    # and writes share the fixture's transaction on purpose.
    return tenant


async def _approval(  # type: ignore[no-untyped-def]
    seeded, *, ttl_s: int, title: str = "việc cần duyệt"
):
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    tasks = TaskRepository(seeded.session, seeded.organization_id)
    # The goal differs per request. Two tasks with the *same* goal are refused as
    # duplicates, which is the guard working: it is also why the bug it sits next
    # to cannot be reproduced by accident.
    task = await tasks.create(
        title=title,
        goal=f"một việc cần người quyết: {title}",
        task_type="execution",
        requester_type="human",
    )
    service = ApprovalService(seeded.session, seeded.organization_id)
    approval = await service.create(
        _request(
            seeded=seeded,
            task_id=str(task.id),
            ttl_s=ttl_s,
        )
    )
    await seeded.session.flush()
    return task, approval


def _request(*, seeded, task_id: str, ttl_s: int):  # type: ignore[no-untyped-def]
    from ai_orchestrator.approvals.service import ApprovalRequest
    from ai_orchestrator.domain.enums import EffectClass, RiskLevel

    return ApprovalRequest(
        organization_id=seeded.organization_id,
        task_id=task_id,
        action_type="hr.open_headcount",
        action_payload={"headcount": 2},
        effect_class=EffectClass.MUTATE_INTERNAL,
        risk_level=RiskLevel.MEDIUM,
        reason="cần người duyệt",
        requested_by="agt_01m3h7j49ffhdd9wyc8v7w046s",
        requested_by_type=ActorType.AGENT,
        ttl_seconds=ttl_s,
    )


class TestTheQueueKnowsWhatItCannotDo:
    async def test_a_live_approval_is_actionable(self, seeded) -> None:  # type: ignore[no-untyped-def]
        _task, approval = await _approval(seeded, ttl_s=3600)
        now = dt.datetime.now(tz=dt.UTC)

        rows = await hitl_inbox(seeded.session, organization_id=seeded.organization_id, now=now)
        row = next(r for r in rows if r.approval_id == str(approval.id))
        payload = row.as_dict(now=now)

        assert payload["actionable"] is True
        assert payload["expired"] is False
        assert payload["expires_at"]

    async def test_an_expired_approval_is_not_actionable(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The fault. `approve` on this row raises, so the button must not exist."""
        _task, approval = await _approval(seeded, ttl_s=1)
        later = dt.datetime.now(tz=dt.UTC) + dt.timedelta(hours=2)

        rows = await hitl_inbox(seeded.session, organization_id=seeded.organization_id, now=later)
        row = next(r for r in rows if r.approval_id == str(approval.id))
        payload = row.as_dict(now=later)

        assert payload["actionable"] is False
        assert payload["expired"] is True

    async def test_the_row_carries_the_deadline_at_all(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The root of it. Without `expires_at` there is nothing to compute from,
        and every downstream surface has to guess."""
        _task, approval = await _approval(seeded, ttl_s=3600)
        now = dt.datetime.now(tz=dt.UTC)

        rows = await hitl_inbox(seeded.session, organization_id=seeded.organization_id, now=now)
        row = next(r for r in rows if r.approval_id == str(approval.id))

        assert row.expires_at is not None
        assert row.expired(now) is False


class TestTheRowIsIdentifiable:
    async def test_two_requests_for_the_same_action_on_different_tasks_differ(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The queue showed two identical lines for `hr.open_headcount`.

        They were two different tasks from two different runs, and the row carried
        no task title, so a person could not tell "asked twice" from "asked about
        two things" -- and the second is a different decision.
        """
        _first, a = await _approval(seeded, ttl_s=3600, title="Tuyển trưởng phòng Mua sắm")
        _second, b = await _approval(seeded, ttl_s=3600, title="Tuyển trưởng phòng Tài chính")
        now = dt.datetime.now(tz=dt.UTC)

        rows = {
            r.approval_id: r.as_dict(now=now)
            for r in await hitl_inbox(
                seeded.session, organization_id=seeded.organization_id, now=now
            )
        }
        first, second = rows[str(a.id)], rows[str(b.id)]

        assert first["action_type"] == second["action_type"], "same action, as in the bug"
        assert first["task_title"] == "Tuyển trưởng phòng Mua sắm"
        assert second["task_title"] == "Tuyển trưởng phòng Tài chính"
        assert first["task_title"] != second["task_title"]


def test_something_actually_calls_the_sweep() -> None:
    """The wiring, asserted on the source.

    A deadline nobody owns is a row that lies forever, and `expire_stale` sat
    uncalled for the life of the approval service. Importing the script to prove
    it would prove nothing -- importing is not calling -- so this reads the source
    and checks the call is there, next to the one that reaps executions and next to
    the `make page` target that runs it.
    """
    from pathlib import Path

    spine = Path(__file__).resolve().parents[2] / "scripts" / "sweep_stranded.py"
    assert "expire_stale" in spine.read_text(), (
        "nothing calls ApprovalService.expire_stale, so every approval past its "
        "deadline stays `pending` and is offered to a person who cannot answer it"
    )

    makefile = Path(__file__).resolve().parents[2] / "Makefile"
    assert "sweep_stranded" in makefile.read_text(), (
        "the sweep is not run by any target, so calling it in the script is not "
        "the same as it happening"
    )


class TestTheSweepRuns:
    async def test_expire_stale_flips_the_status_and_nothing_else_calls_it(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The second half of the fault: a deadline nobody owns.

        `expire_stale` has existed since the approval service was written and its
        docstring says "Run by a scheduled task". There is no scheduled task, which
        is why two approvals sat in the queue for a day offering a button that could
        only fail. `scripts/sweep_stranded.py` calls it, and `make page` runs that
        on every start.
        """
        _task, approval = await _approval(seeded, ttl_s=1)
        # Backdate it, because a sweep run a second later finds nothing to do.
        await seeded.session.execute(
            Approval.__table__.update()
            .where(Approval.id == str(approval.id))
            .values(expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1))
        )
        await seeded.session.flush()

        # Still `pending`: nothing has swept, so the row is a lie in the table.
        before = (
            await seeded.session.execute(
                select(Approval.status).where(Approval.id == str(approval.id))
            )
        ).scalar_one()
        assert before == "pending", "the premise: a deadline nobody has acted on"

        service = ApprovalService(seeded.session, seeded.organization_id)
        expired = await service.expire_stale()
        await seeded.session.flush()

        assert str(approval.id) in expired
        after = (
            await seeded.session.execute(
                select(Approval.status).where(Approval.id == str(approval.id))
            )
        ).scalar_one()
        assert after == "expired"

    async def test_a_decision_on_an_expired_row_is_refused(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Which is why the button must not be there. The refusal is correct and
        the page was the thing that was wrong."""
        from ai_orchestrator.domain.errors import PreconditionError

        _task, approval = await _approval(seeded, ttl_s=1)
        await seeded.session.execute(
            Approval.__table__.update()
            .where(Approval.id == str(approval.id))
            .values(expires_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=1))
        )
        await seeded.session.flush()

        service = ApprovalService(seeded.session, seeded.organization_id)
        with pytest.raises((PreconditionError, Exception)):
            await service.decide(
                str(approval.id),
                approver=Actor(id="usr_local", kind=ActorType.HUMAN, is_privileged_human=True),
                approve=True,
            )
