"""The learning loop, end to end, against a live database.

The domain rules are in `tests/unit/test_autonomy_and_promotion.py`. This file is
about the three things a unit test cannot see, and the first of them is the reason
this file exists at all:

* **The loop actually closes.** Propose → shadow → record runs → promote, and the
  promoted version is the one a query returns. Before migration `0016` and this
  module, `load_approved()` had no callers and no `procedures` table existed — the
  platform could learn, propose, get an approval, and then do nothing. This is the
  test that says it no longer cannot.
* **The refusals are refusals.** Every gate is exercised by *attempting* the
  promotion, not by calling the rule directly, so a constraint that stopped firing
  would fail here.
* **The log records the "no".** A promotion that refused and wrote nothing is
  invisible, and the interesting row in an AI decision log is the refusal.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from ai_orchestrator.application.procedure_operations import (
    ShadowRunRecord,
    promote_version,
    propose_version,
    record_shadow_run,
)
from ai_orchestrator.domain.promotion import Block, PromotionPolicy
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 28, 9, 0, tzinfo=dt.UTC)

#: `autonomy_policies` is seeded per organisation by `make seed-process`, and a test
#: tenant has none. These are the same six rows from Tập 1 §5.3, written here so the
#: gate is tested against the dossier rather than against whatever the seeder
#: currently produces.
DOSSIER_POLICIES = (
    ("hr_personnel_decision", "L2", False),
    ("financial_commitment", "L3", False),
    ("safety_conclusion", "L1", True),
    ("supplier_risk_flagged", "L1", True),
    ("contract_signature", "L3", False),
    ("routine_classification", "L4", False),
)


@pytest.fixture
async def dossier_policies(tenant: Tenant) -> None:
    """Seed the six Tập 1 §5.3 policies into this test's tenant.

    The ids are derived from the **organization**, not from the action class alone.
    `tenant.run()` commits whatever is in the session, and this fixture's inserts are
    in the same session -- so they survive into the next test even though the `tenant`
    fixture's teardown rolls back. An id like `aut_safety_concl` is therefore inserted
    again by the next test and collides on `pk_autonomy_policies`, in a test that has
    nothing to do with policies. This is the same trap as the ULID timestamp slice in
    F111: an identifier that is not unique is a collision waiting for a second caller.
    """
    org = tenant.organization_id[-8:]
    for action_class, max_level, hard in DOSSIER_POLICIES:
        await tenant.session.execute(
            text(
                "INSERT INTO autonomy_policies (id, organization_id, action_class, "
                "name_vi, max_level, is_hard_block, rationale) "
                "VALUES (:i, :o, :c, :n, CAST(:m AS varchar(2)), :h, 'Tập 1 §5.3')"
            ),
            {
                "i": f"aut_{org}_{action_class[:10]}",
                "o": tenant.organization_id,
                "c": action_class,
                "n": action_class,
                "m": max_level,
                "h": hard,
            },
        )


async def _procedure(tenant: Tenant, code: str = "SOP-HR-01") -> str:
    """A procedure in this tenant, with an id that is unique across tenants.

    Org-scoped for the reason `dossier_policies` documents: `tenant.run()` commits
    fixture inserts, so a fixed id collides with the previous test's row. There is no
    `DELETE` first, because deleting the previous tenant's row to make room would be a
    fixture reaching across a tenancy boundary to work around its own key scheme.
    """
    procedure_id = f"prc_{tenant.organization_id[-8:]}_{code.lower()}"
    await tenant.session.execute(
        text(
            "INSERT INTO procedures (id, organization_id, code, name) "
            "VALUES (:i, :o, :c, 'HR agent SOP')"
        ),
        {"i": procedure_id, "o": tenant.organization_id, "c": code},
    )
    return procedure_id


async def _shadow_runs(tenant: Tenant, version_id: str, agreed: int, total: int) -> None:
    """Record `total` runs of which `agreed` matched reality."""
    for i in range(total):
        # Bound as a default argument, not closed over. `agrees` is rebound on every
        # iteration, so a plain closure makes all `total` runs record the *last*
        # value -- which is right by accident when every run agrees and completely
        # wrong the moment the counts differ, and the aggregate would then disagree
        # with the rows behind it.
        record = ShadowRunRecord(
            would_have_decided="propose to parent",
            actually_decided="propose to parent" if i < agreed else "escalate",
            agreed=i < agreed,
            divergence="" if i < agreed else "would have stopped the payroll run",
        )
        await tenant.run(
            lambda s, r=record: record_shadow_run(
                s,
                organization_id=tenant.organization_id,
                observed_at=NOW,
                version_id=version_id,
                run=r,
            )
        )


async def _live_version(tenant: Tenant, procedure_id: str) -> str | None:
    return (
        await tenant.session.execute(
            text("SELECT current_version_id FROM procedures WHERE id = :p"),
            {"p": procedure_id},
        )
    ).scalar_one()


async def _status(tenant: Tenant, version_id: str) -> str:
    return (
        await tenant.session.execute(
            text("SELECT status FROM procedure_versions WHERE id = :i"),
            {"i": version_id},
        )
    ).scalar_one()


async def _decisions(tenant: Tenant) -> list[dict[str, object]]:
    rows = (
        (
            await tenant.session.execute(
                text(
                    "SELECT decision, rationale, policy_rule FROM ai_decision_log "
                    "ORDER BY occurred_at, id"
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


class TestTheLoopCloses:
    async def test_propose_shadow_record_promote(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """The whole path, and the last step is a change in behaviour.

        The final assertion is the one that matters: `procedures.current_version_id`
        points at the new version, so a query that asks "which version is live" gets
        the new one. Before this existed there was no such question to ask.
        """
        procedure_id = await _procedure(tenant)
        approval = await _approval(tenant)

        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="Escalate payroll changes to the parent before applying.",
                autonomy_ceiling="L2",
                rationale="four payroll runs escalated when they should not have",
                source="agent_proposal",
                source_actor="agent:hr",
                proposal_id="prp_0000000000000000000000000000",
                approval_id=approval,
            )
        )
        # A newly written version is in `shadow` and the procedure points at nothing.
        assert await _status(tenant, version_id) == "shadow"
        assert await _live_version(tenant, procedure_id) is None

        await _shadow_runs(tenant, version_id, agreed=18, total=20)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )

        assert outcome.promoted
        assert await _status(tenant, version_id) == "active"
        assert await _live_version(tenant, procedure_id) == version_id

    async def test_a_second_promotion_supersedes_the_first(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """Replacing a live version is the ordinary case, not an edge case.

        And the order matters: supersede first, because the partial unique index
        permits one `active` version per procedure and the reverse order is refused.
        """
        procedure_id = await _procedure(tenant)
        approval = await _approval(tenant)

        first = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="first",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, first, agreed=20, total=20)
        await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=first,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert await _live_version(tenant, procedure_id) == first

        second = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="second",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, second, agreed=20, total=20)
        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=second,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )

        assert outcome.superseded == (first,)
        assert await _status(tenant, first) == "superseded"
        assert await _status(tenant, second) == "active"
        assert await _live_version(tenant, procedure_id) == second
        assert approval  # the approval helper ran; kept for symmetry with the loop test


class TestWhatThePromotionRefuses:
    async def test_too_few_shadow_runs(self, tenant: Tenant, dossier_policies: None) -> None:
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=3, total=3)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert not outcome.promoted
        assert outcome.verdict.blocks == (Block.NOT_ENOUGH_RUNS,)
        assert await _status(tenant, version_id) == "shadow", "still in shadow"
        assert await _live_version(tenant, procedure_id) is None

    async def test_an_agreement_rate_below_the_floor(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=5, total=10)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert outcome.verdict.blocks == (Block.AGREEMENT_TOO_LOW,)
        assert "50%" in outcome.verdict.reasons[0]

    async def test_a_hard_blocked_action_class_is_never_promoted(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """Tập 1 §5.3's absolute prohibition, through the whole write path.

        Twenty perfect shadow runs and a generous ceiling, and it still refuses —
        because there is no level at which automating a labour-safety conclusion is
        permitted, so there is no level to cap it at.
        """
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="conclude the site is safe",
                autonomy_ceiling="L4",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=20, total=20)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("safety_conclusion",),
            )
        )
        assert not outcome.promoted
        assert Block.AUTONOMY_REFUSED in outcome.verdict.blocks
        assert outcome.verdict.permitted_level is None
        assert any("absolute prohibition" in r for r in outcome.verdict.reasons)

    async def test_an_action_class_nobody_classified(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=20, total=20)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("mystery_action",),
            )
        )
        assert not outcome.promoted
        assert Block.UNCLASSIFIED_ACTION in outcome.verdict.blocks

    async def test_a_version_claiming_more_than_its_actions_permit(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """`hr_personnel_decision` permits L2; the version claims L4.

        Silently lowering it would promote a document nobody approved at the level it
        was written for, so it refuses and says which level would have worked.
        """
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L4",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=20, total=20)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert Block.AUTONOMY_REFUSED in outcome.verdict.blocks
        assert "at most L2" in outcome.verdict.reasons[0]

    async def test_an_author_whose_kill_switch_is_thrown(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """A version proposed by a killed agent is not promoted.

        The version is left in `shadow` rather than rejected, because what was learned
        before the switch was thrown is still worth reading.
        """
        procedure_id = await _procedure(tenant)
        agent_id = await _agent(tenant)
        approval = await _approval(tenant)
        await tenant.session.execute(
            text(
                "UPDATE agents SET kill_switch = true, kill_reason = 'producing "
                "refusals with no rationale', killed_at = :now WHERE id = :i"
            ),
            {"i": agent_id, "now": NOW},
        )
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
                source="agent_proposal",
                source_actor=agent_id,
                proposal_id="prp_00000000000000000000000000k1",
                approval_id=approval,
            )
        )
        await _shadow_runs(tenant, version_id, agreed=20, total=20)

        outcome = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert outcome.verdict.blocks == (Block.AUTHOR_KILLED,)
        assert await _status(tenant, version_id) == "shadow", "kept, not deleted"

    async def test_a_version_that_is_not_a_real_row(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """A missing version is a question about nothing, not a refusal.

        Refusing to promote something that does not exist would be indistinguishable
        from a gate that works, which is the failure mode the whole refusal-is-not-an
        error design exists to avoid.
        """
        with pytest.raises(ValueError, match="no version"):
            await tenant.run(
                lambda s: promote_version(
                    s,
                    organization_id=tenant.organization_id,
                    version_id="prv_does_not_exist",
                    decided_at=NOW,
                    action_classes=("hr_personnel_decision",),
                )
            )


class TestARefusedPromotionStillLeavesItsEvidence:
    async def test_the_runs_survive_a_refusal(self, tenant: Tenant, dossier_policies: None) -> None:
        """Otherwise the same refusal is re-derived from nothing next time.

        A gate that consumed its evidence on the way out would need the shadow runs
        re-recorded before anybody could retry, and the operator would have no way to
        know that.
        """
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=2, total=2)

        for _ in range(2):
            outcome = await tenant.run(
                lambda s: promote_version(
                    s,
                    organization_id=tenant.organization_id,
                    version_id=version_id,
                    decided_at=NOW,
                    action_classes=("hr_personnel_decision",),
                )
            )
            assert not outcome.promoted

        rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM agent_shadow_runs WHERE procedure_version_id = :v"),
                {"v": version_id},
            )
        ).scalar_one()
        assert rows == 2, "the evidence is still there to be re-evaluated against"

    async def test_a_tighter_policy_refuses_what_a_looser_one_allows(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """So `min_agreement` is a live decision rather than a constant in a comment."""
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=9, total=10)

        lenient = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        assert lenient.promoted

        # A second version with the same evidence, refused by a stricter policy.
        second = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b2",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, second, agreed=9, total=10)
        strict = await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=second,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
                policy=PromotionPolicy(min_agreement=0.95),
            )
        )
        assert not strict.promoted
        assert Block.AGREEMENT_TOO_LOW in strict.verdict.blocks


class TestTheDecisionLogKeepsTheRefusals:
    async def test_a_refused_promotion_is_written_down(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """The interesting row in an AI decision log is the one where it declined.

        A log of what the system *did* teaches you what it can do. A log of what it
        declined to do, and why, is the only thing that lets anybody audit whether the
        autonomy levels are set correctly.
        """
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=1, total=10)
        await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )

        decisions = await _decisions(tenant)
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "refused"
        assert "agreed" in str(decisions[0]["rationale"]).lower()
        assert decisions[0]["policy_rule"].startswith("promotion.")

    async def test_a_successful_promotion_is_written_down_too(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        await _shadow_runs(tenant, version_id, agreed=20, total=20)
        await tenant.run(
            lambda s: promote_version(
                s,
                organization_id=tenant.organization_id,
                version_id=version_id,
                decided_at=NOW,
                action_classes=("hr_personnel_decision",),
            )
        )
        decisions = await _decisions(tenant)
        assert [d["decision"] for d in decisions] == ["acted"]
        assert decisions[0]["policy_rule"] == "promotion.all_gates_passed"


class TestTheServiceRefusesBadInputRatherThanTheDatabase:
    async def test_a_naive_timestamp_is_refused(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """At the boundary, with a message that names the column type.

        Without it the failure is a `TypeError` from inside a subtraction, several
        frames below the caller that passed the bad value.
        """
        procedure_id = await _procedure(tenant)
        with pytest.raises(ValueError, match="timezone-aware"):
            await tenant.run(
                lambda s: propose_version(
                    s,
                    organization_id=tenant.organization_id,
                    procedure_id=procedure_id,
                    proposed_at=dt.datetime(2026, 9, 28, 9, 0),
                    body="b",
                )
            )

    async def test_an_agent_proposal_without_an_approval_is_refused_in_the_service(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """And the table refuses it too, which is the invariant test.

        The service check exists so the caller gets a sentence; the `CHECK` exists so
        a second writer cannot get around it.
        """
        procedure_id = await _procedure(tenant)
        with pytest.raises(ValueError, match="cite the approval"):
            await tenant.run(
                lambda s: propose_version(
                    s,
                    organization_id=tenant.organization_id,
                    procedure_id=procedure_id,
                    proposed_at=NOW,
                    body="b",
                    source="agent_proposal",
                    proposal_id="prp_x",
                    approval_id=None,
                )
            )

    async def test_a_disagreement_with_no_explanation_is_refused(
        self, tenant: Tenant, dossier_policies: None
    ) -> None:
        """Promotion is a rate, and a rate over unexplained disagreements is not
        actionable — so it is refused at the moment of recording, not months later."""
        procedure_id = await _procedure(tenant)
        version_id = await tenant.run(
            lambda s: propose_version(
                s,
                organization_id=tenant.organization_id,
                procedure_id=procedure_id,
                proposed_at=NOW,
                body="b",
                autonomy_ceiling="L2",
            )
        )
        with pytest.raises(ValueError, match="must say how"):
            await tenant.run(
                lambda s: record_shadow_run(
                    s,
                    organization_id=tenant.organization_id,
                    observed_at=NOW,
                    version_id=version_id,
                    run=ShadowRunRecord(
                        would_have_decided="a",
                        actually_decided="b",
                        agreed=False,
                        divergence="",
                    ),
                )
            )


async def _approval(tenant: Tenant) -> str:
    """A real approval row, because the composite FK means it."""
    approval_id = f"apr_{tenant.organization_id[-8:]}"
    await tenant.session.execute(
        text(
            "INSERT INTO approvals (id, organization_id, action_type, action_payload, "
            "payload_hash, requested_by, status, decided_at) "
            "VALUES (:i, :o, 'procedure.change', CAST(:p AS jsonb), :h, "
            "'system:test', 'approved', :now) "
            "ON CONFLICT (id) DO NOTHING"
        ),
        {
            "i": approval_id,
            "o": tenant.organization_id,
            "p": '{"c":1}',
            "h": "0" * 64,
            "now": NOW,
        },
    )
    return approval_id


async def _agent(tenant: Tenant) -> str:
    """A minimal `agents` row, which needs a definition, a role and an org unit."""
    row_id = f"agt_{tenant.organization_id[-10:]}"
    role = f"rol_{tenant.organization_id[-10:]}"
    unit = f"oru_{tenant.organization_id[-10:]}"
    definition = f"def_{tenant.organization_id[-10:]}"
    for statement, params in (
        (
            "INSERT INTO roles (id, organization_id, name) VALUES (:i, :o, :n) "
            "ON CONFLICT (id) DO NOTHING",
            {"i": role, "o": tenant.organization_id, "n": f"probe {row_id}"},
        ),
        (
            "INSERT INTO organizational_units (id, organization_id, name, slug) "
            "VALUES (:i, :o, :n, :s) ON CONFLICT (id) DO NOTHING",
            {"i": unit, "o": tenant.organization_id, "n": f"probe {unit}", "s": unit},
        ),
        (
            "INSERT INTO agent_definitions (id, organization_id, name, role_id) "
            "VALUES (:i, :o, :n, :r) ON CONFLICT (id) DO NOTHING",
            {"i": definition, "o": tenant.organization_id, "n": f"probe {definition}", "r": role},
        ),
        (
            "INSERT INTO agents (id, organization_id, name, definition_id, role_id, "
            "org_unit_id) VALUES (:i, :o, :n, :d, :r, :u) ON CONFLICT (id) DO NOTHING",
            {
                "i": row_id,
                "o": tenant.organization_id,
                "n": "probe",
                "d": definition,
                "r": role,
                "u": unit,
            },
        ),
    ):
        await tenant.session.execute(text(statement), params)
    return row_id
