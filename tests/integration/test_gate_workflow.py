"""The Gate workflow, end to end against a live database.

`domain/gates.py` is unit-tested as pure logic. This exercises the parts that
only exist once there is a database: registration generating a checklist, a
decision writing a real row, and RLS hiding the result from another tenant.

The point of the last one is the security claim. Every one of the 36 domain
tables carries `organization_id` and an RLS policy, and this is the test that says
so for the Gate spine specifically rather than trusting the catalogue.
"""

from __future__ import annotations

import datetime as dt
from datetime import UTC

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application import gate_operations as GOPS
from ai_orchestrator.domain import gates as G
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence import process as P
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant, _create_organization

pytestmark = [pytest.mark.integration]


async def _gate(t: Tenant, code: str = "G2") -> str:
    gid = f"gdf_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO gate_definitions (id, organization_id, code, sequence, name_vi, "
            "chair_role_key, member_role_keys, lead_time_days, max_extensions) "
            "VALUES (:i, :o, :c, 2, 'Handover', 'deputy_ceo_operations', "
            "CAST('[\"cfo\"]' AS jsonb), 5, 2)"
        ),
        {"i": gid, "o": t.organization_id, "c": code},
    )
    await t.commit()
    return gid


async def _criteria(t: Tenant, gate_id: str) -> dict[str, str]:
    ids = {}
    for code, title, mandatory in (
        ("E1", "Hợp đồng ký + phụ lục", True),
        ("E2", "Baseline budget", True),
        ("E3", "Cam kết đặc biệt với CĐT", False),
    ):
        cid = f"gcr_{new_ulid()}"
        await t.session.execute(
            text(
                "INSERT INTO gate_criteria (id, organization_id, gate_definition_id, code, "
                "criterion_type, title, is_mandatory, waiver_role_key) "
                "VALUES (:i, :o, :g, :c, 'entry', :t, :m, 'deputy_ceo_operations')"
            ),
            {"i": cid, "o": t.organization_id, "g": gate_id, "c": code, "t": title, "m": mandatory},
        )
        ids[code] = cid
    await t.commit()
    return ids


class TestRegistrationGeneratesTheChecklist:
    """Tập 2 §E.1 step 1: "hệ thống sinh checklist hồ sơ Entry Criteria theo loại Gate"."""

    async def test_registering_creates_a_row_per_entry_criterion(self, tenant: Tenant) -> None:
        gate_id = await _gate(tenant)
        criteria = await _criteria(tenant, gate_id)

        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            scheduled_for=dt.date(2026, 10, 1),
            actor="project_director",
        )
        await tenant.commit()

        rows = (
            await tenant.session.execute(
                text(
                    "SELECT c.code, e.state FROM gate_criterion_evaluations e "
                    "JOIN gate_criteria c ON c.id = e.gate_criterion_id "
                    "WHERE e.gate_instance_id = :i"
                ),
                {"i": instance_id},
            )
        ).all()
        assert {r[0] for r in rows} == set(criteria)
        assert all(r[1] == "not_applicable" for r in rows), (
            "a freshly generated checklist must be unanswered, not pre-ticked"
        )

    async def test_exit_criteria_are_not_in_the_generated_checklist(self, tenant: Tenant) -> None:
        """Exit criteria describe the next Gate's readiness.

        Generating them here would put lines in front of an operator that cannot
        be satisfied until after the decision they are part of.
        """
        gate_id = await _gate(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO gate_criteria (id, organization_id, gate_definition_id, code, "
                "criterion_type, title) "
                "VALUES (:i, :o, :g, 'X1', 'exit', 'PM ký xác nhận')"
            ),
            {"i": f"gcr_{new_ulid()}", "o": tenant.organization_id, "g": gate_id},
        )
        await tenant.commit()
        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await tenant.commit()
        codes = (
            (
                await tenant.session.execute(
                    text(
                        "SELECT c.code FROM gate_criterion_evaluations e "
                        "JOIN gate_criteria c ON c.id = e.gate_criterion_id "
                        "WHERE e.gate_instance_id = :i"
                    ),
                    {"i": instance_id},
                )
            )
            .scalars()
            .all()
        )
        assert "X1" not in codes


