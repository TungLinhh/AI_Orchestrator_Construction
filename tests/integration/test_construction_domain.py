"""Construction domain against a live database.

The unit test in `tests/unit/test_construction_schema.py` reads the ORM. This
one asks the database, which is the only version of the question that matters
for a constraint. A check constraint that exists in Python and not in Postgres
is a comment.

It exists because the first version of this tranche shipped with the provenance
constraint attached to zero of its ten tables, and every test in the repository
still passed. The mixin's `__table_args__` had been replaced, not merged, by
each subclass that declared its own indexes. Nothing failed because nothing
looked. These tests each write a row that *must* be refused and assert the
database refused it **for the named reason** — a blind `pytest.raises(Exception)`
would pass just as happily if the insert failed because of a typo, which is
exactly the kind of green that hid the original bug.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

import pytest
from sqlalchemy import text

from ai_orchestrator.persistence.rls import GLOBAL_TABLES, verify_rls
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

#: The tables migration 0007 creates. Also asserted against `TENANT_TABLES` in
#: the migration itself, so a table added to one and not the other fails.
CONSTRUCTION_TABLES = (
    "clients",
    "client_contacts",
    "projects",
    "project_roles",
    "project_phases",
    "units_dictionary",
    "zones",
    "wbs",
    "wbs_items",
    "milestones",
    "opportunities",
    "tenders",
    "tender_documents",
    "tender_requirements",
    "bids",
    "bid_items",
)

_INSERT_CLIENT = (
    "INSERT INTO clients (id, organization_id, code, name, source, source_actor, proposal_id) "
    "VALUES (:id, :org, :code, 'Acme', :source, 'test', :proposal)"
)


@contextmanager
def refused_because(*fragments: str) -> Iterator[None]:
    """Require the enclosed call to fail, and to fail for a stated reason.

    Postgres reports a check-constraint violation with the constraint's name and
    a foreign-key violation with the constraint's name, so the name is the
    cheapest available proof that the rule under test fired rather than some
    unrelated one. This is the difference between asserting the platform refuses
    a bad row and asserting that Postgres refuses *this particular* bad row.
    """
    # No `noqa` here: the enclosing `with` has no call in its body, so B017
    # does not fire, and the actual call sites carry the reason check.
    with pytest.raises(Exception) as excinfo:
        yield
    message = str(excinfo.value).lower()
    missing = [f for f in fragments if f.lower() not in message]
    assert not missing, f"refused, but not for the expected reason {missing}. Got: {excinfo.value}"


async def _insert_client(t: Tenant, **overrides: object) -> None:
    """One insert into `clients`, with the provenance fields under test."""
    params: dict[str, object] = {
        "id": f"cli_{uuid.uuid4().hex[:26]}",
        "org": t.organization_id,
        "code": f"C-{uuid.uuid4().hex[:6]}",
        "source": "human",
        "proposal": None,
    }
    params.update(overrides)
    await t.session.execute(text(_INSERT_CLIENT), params)


class TestSchemaIsPresent:
    async def test_every_construction_table_exists(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list(CONSTRUCTION_TABLES)},
            )
            found = set(rows.scalars())
        assert found == set(CONSTRUCTION_TABLES), f"missing: {set(CONSTRUCTION_TABLES) - found}"

    async def test_every_construction_table_is_tenant_protected(self, admin_db: Database) -> None:
        """RLS enabled, forced, and with a policy — all three, on all ten.

        Checked by name against this tranche's list rather than by re-running
        `verify_rls`, which reports the whole schema. A whole-schema check passes
        while one new table is unprotected, because the 44 that are protected
        drown out the one that is not.
        """
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
                        {"names": list(CONSTRUCTION_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
        unprotected = [
            r["name"] for r in rows if not (r["enabled"] and r["forced"] and r["has_policy"])
        ]
        assert not unprotected, f"no isolation policy: {unprotected}"
        assert len(rows) == len(CONSTRUCTION_TABLES)

    async def test_whole_schema_still_verifies(self, admin_db: Database) -> None:
        """The global check, so this tranche did not weaken the existing 44."""
        async with admin_db.engine.connect() as conn:
            report = await verify_rls(conn)
        assert report["ok"], f"unprotected tables: {report['unprotected']}"
        assert set(GLOBAL_TABLES).isdisjoint(report["unprotected"])


class TestProvenanceIsEnforcedByTheDatabase:
    """The rule: an AI service may not write a business row, and a row may not
    claim agent provenance without naming the proposal that authorised it."""

    async def test_a_human_row_is_accepted(self, tenant: Tenant) -> None:
        await _insert_client(tenant)
        await tenant.commit()

    async def test_an_agent_row_naming_its_proposal_is_accepted(self, tenant: Tenant) -> None:
        """The legitimate path, which must keep working.

        Without this the constraint could be satisfied by refusing everything,
        which is a green test suite and a broken platform.
        """
        await _insert_client(
            tenant, source="agent_proposal", proposal=f"apr_{uuid.uuid4().hex[:26]}"
        )
        await tenant.commit()

    async def test_agent_source_without_a_proposal_is_refused(self, tenant: Tenant) -> None:
        with refused_because("agent_source_needs_proposal"):
            await _insert_client(tenant, source="agent_proposal")
        await tenant.session.rollback()

    async def test_human_row_citing_a_proposal_is_refused(self, tenant: Tenant) -> None:
        """The laundering direction, and the easy one to forget.

        A row with a `proposal_id` but `source='human'` would mean a model write
        was re-labelled as manual input to skip the approval engine. Both halves
        of the equivalence are checked.
        """
        with refused_because("agent_source_needs_proposal"):
            await _insert_client(tenant, source="human", proposal=f"apr_{uuid.uuid4().hex[:26]}")
        await tenant.session.rollback()

    async def test_an_unknown_source_is_refused(self, tenant: Tenant) -> None:
        with refused_because("source_known"):
            await _insert_client(tenant, source="whenever")
        await tenant.session.rollback()


class TestTenantIsolation:
    async def test_another_tenants_row_cannot_be_written(self, tenant: Tenant) -> None:
        """The WITH CHECK direction.

        Reading across tenants is a leak; writing across them is a
        data-integrity incident, and only one of the two is usually tested.
        """
        other = f"org_{uuid.uuid4().hex[:26]}"
        assert other != tenant.organization_id
        with refused_because("row-level security"):
            await _insert_client(tenant, org=other)
        await tenant.session.rollback()


class TestQuantityAndUnit:
    """`wbs_items.unit_code` is half of a composite key with `organization_id`."""

    @staticmethod
    async def _seed_wbs(t: Tenant) -> str:
        """Create a project and a WBS node, returning the node's id."""
        project_id = f"prj_{uuid.uuid4().hex[:26]}"
        await t.session.execute(
            text(
                "INSERT INTO projects (id, organization_id, code, name) "
                "VALUES (:id, :org, :code, 'P')"
            ),
            {"id": project_id, "org": t.organization_id, "code": f"P-{uuid.uuid4().hex[:6]}"},
        )
        wbs_id = (
            await t.session.execute(
                text(
                    "INSERT INTO wbs (id, organization_id, project_id, code, name) "
                    "VALUES (:id, :org, :pid, 'W-1', 'Substructure') RETURNING id"
                ),
                {
                    "id": f"wbs_{uuid.uuid4().hex[:26]}",
                    "org": t.organization_id,
                    "pid": project_id,
                },
            )
        ).scalar()
        await t.commit()
        return str(wbs_id)

    @staticmethod
    async def _insert_item(t: Tenant, wbs_id: str, unit: str = "m3") -> None:
        await t.session.execute(
            text(
                "INSERT INTO wbs_items "
                "(id, organization_id, wbs_id, code, description, unit_code, quantity) "
                "VALUES (:id, :org, :wbs, '1.1', 'Concrete C30', :unit, 120.5)"
            ),
            {
                "id": f"wbi_{uuid.uuid4().hex[:26]}",
                "org": t.organization_id,
                "wbs": wbs_id,
                "unit": unit,
            },
        )

    async def test_an_undeclared_unit_is_refused(self, tenant: Tenant) -> None:
        wbs_id = await self._seed_wbs(tenant)
        with refused_because("fk_wbs_items_organization_id_units_dictionary"):
            await self._insert_item(tenant, wbs_id)
        await tenant.session.rollback()

    async def test_a_unit_belonging_to_another_tenant_is_refused(self, tenant: Tenant) -> None:
        """The point of the composite key, stated as a test.

        Another tenant has `m3` in their vocabulary. This tenant does not. The
        insert must fail on the *foreign key* rather than quietly matching the
        other tenant's row — which is exactly what a single-column FK to
        `units_dictionary.code` would have done, had the database allowed it.
        """
        other = f"org_{uuid.uuid4().hex[:26]}"
        admin = Database.from_settings(use_admin_role=True)
        try:
            async with admin.session() as s:
                await s.execute(
                    text(
                        "INSERT INTO organizations (id, slug, name, status) "
                        "VALUES (:i, :sl, 'Other', 'active')"
                    ),
                    {"i": other, "sl": f"other-{uuid.uuid4().hex[:8]}"},
                )
                await s.execute(
                    text(
                        "INSERT INTO units_dictionary "
                        "(organization_id, code, name_vi, name_en, dimension) "
                        "VALUES (:org, 'm3', 'mét khối', 'cubic metre', 'volume')"
                    ),
                    {"org": other},
                )
                await s.commit()
        finally:
            await admin.dispose()

        wbs_id = await self._seed_wbs(tenant)
        with refused_because("fk_wbs_items_organization_id_units_dictionary"):
            await self._insert_item(tenant, wbs_id)
        await tenant.session.rollback()

    async def test_a_row_without_a_tenant_is_refused(self, tenant: Tenant) -> None:
        """An insert that omits `organization_id` must fail, and RLS is why.

        `organization_id` has no server default on purpose: a row that acquires
        its tenant by default is a row whose tenant is whatever the connection
        happened to be bound to. Leaving it unset makes the WITH CHECK clause
        fail, and the error names row-level security rather than a constraint
        nobody remembered to add.

        This test exists because it is the mistake a caller makes first, and it
        was the mistake made while writing the test above.
        """
        wbs_id = await self._seed_wbs(tenant)
        with refused_because("row-level security"):
            await tenant.session.execute(
                text(
                    "INSERT INTO wbs_items "
                    "(id, wbs_id, code, description, unit_code, quantity) "
                    "VALUES (:id, :wbs, '9.9', 'No tenant', 'm3', 1)"
                ),
                {"id": f"wbi_{uuid.uuid4().hex[:26]}", "wbs": wbs_id},
            )
        await tenant.session.rollback()

    async def test_a_declared_unit_is_accepted_with_exact_decimals(self, tenant: Tenant) -> None:
        """The same line, once `m3` exists in this tenant.

        Confirms the FK is a real reference rather than a table that refuses
        everything, and that a quantity comes back as the decimal that went in.
        """
        await tenant.session.execute(
            text(
                "INSERT INTO units_dictionary "
                "(organization_id, code, name_vi, name_en, dimension) "
                "VALUES (:org, 'm3', 'mét khối', 'cubic metre', 'volume')"
            ),
            {"org": tenant.organization_id},
        )
        await tenant.commit()
        wbs_id = await self._seed_wbs(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO wbs_items (id, organization_id, wbs_id, code, description, "
                "unit_code, quantity, unit_rate, amount) "
                "VALUES (:id, :org, :wbs, '1.1', 'Concrete C30', 'm3', 120.5, 1500000, 180750000)"
            ),
            {
                "id": f"wbi_{uuid.uuid4().hex[:26]}",
                "org": tenant.organization_id,
                "wbs": wbs_id,
            },
        )
        await tenant.commit()
        stored = (
            await tenant.session.execute(
                text("SELECT quantity, unit_code, amount FROM wbs_items WHERE wbs_id = :wbs"),
                {"wbs": wbs_id},
            )
        ).one()
        # Exact decimal, not a float approximation. 120.5 must come back as 120.5,
        # which is the whole reason the column is NUMERIC and not double.
        assert stored.quantity == 120.5
        assert stored.unit_code == "m3"
        assert stored.amount == 180750000


