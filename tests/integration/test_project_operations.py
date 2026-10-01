"""Writing the project spine, and the refusals that stop it.

The design claim in `project_operations` is that **the decision is an input, not an
output**: which spelling of an address is canonical is a business decision, so a caller
that has not made one cannot write a project. These tests are organised around whether
that claim holds, because a claim like that is easy to state and easy to leak — one
defaulted argument and every writer in the system picks a spelling silently.

Three shapes of refusal are tested:

* **Unsettled** — no canonical form chosen, or chosen by nobody, or chosen on no date.
* **Invented** — a canonical form that is not among the spellings the corpus contains.
* **Truncated** — a package that is a strict prefix of another value the corpus has.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

from ai_orchestrator.application.project_operations import (
    AddressResolution,
    list_projects,
    observed_spellings,
    project_by_code,
    write_project,
)
from ai_orchestrator.ingest.project_reader import (
    HeaderRead,
    ProjectHeader,
    SurveyFinding,
    survey_project_headers,
)
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 28, 9, 0, tzinfo=dt.UTC)
D = dt.date

#: The ten address spellings the corpus contains, verbatim.
CORPUS_ADDRESSES = (
    "HOA THANH, XUAN CANH, SONG CAU, PHU YEN PROVINCE, VIETNAM",
    "HÒA THẠNH, XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN",
    "Hòa Thanh, Xuân Cảnh, Sông Cầu , Phú Yên",
    "KHU D8b, BẮC BÁN ĐẢO CAM RANH, CAM LÂM, KHÁNH HÒA",
    "Khu D8b, Bắc Bán Đảo Cam Ranh, Cam Hải Đông, Cam Lâm,  Khánh Hòa, Việt Nam",
    "Thôn Hòa Thạnh, Xã Xuân Cảnh, Huyện Sông Cầu, Tình Phú Yên",
    "XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN",
    "Xuân Cảnh, Sông cầu, Phú Yên",
    "Xã Xuân Cảnh, Thị xã Sông Cầu, Tỉnh Phú Yên/",
    "xã Xuân Cảnh, huyện Sông Cầu, tỉnh Phú Yên",
)

CORPUS_PACKAGES = (
    "Cơ",
    "Cơ điện",
    "Cơ điện khách sạn và nhà phụ trợ",
    "MEP HOTEL AND ANCILLARY BUILDINGS",
    "Mep",
)

OBSERVED = {"address": CORPUS_ADDRESSES, "package": CORPUS_PACKAGES}


def _read(**over: str) -> HeaderRead:
    base = {
        "project_name": "Bãi Tràm Estates",
        "address": CORPUS_ADDRESSES[2],
        "package": "Cơ điện khách sạn và nhà phụ trợ",
        "item": "MEP",
    }
    base.update(over)
    return HeaderRead(header=ProjectHeader(**base), source="TĐ BOH.xlsx::TĐ .BOH")


def _settled(chosen: str | None = CORPUS_ADDRESSES[2]) -> AddressResolution:
    return AddressResolution(
        chosen=chosen, seen=CORPUS_ADDRESSES, decided_by="ops-lead", decided_on=NOW
    )


async def _write(tenant: Tenant, **over: object):
    kwargs: dict[str, object] = {
        "organization_id": tenant.organization_id,
        "read": _read(),
        "code": "HBG-PY-01",
        "address": _settled(),
        "observed": OBSERVED,
        "written_on": NOW,
    }
    kwargs.update(over)
    return await tenant.run(lambda s: write_project(s, **kwargs))  # type: ignore[arg-type]


class TestTheDecisionIsAnInput:
    async def test_a_settled_address_writes(self, tenant: Tenant) -> None:
        outcome = await _write(tenant)
        assert outcome.written
        assert outcome.refusals == ()
        assert outcome.address_written == "Hòa Thanh, Xuân Cảnh, Sông Cầu , Phú Yên"

    async def test_the_alternatives_come_back_on_the_outcome(self, tenant: Tenant) -> None:
        """Not just logged — returned, so the decision can be shown to whoever made it.

        A canonical form with no record of what it displaced cannot be reviewed, and
        the next reader produces an eleventh spelling with no way to know it is new.
        """
        outcome = await _write(tenant)
        assert len(outcome.address_alternatives) == 9
        assert "HÒA THẠNH, XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN" in outcome.address_alternatives

    async def test_an_unsettled_address_refuses(self, tenant: Tenant) -> None:
        outcome = await _write(
            tenant, address=AddressResolution(chosen=None, seen=CORPUS_ADDRESSES)
        )
        assert not outcome.written
        assert any("none chosen" in r for r in outcome.refusals)
        assert await list_projects(tenant.session, organization_id=tenant.organization_id) == []

    async def test_a_canonical_form_with_nobody_attached_refuses(self, tenant: Tenant) -> None:
        """The silent decision this service exists to prevent."""
        outcome = await _write(
            tenant,
            address=AddressResolution(
                chosen=CORPUS_ADDRESSES[0],
                seen=CORPUS_ADDRESSES,
                decided_by="",
                decided_on=NOW,
            ),
        )
        assert not outcome.written
        assert any("nobody attached to it" in r for r in outcome.refusals)

    async def test_a_canonical_form_with_no_date_refuses(self, tenant: Tenant) -> None:
        outcome = await _write(
            tenant,
            address=AddressResolution(
                chosen=CORPUS_ADDRESSES[0],
                seen=CORPUS_ADDRESSES,
                decided_by="ops-lead",
                decided_on=None,
            ),
        )
        assert not outcome.written
        assert any("so the decision has a date" in r for r in outcome.refusals)

    async def test_a_canonical_form_the_corpus_never_contained_refuses(
        self, tenant: Tenant
    ) -> None:
        """An invented spelling is the worst case, and it must be loud.

        It would be a canonical form nobody approved, matching nothing in any file.
        """
        outcome = await _write(
            tenant,
            address=AddressResolution(
                chosen="Phú Yên, Việt Nam",
                seen=CORPUS_ADDRESSES,
                decided_by="ops-lead",
                decided_on=NOW,
            ),
        )
        assert not outcome.written
        assert any("was invented here" in r for r in outcome.refusals)

    async def test_every_reason_is_reported_at_once(self, tenant: Tenant) -> None:
        """A person fixing one problem at a time is a slower route to the same place."""
        outcome = await _write(
            tenant,
            address=AddressResolution(chosen=None, seen=CORPUS_ADDRESSES),
            code="  ",
        )
        assert not outcome.written
        assert len(outcome.refusals) >= 3
        assert any("code: required" in r for r in outcome.refusals)
        assert any("none chosen" in r for r in outcome.refusals)


class TestWhatTheWriterWillNotInvent:
    async def test_a_code_is_required(self, tenant: Tenant) -> None:
        """The sheets carry names; the codes live in filenames.

        A slug derived from a name manufactures an identifier the business does not
        use, and `projects.code` is `UNIQUE (organization_id, code)` — so a derived
        code that collides fails on a row nobody can explain.
        """
        outcome = await _write(tenant, code="   ")
        assert not outcome.written
        assert any("cannot be derived from a header" in r for r in outcome.refusals)

    async def test_a_sheet_with_no_project_name_refuses(self, tenant: Tenant) -> None:
        outcome = await _write(tenant, read=_read(project_name=""))
        assert not outcome.written
        assert any("does not identify a job" in r for r in outcome.refusals)

    async def test_a_truncated_package_refuses(self, tenant: Tenant) -> None:
        """`Cơ` is a strict prefix of `Cơ điện`, which the corpus also contains.

        The check is only possible because the caller passes the survey: a single
        sheet cannot know that a longer value exists in a different workbook.
        """
        outcome = await _write(tenant, read=_read(package="Cơ"))
        assert not outcome.written
        assert any("cut short by its column width" in r for r in outcome.refusals)
        assert "'Cơ'" in outcome.refusals[0]

    async def test_the_second_truncated_package_also_refuses(self, tenant: Tenant) -> None:
        """Both prefixes, not just the shortest.

        A detector that caught `Cơ` and missed `Cơ điện` would look like it worked.
        """
        outcome = await _write(tenant, read=_read(package="Cơ điện"))
        assert not outcome.written
        assert any("cut short" in r for r in outcome.refusals)

    async def test_a_complete_package_writes(self, tenant: Tenant) -> None:
        assert (await _write(tenant, read=_read(package="Mep"))).written, (
            "a prefix rule must not refuse a value that merely shares a beginning"
        )

    async def test_a_package_the_survey_never_saw_is_not_a_prefix(self, tenant: Tenant) -> None:
        """The rule is about the corpus, not about how short a string looks.

        `'Mep'` is 3 characters and is a real package in the corpus. A length heuristic
        would refuse it.
        """
        outcome = await _write(
            tenant, read=_read(package="Cơ điện"), observed={"package": ("Cơ điện",)}
        )
        assert outcome.written, "no other value begins with it, so nothing is cut short"

    async def test_a_client_is_optional(self, tenant: Tenant) -> None:
        """The headers carry no client at all.

        `clients` needs a code and a name and the corpus supplies neither, so a
        project with no client is a real, writable state rather than a gap.
        """
        outcome = await _write(tenant, client_id=None)
        assert outcome.written
        row = await project_by_code(
            tenant.session, organization_id=tenant.organization_id, code="HBG-PY-01"
        )
        assert row is not None
        assert row["client_id"] is None


class TestWhatGetsWritten:
    async def test_the_project_name_is_verbatim(self, tenant: Tenant) -> None:
        """Not title-cased, not accent-stripped.

        A writer that tidies has made a decision, and the tidied value cannot be
        checked against the file it came from.
        """
        await _write(tenant, read=_read(project_name="BÃI TRÀM ESTATES"))
        row = await project_by_code(
            tenant.session, organization_id=tenant.organization_id, code="HBG-PY-01"
        )
        assert row["name"] == "BÃI TRÀM ESTATES"

    async def test_the_defaults_are_the_corpus_ones(self, tenant: Tenant) -> None:
        """VND and Asia/Ho_Chi_Minh are not opinions, they are what the files are.

        The address column carries `PHÚ YÊN` and `KHÁNH HÒA` — both in Vietnam — so a
        default of UTC would put every planned date a day out of alignment with the
        documents.
        """
        await _write(tenant)
        row = await project_by_code(
            tenant.session, organization_id=tenant.organization_id, code="HBG-PY-01"
        )
        assert row["currency_code"] == "VND"
        assert row["status"] == "active"

    async def test_it_can_be_read_back_by_code(self, tenant: Tenant) -> None:
        """The round trip a caller needs before overwriting 34 rows.

        The difference between a confirmation dialog and a lost morning.
        """
        assert (
            await project_by_code(
                tenant.session, organization_id=tenant.organization_id, code="HBG-PY-01"
            )
            is None
        ), "nothing there before the write, so the read is not just echoing the insert"
        await _write(tenant)
        assert (
            await project_by_code(
                tenant.session, organization_id=tenant.organization_id, code="HBG-PY-01"
            )
            is not None
        )

    async def test_a_second_write_of_the_same_code_is_refused_by_the_database(
        self, tenant: Tenant
    ) -> None:
        """`UNIQUE (organization_id, code)`, so the business code is the identity.

        Not caught by the service, and it should not be: the check is the
        constraint's job and the service has no way to phrase a better message than
        the one the database gives.
        """
        await _write(tenant)
        with pytest.raises(IntegrityError) as caught:
            await _write(tenant)
        # The index is `ix_projects_org_code`, not `uq_...`: it was declared as a
        # unique *Index* rather than a UniqueConstraint, so the `uq_` half of the
        # naming convention never applied. Asserting on `org_code` rather than the
        # whole name keeps this test true across a rename while still proving
        # uniqueness is what refused it.
        assert "unique constraint" in str(caught.value)
        assert "org_code" in str(caught.value)

    async def test_projects_are_listed_by_name(self, tenant: Tenant) -> None:
        """The first read any role-shaped surface needs.

        An endpoint over this table returned an empty list for the whole of the
        project's life, and an empty list is indistinguishable from a broken one
        unless somebody has counted the table.
        """
        await _write(tenant, code="HBG-MCR-01", read=_read(project_name="Melia Cam Ranh"))
        await _write(tenant, code="HBG-PY-01", read=_read(project_name="Bãi Tràm Estates"))
        got = await list_projects(tenant.session, organization_id=tenant.organization_id)
        assert [r["name"] for r in got] == ["Bãi Tràm Estates", "Melia Cam Ranh"]


class TestTheSurveyFeedsTheWriter:
    def test_observed_spellings_is_the_surveys_own_output(self) -> None:
        """One survey, three projects.

        The caller reads the corpus once and writes every project against the same
        spellings, which is the only way `write_project` can see that `Cơ` is a prefix
        of a value in a *different* workbook.
        """
        survey = survey_project_headers(
            [
                HeaderRead(header=ProjectHeader(project_name="x", address=a, package=p))
                for a in CORPUS_ADDRESSES[:2]
                for p in CORPUS_PACKAGES
            ]
        )
        got = observed_spellings(survey.findings)
        assert got["package"] == tuple(sorted(CORPUS_PACKAGES))
        assert len(got["address"]) == 2

    def test_it_accepts_the_findings_directly(self) -> None:
        """A hand-built finding, so a test need not run the whole survey."""
        finding = SurveyFinding(field_name="package", values=("Cơ", "Cơ điện"))
        assert observed_spellings([finding]) == {"package": ("Cơ", "Cơ điện")}


class TestTheRefusalReasonsAreReadable:
    """These strings are the output. A person decides with them in front of them."""

    def test_each_reason_says_what_to_do(self) -> None:
        reasons = AddressResolution(chosen=None, seen=CORPUS_ADDRESSES).refusal_reasons()
        joined = " ".join(reasons)
        assert "10 spelling(s) seen" in joined, "the number of alternatives is the fact"
        assert "business decision" in joined
        assert "decided_by" in joined
        assert "decided_on" in joined

    def test_a_settled_resolution_has_no_reasons(self) -> None:
        assert _settled().refusal_reasons() == ()

    def test_a_resolution_with_no_alternatives_is_settled(self) -> None:
        """One project, one address, nothing to choose between."""
        assert (
            AddressResolution(
                chosen="Phú Yên", seen=("Phú Yên",), decided_by="ops", decided_on=NOW
            ).refusal_reasons()
            == ()
        )