class TestTheGateHoldsUntilItIsAnswered:
    async def test_a_freshly_registered_gate_cannot_pass(self, tenant: Tenant) -> None:
        """A Gate that opens on an unassessed checklist passes by never being read."""
        gate_id = await _gate(tenant)
        await _criteria(tenant, gate_id)
        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await tenant.commit()
        session = await GOPS.load_session(await _conn(tenant), instance_id)
        assert not G.can_pass(session)
        # Two, not three: E3 is optional, so an unassessed optional criterion is
        # not a blocker. A checklist generated for a Gate with mandatory and
        # optional criteria must not report the optional ones as failures, or an
        # operator learns to ignore the list.
        blockers = G.block_can_pass(session)
        assert {b.criterion_code for b in blockers} == {"E1", "E2"}
        assert len(blockers) == 2
        assert (
            G.proposed_outcome(session, has_conditions=False, attendee_count=6, quorum=3)
            == P.GATE_HOLD
        )

    async def test_answering_the_mandatory_criteria_opens_the_gate(self, tenant: Tenant) -> None:
        gate_id = await _gate(tenant)
        criteria = await _criteria(tenant, gate_id)
        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await tenant.session.execute(
            text(
                "UPDATE gate_criterion_evaluations SET state = 'met', assessed_by = "
                "'pmo_specialist', assessed_at = now() "
                "WHERE gate_instance_id = :i AND gate_criterion_id IN (:a, :b)"
            ),
            {"i": instance_id, "a": criteria["E1"], "b": criteria["E2"]},
        )
        await tenant.commit()
        session = await GOPS.load_session(await _conn(tenant), instance_id)
        assert G.can_pass(session), [b.as_dict() for b in G.block_can_pass(session)]
        assert (
            G.proposed_outcome(session, has_conditions=False, attendee_count=6, quorum=3)
            == P.GATE_PASS
        )