# ============================================================================
# Commercial front end (migration 0008)
# ============================================================================


async def _seed_client_and_opportunity(t: Tenant) -> tuple[str, str]:
    """A client and an opportunity for it, returning both ids."""
    client_id = f"cli_{uuid.uuid4().hex[:26]}"
    opportunity_id = f"opp_{uuid.uuid4().hex[:26]}"
    await t.session.execute(
        text("INSERT INTO clients (id, organization_id, code, name) VALUES (:i, :o, :c, 'Client')"),
        {"i": client_id, "o": t.organization_id, "c": f"C-{uuid.uuid4().hex[:6]}"},
    )
    await t.session.execute(
        text(
            "INSERT INTO opportunities (id, organization_id, client_id, code, title) "
            "VALUES (:i, :o, :c, :code, 'Tower B fit-out')"
        ),
        {
            "i": opportunity_id,
            "o": t.organization_id,
            "c": client_id,
            "code": f"OPP-{uuid.uuid4().hex[:6]}",
        },
    )
    await t.commit()
    return client_id, opportunity_id


async def _seed_tender(t: Tenant) -> str:
    client_id, opportunity_id = await _seed_client_and_opportunity(t)
    tender_id = f"tnd_{uuid.uuid4().hex[:26]}"
    await t.session.execute(
        text(
            "INSERT INTO tenders (id, organization_id, opportunity_id, client_id, "
            "reference_no, title) VALUES (:i, :o, :opp, :c, :ref, 'Hồ sơ mời thầu')"
        ),
        {
            "i": tender_id,
            "o": t.organization_id,
            "opp": opportunity_id,
            "c": client_id,
            "ref": f"HSMT-{uuid.uuid4().hex[:6]}",
        },
    )
    await t.commit()
    return tender_id


