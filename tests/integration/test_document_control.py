"""Document control, against the live database. **Mostly about what must not happen.**

`sop_definitions` was a catalogue: twenty-eight rows, a code as a flat string, a version
table for the *procedure* and nothing at all for the *document*. Three claims followed, none
of them enforced:

1. a document's identity is `ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]`, parsed — so a hand-typed code
   with four segments cannot be stored;
2. a version's window says which revision was in force, so a superseded revision that never
   ends cannot be stored;
3. a distribution is an obligation somebody can be shown to have, so an acknowledgement with
   no name, or dated before the send, cannot be stored.

## Why this file is mostly negative assertions

Each constraint below is tested by inserting the row that violates it and requiring the
database to refuse. A test that inserts a *good* row and reads it back only proves the
column exists; it passes whether or not the `CHECK` is there, and a `CHECK` that was never
created is the failure that matters, because the schema's whole claim is that these states
are unrepresentable. The positive path gets one test each, for the same reason a smoke
detector needs a working alarm: a test suite where every negative passes because nothing
throws has not shown that inserts work at all.

So the count is deliberately lopsided: **more "must be refused" than "must be accepted"**.

## The permission to be wrong is checked, not assumed

`document_versions.approval_id` exists so Tập 3 §4.1 -- *an agent is never Accountable* --
is a constraint rather than a review convention: a version an agent proposed cannot be
marked `approved` without an approval somebody gave. If that check were missing, an agent
could mark its own document approved and every downstream number would be unauditable. It
is the single most important negative assertion in this file, and it is asserted in **both**
directions: an agent row with no approval is refused, *and* a human row that claims a
proposal is refused. The second direction is the one that keeps the audit trail honest when
somebody wants a row to look agent-written.

## Expiry and retention are separate, and the combination is refused

`documents.expires_on` is "is this still valid" and `documents.retention_until` is "how long
must we keep it". A construction permit has a three-year life and a seven-year retention
obligation, so the two dates are genuinely different and a single date column would have to
record the wrong one. `ck_documents_expiry_precedes_retention` refuses an expiry that
outlives the retention deadline -- a document still in force when it must be destroyed. The
tests below build the permit case explicitly, because it is the case that proves the two
columns are not the same column wearing two names.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_orchestrator.domain.document_code import Block, DocumentCode
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]


def _id(tenant: Tenant, prefix: str, suffix: str = "a") -> str:
    """A row id that is unique **per test**, not per name.

    `documents.id` is the primary key on its own -- not `(organization_id, id)` -- and the
    test database is never truncated. So a fixed id like `doc_a` is written by the first
    test that commits it and refused by every test after, on a *different* organization's
    row. The symptom is `duplicate key ... "pk_documents"` in a test about something else,
    which is a support ticket rather than an answer.

    Deriving the tail from the organization id makes each test's rows its own, and keeps
    the test database's growth to the organization it already leaks.
    """
    return f"{prefix}_{tenant.organization_id[-8:]}_{suffix}"


async def _document(tenant: Tenant, **overrides: object) -> str:
    """Insert a valid `documents` row and return its id."""
    suffix = str(overrides.pop("suffix", "a"))
    values: dict[str, object] = {
        "org": tenant.organization_id,
        "id": _id(tenant, "doc", suffix),
        "title": "Kế hoạch kiểm soát chất lượng",
        "hash": f"{overrides.pop('hash', 'h1')}".ljust(64, "0")[:64],
        "code": str(overrides.pop("code", DocumentCode.make(Block.MO, "QA", 7))),
    }
    values.update(overrides)
    await tenant.session.execute(
        text(
            "INSERT INTO documents (id, organization_id, title, content_hash, code, "
            "  expires_on, retention_until) "
            "VALUES (:id, CAST(:org AS varchar(64)), :title, :hash, :code, "
            "  :expires, :retention)"
        ),
        {
            "id": values["id"],
            "org": values["org"],
            "title": values["title"],
            "hash": values["hash"],
            "code": values["code"],
            "expires": values.get("expires"),
            "retention": values.get("retention"),
        },
    )
    return str(values["id"])


async def _version(tenant: Tenant, document_id: str, **overrides: object) -> str:
    """Insert a valid `document_versions` row and return its id."""
    values: dict[str, object] = {
        "id": _id(tenant, "dv", str(overrides.pop("suffix", "1"))),
        "no": overrides.pop("no", 1),
        "hash": f"{overrides.pop('hash', 'v1')}".ljust(64, "0")[:64],
        "status": overrides.pop("status", "approved"),
        "issued": overrides.pop("issued", dt.date(2026, 1, 15)),
        "effective_from": overrides.pop("effective_from", dt.date(2026, 2, 1)),
        "effective_to": overrides.pop("effective_to", None),
        "source": overrides.pop("source", "human"),
        "actor": overrides.pop("actor", ""),
        "proposal": overrides.pop("proposal", None),
        "approval": overrides.pop("approval", None),
    }
    await tenant.session.execute(
        text(
            "INSERT INTO document_versions (id, organization_id, document_id, version_no, "
            "  status, content_hash, issued_on, effective_from, effective_to, "
            "  source, source_actor, proposal_id, approval_id) "
            "VALUES (:id, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), :no, "
            "  :status, :hash, :issued, :ef, :et, :source, :actor, :proposal, :approval)"
        ),
        {
            "id": values["id"],
            "org": tenant.organization_id,
            "doc": document_id,
            "no": values["no"],
            "status": values["status"],
            "hash": values["hash"],
            "issued": values["issued"],
            "ef": values["effective_from"],
            "et": values["effective_to"],
            "source": values["source"],
            "actor": values["actor"],
            "proposal": values["proposal"],
            "approval": values["approval"],
        },
    )
    return str(values["id"])


#: Table name to id prefix, for the row ids `_refused` has to invent. Kept here rather
#: than at each call site so that a refusal statement reads as the *rule* being tested and
#: nothing else.
_PREFIX = {"documents": "doc", "document_versions": "dv", "document_distributions": "dd"}


async def _refused(tenant: Tenant, statement: object, params: dict) -> str:
    """Run an insert that must fail, and return the name of the constraint that refused it.

    Asserting the *constraint name* rather than merely that something raised is what makes
    this a test of the rule and not a test of the connection: a typo in a column name also
    raises, and only the name distinguishes the two. Every kind of constraint counts --
    `ck_`, `uq_`, `fk_` -- because "this row is impossible" is enforced by all three, and an
    earlier version of this helper matched on `ck_` alone, so every unique-constraint test
    below it was passing for the wrong reason.

    A statement naming `:rid` gets its row id filled in here rather than at the call site.
    That is not a convenience: the id has to be unique per test, because these inserts are
    expected to fail and a *primary key* collision would be refused too -- so a fixed
    literal made the test assert `pk_documents` on the second run and the intended check
    on the first. Deriving it from the tenant's own organization removes the whole class.
    """
    sql = str(getattr(statement, "text", statement))
    if ":rid" in sql:
        table = re.search(r"INSERT INTO (\w+)", sql)
        assert table is not None, f"no INSERT INTO in the statement under test: {sql}"
        params = {**params, "rid": _id(tenant, _PREFIX[table.group(1)], "bad")}
    try:
        await tenant.session.execute(statement, params)
    except (IntegrityError, DBAPIError) as refusal:
        message = str(getattr(refusal, "orig", refusal))
        named = re.search(r'constraint\s+"([^"]+)"', message) or re.search(
            r"violates\s+(\w+)", message
        )
        assert named is not None, (
            f"refused, but not by a named constraint -- so this is a typo, not the rule: {message}"
        )
        await tenant.session.rollback()
        return named.group(1)
    raise AssertionError("the database accepted a row the schema says is impossible")


class TestTheCode:
    async def test_a_five_segment_code_is_stored(self, tenant: Tenant) -> None:
        await _document(tenant, code=DocumentCode.make(Block.BO, "HR", 4))
        await tenant.commit()
        stored = (await tenant.session.execute(text("SELECT code FROM documents"))).scalar()
        assert stored == "ONX-BO-HR-SOP-004"

    async def test_a_four_segment_code_is_refused(self, tenant: Tenant) -> None:
        """The `CHECK` is the *shape*; the parser is the grammar. This tests the shape."""
        message = await _refused(
            tenant,
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash, code) "
                "VALUES (:rid, CAST(:org AS varchar(64)), 'x', :h, 'ONX-BO-HR-SOP')"
            ),
            {"org": tenant.organization_id, "h": "h".ljust(64, "0")},
        )
        assert "code_is_well_formed" in message, message

    async def test_a_lowercase_code_is_refused(self, tenant: Tenant) -> None:
        """`parse` refuses case too. The `CHECK` must not be the softer one."""
        await _refused(
            tenant,
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash, code) "
                "VALUES (:rid, CAST(:org AS varchar(64)), 'x', :h, 'onx-bo-hr-sop-004')"
            ),
            {"org": tenant.organization_id, "h": "h2".ljust(64, "0")},
        )

    async def test_a_code_is_unique_per_tenant(self, tenant: Tenant) -> None:
        await _document(tenant, suffix="u1", code=DocumentCode.make(Block.MO, "PM", 2))
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash, code) "
                "VALUES (:rid, CAST(:org AS varchar(64)), 'y', :h, 'ONX-MO-PM-SOP-002')"
            ),
            {"org": tenant.organization_id, "h": "h3".ljust(64, "0")},
        )
        assert "uq_documents_org_code" in message, message

    async def test_the_same_code_in_another_tenant_is_fine(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """The unique key is `(organization_id, code)`, not `code`.

        Two companies both have `ONX-BO-HR-SOP-004`. A unique key on the code alone would
        make the second company's HR department unable to file anything.
        """
        await _document(tenant, code=DocumentCode.make(Block.BO, "HR", 4))
        await tenant.commit()
        await _document(other_tenant, code=DocumentCode.make(Block.BO, "HR", 4))
        await other_tenant.commit()

        # Counted **from each session**, not once from a single one. RLS means a session
        # sees only its own organization, so one count across both can only ever return
        # 1 -- the first version of this test asserted 2 and was measuring the policy
        # rather than the key. Each session finding its own row is the actual claim.
        for holder in (tenant, other_tenant):
            found = (
                await holder.session.execute(
                    text("SELECT count(*) FROM documents WHERE code = 'ONX-BO-HR-SOP-004'")
                )
            ).scalar()
            assert found == 1, (
                f"{holder.slug} holds {found} rows with the code; each should hold its own"
            )


class TestExpiryIsNotRetention:
    async def test_a_permit_expires_long_before_it_may_be_destroyed(self, tenant: Tenant) -> None:
        """The case that makes two columns necessary rather than redundant.

        A construction permit: valid three years, kept seven. If `expires_on` and
        `retention_until` were one column, one of those two facts would have to live
        somewhere unrecorded.
        """
        await _document(
            tenant,
            expires=dt.date(2029, 3, 1),
            retention=dt.datetime(2033, 3, 1, tzinfo=dt.UTC),
        )
        await tenant.commit()
        row = (
            await tenant.session.execute(text("SELECT expires_on, retention_until FROM documents"))
        ).first()
        assert row is not None
        assert row[0] == dt.date(2029, 3, 1)
        assert row[1].year == 2033

    async def test_an_expiry_after_the_retention_deadline_is_refused(self, tenant: Tenant) -> None:
        """Still in force when it must be destroyed. Nobody decides that on purpose."""
        message = await _refused(
            tenant,
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash, code, "
                "  expires_on, retention_until) VALUES "
                "(:rid, CAST(:org AS varchar(64)), 'x', :h, 'ONX-MO-HSE-SOP-008', "
                "  DATE '2035-01-01', TIMESTAMPTZ '2030-01-01 00:00:00+00')"
            ),
            {"org": tenant.organization_id, "h": "he".ljust(64, "0")},
        )
        assert "expiry_precedes_retention" in message, message

    async def test_a_null_either_way_is_accepted(self, tenant: Tenant) -> None:
        """Neither date implies the other, so each may be null on its own."""
        # Distinct codes: `(organization_id, code)` is unique, so three rows that all
        # defaulted to `ONX-MO-QA-SOP-007` are one document and two violations.
        await _document(
            tenant,
            suffix="n1",
            code=DocumentCode.make(Block.MO, "QA", 1),
            expires=dt.date(2030, 1, 1),
        )
        await _document(
            tenant,
            suffix="n2",
            code=DocumentCode.make(Block.MO, "QA", 2),
            hash="h9",
            retention=dt.datetime(2035, 1, 1, tzinfo=dt.UTC),
        )
        await _document(tenant, suffix="n3", code=DocumentCode.make(Block.MO, "QA", 3), hash="h8")
        await tenant.commit()
        assert (await tenant.session.execute(text("SELECT count(*) FROM documents"))).scalar() == 3


class TestTheVersionWindow:
    async def test_a_superseded_version_must_end(self, tenant: Tenant) -> None:
        """A version table that permits a superseded row with no end date cannot answer
        "which revision was in force on the tenth"."""
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'superseded', :h, DATE '2026-01-01')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "vs".ljust(64, "0")},
        )
        assert "a_superseded_version_ends" in message, message

    async def test_a_window_that_ends_before_it_starts_is_refused(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                # `issued_on` is here so that the *only* rule this row breaks is the
                # window. Postgres reports whichever check it evaluates first, and a row
                # that breaks two of them asserts a constraint name chosen by the planner
                # rather than by the test.
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on, effective_from, "
                "  effective_to) VALUES (:rid, CAST(:org AS varchar(64)), "
                "  CAST(:doc AS varchar(64)), 1, 'approved', :h, DATE '2026-01-01', "
                "  DATE '2026-06-01', DATE '2026-01-01')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "vw".ljust(64, "0")},
        )
        assert "the_window_is_not_inverted" in message, message

    async def test_an_approved_version_must_be_issued(self, tenant: Tenant) -> None:
        """`approved` with no `issued_on` is a document in force that was never published."""
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'approved', :h)"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "vi".ljust(64, "0")},
        )
        assert "an_approved_version_is_issued" in message, message

    async def test_an_unknown_status_is_refused(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'published', :h, DATE '2026-01-01')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "vu".ljust(64, "0")},
        )
        assert "status_known" in message, message

    async def test_version_numbers_are_unique_per_document(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        await _version(tenant, document, suffix="1")
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'approved', :h, DATE '2026-01-01')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "vd".ljust(64, "0")},
        )
        assert "uq_document_versions_number" in message, message

    async def test_a_version_may_not_point_at_another_tenants_document(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """The composite key is the point of the retrofit in migrations 0020-0023.

        A bare `document_id` foreign key would let tenant B write a version against
        tenant A's document, and the row would be invisible to tenant A's own queries --
        a cross-tenant write that no RLS policy sees, because RLS filters rows by
        `organization_id` and this row *claims* tenant B.
        """
        theirs = await _document(other_tenant, code=DocumentCode.make(Block.FO, "BD", 1))
        await other_tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'approved', :h, DATE '2026-01-01')"
            ),
            {"org": tenant.organization_id, "doc": theirs, "h": "vx".ljust(64, "0")},
        )
        assert "fk_document_versions_document" in message, message


class TestAnAgentIsNeverAccountable:
    """Tập 3 §4.1, as a `CHECK`. Both directions, because either one alone is a hole."""

    async def test_an_agent_version_without_an_approval_is_refused(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on, source, proposal_id) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'approved', :h, DATE '2026-01-01', 'agent_proposal', 'pr_1')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "aa".ljust(64, "0")},
        )
        assert "agent_needs_approval" in message, message

    async def test_an_agent_version_with_an_approval_is_accepted(self, tenant: Tenant) -> None:
        """The positive path, so the negative test above is not passing for the wrong
        reason -- a check that refused everything would also pass it."""
        document = await _document(tenant)
        await _version(
            tenant,
            document,
            suffix="ok",
            source="agent_proposal",
            actor="Knowledge Agent",
            proposal="pr_1",
            approval="ap_1",
        )
        await tenant.commit()
        assert (
            await tenant.session.execute(
                text("SELECT approval_id FROM document_versions WHERE id = :i"),
                {"i": _id(tenant, "dv", "ok")},
            )
        ).scalar() == "ap_1"

    async def test_a_human_row_claiming_a_proposal_is_refused(self, tenant: Tenant) -> None:
        """The other direction of the house rule.

        Without it, a row can name a proposal while claiming to be human-entered, and
        "did a model write this" becomes a question with two answers.
        """
        document = await _document(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_versions (id, organization_id, document_id, "
                "  version_no, status, content_hash, issued_on, source, proposal_id) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:doc AS varchar(64)), 1, "
                "  'approved', :h, DATE '2026-01-01', 'human', 'pr_1')"
            ),
            {"org": tenant.organization_id, "doc": document, "h": "ah".ljust(64, "0")},
        )
        assert "agent_source_needs_proposal" in message, message


class TestTheDistributionMatrix:
    async def _distributed(self, tenant: Tenant, **overrides: object) -> tuple[str, str]:
        document = await _document(tenant, code=DocumentCode.make(Block.PMO, "GOV", 1))
        await tenant.commit()
        version = await _version(tenant, document, suffix="d1")
        await tenant.commit()
        values: dict[str, object] = {
            "id": _id(tenant, "dd", str(overrides.pop("suffix", "1"))),
            "audience": overrides.pop("audience", "site_manager"),
            "channel": overrides.pop("channel", "system"),
            "mandatory": overrides.pop("mandatory", True),
            "sent": overrides.pop("sent", dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.UTC)),
            "acked": overrides.pop("acked", None),
            "by": overrides.pop("by", ""),
        }
        await tenant.session.execute(
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, channel, is_mandatory, "
                "  distributed_at, acknowledged_at, acknowledged_by) VALUES "
                "(:id, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), :aud, :ch, "
                "  :mand, :sent, :acked, :by)"
            ),
            {
                "id": values["id"],
                "org": tenant.organization_id,
                "v": version,
                "aud": values["audience"],
                "ch": values["channel"],
                "mand": values["mandatory"],
                "sent": values["sent"],
                "acked": values["acked"],
                "by": values["by"],
            },
        )
        return document, version

    async def test_a_sent_but_unacknowledged_row_is_valid(self, tenant: Tenant) -> None:
        """The state the matrix exists to make visible: outstanding, not lost."""
        await self._distributed(tenant)
        await tenant.commit()
        assert (
            await tenant.session.execute(
                text("SELECT count(*) FROM document_distributions WHERE acknowledged_at IS NULL")
            )
        ).scalar() == 1

    async def test_an_acknowledgement_with_nobody_behind_it_is_refused(
        self, tenant: Tenant
    ) -> None:
        """ "Acknowledged" must mean somebody said so. A timestamp alone does not."""
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, is_mandatory, "
                "  distributed_at, acknowledged_at, acknowledged_by) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), "
                "  'site_manager', false, TIMESTAMPTZ '2026-03-01 09:00:00+00', "
                "  TIMESTAMPTZ '2026-03-02 09:00:00+00', '')"
            ),
            {"org": tenant.organization_id, "v": await self._version_id(tenant)},
        )
        assert "an_acknowledgement_names_who" in message, message

    async def test_an_acknowledgement_before_the_send_is_refused(self, tenant: Tenant) -> None:
        """A clock fault that would otherwise make a lag calculation negative."""
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, distributed_at, "
                "  acknowledged_at, acknowledged_by) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), "
                "  'site_manager', TIMESTAMPTZ '2026-03-05 09:00:00+00', "
                "  TIMESTAMPTZ '2026-03-01 09:00:00+00', 'Nguyễn Văn A')"
            ),
            {"org": tenant.organization_id, "v": await self._version_id(tenant)},
        )
        assert "acknowledged_after_distribution" in message, message

    async def test_an_unknown_channel_is_refused(self, tenant: Tenant) -> None:
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, is_mandatory, channel) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), "
                "  'site_manager', false, 'carrier_pigeon')"
            ),
            {"org": tenant.organization_id, "v": await self._version_id(tenant)},
        )
        assert "channel_known" in message, message

    async def test_a_resend_updates_the_obligation_rather_than_doubling_it(
        self, tenant: Tenant
    ) -> None:
        """The unique key is the whole reason a retry cannot inflate the compliance figure.

        Without it, retrying a failed distribution silently doubles a mandatory count, and
        a number that can go up by retrying is worse than no number.
        """
        _, version = await self._distributed(tenant)
        await tenant.commit()
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, is_mandatory, channel) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), "
                "  'site_manager', false, 'system')"
            ),
            {"org": tenant.organization_id, "v": version},
        )
        assert "uq_document_distributions_target" in message, message

    async def test_an_acknowledgement_carries_provenance_too(self, tenant: Tenant) -> None:
        """A distribution is a business row, so the house rule applies to it as well."""
        message = await _refused(
            tenant,
            text(
                "INSERT INTO document_distributions (id, organization_id, "
                "  document_version_id, audience_role_key, is_mandatory, source) VALUES "
                "(:rid, CAST(:org AS varchar(64)), CAST(:v AS varchar(64)), "
                "  'site_manager', false, 'agent_proposal')"
            ),
            {"org": tenant.organization_id, "v": await self._version_id(tenant)},
        )
        assert "agent_source_needs_proposal" in message, message

    async def _version_id(self, tenant: Tenant) -> str:
        """A version to hang a bad distribution off, reusing one if the test made it."""
        existing = (
            await tenant.session.execute(text("SELECT id FROM document_versions LIMIT 1"))
        ).first()
        if existing is not None:
            return str(existing[0])
        document = await _document(tenant, code=DocumentCode.make(Block.PMO, "KNW", 6))
        await tenant.commit()
        return await _version(tenant, document, suffix="aux")