class TestDecisionsAreRealRows:
    async def _ready_session(self, t: Tenant) -> G.GateSession:
        """A Gate with every criterion answered, ready to be decided."""
        gate_id = await _gate(t)
        await _criteria(t, gate_id)
        instance_id = await GOPS.register_session(
            await _conn(t),
            organization_id=t.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await t.session.execute(
            text("UPDATE gate_criterion_evaluations SET state = 'met' WHERE gate_instance_id = :i"),
            {"i": instance_id},
        )
        await t.commit()
        return await GOPS.load_session(await _conn(t), instance_id)

    async def test_a_pass_writes_a_decision_and_closes_the_gate(self, tenant: Tenant) -> None:
        session = await self._ready_session(tenant)
        decision_id = await GOPS.record_decision(
            await _conn(tenant),
            session=session,
            outcome=P.GATE_PASS,
            approved_by="deputy_ceo_operations",
            rationale="Entry criteria complete; baseline agrees with the estimate",
            compiled_by="pmo_specialist",
            attendee_count=5,
            quorum=3,
            decided_at=dt.datetime.now(UTC),
        )
        await tenant.commit()
        row = (
            await tenant.session.execute(
                text(
                    "SELECT d.outcome, d.rationale, i.status FROM gate_decisions d "
                    "JOIN gate_instances i ON i.id = d.gate_instance_id WHERE d.id = :i"
                ),
                {"i": decision_id},
            )
        ).one()
        assert row.outcome == "PASS"
        assert row.status == "passed", "the instance must follow the decision"

    async def test_a_conditional_pass_creates_action_items(self, tenant: Tenant) -> None:
        """Tập 2 §E.1 step 4: conditions become action items with a deadline and an owner."""
        session = await self._ready_session(tenant)
        decision_id = await GOPS.record_decision(
            await _conn(tenant),
            session=session,
            outcome=P.GATE_PASS_WITH_CONDITIONS,
            approved_by="deputy_ceo_operations",
            rationale="Pass subject to a 30-day mobilisation plan",
            compiled_by="pmo_specialist",
            attendee_count=5,
            quorum=3,
            decided_at=dt.datetime.now(dt.UTC),
            conditions=(
                {
                    "description": "Kế hoạch huy động 30 ngày đầu",
                    "owner_role_key": "project_director",
                    "owner_person": "Nguyen Van A",
                    "due_on": dt.date(2026, 10, 15),
                },
            ),
        )
        await tenant.commit()
        conditions = (
            await tenant.session.execute(
                text(
                    "SELECT description, owner_role_key, due_on, status FROM gate_conditions "
                    "WHERE gate_decision_id = :d"
                ),
                {"d": decision_id},
            )
        ).all()
        assert len(conditions) == 1
        assert conditions[0][3] == "open"
        assert conditions[0][2] == dt.date(2026, 10, 15)

    async def test_a_conditional_pass_without_conditions_is_refused(self, tenant: Tenant) -> None:
        """A pass that creates obligations, with none created, is a contradiction.

        Caught here rather than by a check constraint because the conditions are
        child rows and only the application knows whether any exist.
        """
        session = await self._ready_session(tenant)
        with pytest.raises(ValueError, match="meaningless with no conditions"):
            await GOPS.record_decision(
                await _conn(tenant),
                session=session,
                outcome=P.GATE_PASS_WITH_CONDITIONS,
                approved_by="deputy_ceo_operations",
                rationale="pass with nothing attached",
                decided_at=dt.datetime.now(UTC),
            )
        await tenant.session.rollback()

    async def test_a_decision_without_a_rationale_is_refused(self, tenant: Tenant) -> None:
        session = await self._ready_session(tenant)
        with pytest.raises(ValueError, match="must state its reason"):
            await GOPS.record_decision(
                await _conn(tenant),
                session=session,
                outcome=P.GATE_PASS,
                approved_by="deputy_ceo_operations",
                rationale="   ",
                decided_at=dt.datetime.now(UTC),
            )
        await tenant.session.rollback()

    async def test_the_compiler_cannot_approve(self, tenant: Tenant) -> None:
        """Tập 1 §1.2, refused in Python before the constraint is reached.

        The check constraint is the guarantee; this is the fast, clear error. Two
        layers because a constraint violation surfaces as a database error
        carrying a name, and the operator who typed the decision should be told
        what they did wrong in a sentence.
        """
        session = await self._ready_session(tenant)
        with pytest.raises(ValueError, match="cannot approve"):
            await GOPS.record_decision(
                await _conn(tenant),
                session=session,
                outcome=P.GATE_PASS,
                approved_by="pmo_specialist",
                compiled_by="pmo_specialist",
                rationale="self-approved",
                decided_at=dt.datetime.now(UTC),
            )
        await tenant.session.rollback()

    async def test_an_unknown_outcome_is_refused(self, tenant: Tenant) -> None:
        session = await self._ready_session(tenant)
        with pytest.raises(ValueError, match="unknown gate outcome"):
            await GOPS.record_decision(
                await _conn(tenant),
                session=session,
                outcome="PROBABLY_FINE",
                approved_by="deputy_ceo_operations",
                rationale="x",
                decided_at=dt.datetime.now(UTC),
            )
        await tenant.session.rollback()


class TestGateSpineIsTenantIsolated:
    async def test_another_tenant_cannot_see_a_gate_instance(
        self, tenant: Tenant, db: Database
    ) -> None:
        """RLS on the Gate spine, specifically.

        A Gate decision names a council, a rationale and a commercial assessment.
        Another tenant reading it learns the client's project and our margin
        position, and the catalogue-level RLS check does not prove that *this*
        table is covered.
        """
        gate_id = await _gate(tenant)
        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await tenant.commit()

        other = await _create_other_org()
        async with db.tenant_session(other) as conn:
            found = (
                await conn.execute(
                    text("SELECT count(*) FROM gate_instances WHERE id = :i"),
                    {"i": instance_id},
                )
            ).scalar()
        assert found == 0, "another tenant read a Gate instance"

    async def test_another_tenant_cannot_decide_our_gate(
        self, tenant: Tenant, db: Database
    ) -> None:
        """The write direction, which is the one usually untested.

        Reading across tenants is a disclosure problem. Writing a decision into
        another tenant's Gate is an integrity problem, and it is the more serious
        of the two: a decision someone else made would be attributed to us.

        Two details copied from `test_tenant_isolation.py` because getting either
        wrong makes the test pass for the wrong reason:

        * the organisation row is created through the **owner** role, since
          `organizations` has no RLS and is the tenant root;
        * the insert is attempted **while bound to our tenant**, naming theirs in
          `organization_id`. That is what exercises `WITH CHECK`.

        The first draft of this test bound the session to the *other* tenant and
        inserted its own id, so RLS correctly allowed it and the test reported
        "DID NOT RAISE" — a real gap in the test, and a reminder that a passing
        isolation test proves only the thing it actually attempted.
        """
        from sqlalchemy.exc import DBAPIError

        other = await _create_other_org()
        gate_id = await _gate(tenant)
        instance_id = await GOPS.register_session(
            await _conn(tenant),
            organization_id=tenant.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await tenant.commit()
        with pytest.raises(DBAPIError, match="row-level security"):
            async with db.tenant_session(tenant.organization_id) as conn:
                await conn.execute(
                    text(
                        "INSERT INTO gate_decisions (id, organization_id, gate_instance_id, "
                        "outcome, decided_at, approved_by, rationale) "
                        "VALUES (:i, :o, :g, 'PASS', now(), 'ceo', 'not our gate')"
                    ),
                    {"i": f"gdc_{new_ulid()}", "o": other, "g": instance_id},
                )

        # And nothing landed, checked from *their* side. The refusal is the
        # first assertion; this is what proves the refusal was RLS rather than
        # a foreign key complaining about a made-up instance id.
        async with db.tenant_session(other) as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM gate_decisions WHERE gate_instance_id = :g"),
                    {"g": instance_id},
                )
            ).scalar()
        assert count == 0