class TestCommercialVocabularies:
    """Closed sets, enforced. A typo in a stage must not become a stage.

    These are the columns the pipeline report groups by, so an unrecognised value
    is a row that silently disappears from every funnel number — which is the
    failure mode of a status column that accepts anything.
    """

    async def test_an_unknown_opportunity_stage_is_refused(self, tenant: Tenant) -> None:
        client_id, _ = await _seed_client_and_opportunity(tenant)
        with refused_because("ck_opportunities_stage_known"):
            await tenant.session.execute(
                text(
                    "INSERT INTO opportunities "
                    "(id, organization_id, client_id, code, title, stage) "
                    "VALUES (:i, :o, :c, :code, 'x', 'almost_won')"
                ),
                {
                    "i": uuid.uuid4().hex[:26],
                    "o": tenant.organization_id,
                    "c": client_id,
                    "code": f"O-{uuid.uuid4().hex[:6]}",
                },
            )
        await tenant.session.rollback()

    async def test_a_probability_outside_zero_to_a_hundred_is_refused(self, tenant: Tenant) -> None:
        client_id, _ = await _seed_client_and_opportunity(tenant)
        with refused_because("ck_opportunities_probability_in_range"):
            await tenant.session.execute(
                text(
                    "INSERT INTO opportunities "
                    "(id, organization_id, client_id, code, title, probability_pct) "
                    "VALUES (:i, :o, :c, :code, 'x', 140)"
                ),
                {
                    "i": uuid.uuid4().hex[:26],
                    "o": tenant.organization_id,
                    "c": client_id,
                    "code": f"O-{uuid.uuid4().hex[:6]}",
                },
            )
        await tenant.session.rollback()

    async def test_a_deeply_negative_margin_is_storable_so_it_can_be_refused(
        self, tenant: Tenant
    ) -> None:
        """-140% must be a value the gate can hold.

        Clamping the column would hide exactly the bids that should never be
        submitted, and the clamp is invisible in the data — it looks like a
        number somebody computed.
        """
        tender_id = await _seed_tender(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO bids (id, organization_id, tender_id, code, margin_pct) "
                "VALUES (:i, :o, :t, :code, -140.5)"
            ),
            {
                "i": f"bid_{uuid.uuid4().hex[:26]}",
                "o": tenant.organization_id,
                "t": tender_id,
                "code": f"B-{uuid.uuid4().hex[:6]}",
            },
        )
        await tenant.commit()
        stored = (
            await tenant.session.execute(
                text("SELECT margin_pct FROM bids WHERE tender_id = :t"),
                {"t": tender_id},
            )
        ).scalar()
        assert stored == -140.5


