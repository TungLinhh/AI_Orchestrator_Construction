"""The material master and supplier diligence, against a live database.

The interesting constraints here are not about storage. They are about three
findings from the corpus that changed the shape of the schema, and each of them
is a thing a plausible-looking design would have got wrong.

* The 12-character material code does not exist in the data, so `code` is
  nullable and the uniqueness index is partial.
* A drawing reference is many-to-many, so it is not on `materials` at all.
* The corpus's own "Mã Hiệu" column is unlabelled, so `raw_cells` holds it
  rather than a guess.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant
from tests.integration.test_construction_domain import refused_because

pytestmark = [pytest.mark.integration]

SUPPLY_TABLES = (
    "material_categories",
    "materials",
    "supplier_documents",
    "supplier_risk_flags",
    "supplier_assessments",
)


async def _supplier(t: Tenant) -> str:
    sid = f"sup_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, status, "
            "qualification_expires_on) "
            "VALUES (:i, :o, :c, 'Supplier', 'approved', CURRENT_DATE + 365)"
        ),
        {"i": sid, "o": t.organization_id, "c": f"S-{uuid.uuid4().hex[:6]}"},
    )
    await t.commit()
    return sid


class TestTheMaterialCodeIsOptionalAndUniqueWhenPresent:
    """Tập 1 §4.3 specifies a 12-character code; the corpus has none.

    `code` is nullable because inventing codes for the master is prohibited —
    Procurement is the sole owner — and refusing to record materials we have is
    worse. The uniqueness index is partial, which is the only way to get both.
    """

    async def test_a_material_without_a_code_is_accepted(self, tenant: Tenant) -> None:
        """What the corpus actually looks like: `Mã Hiệu` is a drawing number."""
        await tenant.session.execute(
            text(
                "INSERT INTO materials (id, organization_id, name_vi, system_code, "
                "specification) VALUES (:i, :o, 'Ống đồng và phụ kiện', 'AC', 'Ø 200mm')"
            ),
            {"i": f"mat_{new_ulid()}", "o": tenant.organization_id},
        )
        await tenant.commit()

    async def test_several_materials_may_have_no_code(self, tenant: Tenant) -> None:
        """The point of a partial index. A plain unique index would allow one."""
        for name in ("Ống nước ngưng", "Bảo ôn ống nước"):
            await tenant.session.execute(
                text("INSERT INTO materials (id, organization_id, name_vi) VALUES (:i, :o, :n)"),
                {"i": f"mat_{new_ulid()}", "o": tenant.organization_id, "n": name},
            )
        await tenant.commit()
        count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM materials WHERE organization_id = :o AND code IS NULL"),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == 2

    async def test_two_materials_may_not_share_a_code(self, tenant: Tenant) -> None:
        """ "Cấm tạo mã trùng" as a constraint rather than a review step."""
        for _ in range(2):
            try:
                await tenant.session.execute(
                    text(
                        "INSERT INTO materials (id, organization_id, code, name_vi) "
                        "VALUES (:i, :o, 'AC-CBL-PWR-001', 'Cable')"
                    ),
                    {"i": f"mat_{new_ulid()}", "o": tenant.organization_id},
                )
                await tenant.commit()
            except Exception:
                await tenant.session.rollback()
                return
        pytest.fail("two materials were accepted with the same code")

    async def test_a_code_must_follow_the_dossier_scheme(self, tenant: Tenant) -> None:
        """A present-but-wrong code is a second numbering series.

        Tập 1's scheme is a *control*: `ONX-[BLOCK]-...` is enforced the same way
        on SOPs, and a typo in a material code is just as invisible until an
        audit asks for "all AC cable".
        """
        with refused_because("ck_materials_code_matches_the_dossier_scheme"):
            await tenant.session.execute(
                text(
                    "INSERT INTO materials (id, organization_id, code, name_vi) "
                    "VALUES (:i, :o, 'CABLE-001', 'Cable')"
                ),
                {"i": f"mat_{new_ulid()}", "o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_an_unknown_system_code_is_refused(self, tenant: Tenant) -> None:
        """PW/LV/WD/AC/FP are the five MEPF systems; anything else is a typo."""
        with refused_because("ck_materials_system_code_known"):
            await tenant.session.execute(
                text(
                    "INSERT INTO materials (id, organization_id, name_vi, system_code) "
                    "VALUES (:i, :o, 'Thing', 'ZZ')"
                ),
                {"i": f"mat_{new_ulid()}", "o": tenant.organization_id},
            )
        await tenant.session.rollback()


class TestTheUnlabelledColumnsAreKeptNotGuessed:
    """`SSA | HBG | 2` under a header that labels none of them."""

    async def test_raw_cells_survive_a_round_trip(self, tenant: Tenant) -> None:
        raw = {"col2": "SSA", "col4": "HBG", "col5": 2}
        material_id = f"mat_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO materials (id, organization_id, name_vi, raw_cells) "
                "VALUES (:i, :o, 'Ống đồng và phụ kiện', CAST(:r AS jsonb))"
            ),
            {
                "i": material_id,
                "o": tenant.organization_id,
                "r": '{"col2":"SSA","col4":"HBG","col5":2}',
            },
        )
        await tenant.commit()
        stored = (
            await tenant.session.execute(
                text("SELECT raw_cells FROM materials WHERE id = :i"),
                {"i": material_id},
            )
        ).scalar()
        assert stored == raw, (
            "the corpus columns that carry no header must be preserved verbatim; "
            "a parser that assigned them meanings would be inventing"
        )


class TestSupplierDiligenceIsConclusionsNotScores:
    async def test_a_risk_flag_without_a_basis_is_still_recordable_but_blocking_needs_one(
        self, tenant: Tenant
    ) -> None:
        """A flag with no basis is a rumour, and a rumour can freeze a supplier.

        So a flag may be raised without a basis — someone saw something — but a
        *blocking* one may not, because blocking a supplier is a commercial
        decision and it needs a citation.
        """
        supplier_id = await _supplier(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO supplier_risk_flags (id, organization_id, supplier_id, "
                "risk_kind, severity, is_blocking, summary, basis) "
                "VALUES (:i, :o, :s, 'abnormal_price', 'high', false, "
                "'Quote 40% below market', 'Compared against three competitors')"
            ),
            {"i": f"srf_{new_ulid()}", "o": tenant.organization_id, "s": supplier_id},
        )
        await tenant.commit()

    async def test_blocking_must_be_the_top_severity(self, tenant: Tenant) -> None:
        """A flag that blocks while reading as `medium` is a contradiction.

        Tập 1 §5.3 freezes a supplier on a legal or banned-risk finding. A
        blocking flag at `low` would make the severity column decorative and the
        freeze unreviewable.
        """
        supplier_id = await _supplier(tenant)
        with refused_because("ck_supplier_risk_flags_blocking_is_the_top_severity"):
            await tenant.session.execute(
                text(
                    "INSERT INTO supplier_risk_flags (id, organization_id, supplier_id, "
                    "risk_kind, severity, is_blocking, summary) "
                    "VALUES (:i, :o, :s, 'abnormal_price', 'low', true, 'cheaper')"
                ),
                {"i": f"srf_{new_ulid()}", "o": tenant.organization_id, "s": supplier_id},
            )
        await tenant.session.rollback()

    async def test_a_resolution_must_state_why(self, tenant: Tenant) -> None:
        """Tập 1's `cấm tạo mã trùng` cousin: clearing a flag needs a reason.

        A flag resolved with no note is a flag that will be raised again by
        whoever raised it first, and the second time nobody will know it was
        already reviewed.
        """
        supplier_id = await _supplier(tenant)
        with refused_because("ck_supplier_risk_flags_a_resolution_states_why"):
            await tenant.session.execute(
                text(
                    "INSERT INTO supplier_risk_flags (id, organization_id, supplier_id, "
                    "risk_kind, severity, summary, resolved_at) "
                    "VALUES (:i, :o, :s, 'quality_history', 'medium', 'Late delivery', now())"
                ),
                {"i": f"srf_{new_ulid()}", "o": tenant.organization_id, "s": supplier_id},
            )
        await tenant.session.rollback()


class TestAnAssessmentIsARunNotAField:
    """Tập 3 §1.4: score out of 100, colour, A/B/C, valid twelve months."""

    @staticmethod
    async def _assessment(t: Tenant, **overrides: object) -> None:
        supplier_id = await _supplier(t)
        params: dict[str, object] = {
            "i": f"sas_{new_ulid()}",
            "o": t.organization_id,
            "s": supplier_id,
            "score": 82,
            "rating": "A",
            "blocking": False,
        }
        params.update(overrides)
        await t.session.execute(
            text(
                "INSERT INTO supplier_assessments (id, organization_id, supplier_id, "
                "assessed_on, assessed_by, group_scores, total_score, rating, "
                "is_blocking) VALUES (:i, :o, :s, CURRENT_DATE, 'procurement_lead', "
                "CAST(:gs AS jsonb), :score, :rating, :blocking)"
            ),
            {
                "gs": (
                    '{"legal":90,"financial":80,"capability":85,"commercial":75,"compliance":80}'
                ),
                **params,
            },
        )
        await t.commit()

    async def test_an_a_rating_is_accepted(self, tenant: Tenant) -> None:
        await self._assessment(tenant)

    async def test_a_red_rating_must_block(self, tenant: Tenant) -> None:
        """Tập 3 pairs C with red. A red assessment that does not block is a
        rating nobody acted on, and the next purchase request will show it as C
        and nobody will know it should have been stopped."""
        with refused_because("ck_supplier_assessments_a_red_assessment_blocks"):
            await self._assessment(tenant, score=35, rating="C", blocking=False)
        await tenant.session.rollback()

    async def test_a_blocking_assessment_must_be_red(self, tenant: Tenant) -> None:
        """The converse. A blocking flag that reads as `A` in the dropdown
        contradicts itself, and Tập 3 requires the rating to be shown with the
        payment request."""
        with refused_because("ck_supplier_assessments_a_blocking_assessment_is_red"):
            await self._assessment(tenant, score=90, rating="A", blocking=True)
        await tenant.session.rollback()

    async def test_a_score_outside_the_hundred_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_supplier_assessments_total_score_in_range"):
            await self._assessment(tenant, score=140, rating="A")
        await tenant.session.rollback()

    async def test_two_assessments_coexist_and_the_latest_is_queryable(
        self, tenant: Tenant
    ) -> None:
        """A re-assessment is a new row, not an overwrite.

        "The most recent assessment" is the query behind every supplier pick in a
        dropdown, and a score that overwrote its predecessor could not be
        reproduced when a rating is challenged.
        """
        await self._assessment(tenant, score=82, rating="A")
        await self._assessment(tenant, score=61, rating="B")
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT total_score, rating FROM supplier_assessments "
                    "WHERE organization_id = :o ORDER BY total_score"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert {r[0] for r in rows} == {61, 82}


class TestSupplierDocumentsExpire:
    async def test_a_validity_window_must_not_be_inverted(self, tenant: Tenant) -> None:
        """A document that expires before it was issued is a data-entry error
        that would read as an expired document and quietly fail a supplier."""
        supplier_id = await _supplier(tenant)
        with refused_because("ck_supplier_documents_validity_window_is_not_inverted"):
            await tenant.session.execute(
                text(
                    "INSERT INTO supplier_documents (id, organization_id, supplier_id, "
                    "document_kind, title, valid_from, valid_to) "
                    "VALUES (:i, :o, :s, 'business_registration', 'GPKD', "
                    "CURRENT_DATE + 365, CURRENT_DATE)"
                ),
                {"i": f"sdp_{new_ulid()}", "o": tenant.organization_id, "s": supplier_id},
            )
        await tenant.session.rollback()

    async def test_a_verified_document_names_its_verifier(self, tenant: Tenant) -> None:
        """Tập 3 §1.4 group 1 requires the evidence *or* a pass/fail. A document
        marked verified with nobody's name on it satisfies neither."""
        supplier_id = await _supplier(tenant)
        with refused_because("ck_supplier_documents_a_verified_document_names_its_verifier"):
            await tenant.session.execute(
                text(
                    "INSERT INTO supplier_documents (id, organization_id, supplier_id, "
                    "document_kind, title, is_verified) "
                    "VALUES (:i, :o, :s, 'tax_code', 'MST', true)"
                ),
                {"i": f"sdp_{new_ulid()}", "o": tenant.organization_id, "s": supplier_id},
            )
        await tenant.session.rollback()


class TestSupplyTablesAreTenantProtected:
    async def test_every_supply_table_is_tenant_protected(self, admin_db: Database) -> None:
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
                        {"names": list(SUPPLY_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
        unprotected = [
            r["name"] for r in rows if not (r["enabled"] and r["forced"] and r["has_policy"])
        ]
        assert not unprotected, f"no isolation policy: {unprotected}"
        assert len(rows) == len(SUPPLY_TABLES)
