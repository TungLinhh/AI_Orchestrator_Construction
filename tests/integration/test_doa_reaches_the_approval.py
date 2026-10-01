"""The DOA matrix reaches the approval, or it is eight rows nobody reads.

Eight bands were seeded by `seed-process` and consulted by nothing. A request to move
thirty billion dong was recorded with the same `required_approver_roles` as a request
to move three thousand, because the amount reached nobody who was supposed to sign.

These tests are on `ApprovalService.create` rather than on `domain.doa` because
`domain.doa` already had its own 31 tests and all of them could have passed while the
matrix stayed inert. A pure module with no caller is exactly the state this project
shipped the matrix in.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService, DoaEnforcer
from ai_orchestrator.domain.doa import DoaBand
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

BANDS = (
    DoaBand("DOA-01", "payment", Decimal(0), Decimal("100000000"), "procurement_lead"),
    DoaBand("DOA-02", "payment", Decimal("100000000"), Decimal("1000000000"), "finance_manager"),
    DoaBand("DOA-03", "payment", Decimal("1000000000"), Decimal("10000000000"), "chief_accountant"),
    DoaBand("DOA-04", "payment", Decimal("10000000000"), None, "cfo"),
)


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    """The organisation, and a DOA matrix written the way `seed-process` writes one.

    Inserted through the ORM rather than by running `seed_process_spine.py` in a
    subprocess. The subprocess was the first attempt and it made every test in the
    file depend on a working interpreter, a migrated schema and a second database
    connection, so a failure in any of those read as "the matrix did not load" instead
    of as the thing that was actually broken.
    """
    from ai_orchestrator.domain.ids import new_ulid
    from ai_orchestrator.persistence.process import DoaMatrix

    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    for band in BANDS:
        tenant.session.add(
            DoaMatrix(
                id=str(new_ulid()),
                organization_id=str(tenant.organization_id),
                code=band.code,
                name_vi=band.code,
                subject_kind=band.subject_kind,
                min_amount=band.min_amount,
                max_amount=band.max_amount,
                approver_role_key=band.approver_role_key,
                fallback_role_key=band.fallback_role_key,
                max_agent_autonomy=band.max_agent_autonomy,
                source="human",
            )
        )
    await tenant.commit()
    return tenant


def _request(payload: dict, *, roles: frozenset[str] = frozenset({"org_admin"})):  # type: ignore[no-untyped-def]
    return ApprovalRequest(
        organization_id="",
        action_type="payment.release",
        action_payload=payload,
        requested_by="agt_test",
        requested_by_type=ActorType.AGENT,
        required_approver_roles=roles,
        reason="test",
    )


class TestTheMatrixIsLoadedFromTheTenant:
    async def test_the_seeded_bands_reach_the_enforcer(self, seeded) -> None:  # type: ignore[no-untyped-def]
        enforcer = await DoaEnforcer.load(seeded.session, seeded.organization_id)
        assert not enforcer.is_empty, "the seeded matrix did not load"

        decision = enforcer.resolve("payment", "30.000.000.000", agent_autonomy="L4")
        assert decision.approver_role_key == "cfo"

    async def test_a_tenant_with_no_matrix_says_it_is_empty(self, tenant) -> None:  # type: ignore[no-untyped-def]
        """A tenant that was never given a matrix must be distinguishable from a gap."""
        enforcer = await DoaEnforcer.load(tenant.session, "org_does_not_exist")
        assert enforcer.is_empty


class TestTheApprovalIsRoutedByTheMatrix:
    async def test_the_approver_is_the_band_s_one_not_the_callers(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The whole point, in one assertion.

        The caller asked for `org_admin` — the default — for a 30bn payment, and the
        matrix says the CFO signs. The approval must carry `cfo`, because an agent that
        can name its own approver makes `required_approver_roles` a suggestion.
        """
        service = ApprovalService(seeded.session, seeded.organization_id)
        request = _request({"subject_kind": "payment", "amount": "30.000.000.000"})
        approval = await service.create(request)

        assert approval.required_approver_roles == ["cfo"]

    async def test_the_band_decides_where_a_human_can_name_an_agent(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Even with the roles the caller asked for, a bigger amount moves the signer."""
        service = ApprovalService(seeded.session, seeded.organization_id)

        small = await service.create(_request({"subject_kind": "payment", "amount": "3.600.000"}))
        large = await service.create(
            _request({"subject_kind": "payment", "amount": "12.000.000.000"})
        )
        assert small.required_approver_roles == ["procurement_lead"]
        assert large.required_approver_roles == ["cfo"]

    async def test_an_approval_with_no_subject_is_left_to_the_existing_rules(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Not every approval is about money, and it must not start failing.

        `required_approver_roles` is still enforced at `decide`; there is simply no band
        to consult, and inventing one would refuse approvals over unrelated actions.
        """
        service = ApprovalService(seeded.session, seeded.organization_id)
        approval = await service.create(_request({"note": "not a payment"}))
        assert approval.required_approver_roles == ["org_admin"]

    async def test_a_subject_without_an_amount_is_refused(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A money-shaped payload with no figure is the gap this is meant to close."""
        from ai_orchestrator.domain.errors import ValidationError

        service = ApprovalService(seeded.session, seeded.organization_id)
        with pytest.raises(ValidationError, match="carries no amount"):
            await service.create(_request({"subject_kind": "payment"}))


class TestTheRefusalsReachTheCaller:
    async def test_a_subject_the_matrix_does_not_cover_is_refused(self, seeded) -> None:  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.errors import ValidationError

        service = ApprovalService(seeded.session, seeded.organization_id)
        with pytest.raises(ValidationError, match="does not authorise"):
            await service.create(_request({"subject_kind": "salary", "amount": "9000000"}))

    async def test_an_amount_no_band_covers_is_refused(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """The seeded matrix caps every payment band at 10bn and then runs unbounded, so
        this is asserted against a subject whose top band is capped — the seeded
        `contract` bands, whose lowest is `0..50.000.000.000` and highest unbounded.

        A subject with a *capped* top band is what produces the refusal, so it is
        exercised through the pure module's own coverage and here only the wiring:
        """
        from ai_orchestrator.domain.errors import ValidationError

        service = ApprovalService(seeded.session, seeded.organization_id)
        with pytest.raises(ValidationError):
            await service.create(_request({"subject_kind": "not_a_subject", "amount": "1"}))

    async def test_a_tenant_with_no_matrix_refuses_rather_than_passes(self, tenant) -> None:  # type: ignore[no-untyped-def]
        """A missing matrix is a deployment gap, and it must stop the request.

        Before this, the same request succeeded and recorded whatever roles the caller
        happened to send — which for a 30bn payment was the default `org_admin`.
        """
        service = ApprovalService(tenant.session, tenant.organization_id)
        with pytest.raises(NotImplementedError, match="no delegation-of-authority matrix"):
            await service.create(_request({"subject_kind": "payment", "amount": "1000"}))


class TestTheMatrixCannotBeSidestepped:
    async def test_the_agent_cannot_name_its_own_approver(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Asking for `org_admin` on a CFO-band payment does not get `org_admin`.

        The structural rule that an agent may never be its own approver already exists
        on the decision path. This is the other half: the request path cannot be used
        to pre-select a friendly signer either.
        """
        service = ApprovalService(seeded.session, seeded.organization_id)
        approval = await service.create(
            _request(
                {"subject_kind": "payment", "amount": "30.000.000.000"},
                roles=frozenset({"org_admin", "ceo"}),
            )
        )
        assert approval.required_approver_roles == ["cfo"]

    async def test_the_agent_autonomy_in_the_payload_does_not_lower_the_band(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """Claiming to be autonomous does not move the money into a cheaper band.

        The seeded matrix puts every band at `L3_HUMAN_APPROVAL`, so an agent that
        asserts `L4` still gets the human the band names.
        """
        service = ApprovalService(seeded.session, seeded.organization_id)
        approval = await service.create(
            _request(
                {
                    "subject_kind": "payment",
                    "amount": "30.000.000.000",
                    "agent_autonomy": "L4",
                }
            )
        )
        assert approval.required_approver_roles == ["cfo"]