class TestExtractedValuesAreTriageable:
    """`tender_requirements` is the first table an AI service writes.

    The rules below are what stop it being trusted by accident: a model-authored
    value must state its confidence, must be attributable to a document, and a
    rejected one must survive so the correction is not lost.
    """

    @staticmethod
    async def _requirement(t: Tenant, **overrides: object) -> None:
        params: dict[str, object] = {
            "id": f"trq_{uuid.uuid4().hex[:26]}",
            "o": t.organization_id,
            "proposal": None,
            "confidence": None,
        }
        params.update(overrides)
        await t.session.execute(
            text(
                "INSERT INTO tender_requirements "
                "(id, organization_id, tender_id, title, source, source_actor, proposal_id, "
                " extraction_confidence) "
                "VALUES (:id, :o, :t, 'Bid bond of 3%', :source, 'tender_intelligence', "
                "        :proposal, :confidence)"
            ),
            {"t": overrides["tender_id"], **params},
        )

    async def test_a_model_row_without_a_confidence_is_refused(self, tenant: Tenant) -> None:
        """The constraint the whole review queue depends on.

        An agent row with no confidence cannot be sorted into the review queue,
        so it is a requirement that looks recorded and will never be read — and
        the ones that will disqualify the bid are precisely the ones nobody
        thinks to look at.
        """
        tender_id = await _seed_tender(tenant)
        with refused_because("ck_tender_requirements_agent_row_needs_a_confidence"):
            await self._requirement(
                tenant,
                tender_id=tender_id,
                source="agent_proposal",
                proposal=f"apr_{uuid.uuid4().hex[:26]}",
            )
        await tenant.session.rollback()

    async def test_a_model_row_with_a_confidence_is_accepted(self, tenant: Tenant) -> None:
        tender_id = await _seed_tender(tenant)
        await self._requirement(
            tenant,
            tender_id=tender_id,
            source="agent_proposal",
            proposal=f"apr_{uuid.uuid4().hex[:26]}",
            confidence="0.8200",
        )
        await tenant.commit()

    async def test_a_human_row_needs_no_confidence(self, tenant: Tenant) -> None:
        """The reverse is deliberately not required.

        A requirement typed by a person has no model confidence, and requiring
        one would mean fabricating a number in the column the review process
        sorts by.
        """
        tender_id = await _seed_tender(tenant)
        await self._requirement(tenant, tender_id=tender_id, source="human")
        await tenant.commit()

    async def test_a_confidence_above_one_is_refused(self, tenant: Tenant) -> None:
        tender_id = await _seed_tender(tenant)
        with refused_because("ck_tender_requirements_confidence_in_range"):
            await self._requirement(
                tenant,
                tender_id=tender_id,
                source="agent_proposal",
                proposal=f"apr_{uuid.uuid4().hex[:26]}",
                confidence="1.4000",
            )
        await tenant.session.rollback()

    async def test_a_rejected_extraction_survives_with_its_reason(self, tenant: Tenant) -> None:
        """A wrong answer nobody kept is a wrong answer the next run repeats.

        The row stays, marked rejected, carrying what the human said. Deleting
        it would be tidier and would throw away the only input the correction
        loop has.
        """
        tender_id = await _seed_tender(tenant)
        await self._requirement(
            tenant,
            tender_id=tender_id,
            source="agent_proposal",
            proposal=f"apr_{uuid.uuid4().hex[:26]}",
            confidence="0.6000",
        )
        await tenant.session.execute(
            text(
                "UPDATE tender_requirements SET status = 'rejected', review_note = "
                "'the 3% applies to the advance payment, not the bid bond', "
                "reviewed_by = 'estimator', reviewed_at = now() "
                "WHERE organization_id = :o"
            ),
            {"o": tenant.organization_id},
        )
        await tenant.commit()
        row = (
            await tenant.session.execute(
                text(
                    "SELECT status, review_note, reviewed_by, extraction_confidence "
                    "FROM tender_requirements WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        ).one()
        assert row.status == "rejected"
        assert "3%" in row.review_note
        assert row.reviewed_by == "estimator"
        # `0.6000`, not `0.6`: the NUMERIC column keeps the scale the
        # extractor wrote, so a confidence of 0.6 stays distinguishable
        # from 0.60.
        assert row.extraction_confidence == Decimal("0.6000")


class TestTenderPackageIntegrity:
    async def test_a_document_cannot_appear_twice_in_one_package(self, tenant: Tenant) -> None:
        """Re-ingesting the same file must not double-count its requirements.

        Without the unique index a second run of the classifier produces a second
        `tender_documents` row and every figure derived from the package is
        silently doubled.
        """
        tender_id = await _seed_tender(tenant)
        document_id = f"doc_{uuid.uuid4().hex[:26]}"
        await tenant.session.execute(
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash) "
                "VALUES (:i, :o, 'HSMT.pdf', :h)"
            ),
            {"i": document_id, "o": tenant.organization_id, "h": uuid.uuid4().hex},
        )
        for _ in range(2):
            try:
                await tenant.session.execute(
                    text(
                        "INSERT INTO tender_documents "
                        "(id, organization_id, tender_id, document_id) "
                        "VALUES (:i, :o, :t, :d)"
                    ),
                    {
                        "i": f"tdo_{uuid.uuid4().hex[:26]}",
                        "o": tenant.organization_id,
                        "t": tender_id,
                        "d": document_id,
                    },
                )
                await tenant.commit()
            except Exception:
                await tenant.session.rollback()
                return
        pytest.fail("the same document was accepted twice in one tender package")

    async def test_a_bid_line_cannot_use_another_tenants_unit(self, tenant: Tenant) -> None:
        """`bid_items` carries the composite FK too.

        A rate quoted against a unit belonging to another tenant is a wrong
        number in the bid that submitted the tender, which is the last place a
        wrong number can be caught.
        """
        other = f"org_{uuid.uuid4().hex[:26]}"
        admin = Database.from_settings(use_admin_role=True)
        try:
            async with admin.session() as s:
                await s.execute(
                    text(
                        "INSERT INTO organizations (id, slug, name, status) "
                        "VALUES (:i, :sl, 'O', 'active')"
                    ),
                    {"i": other, "sl": f"o-{uuid.uuid4().hex[:8]}"},
                )
                await s.execute(
                    text(
                        "INSERT INTO units_dictionary (organization_id, code, name_en, dimension) "
                        "VALUES (:o, 'm2', 'square metre', 'area')"
                    ),
                    {"o": other},
                )
                await s.commit()
        finally:
            await admin.dispose()

        tender_id = await _seed_tender(tenant)
        bid_id = (
            await tenant.session.execute(
                text(
                    "INSERT INTO bids (id, organization_id, tender_id, code) "
                    "VALUES (:i, :o, :t, :code) RETURNING id"
                ),
                {
                    "i": f"bid_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "t": tender_id,
                    "code": f"B-{uuid.uuid4().hex[:6]}",
                },
            )
        ).scalar()
        await tenant.commit()

        with refused_because("fk_bid_items_organization_id_units_dictionary"):
            await tenant.session.execute(
                text(
                    "INSERT INTO bid_items (id, organization_id, bid_id, code, description, "
                    "unit_code, quantity, unit_rate) "
                    "VALUES (:i, :o, :b, '1.1', 'Partition', 'm2', 85.5, 950000)"
                ),
                {
                    "i": f"bi_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "b": bid_id,
                },
            )
        await tenant.session.rollback()


# ============================================================================
# Contracts, variations, claims, suppliers (migration 0009)
# ============================================================================


async def _seed_contract(t: Tenant, **overrides: object) -> str:
    """A signed contract with a client, which is the ordinary case.

    Returns the contract id. Supplier-side contracts are exercised separately,
    because the XOR check is the interesting part.
    """
    await t.session.execute(
        text("INSERT INTO clients (id, organization_id, code, name) VALUES (:i, :o, :c, 'Client')"),
        {
            "i": f"cli_{uuid.uuid4().hex[:26]}",
            "o": t.organization_id,
            "c": f"C-{uuid.uuid4().hex[:6]}",
        },
    )
    await t.commit()
    params: dict[str, object] = {
        "i": f"con_{uuid.uuid4().hex[:26]}",
        "o": t.organization_id,
        "code": f"HD-{uuid.uuid4().hex[:6]}",
        # `signed` by default, and every test that needs a different state
        # passes it. Every state past `approved` requires `signed_at`, so a
        # default of `draft` here would make all of them fail on the wrong
        # constraint.
        "status": "signed",
        "client": (
            await t.session.execute(
                text("SELECT id FROM clients WHERE organization_id = :o LIMIT 1"),
                {"o": t.organization_id},
            )
        ).scalar(),
    }
    params.update(overrides)
    await t.session.execute(
        text(
            "INSERT INTO contracts (id, organization_id, code, client_id, status, signed_at) "
            "VALUES (:i, :o, :code, :client, :status, CURRENT_DATE)"
        ),
        params,
    )
    await t.commit()
    return str(params["i"])


class TestContractCounterpartyIsExactlyOne:
    async def test_a_contract_with_no_counterparty_is_refused(self, tenant: Tenant) -> None:
        """Both null orphans every milestone, variation and claim under it.

        A framework contract awaiting a call-off has a *project* but still has a
        client, so there is no legitimate version of this row.
        """
        with refused_because("ck_contracts_exactly_one_counterparty"):
            await tenant.session.execute(
                text("INSERT INTO contracts (id, organization_id, code) VALUES (:i, :o, :code)"),
                {
                    "i": f"con_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "code": f"HD-{uuid.uuid4().hex[:6]}",
                },
            )
        await tenant.session.rollback()

    async def test_a_contract_with_two_counterparties_is_refused(self, tenant: Tenant) -> None:
        await _seed_contract(tenant)
        client_id, supplier_id = await _seed_supplier_pair(tenant)
        with refused_because("ck_contracts_exactly_one_counterparty"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contracts (id, organization_id, code, client_id, supplier_id) "
                    "VALUES (:i, :o, :code, :c, :s)"
                ),
                {
                    "i": f"con_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "code": f"HD-{uuid.uuid4().hex[:6]}",
                    "c": client_id,
                    "s": supplier_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_supplier_contract_is_accepted(self, tenant: Tenant) -> None:
        """The other side of the XOR, so the constraint is not just a wall."""
        _, supplier_id = await _seed_supplier_pair(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO contracts (id, organization_id, code, supplier_id, party_role) "
                "VALUES (:i, :o, :code, :s, 'supplier')"
            ),
            {
                "i": f"con_{uuid.uuid4().hex[:26]}",
                "o": tenant.organization_id,
                "code": f"HD-{uuid.uuid4().hex[:6]}",
                "s": supplier_id,
            },
        )
        await tenant.commit()


async def _seed_supplier_pair(t: Tenant) -> tuple[str, str]:
    """A client and an approved supplier, returning both ids."""
    client_id = f"cli_{uuid.uuid4().hex[:26]}"
    supplier_id = f"sup_{uuid.uuid4().hex[:26]}"
    await t.session.execute(
        text("INSERT INTO clients (id, organization_id, code, name) VALUES (:i, :o, :c, 'C')"),
        {"i": client_id, "o": t.organization_id, "c": f"C-{uuid.uuid4().hex[:6]}"},
    )
    await t.session.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, status, "
            "qualification_expires_on) VALUES (:i, :o, :code, 'Supplier', 'approved', "
            "CURRENT_DATE + 365)"
        ),
        {"i": supplier_id, "o": t.organization_id, "code": f"S-{uuid.uuid4().hex[:6]}"},
    )
    await t.commit()
    return client_id, supplier_id


