"""Document control end to end: the operations, and the three rules only they can hold.

## What is tested, and what is not

The **schema's** refusals live in `test_document_control.py` — 23 tests that insert rows the
`CHECK` constraints forbid and require the database to refuse. This file tests the
**operations**: the SQL in `application/document_control.py`, the routes in
`api/documents.py`, and the shape of what a reader gets back.

The split is the point. A `CHECK` proves a state is unrepresentable; it says nothing about
whether the code that writes rows produces the right ones. Those are different questions and
one suite answering both is a suite where a failure does not say where to look.

## The three rules, and the state each one is for

1. **Issuing a revision closes the previous one.** Tested by reading the lineage back, not
   by inspecting the code: `v1` must come out `superseded` with an `effective_to`, and
   `v2` `approved` with the superseding pointer. The reason this needs a test is that
   nothing in the database makes them agree — the `CHECK` only refuses a `superseded` row
   *that has an end date*, and a writer that never sets one produces a history with holes
   that still satisfies every constraint.
2. **A re-send updates the obligation.** The same target, twice, must leave **one** row.
   Without the `ON CONFLICT` a retry inserts a second row, and a compliance figure that
   rises when you retry it is not a measurement of anything.
3. **An acknowledgement keeps the first reader.** The second press must return the *first*
   name and the *first* timestamp. The first version of the operation updated
   unconditionally and checked afterwards, so it overwrote the record and then reported the
   overwrite as `already_acknowledged: true` — a guard that ran after the effect, and so
   could only ever be wrong.

## The refusals are asserted by their sentences

Every refusal test matches on the wording, not merely on "it raised". `acknowledge` exists
to tell a person what to fix; a test that only asserts `ValidationError` would pass against
a message saying "invalid input", and the next author would have no reason to keep the good
one.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from ai_orchestrator.application.document_control import (
    Compliance,
    acknowledge,
    distribute,
    document_detail,
    document_register,
    issue_version,
)
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

D1 = "aaaaaaaabbbbccccddddeeeeffff00001111"
D2 = "11112222333344445555666677778888"

CODE = "ONX-BO-HR-SOP-004"
OWNER = "site_manager"


async def _document(tenant: Tenant, *, code: str = CODE) -> str:
    """A controlled document at revision 1, with nothing sent yet."""
    # The id carries the code as well as the organization. `documents.id` is the primary
    # key on its own and the test database is never truncated, so a fixed id is written by
    # the first test that commits it and refused by every test after -- including a test
    # that legitimately wants two documents. F156, the fourth time it has come up.
    serial = code.split("-")[-1].lower()
    document_id = f"doc_{tenant.organization_id[-8:]}_{serial}"
    # `UNIQUE (organization_id, content_hash)`: two documents in one tenant cannot share a
    # hash, which is the right rule -- identical bytes means identical content, and two
    # numbered documents with the same content is a filing problem rather than a data
    # accident. The hash therefore carries the code, so two documents in a test differ.
    digest = f"{(D1 if serial == '004' else D2)}{serial}".ljust(64, "0")[:64]

    # Idempotent, because a test is entitled to ask for "a controlled document" twice --
    # the resend test does exactly that, and the second ask is the point of the test rather
    # than a setup error. `PRIMARY KEY (id)` is on `id` alone, so the honest shapes are
    # "return what is there" or "refuse"; returning it is what a fixture is for.
    already = (
        await tenant.session.execute(
            text("SELECT 1 FROM documents WHERE id = :id"), {"id": document_id}
        )
    ).first()
    if already is not None:
        return document_id

    await tenant.session.execute(
        text(
            "INSERT INTO documents (id, organization_id, title, content_hash, code) "
            "VALUES (:id, CAST(:o AS varchar(64)), 'Quy trình nhân sự', :h, :c)"
        ),
        {"id": document_id, "o": tenant.organization_id, "h": digest, "c": code},
    )
    await tenant.commit()
    await issue_version(
        tenant.session,
        organization_id=tenant.organization_id,
        document_id=document_id,
        content_hash=digest,
        change_note="Phát hành lần 1.",
        issued_on=dt.date(2026, 3, 1),
        approved_by="test",
        source_actor="test",
    )
    await tenant.commit()
    return document_id


class TestTheRegister:
    async def test_an_empty_register_is_empty_not_missing(self, tenant: Tenant) -> None:
        """A tenant with no documents has no documents, and that is a 200 with a list.

        The alternative -- a 404 -- tells a person their organisation is broken when it
        simply has not published anything yet.
        """
        register = await document_register(tenant.session, organization_id=tenant.organization_id)
        assert register == {"items": [], "total": 0, "limit": 50, "offset": 0}

    async def test_the_code_travels_parsed_not_as_a_string(self, tenant: Tenant) -> None:
        """A reader filters on *block* and *department*, so the register hands them out.

        The alternative is a client that splits the code itself, which means the grammar
        exists in two places and only one of them is tested.
        """
        await _document(tenant)
        register = await document_register(tenant.session, organization_id=tenant.organization_id)
        item = register["items"][0]
        assert (item["code"], item["block"], item["department"]) == (CODE, "BO", "HR")
        assert item["block_name"] == "Back Office", "a code a person reads needs a name they read"

    async def test_the_filter_is_the_parsed_prefix_not_a_like(self, tenant: Tenant) -> None:
        await _document(tenant, code=CODE)
        await _document(tenant, code="ONX-MO-PM-SOP-002")
        await tenant.commit()
        picked = await document_register(
            tenant.session, organization_id=tenant.organization_id, block="bo"
        )
        assert [i["code"] for i in picked["items"]] == [CODE], "the filter is case-tolerant"
        assert picked["total"] == 1, "the count and the list must not disagree"

    async def test_a_search_matches_the_title_and_the_code(self, tenant: Tenant) -> None:
        await _document(tenant)
        by_code = await document_register(
            tenant.session, organization_id=tenant.organization_id, q="HR-SOP"
        )
        by_title = await document_register(
            tenant.session, organization_id=tenant.organization_id, q="nhân sự"
        )
        assert by_code["total"] == 1 and by_title["total"] == 1

    async def test_a_document_with_no_dossier_number_is_still_listed(self, tenant: Tenant) -> None:
        """An uploaded attachment has no number, and inventing one would be a lie.

        So the row is legal, it appears, and its block is `None` rather than a guess.
        """
        await tenant.session.execute(
            text(
                "INSERT INTO documents (id, organization_id, title, content_hash) "
                "VALUES (:id, CAST(:o AS varchar(64)), 'Ảnh hiện trường', :h)"
            ),
            {"id": f"doc_{tenant.organization_id[-8:]}_b", "o": tenant.organization_id, "h": D2},
        )
        await tenant.commit()
        register = await document_register(tenant.session, organization_id=tenant.organization_id)
        unnumbered = [i for i in register["items"] if i["code"] is None]
        assert len(unnumbered) == 1
        assert unnumbered[0]["block"] is None and unnumbered[0]["department"] is None


class TestARevisionCannotPredateItsPredecessor:
    """A revision that starts before the one it replaces would leave nobody holding the document.

    Found by walking every write the page makes, not by reading this operation. Issuing a
    revision with an earlier effective date returned a 500 carrying a database constraint name:

        CheckViolationError) new row for relation "document_versions"
        violates check constraint "ck_document_versions_the_window_is_not_inverted"

    The constraint was telling the truth about an impossible window. Ending the live version
    at the new revision's date gives it an `effective_to` *before* its own `effective_from`,
    so the **operation** asked for something impossible and the database caught it. A person
    given a 500 with a constraint name has no idea what to do, which is the other half of
    why this is a defect and not a curiosity.
    """

    async def test_it_is_refused_before_anything_is_written(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        # Revision 1 was issued effective 2026-03-01 by `_document`.
        with pytest.raises(ValidationError) as caught:
            await issue_version(
                tenant.session,
                organization_id=tenant.organization_id,
                document_id=document,
                content_hash=D2,
                change_note="Sửa lại bản cũ.",
                issued_on=dt.date(2026, 1, 1),
                effective_from=dt.date(2026, 1, 1),
                approved_by="Trần Văn Bình",
                source_actor="test",
            )
        await tenant.commit()
        # The refusal has to say what to do, or a person cannot act on it. Asserted on the
        # sentence, per the rule this file follows: a test that only asserts the exception
        # passes against a message saying "invalid input".
        assert "before the revision it replaces" in str(caught.value)
        assert "2026-03-01" in str(caught.value), "the refusal names the date it collides with"

    async def test_nothing_was_written_by_the_refused_call(self, tenant: Tenant) -> None:
        """A refusal that leaves a partial write behind is not a refusal.

        Checked by reading the lineage rather than by trusting the exception, because the
        order of the two writes is what is at stake: the operation reads, ends the live
        version, and inserts -- all in one transaction. If the check ran after the first
        write, the document would be left with no version in force at all.
        """
        document = await _document(tenant)
        with pytest.raises(ValidationError):
            await issue_version(
                tenant.session,
                organization_id=tenant.organization_id,
                document_id=document,
                content_hash=D2,
                change_note="Sửa lại bản cũ.",
                issued_on=dt.date(2026, 1, 1),
                effective_from=dt.date(2026, 1, 1),
                approved_by="Trần Văn Bình",
                source_actor="test",
            )
        await tenant.commit()
        detail = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        assert len(detail["versions"]) == 1, "the refused revision must not be in the lineage"
        assert detail["versions"][0]["status"] == "approved", (
            "and the one in force must still be in force"
        )
        assert detail["versions"][0]["effective_to"] is None, (
            "the live version must not have been closed by a call that refused"
        )

    async def test_the_same_day_is_allowed(self, tenant: Tenant) -> None:
        """Refusing `earlier` must not refuse `equal`.

        A revision that takes effect on the same day as the one it replaces ends it on that
        day: a zero-length window, which is what a same-day reissue means. Off-by-one in the
        comparison would make a legitimate reissue impossible, and a rule that is too strict
        is a rule people work around.
        """
        document = await _document(tenant)
        result = await issue_version(
            tenant.session,
            organization_id=tenant.organization_id,
            document_id=document,
            content_hash=D2,
            change_note="Phát hành lại cùng ngày.",
            issued_on=dt.date(2026, 3, 1),
            effective_from=dt.date(2026, 3, 1),
            approved_by="Trần Văn Bình",
            source_actor="test",
        )
        await tenant.commit()
        assert result["version_no"] == 2


class TestVersioning:
    async def test_issuing_a_revision_closes_the_previous_one(self, tenant: Tenant) -> None:
        """The rule the schema cannot hold, tested by reading the lineage back.

        `ck_document_versions_a_superseded_version_ends` refuses a superseded row with no
        end date -- but a writer that never *sets* one produces a history with holes and
        still satisfies every constraint. This is the assertion that closes the previous
        version rather than merely allowing it.
        """
        document = await _document(tenant)
        result = await issue_version(
            tenant.session,
            organization_id=tenant.organization_id,
            document_id=document,
            content_hash=D2,
            change_note="Bổ sung bước đào tạo.",
            issued_on=dt.date(2026, 6, 1),
            effective_from=dt.date(2026, 6, 15),
            approved_by="Trần Văn Bình",
            source_actor="test",
        )
        await tenant.commit()
        assert result["version_no"] == 2 and result["superseded"]

        detail = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        by_no = {v["version_no"]: v for v in detail["versions"]}
        assert by_no[1]["status"] == "superseded"
        assert by_no[1]["effective_to"] == dt.date(2026, 6, 15), (
            "the end date is the new revision's effective date, not today -- otherwise a "
            "revision issued in advance would leave a gap nobody was governing"
        )
        assert by_no[2]["status"] == "approved" and by_no[2]["effective_to"] is None
        assert detail["live_version_no"] == 2

    async def test_a_draft_is_not_in_force(self, tenant: Tenant) -> None:
        """`activate = False` writes a draft, and a draft is not what people are holding."""
        document = await _document(tenant)
        await issue_version(
            tenant.session,
            organization_id=tenant.organization_id,
            document_id=document,
            content_hash=D2,
            change_note="Bản nháp chưa duyệt.",
            issued_on=dt.date(2026, 6, 1),
            activate=False,
            source_actor="test",
        )
        await tenant.commit()
        detail = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        draft = next(v for v in detail["versions"] if v["version_no"] == 2)
        assert draft["status"] == "draft"
        assert detail["live_version_no"] == 1, "a draft must not become the current revision"

    async def test_a_revision_with_no_change_note_is_refused(self, tenant: Tenant) -> None:
        document = await _document(tenant)
        with pytest.raises(ValidationError) as refusal:
            await issue_version(
                tenant.session,
                organization_id=tenant.organization_id,
                document_id=document,
                content_hash=D2,
                change_note="   ",
                issued_on=dt.date(2026, 6, 1),
                source_actor="test",
            )
        assert "change note" in str(refusal.value), str(refusal.value)

    async def test_an_agent_proposed_revision_must_cite_its_approval(self, tenant: Tenant) -> None:
        """Tập 3 §4.1 as a refusal, and the table refuses it too."""
        document = await _document(tenant)
        with pytest.raises(ValidationError) as refusal:
            await issue_version(
                tenant.session,
                organization_id=tenant.organization_id,
                document_id=document,
                content_hash=D2,
                change_note="Đề xuất của agent.",
                issued_on=dt.date(2026, 6, 1),
                source="agent_proposal",
                proposal_id="pr_1",
                source_actor="HR Agent",
            )
        assert "approval" in str(refusal.value), str(refusal.value)

    async def test_a_revision_of_a_document_that_does_not_exist(self, tenant: Tenant) -> None:
        with pytest.raises(NotFoundError):
            await issue_version(
                tenant.session,
                organization_id=tenant.organization_id,
                document_id="doc_nope",
                content_hash=D2,
                change_note="Không có tài liệu nào.",
                issued_on=dt.date(2026, 6, 1),
                source_actor="test",
            )


class TestDistributing:
    async def _sent(self, tenant: Tenant, **overrides: object) -> dict:
        document = await _document(tenant)
        await tenant.commit()
        detail = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        return await distribute(
            tenant.session,
            organization_id=tenant.organization_id,
            document_version_id=detail["versions"][0]["id"],
            audience_role_key=str(overrides.pop("audience", OWNER)),
            channel=str(overrides.pop("channel", "system")),
            is_mandatory=bool(overrides.pop("mandatory", True)),
            distributed_at=overrides.pop("at", dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC)),
            note=str(overrides.pop("note", "")),
            source_actor="test",
        )

    async def test_a_resend_is_the_same_obligation(self, tenant: Tenant) -> None:
        """Twice to the same target on the same channel leaves **one** row.

        The compliance figure is the reason. A retry that inserted a second row would make
        the count rise when nothing new was told to anybody, and a number that goes up when
        you retry it is not a measurement of anything.
        """
        first = await self._sent(tenant)
        await tenant.commit()
        second = await self._sent(tenant)
        await tenant.commit()
        assert first["distribution_id"] == second["distribution_id"], (
            "a resend created a second obligation"
        )
        rows = (
            await tenant.session.execute(text("SELECT count(*) FROM document_distributions"))
        ).scalar()
        assert rows == 1, f"{rows} obligations exist for one send"

    async def test_a_draft_cannot_be_circulated(self, tenant: Tenant) -> None:
        """Handing out a draft defeats the point of a controlled document.

        People then hold a revision nobody has signed, and the register cannot say which.
        """
        document = await _document(tenant)
        issued = await issue_version(
            tenant.session,
            organization_id=tenant.organization_id,
            document_id=document,
            content_hash=D2,
            change_note="Bản nháp.",
            issued_on=dt.date(2026, 6, 1),
            activate=False,
            source_actor="test",
        )
        await tenant.commit()
        with pytest.raises(ValidationError) as refusal:
            await distribute(
                tenant.session,
                organization_id=tenant.organization_id,
                document_version_id=issued["version_id"],
                audience_role_key=OWNER,
                distributed_at=dt.datetime(2026, 6, 2, 9, 0, tzinfo=dt.UTC),
                source_actor="test",
            )
        assert "draft" in str(refusal.value), str(refusal.value)

    async def test_an_unknown_channel_is_refused_by_name(self, tenant: Tenant) -> None:
        with pytest.raises(ValidationError) as refusal:
            await self._sent(tenant, channel="carrier_pigeon")
        assert "carrier_pigeon" in str(refusal.value), str(refusal.value)

    async def test_a_distribution_with_no_audience_is_refused(self, tenant: Tenant) -> None:
        with pytest.raises(ValidationError) as refusal:
            await self._sent(tenant, audience="   ")
        assert "audience" in str(refusal.value), str(refusal.value)

    async def test_a_naive_send_time_is_refused(self, tenant: Tenant) -> None:
        """A naive datetime is stored against the *server's* zone.

        Two offices writing the same row then come out in the wrong order, and the
        time-to-acknowledge figure becomes a difference between two timezones.
        """
        with pytest.raises(ValidationError) as refusal:
            await self._sent(tenant, at=dt.datetime(2026, 3, 2, 9, 0))
        assert "timezone-aware" in str(refusal.value), str(refusal.value)


class TestAcknowledging:
    async def _obligation(self, tenant: Tenant) -> str:
        document = await _document(tenant)
        sent = await distribute(
            tenant.session,
            organization_id=tenant.organization_id,
            document_version_id=(
                await document_detail(
                    tenant.session,
                    organization_id=tenant.organization_id,
                    document_id=document,
                )
            )["versions"][0]["id"],
            audience_role_key=OWNER,
            distributed_at=dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC),
            source_actor="test",
        )
        await tenant.commit()
        return str(sent["distribution_id"])

    async def test_the_first_acknowledgement_is_the_record(self, tenant: Tenant) -> None:
        obligation = await self._obligation(tenant)
        first = await acknowledge(
            tenant.session,
            organization_id=tenant.organization_id,
            distribution_id=obligation,
            acknowledged_by="Nguyễn Thị Lan",
            acknowledged_at=dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC),
        )
        await tenant.commit()
        assert first["already_acknowledged"] is False
        assert first["acknowledged_by"] == "Nguyễn Thị Lan"

    async def test_a_second_press_keeps_the_first_reader_and_the_first_time(
        self, tenant: Tenant
    ) -> None:
        """The falsifier for the bug this operation actually had.

        It updated unconditionally and checked *afterwards*, so the second press overwrote
        the first reader's name and timestamp and then reported the overwrite as
        `already_acknowledged: true`. A guard that runs after the effect cannot be a guard.
        """
        obligation = await self._obligation(tenant)
        await acknowledge(
            tenant.session,
            organization_id=tenant.organization_id,
            distribution_id=obligation,
            acknowledged_by="Nguyễn Thị Lan",
            acknowledged_at=dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC),
        )
        await tenant.commit()
        second = await acknowledge(
            tenant.session,
            organization_id=tenant.organization_id,
            distribution_id=obligation,
            acknowledged_by="Trần Văn Bình",
            acknowledged_at=dt.datetime(2026, 3, 9, 8, 0, tzinfo=dt.UTC),
        )
        await tenant.commit()
        assert second["already_acknowledged"] is True
        assert second["acknowledged_by"] == "Nguyễn Thị Lan", (
            "the second press overwrote the record; 'when did they read it' is the only "
            "reason that column has a timestamp"
        )
        assert second["acknowledged_at"] == dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC)

    async def test_an_acknowledgement_with_no_name_is_refused(self, tenant: Tenant) -> None:
        obligation = await self._obligation(tenant)
        with pytest.raises(ValidationError) as refusal:
            await acknowledge(
                tenant.session,
                organization_id=tenant.organization_id,
                distribution_id=obligation,
                acknowledged_by="   ",
                acknowledged_at=dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC),
            )
        assert "name" in str(refusal.value), str(refusal.value)

    async def test_an_acknowledgement_before_its_send_is_refused(self, tenant: Tenant) -> None:
        """A clock fault that would otherwise make a lag figure negative."""
        obligation = await self._obligation(tenant)
        with pytest.raises(ValidationError) as refusal:
            await acknowledge(
                tenant.session,
                organization_id=tenant.organization_id,
                distribution_id=obligation,
                acknowledged_by="Nguyễn Thị Lan",
                acknowledged_at=dt.datetime(2026, 3, 1, 8, 0, tzinfo=dt.UTC),
            )
        message = str(refusal.value)
        assert "before the distribution" in message, message
        assert "2026-03-01" in message and "2026-03-02" in message, (
            "the refusal should name both timestamps, so the person can see which way round "
            f"they are: {message}"
        )

    async def test_acknowledging_a_row_that_does_not_exist(self, tenant: Tenant) -> None:
        with pytest.raises(NotFoundError):
            await acknowledge(
                tenant.session,
                organization_id=tenant.organization_id,
                distribution_id="dd_nope",
                acknowledged_by="Nguyễn Thị Lan",
                acknowledged_at=dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC),
            )


class TestCompliance:
    def test_no_requirement_is_not_a_failure(self) -> None:
        """`required == 0` is **100%**, not 0% and not a division error.

        A document nobody is required to hold is fully distributed. Reporting it as 0% would
        put a red figure on a document that has no outstanding obligation at all.
        """
        assert Compliance(0, 0).percent == 100
        assert Compliance(0, 0).outstanding == 0

    def test_the_percentage_is_whole_numbers_that_add_up(self) -> None:
        assert Compliance(3, 0).percent == 0
        assert Compliance(3, 1).percent == 33
        assert Compliance(3, 2).percent == 67
        assert Compliance(3, 3).percent == 100
        for required in range(1, 12):
            for done in range(required + 1):
                c = Compliance(required, done)
                assert c.outstanding == required - done
                assert 0 <= c.percent <= 100

    async def test_reading_an_old_revision_does_not_clear_the_current_one(
        self, tenant: Tenant
    ) -> None:
        """The audit question, and the one a naive figure gets wrong.

        Somebody acknowledged revision 1 and has not seen revision 4. They have **not**
        acknowledged the current document, and a figure that credits them for the old one
        is the number an audit rejects.
        """
        document = await _document(tenant)
        v1 = (
            await document_detail(
                tenant.session, organization_id=tenant.organization_id, document_id=document
            )
        )["versions"][0]
        sent = await distribute(
            tenant.session,
            organization_id=tenant.organization_id,
            document_version_id=v1["id"],
            audience_role_key=OWNER,
            distributed_at=dt.datetime(2026, 3, 2, 9, 0, tzinfo=dt.UTC),
            source_actor="test",
        )
        await acknowledge(
            tenant.session,
            organization_id=tenant.organization_id,
            distribution_id=str(sent["distribution_id"]),
            acknowledged_by="Nguyễn Thị Lan",
            acknowledged_at=dt.datetime(2026, 3, 3, 8, 0, tzinfo=dt.UTC),
        )
        detail = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        assert detail["compliance"] == {
            "required": 1,
            "acknowledged": 1,
            "outstanding": 0,
            "percent": 100,
        }, "revision 1 is acknowledged and it is the one in force"

        await issue_version(
            tenant.session,
            organization_id=tenant.organization_id,
            document_id=document,
            content_hash=D2,
            change_note="Bản 2.",
            issued_on=dt.date(2026, 6, 1),
            source_actor="test",
        )
        await tenant.commit()
        after = await document_detail(
            tenant.session, organization_id=tenant.organization_id, document_id=document
        )
        assert after["live_version_no"] == 2
        assert after["compliance"] == {
            "required": 0,
            "acknowledged": 0,
            "outstanding": 0,
            "percent": 100,
        }, (
            "revision 2 has nobody required to hold it yet, which is 0 obligations rather "
            "than 1 unacknowledged -- the page has to say so and offer to send it"
        )


class TestTenancy:
    async def test_another_tenants_document_is_a_404_not_a_403(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """Saying "forbidden" confirms the row exists.

        The whole reason the keys are composite is that a reader must not be able to tell
        the difference between "not yours" and "not there".
        """
        theirs = await _document(other_tenant)
        await other_tenant.commit()
        with pytest.raises(NotFoundError):
            await document_detail(
                tenant.session, organization_id=tenant.organization_id, document_id=theirs
            )

    async def test_the_register_does_not_leak_across_tenants(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        await _document(tenant)
        await tenant.commit()
        await _document(other_tenant, code="ONX-MO-PM-SOP-002")
        await other_tenant.commit()
        mine = await document_register(tenant.session, organization_id=tenant.organization_id)
        theirs = await document_register(
            other_tenant.session, organization_id=other_tenant.organization_id
        )
        assert [i["code"] for i in mine["items"]] == [CODE]
        assert [i["code"] for i in theirs["items"]] == ["ONX-MO-PM-SOP-002"]
