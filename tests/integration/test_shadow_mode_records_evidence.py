"""Shadow mode writes what the promotion gate already waits for.

`agent_shadow_runs` had its columns and `domain/promotion.py` had its thresholds
before anything wrote to either. These tests are on the writer, because the reader was
already covered and a reader with no writer reports a rate of zero forever — which
looks like "the model disagrees with everybody" rather than like "nothing is
recorded".

The three that would each have produced a false go-live answer on their own:

* a duplicate comparison **appending** rather than replacing, which moves the
  agreement rate toward whatever the duplicate said;
* an uncomparable pair **skipped** rather than recorded, which silently improves every
  rate it feeds;
* four perfect runs in one day reported as ready, which is 100% and no elapsed time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.shadow import (
    go_live_readiness,
    observations,
    record_comparison,
)
from ai_orchestrator.domain.autonomy import Policy
from ai_orchestrator.domain.promotion import (
    Block,
    PromotionPolicy,
    VersionFacts,
    VersionState,
    evaluate,
)
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

HUMAN = {
    "verdicts": {
        "Minh Châu 3.600.000": "approve",
        "Văn phòng pháp chế 32.000.000": "conditional",
        "Kiên Phát 18.500.000": "reject",
    },
    "reason": "the lease contract is missing for the third claim",
}
SAME = dict(HUMAN)


async def _compare_over_days(
    tenant,  # type: ignore[no-untyped-def]
    *,
    days: int,
    model_for,
    end: datetime = NOW,
) -> str:
    """Record one comparison per day, each on its own task.

    The writer's dedup key is (task, version). Two observations of the *same* version
    on the *same* task are one observation measured twice, and recording both would
    move the agreement rate. So a set of distinct observations needs distinct tasks,
    which is also how a real deployment produces them.
    """
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    org = str(tenant.organization_id)
    agent = await _agent_id(tenant.session, org)
    tasks = TaskRepository(tenant.session, org)
    for day in range(days):
        when = end - timedelta(days=days - 1 - day)
        task = await tasks.create(
            title=f"Shadow run {when:%Y-%m-%d}",
            # Distinct goals, because these are distinct pieces of work: the task
            # repository refuses an equivalent active task, which is right -- one
            # expense review repeated is one task, not forty.
            goal=f"Decide the three expense claims of {when:%Y-%m-%d} against the policy",
            task_type="execution",
            requester_type="human",
        )
        await record_comparison(
            tenant.session,
            org,
            agent_id=agent,
            task_id=str(task.id),
            procedure_version_id=None,
            model_answer=model_for(day),
            human_answer=HUMAN,
            observed_on=when,
        )
    return org


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def _agent_id(session, org: str) -> str:  # type: ignore[no-untyped-def]
    from ai_orchestrator.persistence.models import Agent

    return str(
        (
            await session.execute(
                select(Agent).where(Agent.organization_id == org, Agent.name == "Finance Agent")
            )
        )
        .scalar_one()
        .id
    )


class TestItWritesTheComparison:
    async def test_an_agreeing_pair_is_recorded_as_agreeing(self, seeded) -> None:  # type: ignore[no-untyped-def]
        org = str(seeded.organization_id)
        outcome = await record_comparison(
            seeded.session,
            org,
            agent_id=await _agent_id(seeded.session, org),
            task_id=None,
            procedure_version_id=None,
            model_answer=SAME,
            human_answer=HUMAN,
            observed_on=NOW,
        )
        assert outcome.recorded
        assert outcome.agreed is True

        rows = await observations(seeded.session, org)
        assert len(rows) == 1
        assert rows[0].agreed is True
        assert rows[0].divergence == ""
        assert "Minh Châu" in rows[0].would_have_decided
        assert rows[0].would_have_decided == rows[0].actually_decided

    async def test_a_disagreement_is_recorded_with_the_reason(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A disagreement nobody can act on is not a finding."""
        org = str(seeded.organization_id)
        model = {
            "verdicts": {**HUMAN["verdicts"], "Văn phòng pháp chế 32.000.000": "approve"},
            "reason": HUMAN["reason"],
        }
        await record_comparison(
            seeded.session,
            org,
            agent_id=await _agent_id(seeded.session, org),
            task_id=None,
            procedure_version_id=None,
            model_answer=model,
            human_answer=HUMAN,
            observed_on=NOW,
        )
        rows = await observations(seeded.session, org)
        assert rows[0].agreed is False
        assert "Văn phòng pháp chế" in rows[0].divergence
        assert "approve" in rows[0].divergence and "escalate" in rows[0].divergence

    async def test_the_two_answers_are_stored_as_given(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Not normalised into agreement.

        The stored text is what was compared, so a reader can see the model's own words
        and check the comparison rather than trusting the flag.
        """
        org = str(seeded.organization_id)
        await record_comparison(
            seeded.session,
            org,
            agent_id=await _agent_id(seeded.session, org),
            task_id=None,
            procedure_version_id=None,
            model_answer=SAME,
            human_answer=HUMAN,
            observed_on=NOW,
        )
        row = (await observations(seeded.session, org))[0]
        assert "conditional" in row.would_have_decided
        assert "conditional" in row.actually_decided


class TestWhatWouldCorruptTheRate:
    async def test_recording_the_same_observation_twice_does_not_inflate_it(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Re-measuring must not move the answer.

        An appended duplicate raises the numerator and the denominator together, so the
        rate drifts toward whatever the duplicate said. Replacing keeps the rate a
        function of distinct observations.
        """
        org = str(seeded.organization_id)
        agent = await _agent_id(seeded.session, org)
        for _ in range(3):
            await record_comparison(
                seeded.session,
                org,
                agent_id=agent,
                task_id=None,
                procedure_version_id=None,
                model_answer=SAME,
                human_answer=HUMAN,
                observed_on=NOW,
            )
        assert len(await observations(seeded.session, org)) == 1

    async def test_an_uncomparable_pair_is_still_recorded(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Skipping it would silently improve every rate it feeds.

        A run the model could not be compared on is evidence about the model. Dropping
        it makes a broken model look better, which is the opposite of the point.
        """
        org = str(seeded.organization_id)
        outcome = await record_comparison(
            seeded.session,
            org,
            agent_id=await _agent_id(seeded.session, org),
            task_id=None,
            procedure_version_id=None,
            model_answer={"verdicts": {"only one claim": "approve"}},
            human_answer=HUMAN,
            observed_on=NOW,
        )
        assert outcome.recorded
        assert outcome.agreed is None
        assert "could not be compared" in outcome.uncomparable

        rows = await observations(seeded.session, org)
        assert len(rows) == 1
        assert rows[0].agreed is False
        assert "could not be compared" in rows[0].divergence

    async def test_a_missing_model_answer_is_recorded_not_dropped(self, seeded) -> None:  # type: ignore[no-untyped-def]
        org = str(seeded.organization_id)
        await record_comparison(
            seeded.session,
            org,
            agent_id=await _agent_id(seeded.session, org),
            task_id=None,
            procedure_version_id=None,
            model_answer=None,
            human_answer=HUMAN,
            observed_on=NOW,
        )
        rows = await observations(seeded.session, org)
        assert len(rows) == 1
        assert rows[0].agreed is False


class TestTheGoLiveReport:
    async def test_a_few_perfect_runs_are_not_ready(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """100% is a rate. Four weeks is the evidence. Both, or neither."""
        org = await _compare_over_days(seeded, days=10, model_for=lambda _d: SAME)
        report = await go_live_readiness(seeded.session, org, now=NOW)
        assert report.agreement == 1.0
        assert report.runs == 10
        assert not report.ready
        assert any("day(s) of parallel running" in m for m in report.missing)

    async def test_nothing_recorded_is_not_ready_and_says_so(self, seeded) -> None:  # type: ignore[no-untyped-def]
        report = await go_live_readiness(seeded.session, str(seeded.organization_id), now=NOW)
        assert not report.ready
        assert report.runs == 0
        assert any("no shadow runs" in m for m in report.missing)

    async def test_a_disagreement_blocks_on_the_rate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        wrong = {
            "verdicts": {**HUMAN["verdicts"], "Kiên Phát 18.500.000": "approve"},
            "reason": HUMAN["reason"],
        }
        org = await _compare_over_days(
            seeded, days=40, model_for=lambda d: wrong if d % 4 == 0 else SAME
        )
        report = await go_live_readiness(seeded.session, org, now=NOW)
        assert report.observed_days >= 28
        assert not report.ready
        assert any("not a rounding error" in m for m in report.missing)

    async def test_four_weeks_of_agreement_is_ready(self, seeded) -> None:  # type: ignore[no-untyped-def]
        org = await _compare_over_days(seeded, days=35, model_for=lambda _d: SAME)
        report = await go_live_readiness(seeded.session, org, now=NOW)
        assert report.ready, report.missing
        assert report.observed_days >= 28


class TestItFeedsTheGateThatWasWaiting:
    async def test_the_recorded_runs_satisfy_the_promotion_gate(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The point of writing it: `promotion.evaluate` has been waiting for this.

        Not wired into it — the gate takes counts a caller supplies — but the numbers
        the writer produces are exactly the numbers the gate blocks on, so this asserts
        the two agree rather than asserting one in isolation.
        """
        org = await _compare_over_days(seeded, days=30, model_for=lambda _d: SAME)
        rows = await observations(seeded.session, org)
        agreements = sum(1 for r in rows if r.agreed)

        verdict = evaluate(
            VersionFacts(
                version_id="pv_test",
                status=VersionState.SHADOW.value,
                shadow_runs=len(rows),
                shadow_agreements=agreements,
                autonomy_ceiling="l2_parent_review",
                action_classes=("mutate_internal",),
            ),
            policies={
                "mutate_internal": Policy(
                    action_class="mutate_internal",
                    max_level="l2_parent_review",
                    is_hard_block=False,
                )
            },
            policy=PromotionPolicy(min_runs=5, min_agreement=0.80),
        )
        assert Block.NOT_ENOUGH_RUNS not in verdict.blocks
        assert Block.AGREEMENT_TOO_LOW not in verdict.blocks
