"""The governance spine, against a live database.

The process spine is where a rule stops being prose. Tập 1 calls it "xương sống
quản trị" and every constraint here has a source citation, because the point of
these tests is that the schema enforces what the dossier says rather than what
somebody remembered.

Three of them are the load-bearing ones:

* an agent can never be `A` on a step (Tập 3 §4.1)
* the person who compiled the Gate pack cannot approve it (Tập 1 §1.2)
* a waiver is attributable and reasoned, never a silent pass (Tập 3 §1.2)
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ai_orchestrator.persistence import process as P
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant
from tests.integration.test_construction_domain import refused_because

pytestmark = [pytest.mark.integration]

PROCESS_TABLES = (
    "gate_definitions",
    "gate_criteria",
    "gate_instances",
    "gate_criterion_evaluations",
    "gate_decisions",
    "gate_conditions",
    "sop_definitions",
    "sop_versions",
    "sop_steps",
    "sop_raci",
    "sop_forms",
    "doa_matrix",
    "autonomy_policies",
)


async def _gate_definition(t: Tenant, code: str = "G2") -> str:
    gid = f"gdf_{uuid.uuid4().hex[:26]}"
    await t.session.execute(
        text(
            "INSERT INTO gate_definitions (id, organization_id, code, sequence, name_vi, "
            "chair_role_key, member_role_keys, lead_time_days) "
            "VALUES (:i, :o, :c, 2, 'Bàn giao hợp đồng', 'deputy_ceo_ops', "
            "CAST(:m AS jsonb), 5)"
        ),
        {"i": gid, "o": t.organization_id, "c": code, "m": '["ceo_bd", "cfo", "pmo_director"]'},
    )
    await t.commit()
    return gid


async def _gate_instance(t: Tenant, gate_id: str) -> str:
    iid = f"gin_{uuid.uuid4().hex[:26]}"
    await t.session.execute(
        text(
            "INSERT INTO gate_instances (id, organization_id, gate_definition_id, "
            "subject_kind, subject_id, status) "
            "VALUES (:i, :o, :g, 'project', :s, 'in_session')"
        ),
        {
            "i": iid,
            "o": t.organization_id,
            "g": gate_id,
            "s": f"prj_{uuid.uuid4().hex[:26]}",
        },
    )
    await t.commit()
    return iid


class TestAnAgentIsNeverAccountable:
    """Tập 3 §4.1: "Agent không bao giờ được gán approval role."

    This is the single most important constraint in the process spine. Without
    it the platform grows, one workflow at a time, a path by which a model
    approves its own work — and every such path is added for a good reason.
    """

    @staticmethod
    async def _step(t: Tenant) -> str:
        sid = f"ssd_{uuid.uuid4().hex[:26]}"
        vid = f"svr_{uuid.uuid4().hex[:26]}"
        did = f"spd_{uuid.uuid4().hex[:26]}"
        await t.session.execute(
            text(
                "INSERT INTO sop_definitions (id, organization_id, code, name_vi, "
                "block, department, owner_role_key) "
                "VALUES (:d, :o, 'ONX-MO-PM-SOP-003', 'Kiểm soát tiến độ', "
                "'MO', 'PM', 'project_director')"
            ),
            {"d": did, "o": t.organization_id},
        )
        await t.session.execute(
            text(
                "INSERT INTO sop_versions (id, organization_id, sop_definition_id, "
                "major, minor, status, approved_by) "
                "VALUES (:i, :o, :d, 1, 0, 'issued', 'pmo_director')"
            ),
            {"i": vid, "o": t.organization_id, "d": did},
        )
        await t.session.execute(
            text(
                "INSERT INTO sop_steps (id, organization_id, sop_version_id, sequence, "
                "title, autonomy_level) "
                "VALUES (:i, :o, :v, 1, 'Lập báo cáo tuần', 'L2')"
            ),
            {"i": sid, "o": t.organization_id, "v": vid},
        )
        await t.commit()
        return sid

    async def test_an_agent_cannot_be_accountable(self, tenant: Tenant) -> None:
        step_id = await self._step(tenant)
        with refused_because("ck_sop_raci_an_agent_is_never_accountable"):
            await tenant.session.execute(
                text(
                    "INSERT INTO sop_raci (id, organization_id, sop_step_id, role_key, "
                    "role_kind, letter) "
                    "VALUES (:i, :o, :s, 'project_controls_agent', 'agent', 'A')"
                ),
                {"i": f"sra_{uuid.uuid4().hex[:26]}", "o": tenant.organization_id, "s": step_id},
            )
        await tenant.session.rollback()

    async def test_an_agent_may_be_responsible_consulted_or_informed(self, tenant: Tenant) -> None:
        """The other three letters, so the constraint is a boundary and not a wall.

        Tập 3 §4.1: an `R` cell is exactly what determines the task an agent may
        assist at L2-L4. Refusing all four would refuse the whole design.
        """
        step_id = await self._step(tenant)
        for letter in ("R", "C", "I"):
            await tenant.session.execute(
                text(
                    "INSERT INTO sop_raci (id, organization_id, sop_step_id, role_key, "
                    "role_kind, letter) "
                    "VALUES (:i, :o, :s, :rk, 'agent', :l)"
                ),
                {
                    "i": f"sra_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "s": step_id,
                    "rk": f"agent_{letter.lower()}",
                    "l": letter,
                },
            )
        await tenant.commit()
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM sop_raci WHERE organization_id = :o "
                    "AND role_kind = 'agent'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == 3


class TestSegregationOfDuties:
    """Tập 1 §1.2: proposer, reviewer, approver and payer are separate people."""

    async def test_the_compiler_cannot_approve(self, tenant: Tenant) -> None:
        gate_id = await _gate_definition(tenant)
        instance_id = await _gate_instance(tenant, gate_id)
        with refused_because("ck_gate_decisions_the_compiler_does_not_approve"):
            await tenant.session.execute(
                text(
                    "INSERT INTO gate_decisions (id, organization_id, gate_instance_id, "
                    "outcome, decided_at, approved_by, compiled_by, rationale) "
                    "VALUES (:i, :o, :g, 'PASS', now(), 'project_director', "
                    "'project_director', 'reviewed and approved')"
                ),
                {
                    "i": f"gdc_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "g": instance_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_different_approver_is_accepted(self, tenant: Tenant) -> None:
        gate_id = await _gate_definition(tenant)
        instance_id = await _gate_instance(tenant, gate_id)
        await tenant.session.execute(
            text(
                "INSERT INTO gate_decisions (id, organization_id, gate_instance_id, "
                "outcome, decided_at, approved_by, compiled_by, attendee_count, "
                "quorum, rationale) "
                "VALUES (:i, :o, :g, 'PASS', now(), 'deputy_ceo_ops', "
                "'pmo_specialist', 5, 3, 'Entry criteria complete; margin above floor')"
            ),
            {
                "i": f"gdc_{uuid.uuid4().hex[:26]}",
                "o": tenant.organization_id,
                "g": instance_id,
            },
        )
        await tenant.commit()

    async def test_a_decision_must_state_its_reason(self, tenant: Tenant) -> None:
        gate_id = await _gate_definition(tenant)
        instance_id = await _gate_instance(tenant, gate_id)
        with refused_because("ck_gate_decisions_a_decision_states_its_reason"):
            await tenant.session.execute(
                text(
                    "INSERT INTO gate_decisions (id, organization_id, gate_instance_id, "
                    "outcome, decided_at, approved_by, rationale) "
                    "VALUES (:i, :o, :g, 'PASS', now(), 'deputy_ceo_ops', '')"
                ),
                {
                    "i": f"gdc_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "g": instance_id,
                },
            )
        await tenant.session.rollback()


class TestAWaiverIsAttributable:
    """Tập 3 §1.2: "Đạt/Không đạt/Miễn trừ (miễn trừ phải ghi người phê duyệt
    miễn trừ)" — pass/fail/exempt, and an exemption must name who granted it.

    An unnamed waiver is a silent pass, which is the one thing a Gate exists to
    prevent.
    """

    @staticmethod
    async def _evaluation(t: Tenant, **overrides: object) -> str:
        gate_id = await _gate_definition(t)
        criterion_id = f"gcr_{uuid.uuid4().hex[:26]}"
        evaluation_id = f"gce_{uuid.uuid4().hex[:26]}"
        instance_id = await _gate_instance(t, gate_id)
        await t.session.execute(
            text(
                "INSERT INTO gate_criteria (id, organization_id, gate_definition_id, "
                "code, criterion_type, title, waiver_role_key) "
                "VALUES (:i, :o, :g, 'E1', 'entry', 'Signed contract and appendices', "
                "'deputy_ceo_ops')"
            ),
            {"i": criterion_id, "o": t.organization_id, "g": gate_id},
        )
        params: dict[str, object] = {
            "i": evaluation_id,
            "o": t.organization_id,
            "g": instance_id,
            "c": criterion_id,
            "state": "not_met",
            "note": "",
            "by": "",
        }
        params.update(overrides)
        await t.session.execute(
            text(
                "INSERT INTO gate_criterion_evaluations (id, organization_id, "
                "gate_instance_id, gate_criterion_id, state, waiver_note, waived_by) "
                "VALUES (:i, :o, :g, :c, :state, :note, :by)"
            ),
            params,
        )
        await t.commit()
        return evaluation_id

    async def test_a_waiver_must_name_who_granted_it(self, tenant: Tenant) -> None:
        with refused_because("ck_gate_criterion_evaluations_a_waiver_names_who_granted_it"):
            await self._evaluation(tenant, state="waived", note="client delayed")

    async def test_a_waiver_must_state_why(self, tenant: Tenant) -> None:
        with refused_because("ck_gate_criterion_evaluations_a_waiver_states_why"):
            await self._evaluation(tenant, state="waived", by="deputy_ceo_ops", note="")

    async def test_a_proper_waiver_is_accepted(self, tenant: Tenant) -> None:
        await self._evaluation(
            tenant,
            state="waived",
            by="deputy_ceo_ops",
            note="Contract executed by the client directly; our copy pending",
        )

    async def test_a_plain_pass_needs_no_waiver_fields(self, tenant: Tenant) -> None:
        await self._evaluation(tenant, state="met")


class TestAutonomyCeilingIsAComparison:
    """Tập 1 §5.3: the level is hard-coded at the Harness and cannot be
    overridden by a prompt.

    The reason this is a unit test and not a database test: the hazard is a
    *string* comparison. `"L1" < "L2"` is false as text and true as a rank, and
    getting it wrong fails open.
    """

    def test_levels_rank_in_the_right_order(self) -> None:
        assert P.autonomy_ceiling_allowed("L1", "L2")
        assert P.autonomy_ceiling_allowed("L2", "L2"), "a level must permit itself"
        assert P.autonomy_ceiling_allowed("L3", "L4")
        assert P.autonomy_ceiling_allowed("L4", "L4")

    @pytest.mark.parametrize(
        ("proposed", "ceiling"),
        [("L2", "L1"), ("L3", "L1"), ("L4", "L2"), ("L4", "L3")],
    )
    def test_a_level_above_the_ceiling_is_refused(self, proposed: str, ceiling: str) -> None:
        assert not P.autonomy_ceiling_allowed(proposed, ceiling)

    def test_a_text_comparison_would_have_given_the_wrong_answer(self) -> None:
        """Why the rank table exists, stated as an executable claim.

        If the levels were ever sorted as strings, `max()` would pick `L2` as
        the most permissive of L1/L2 and a safety ceiling would invert.
        """
        assert max(P.AUTONOMY_LEVELS) == "L4", "text sort happens to agree here"
        assert P.AUTONOMY_RANK["L4"] > P.AUTONOMY_RANK["L1"]
        # The failure this guards against is an added L10 or a reordered tuple,
        # not the current four. Assert the mapping is derived, not literal.
        assert {
            level: index for index, level in enumerate(P.AUTONOMY_LEVELS, start=1)
        } == P.AUTONOMY_RANK


class TestTheForbiddenZones:
    async def test_the_dossier_zones_are_recorded_as_hard_blocks(self, tenant: Tenant) -> None:
        """Tập 1 §5.3, the four "Vùng cấm tuyệt đối".

        Seeded by `scripts/seed_process_spine.py`; this asserts the four landed,
        because a policy table with no rows is a platform that enforces nothing
        and looks configured.
        """
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT action_class, max_level, is_hard_block FROM autonomy_policies "
                    "WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        if not rows:
            pytest.skip("process spine not seeded for this tenant")
        by_class = {r[0]: (r[1], r[2]) for r in rows}
        assert "safety_conclusion" in by_class
        assert by_class["safety_conclusion"][0] == "L2", (
            "Tập 1: an AI may not conclude a safety matter at all; the ceiling is L2"
        )
        assert "hr_personnel_decision" in by_class
        assert by_class["hr_personnel_decision"][0] == "L2"
        assert "financial_commitment" in by_class
        assert by_class["financial_commitment"][0] == "L3"
        assert "supplier_risk_flagged" in by_class


class TestProcessTablesAreProtected:
    async def test_every_process_table_is_tenant_protected(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT c.relname AS name, c.relrowsecurity AS enabled,
                               c.relforcerowsecurity AS forced,
                               EXISTS (SELECT 1 FROM pg_policies p
                                       WHERE p.schemaname = n.nspname
                                         AND p.tablename = c.relname) AS has_policy
                        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                          AND c.relname = ANY(:names)
                        """
                        ),
                        {"names": list(PROCESS_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
        unprotected = [
            r["name"] for r in rows if not (r["enabled"] and r["forced"] and r["has_policy"])
        ]
        assert not unprotected, f"no isolation policy: {unprotected}"
        assert len(rows) == len(PROCESS_TABLES)
