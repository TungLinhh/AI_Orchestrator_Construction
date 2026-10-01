"""Approval and audit, against a real database.

The properties under test are the ones that make human-in-the-loop mean
something:

  * an agent can never be the approver, at any configuration
  * the requester can never approve their own request
  * an approval is bound to the payload it was granted for
  * an unanswered approval expires rather than waiting forever
  * the audit log records denials, not just successes
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from ai_orchestrator.approvals import ApprovalRequest, ApprovalService
from ai_orchestrator.audit import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import (
    ActorType,
    ApprovalStatus,
    DataClassification,
    EffectClass,
    RiskLevel,
)
from ai_orchestrator.domain.errors import (
    AuthorizationError,
    ConflictError,
    PreconditionError,
    ValidationError,
)
from ai_orchestrator.domain.ids import OrganizationId

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def admin_user_id(tenant) -> str:
    """A real privileged user row.

    `approvals.decided_by` is a foreign key on purpose: a decision must be
    attributable to a principal that exists, otherwise the audit trail points at
    a name that never was.
    """
    from ai_orchestrator.domain.ids import UserId
    from ai_orchestrator.persistence.models import User

    user_id = str(UserId.create())
    tenant.session.add(
        User(
            id=user_id,
            organization_id=tenant.organization_id,
            email="ceo@example.com",
            display_name="CEO",
            role="admin",
            is_org_admin=True,
            is_privileged=True,
        )
    )
    await tenant.session.flush()
    return user_id


@pytest_asyncio.fixture
async def real_task_id(tenant) -> str:
    """A committed task id.

    `audit_logs.task_id` and `approvals.task_id` are real foreign keys. Using a
    made-up id would be a referential error, and that is the schema working: an
    audit row that points at no task is a broken chain of custody.
    """
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    repo = TaskRepository(tenant.session, tenant.organization_id)
    task = await repo.create(title="Quarterly summary", goal="prepare the quarterly summary")
    # Capture the id before the commit: `commit()` expires the session, and
    # reading a column off an expired object outside the async context raises.
    task_id = task.id
    await tenant.commit()
    return task_id


def _admin(org: str, user_id: str = "usr_admin") -> Actor:
    """A privileged human. `is_privileged_human` is deliberately the full field
    name: "privileged" on its own reads as a general capability, and the whole
    point of the flag is that it is specifically the human-approval privilege."""
    return Actor(
        id=user_id,
        kind=ActorType.HUMAN,
        organization_id=OrganizationId(org),
        display_name="CEO",
        is_privileged_human=True,
    )


def _agent(org: str) -> Actor:
    return Actor(
        id="agt_marketing",
        kind=ActorType.AGENT,
        organization_id=OrganizationId(org),
        display_name="Marketing Agent",
    )


def _request(org: str, **overrides) -> ApprovalRequest:
    defaults = {
        "organization_id": org,
        "action_type": "tool.send_email",
        "action_payload": {"to": "cfo@example.com", "body": "Q3 summary attached."},
        "requested_by": "agt_marketing",
        "requested_by_type": ActorType.AGENT,
        "effect_class": EffectClass.EXTERNAL_SEND,
        "risk_level": RiskLevel.HIGH,
        "reason": "Executive agent wants to send the quarterly summary to the CFO",
        "task_id": None,
        "ttl_seconds": 3600,
    }
    return ApprovalRequest(**(defaults | overrides))


class TestApprovalCreation:
    async def test_creates_pending_with_a_payload_hash(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        service = ApprovalService(tenant.session, tenant.organization_id)
        approval = await service.create(_request(tenant.organization_id, task_id=real_task_id))
        assert approval.status == ApprovalStatus.PENDING.value
        assert approval.payload_hash
        assert approval.expires_at is not None

    async def test_ttl_must_be_positive(self, tenant) -> None:
        """An approval that never expires is a task that waits forever."""
        service = ApprovalService(tenant.session, tenant.organization_id)
        with pytest.raises(ValidationError, match="TTL must be positive"):
            await service.create(_request(tenant.organization_id, ttl_seconds=0))

    async def test_secret_bearing_payload_is_redacted_at_rest(self, tenant) -> None:
        """An approval row is rendered in an inbox and exported, so a secret in an
        action payload becomes visible to anyone who can read approvals."""
        service = ApprovalService(tenant.session, tenant.organization_id)
        approval = await service.create(
            _request(
                tenant.organization_id,
                action_payload={"to": "x@y.z", "api_key": "sk-or-v1-realsecret", "count": 3},
            )
        )
        assert approval.action_payload["api_key"] == "[redacted]"
        assert approval.action_payload["to"] == "x@y.z"

    async def test_restricted_payload_is_reduced_to_a_shape(self, tenant) -> None:
        service = ApprovalService(tenant.session, tenant.organization_id)
        approval = await service.create(
            _request(
                tenant.organization_id,
                action_payload={"salary_table": [...], "ssn": "123-45-6789"},
                classification=DataClassification.RESTRICTED,
            )
        )
        assert approval.action_payload["redacted"] is True
        assert "salary_table" not in approval.action_payload


class TestWhoMayApprove:
    async def test_an_agent_cannot_approve(self, tenant, admin_user_id: str) -> None:
        """Structural, so no policy edit can change it."""
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org))
        with pytest.raises(AuthorizationError, match="agent cannot approve"):
            await service.decide(
                approval.id, approver=_agent(org), approve=True, note="looks fine to me"
            )

    async def test_an_unprivileged_human_cannot_approve(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org, task_id=real_task_id))
        junior = Actor(id="usr_junior", kind=ActorType.HUMAN, organization_id=OrganizationId(org))
        with pytest.raises(AuthorizationError, match="lacks the privilege"):
            await service.decide(approval.id, approver=junior, approve=True)

    async def test_a_privileged_human_may_approve(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org))
        decision = await service.decide(
            approval.id, approver=_admin(org, admin_user_id), approve=True, note="approved"
        )
        assert decision.status is ApprovalStatus.APPROVED

    async def test_listing_answers_for_the_state_asked_about(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        """`listing` must answer about **any** state, not only the pending one.

        The endpoint's `?status=` used to be applied in Python to the result of `inbox`, which
        narrows to `status = pending` in SQL. So `?status=approved` returned zero rows on a
        tenant holding an approval that had just been approved -- measured, not inferred --
        and every value except `pending` returned the same empty answer. A filter that cannot
        change the answer is worse than a missing one, because the caller concludes the row
        is gone.

        The assertion is relational rather than a count: ask for the state the row is in, get
        that row; ask for a state it is not in, do not.
        """
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org))
        await service.decide(
            approval.id, approver=_admin(org, admin_user_id), approve=True, note="approved"
        )
        await tenant.commit()

        approved = await service.listing(status=ApprovalStatus.APPROVED.value)
        assert approval.id in {a.id for a in approved}, (
            "an approved approval must be findable by asking for approved"
        )
        pending = await service.listing(status=ApprovalStatus.PENDING.value)
        assert approval.id not in {a.id for a in pending}, (
            "and must not also be in the pending list"
        )

    async def test_listing_with_no_status_returns_every_state(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        """No filter means no filter, not "the pending ones".

        Checked by contrast: the same tenant holds one approved and one pending row, and the
        unfiltered listing has to contain both. Asserting the count equals 2 would break the
        moment another test in this module commits an approval into the same tenant, which is
        the F156 shape -- the test database is never truncated.
        """
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        decided = await service.create(_request(org, action_type="tool.send_email.2"))
        await service.create(_request(org, action_type="tool.send_email.3"))
        await service.decide(
            decided.id, approver=_admin(org, admin_user_id), approve=True, note="approved"
        )
        await tenant.commit()

        every = {a.id for a in await service.listing()}
        assert decided.id in every
        approved = {a.id for a in await service.listing(status=ApprovalStatus.APPROVED.value)}
        pending = {a.id for a in await service.listing(status=ApprovalStatus.PENDING.value)}
        assert decided.id in approved
        assert approved & pending == set(), "no row can be in two states at once"
        assert approved | pending <= every, "a filtered list is a subset of the whole"

    async def test_an_unknown_state_is_refused_not_silently_empty(
        self, tenant, real_task_id: str
    ) -> None:
        """A typo and an empty table look identical unless one of them raises.

        `?status=apoved` returning zero rows is a legitimate answer to a question nobody asked.
        Refusing it tells the caller they spelled it wrong; a filter that quietly matches
        nothing is how an operator concludes a record has been deleted.
        """
        service = ApprovalService(tenant.session, tenant.organization_id)
        with pytest.raises(ValidationError) as caught:
            await service.listing(status="apoved")
        assert "not a state an approval can be in" in str(caught.value)
        # And the legal states are named, so the refusal is actionable.
        assert ApprovalStatus.PENDING.value in str(caught.value.details)

    async def test_requester_cannot_approve_their_own_request(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        # A human requested it; the same human must not be the approver.
        approval = await service.create(
            _request(
                org,
                task_id=real_task_id,
                requested_by=admin_user_id,
                requested_by_type=ActorType.HUMAN,
            )
        )
        with pytest.raises(AuthorizationError, match="own request"):
            await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)

    async def test_a_decided_approval_cannot_be_re_decided(
        self, tenant, admin_user_id: str
    ) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org))
        await service.decide(
            approval.id, approver=_admin(org, admin_user_id), approve=False, note="no"
        )
        with pytest.raises(ConflictError, match="already rejected"):
            await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)


class TestApprovalIsBoundToItsPayload:
    async def test_unchanged_payload_verifies(self, tenant, admin_user_id: str) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        payload = {"to": "cfo@example.com", "body": "Q3 summary attached."}
        approval = await service.create(
            _request(org, action_type="tool.send_email", action_payload=payload)
        )
        await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)
        await service.verify_payload(approval.id, payload)

    async def test_modified_payload_is_refused(self, tenant, admin_user_id: str) -> None:
        """The TOCTOU hole in human-in-the-loop systems: an approval obtained for
        one message and replayed against another. The hash is bound at creation
        and verified immediately before the side effect."""
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        payload = {"to": "cfo@example.com", "body": "Q3 summary attached."}
        approval = await service.create(
            _request(org, action_type="tool.send_email", action_payload=payload)
        )
        await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)

        tampered = {"to": "attacker@evil.com", "body": "wire the balance to me"}
        with pytest.raises(AuthorizationError, match="different payload"):
            await service.verify_payload(approval.id, tampered)

    async def test_appended_field_is_refused(self, tenant, admin_user_id: str) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        payload = {"to": "cfo@example.com"}
        approval = await service.create(
            _request(org, action_type="tool.send_email", action_payload=payload)
        )
        await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)
        with pytest.raises(AuthorizationError, match="different payload"):
            await service.verify_payload(approval.id, {**payload, "bcc": "attacker@evil.com"})


class TestExpiry:
    async def test_expired_approval_cannot_be_decided(self, tenant, admin_user_id: str) -> None:
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        approval = await service.create(_request(org, ttl_seconds=1))
        # Move the deadline into the past rather than sleeping. The refresh is
        # required: a raw UPDATE does not update the identity map, so the object
        # held here would still carry the original future deadline and the
        # service would happily approve an expired request.
        await tenant.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": approval.id},
        )
        await tenant.session.refresh(approval)
        with pytest.raises(PreconditionError, match="expired"):
            await service.decide(approval.id, approver=_admin(org, admin_user_id), approve=True)

    async def test_stale_approvals_are_swept(self, tenant) -> None:
        """Without a sweep, a pending approval is a task that waits forever."""
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        live = await service.create(_request(org, ttl_seconds=3600))
        stale = await service.create(_request(org, ttl_seconds=3600))
        await tenant.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": stale.id},
        )
        await tenant.session.flush()

        swept = await service.expire_stale()
        assert stale.id in swept
        assert live.id not in swept
        # `expire_stale` updates with synchronize_session="fetch", so a caller
        # that already holds the object sees the new status. Without that the
        # workflow would keep waiting on an approval that has timed out.
        assert stale.status == ApprovalStatus.EXPIRED.value
        assert (await service.get(stale.id)).status == ApprovalStatus.EXPIRED.value

    async def test_inbox_hides_expired_requests(self, tenant) -> None:
        """Showing an operator a request that can no longer be approved is worse
        than not showing it."""
        org = tenant.organization_id
        service = ApprovalService(tenant.session, org)
        await service.create(_request(org, ttl_seconds=3600))
        stale = await service.create(_request(org, ttl_seconds=3600))
        await tenant.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :i"),
            {"i": stale.id},
        )
        await tenant.session.flush()
        inbox = await service.inbox()
        assert stale.id not in {a.id for a in inbox}
        assert len(inbox) == 1


class TestAuditLog:
    async def test_records_who_what_and_which_rule(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        org = tenant.organization_id
        audit = AuditService(tenant.session, org)
        await audit.record(
            actor=_agent(org),
            action="tool.invoke",
            resource_type="tool",
            resource_id="tool_1",
            task_id=real_task_id,
            outcome="denied",
            policy_decision="require_approval",
            policy_rule_id="DENY_EXTERNAL_SEND",
            policy_reason="external side effect requires human approval",
        )
        rows = await audit.query(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_type == "agent"
        assert row.actor_id == "agt_marketing"
        assert row.outcome == "denied"
        assert row.policy_rule_id == "DENY_EXTERNAL_SEND"

    async def test_denials_are_recorded(self, tenant) -> None:
        """An audit log with only successes is worse than no log, because it
        looks complete."""
        org = tenant.organization_id
        audit = AuditService(tenant.session, org)
        await audit.record(
            actor=_agent(org),
            action="policy.change",
            resource_type="policy",
            outcome="denied",
            policy_rule_id="DENY_POLICY_CHANGE",
        )
        await audit.record(
            actor=_admin(org), action="task.create", resource_type="task", resource_id="tsk_1"
        )
        denied = await audit.denied_action_summary()
        assert denied[0]["count"] == 1
        assert denied[0]["policy_rule_id"] == "DENY_POLICY_CHANGE"
        assert await audit.count() == 2

    async def test_timeline_is_ordered_by_sequence_not_the_clock(
        self, tenant, real_task_id: str, admin_user_id: str
    ) -> None:
        """Two entries in the same millisecond must still have a defined order,
        and a wall clock does not provide one."""
        org = tenant.organization_id
        audit = AuditService(tenant.session, org)
        for action in ("task.created", "agent.assigned", "tool.invoked", "task.completed"):
            await audit.record(
                actor=_agent(org),
                action=action,
                resource_type="task",
                resource_id=real_task_id,
                task_id=real_task_id,
            )
        timeline = await audit.timeline(real_task_id)
        assert [row["action"] for row in timeline] == [
            "task.created",
            "agent.assigned",
            "tool.invoked",
            "task.completed",
        ]
        sequences = [row["sequence"] for row in timeline]
        assert sequences == sorted(sequences)

    async def test_long_context_values_are_truncated(self, tenant) -> None:
        """The audit table is for a reviewer. A 5 MB string in it is a denial of
        service against the table."""
        org = tenant.organization_id
        audit = AuditService(tenant.session, org)
        await audit.record(
            actor=_agent(org),
            action="tool.invoke",
            resource_type="tool",
            context={"payload": "x" * 50_000},
        )
        row = (await audit.query(limit=1))[0]
        assert len(row.context["payload"]) < 1000
        assert row.context["payload"].endswith("[truncated]")

    async def test_audit_is_scoped_to_the_tenant(self, tenant) -> None:
        org = tenant.organization_id
        await AuditService(tenant.session, org).record(
            actor=_agent(org), action="task.create", resource_type="task", resource_id="tsk_x"
        )
        other = AuditService(tenant.session, "org_someone_else")
        assert await other.query(limit=10) == []