class TestStatusMeansSomething:
    """A status that can be reached by accident is not a control.

    These four checks are the difference between a state machine and a set of
    labels, and each one closes a way a row arrives somewhere it should not.
    """

    async def test_signed_requires_a_signature_date(self, tenant: Tenant) -> None:
        await _seed_contract(tenant)
        with refused_because("ck_contracts_signed_requires_a_date"):
            await tenant.session.execute(
                text(
                    "UPDATE contracts SET status = 'signed', signed_at = NULL "
                    "WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_approved_requires_current_qualification_papers(self, tenant: Tenant) -> None:
        """`approved` is the word that makes somebody stop looking.

        An approved supplier with lapsed papers is a supplier that was approved
        once, and the date is the only thing that distinguishes the two.
        """
        await _seed_supplier_pair(tenant)
        with refused_because("ck_suppliers_an_approved_supplier_is_qualified"):
            await tenant.session.execute(
                text(
                    "UPDATE suppliers SET qualification_expires_on = CURRENT_DATE - 1 "
                    "WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_a_payment_amount_only_on_a_payment_milestone(self, tenant: Tenant) -> None:
        """Otherwise a delivery milestone rolls into a cash schedule.

        And nobody knows why the payment total exceeds the contract.
        """
        contract_id = await _seed_contract(tenant)
        with refused_because("ck_contract_milestones_amount_only_on_payment_milestones"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contract_milestones (id, organization_id, contract_id, "
                    "code, name, kind, amount) "
                    "VALUES (:i, :o, :c, 'M1', 'Deliver lighting', 'delivery', 5000000)"
                ),
                {
                    "i": f"cms_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "c": contract_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_payment_milestone_needs_an_amount(self, tenant: Tenant) -> None:
        """The other direction, so the pair is a real equivalence."""
        contract_id = await _seed_contract(tenant)
        with refused_because("ck_contract_milestones_amount_only_on_payment_milestones"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contract_milestones (id, organization_id, contract_id, "
                    "code, name, kind) "
                    "VALUES (:i, :o, :c, 'M1', 'Milestone 1', 'payment')"
                ),
                {
                    "i": f"cms_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "c": contract_id,
                },
            )
        await tenant.session.rollback()


class TestVariationGapIsTheMoney:
    """`instructed-but-unvalued` is the query the platform exists to make easy."""

    async def test_a_privileged_variation_requires_an_instruction_date(
        self, tenant: Tenant
    ) -> None:
        contract_id = await _seed_contract(tenant)
        with refused_because("ck_contract_variations_a_privileged_variation_was_instructed"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contract_variations (id, organization_id, contract_id, "
                    "code, title, status) "
                    "VALUES (:i, :o, :c, 'V1', 'Extra lobby finishes', 'instructed')"
                ),
                {
                    "i": f"cva_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "c": contract_id,
                },
            )
        await tenant.session.rollback()

    async def test_an_unvalued_instruction_is_receivable_and_claimable(
        self, tenant: Tenant
    ) -> None:
        """The scenario the claimed/approved split exists for.

        Instructed in writing for 480,000,000, valued at nothing yet. That gap
        is money the company is owed and no ordinary report shows it, because
        every report totals approved values.
        """
        contract_id = await _seed_contract(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO contract_variations (id, organization_id, contract_id, code, "
                "title, status, instructed_at, value_claimed, value_approved) "
                "VALUES (:i, :o, :c, 'V1', 'Extra lobby finishes', 'instructed', "
                "CURRENT_DATE, 480000000, 0)"
            ),
            {"i": f"cva_{uuid.uuid4().hex[:26]}", "o": tenant.organization_id, "c": contract_id},
        )
        await tenant.commit()
        gap = (
            await tenant.session.execute(
                text(
                    "SELECT value_claimed - value_approved AS gap FROM contract_variations "
                    "WHERE organization_id = :o AND status = 'instructed'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert gap == 480000000, (
            "an instructed variation valued at nothing must show its claimed value "
            "as a gap; this is the receivable the schema exists to make findable"
        )

    async def test_a_submitted_claim_must_have_been_notified(self, tenant: Tenant) -> None:
        """Notice is the defence, and `draft` is the one state where it is absent."""
        contract_id = await _seed_contract(tenant)
        with refused_because("ck_contract_claims_a_submitted_claim_was_notified"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contract_claims (id, organization_id, contract_id, code, "
                    "title, status) VALUES (:i, :o, :c, 'CL1', 'Delay claim', 'submitted')"
                ),
                {
                    "i": f"clm_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "c": contract_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_notice_cannot_predate_the_event(self, tenant: Tenant) -> None:
        contract_id = await _seed_contract(tenant)
        with refused_because("ck_contract_claims_notice_is_not_before_the_event"):
            await tenant.session.execute(
                text(
                    "INSERT INTO contract_claims (id, organization_id, contract_id, code, "
                    "title, status, event_date, notified_at) "
                    "VALUES (:i, :o, :c, 'CL1', 'Delay claim', 'notified', "
                    "CURRENT_DATE, CURRENT_DATE - 30)"
                ),
                {
                    "i": f"clm_{uuid.uuid4().hex[:26]}",
                    "o": tenant.organization_id,
                    "c": contract_id,
                },
            )
        await tenant.session.rollback()


class TestRetentionAndAdvanceCannotExceedTheContract:
    async def test_retention_plus_advance_above_a_hundred_is_refused(self, tenant: Tenant) -> None:
        """10% retention and a 95% advance is a contract that is not viable.

        Cheap to catch when the row is written; not cheap at claim time.
        """
        await _seed_contract(tenant)
        with refused_because("ck_contracts_retention_plus_advance_within_contract_value"):
            await tenant.session.execute(
                text(
                    "UPDATE contracts SET retention_pct = 10, advance_payment_pct = 95 "
                    "WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_the_ordinary_ten_and_thirty_is_accepted(self, tenant: Tenant) -> None:
        """The common Vietnamese construction figures, so the check is not a wall."""
        await _seed_contract(tenant)
        await tenant.session.execute(
            text(
                "UPDATE contracts SET retention_pct = 10, advance_payment_pct = 30, "
                "liquidated_damages_pct = 5, payment_terms_days = 45, warranty_months = 12 "
                "WHERE organization_id = :o"
            ),
            {"o": tenant.organization_id},
        )
        await tenant.commit()
        row = (
            await tenant.session.execute(
                text(
                    "SELECT retention_pct, advance_payment_pct, payment_terms_days "
                    "FROM contracts WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        ).one()
        assert row.retention_pct == 10
        assert row.advance_payment_pct == 30
        assert row.payment_terms_days == 45


class TestClaimChronologyIsAppendOnly:
    """Documented append-only, and now actually append-only.

    The module docstring said events are "added, never edited", and the schema
    granted the application role UPDATE and DELETE on the table — so the claim was
    a convention. This is F77's shape: a documented invariant with nothing behind
    it, found by auditing the new module against its own prose.

    The same reasoning migration 0002 used for `audit_logs`, and for the same
    reason: a chronology that can be edited is not evidence. "Your log shows the
    notice on the 4th, ours shows the 9th" is settled by whether the row can be
    changed after the fact.
    """

    @staticmethod
    async def _claim_with_event(t: Tenant) -> tuple[str, str]:
        contract_id = await _seed_contract(t)
        claim_id = f"clm_{uuid.uuid4().hex[:26]}"
        event_id = f"cev_{uuid.uuid4().hex[:26]}"
        await t.session.execute(
            text(
                "INSERT INTO contract_claims (id, organization_id, contract_id, code, "
                "title, status, notified_at) "
                "VALUES (:i, :o, :c, 'CL1', 'Delay claim', 'notified', CURRENT_DATE)"
            ),
            {"i": claim_id, "o": t.organization_id, "c": contract_id},
        )
        await t.session.execute(
            text(
                "INSERT INTO claim_events (id, organization_id, claim_id, event_type, "
                "occurred_at, title) "
                "VALUES (:i, :o, :cl, 'notice', now(), 'Notice issued')"
            ),
            {"i": event_id, "o": t.organization_id, "cl": claim_id},
        )
        await t.commit()
        return claim_id, event_id

    async def test_an_event_can_be_appended(self, tenant: Tenant) -> None:
        """The legitimate path. Without this the grant could be satisfied by
        refusing everything, which is a green suite and a broken system."""
        claim_id, _ = await self._claim_with_event(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO claim_events (id, organization_id, claim_id, event_type, "
                "occurred_at, title) "
                "VALUES (:i, :o, :cl, 'correspondence', now(), 'Chased')"
            ),
            {"i": f"cev_{uuid.uuid4().hex[:26]}", "o": tenant.organization_id, "cl": claim_id},
        )
        await tenant.commit()
        count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM claim_events WHERE claim_id = :cl"),
                {"cl": claim_id},
            )
        ).scalar()
        assert count == 2

    async def test_an_event_cannot_be_rewritten(self, tenant: Tenant) -> None:
        """The date is the whole point. If it can be moved, the chronology proves
        nothing and the table is decoration."""
        _, event_id = await self._claim_with_event(tenant)
        with pytest.raises(Exception) as excinfo:
            await tenant.session.execute(
                text(
                    "UPDATE claim_events SET occurred_at = now() - interval '5 days', "
                    "title = 'Notice issued earlier' WHERE id = :i"
                ),
                {"i": event_id},
            )
        await tenant.session.rollback()
        message = str(excinfo.value).lower()
        assert "privilege" in message or "permission" in message, (
            f"the UPDATE should be refused by the grant, got: {excinfo.value}"
        )

    async def test_an_event_cannot_be_deleted(self, tenant: Tenant) -> None:
        """Deletion is the other half. An event that can be removed is an event
        that was never evidence, and the gap is invisible."""
        _, event_id = await self._claim_with_event(tenant)
        with pytest.raises(Exception) as excinfo:
            await tenant.session.execute(
                text("DELETE FROM claim_events WHERE id = :i"), {"i": event_id}
            )
        await tenant.session.rollback()
        message = str(excinfo.value).lower()
        assert "privilege" in message or "permission" in message, (
            f"the DELETE should be refused by the grant, got: {excinfo.value}"
        )