async def _conn(t: Tenant) -> AsyncConnection:
    """The tenant-bound connection `gates.py` works against.

    **The fixture's own connection**, not a new one. `session.connection()`
    returns a coroutine resolving to an `AsyncConnection` bound to the same
    transaction as the fixture's session, so its uncommitted writes are visible
    to the code under test.

    The first draft made this synchronous and returned the coroutine unawaited,
    which produced `AttributeError: 'coroutine' object has no attribute
    'execute'` in all twelve tests at once. Recorded because the failure was
    identical everywhere and named no call site: a helper's error mode belongs
    in the helper, not in whichever test happened to surface it.
    """
    return await t.session.connection()


async def _create_other_org() -> str:
    """A second organization, created through the owner role.

    `organizations` has no RLS — it is the tenant root — so it must be inserted
    as the owner. Creating it as `ao_app` fails, and creating it inside a tenant
    session puts it in a transaction that may roll back.
    """
    admin = Database.from_settings(use_admin_role=True)
    try:
        return await _create_organization(admin, "other-gate")
    finally:
        await admin.dispose()


class TestTheExtensionLimitConvertsTheGate:
    """Tập 2 §E.1 step 5: a conditional pass extended more than twice is a HOLD.

    The count is on the instance, so this is a row that changes rather than a
    status somebody remembers to update.
    """

    async def _conditional(self, t: Tenant) -> G.GateSession:
        gate_id = await _gate(t)
        await _criteria(t, gate_id)
        instance_id = await GOPS.register_session(
            await _conn(t),
            organization_id=t.organization_id,
            gate_definition_id=gate_id,
            subject_kind="project",
            subject_id=f"prj_{new_ulid()}",
            actor="project_director",
        )
        await t.session.execute(
            text("UPDATE gate_criterion_evaluations SET state = 'met' WHERE gate_instance_id = :i"),
            {"i": instance_id},
        )
        await t.commit()
        session = await GOPS.load_session(await _conn(t), instance_id)
        await GOPS.record_decision(
            await _conn(t),
            session=session,
            outcome=P.GATE_PASS_WITH_CONDITIONS,
            approved_by="deputy_ceo_operations",
            rationale="Pass subject to a 30-day mobilisation plan",
            compiled_by="pmo_specialist",
            attendee_count=5,
            quorum=3,
            decided_at=dt.datetime.now(UTC),
            conditions=(
                {
                    "description": "Kế hoạch huy động 30 ngày đầu",
                    "owner_role_key": "project_director",
                    "due_on": dt.date(2026, 10, 15),
                },
            ),
        )
        await t.commit()
        return await GOPS.load_session(await _conn(t), instance_id)

    async def test_the_first_two_extensions_hold_at_conditional(self, tenant: Tenant) -> None:
        session = await self._conditional(tenant)
        assert session.extension_count == 0
        for expected in (1, 2):
            status = await GOPS.extend_conditional_pass(
                await _conn(tenant),
                session=await GOPS.load_session(await _conn(tenant), session.instance_id),
                decided_at=dt.datetime.now(UTC),
            )
            await tenant.commit()
            assert status == "conditional", f"extension {expected} should not convert"
        reloaded = await GOPS.load_session(await _conn(tenant), session.instance_id)
        assert G.check_extension_limit(reloaded) is False

    async def test_the_third_extension_converts_to_hold(self, tenant: Tenant) -> None:
        session = await self._conditional(tenant)
        for _ in range(3):
            status = await GOPS.extend_conditional_pass(
                await _conn(tenant),
                session=await GOPS.load_session(await _conn(tenant), session.instance_id),
                decided_at=dt.datetime.now(UTC),
            )
            await tenant.commit()
        assert status == "on_hold", (
            "Tập 2 §E.1 step 5: the third extension converts the Gate and it reports to the CEO"
        )
        reloaded = await GOPS.load_session(await _conn(tenant), session.instance_id)
        assert reloaded.extension_count == 3
        assert reloaded.status == "on_hold"
        assert G.check_extension_limit(reloaded) is True
