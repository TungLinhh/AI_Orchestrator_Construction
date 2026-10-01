"""Approvals.

An approval is a pause, not an error. The whole design turns on that
distinction: a platform that models "needs approval" as a failure teaches its
agents to bypass the gate, and one that models it as a success teaches operators
the gate is advisory.

Three properties this module guarantees:

  * the decision is bound to a hash of what was approved. An approval cannot be
    stretched to cover a different payload than the one a human reviewed, which
    is the standard TOCTOU hole in human-in-the-loop systems;
  * the approver is a human, and not the requester. Structural, so no policy
    configuration can route around it;
  * expiry is explicit. An approval that nobody answers is a decision, and the
    default is to fail closed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.authority import require_separate_approver
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
    NotFoundError,
    PreconditionError,
    ValidationError,
)
from ai_orchestrator.domain.ids import ApprovalId
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Approval
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ApprovalRequest:
    """What is being approved.

    Built by the gate that found the action needing approval, not by the agent.
    The agent never constructs one: an agent that could describe its own request
    would be able to describe a more flattering one.
    """

    organization_id: str
    action_type: str
    action_payload: dict[str, Any]
    requested_by: str
    requested_by_type: ActorType = ActorType.AGENT
    effect_class: EffectClass = EffectClass.MUTATE_INTERNAL
    risk_level: RiskLevel = RiskLevel.MEDIUM
    reason: str = ""
    task_id: str | None = None
    execution_id: str | None = None
    required_approver_roles: frozenset[str] = frozenset({"org_admin"})
    ttl_seconds: int = 3600
    classification: DataClassification = DataClassification.INTERNAL
    signal_name: str | None = None
    workflow_id: str | None = None

    def payload_hash(self) -> str:
        """Hash of exactly what will be executed.

        A decision covers this hash and nothing else. Without it, an approval
        obtained for a 10-word email can be replayed against a 10,000-word one.
        """
        canonical = json.dumps(
            {
                "action_type": self.action_type,
                "action_payload": self.action_payload,
                "task_id": self.task_id,
                "organization_id": self.organization_id,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ApprovalDecision:
    approval_id: str
    status: ApprovalStatus
    decided_by: str | None
    note: str
    payload_hash: str
    expires_at: datetime | None


class DoaEnforcer:
    """Resolves a requested action against the tenant's delegation-of-authority matrix.

    Loaded once per tenant and consulted at `create`. A separate class rather than a
    helper inside `ApprovalService` because the matrix is the one thing this service
    cannot infer: a caller with no matrix in scope must get a refusal, not a pass.
    """

    __slots__ = ("_bands",)

    def __init__(self, bands: Sequence[Any]) -> None:
        self._bands = tuple(bands)

    @property
    def is_empty(self) -> bool:
        """A tenant with no matrix is not a tenant permitted to act.

        Reported separately from "no band for this subject", because the two are
        different faults: a missing matrix is a deployment gap and an uncovered subject
        is a policy gap, and they are fixed by different people.
        """
        return not self._bands

    def resolve(self, subject_kind: str, amount: Any, *, agent_autonomy: str | None) -> Any:
        """Delegate to `domain.doa`, and let its refusals through untouched.

        `DoaRefusal` is deliberately not caught here. A caller that downgraded it to a
        warning would be the exact defect this class exists to prevent, and it has to
        be able to fail.
        """
        from ai_orchestrator.domain.doa import resolve

        return resolve(self._bands, subject_kind, amount, agent_autonomy=agent_autonomy)

    @classmethod
    async def load(cls, session: AsyncSession, organization_id: str) -> DoaEnforcer:
        """Read the tenant's bands and turn them into domain values.

        The conversion is explicit rather than passing the ORM rows on. `DoaBand` is a
        frozen value with a `covers()` method, and an ORM row is neither — the first
        version of this handed the rows straight through and every lookup died on
        `AttributeError: 'DoaMatrix' object has no attribute 'covers'`, which is the
        same class of fault as the matrix being unloaded at all: it looks wired and
        behaves inert.

        `min_amount` and `max_amount` come off a `NUMERIC` column as `Decimal`, so the
        money is exact here without a conversion of our own.
        """
        from ai_orchestrator.domain.doa import DoaBand
        from ai_orchestrator.persistence.process import DoaMatrix

        result = await session.execute(
            select(DoaMatrix)
            .where(DoaMatrix.organization_id == organization_id)
            .order_by(DoaMatrix.subject_kind, DoaMatrix.min_amount, DoaMatrix.code)
        )
        return cls(
            [
                DoaBand(
                    code=row.code,
                    subject_kind=row.subject_kind,
                    min_amount=Decimal(row.min_amount),
                    max_amount=Decimal(row.max_amount) if row.max_amount is not None else None,
                    approver_role_key=row.approver_role_key,
                    fallback_role_key=row.fallback_role_key or "",
                    max_agent_autonomy=row.max_agent_autonomy,
                )
                for row in result.scalars().all()
            ]
        )


class ApprovalService:
    def __init__(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        on_approved: Callable[[Approval], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._org = organization_id
        #: Called after an approval is granted, with the row.
        #:
        #: A callback rather than a call into the learning code, because this
        #: class is about recording a decision and has no business knowing that
        #: a decision is a moment where a lesson could be written. Installing it
        #: from the outside also means a test can assert it fired without a
        #: skill, a proposal, or a model.
        #:
        #: It is called for approvals only, never for rejections: a run a person
        #: refused is evidence that the approach was wrong, and the fix for that
        #: is to not repeat it, which is not a lesson to append to the agent's
        #: instructions.
        self._on_approved = on_approved
        #: Loaded lazily and then kept, because the matrix does not change within a
        #: request and re-reading it per approval would be a query per row.
        self._doa: DoaEnforcer | None = None

    async def create(self, request: ApprovalRequest) -> Approval:
        """Record a pending approval.

        Also publishes `approval.requested` through the caller's task repository,
        so a human sees it. That is done by the workflow, not here, to keep this
        class free of event concerns.
        """
        if request.ttl_seconds <= 0:
            msg = "approval TTL must be positive; an approval that never expires is a deadlock"
            raise ValidationError(msg, details={"ttl_seconds": request.ttl_seconds})

        await self._check_authority(request)

        approval = Approval(
            id=str(ApprovalId.create()),
            organization_id=self._org,
            task_id=request.task_id,
            execution_id=request.execution_id,
            action_type=request.action_type,
            action_payload=_redact_action(request.action_payload, request.classification),
            effect_class=request.effect_class.value,
            risk_level=request.risk_level.value,
            reason=request.reason,
            # Bound at creation. The decision is checked against this hash.
            payload_hash=request.payload_hash(),
            requested_by=request.requested_by,
            requested_by_type=request.requested_by_type.value,
            required_approver_roles=sorted(request.required_approver_roles),
            status=ApprovalStatus.PENDING.value,
            expires_at=utcnow() + timedelta(seconds=request.ttl_seconds),
            signal_name=request.signal_name,
            workflow_id=request.workflow_id,
        )
        self._session.add(approval)
        await self._session.flush()
        return approval

    async def _check_authority(self, request: ApprovalRequest) -> None:
        """Route the request through the DOA matrix before it can become an approval.

        **This is the wiring the matrix never had.** Eight bands were seeded and read
        by nothing, so a request for thirty billion dong was recorded with exactly the
        same `required_approver_roles` as a request for three thousand. Nothing about
        the amount reached anybody who was supposed to sign.

        Three outcomes, and all three are refusals rather than warnings:

        * **No matrix in this tenant.** `NotImplementedError`, because an approval
          created without a matrix is an approval whose approver nobody chose.
        * **A subject the matrix does not cover.** `ValidationError` naming the
          subject, so the gap is visible in the row a person reads.
        * **A band exists but the agent's level cannot settle it.** The approval is
          created with the *band's* approver role substituted for whatever the caller
          asked for. An agent cannot name the person who signs its own request; if it
          could, `required_approver_roles` would be a suggestion.

        The third is a correction rather than a refusal on purpose: the request is
        legitimate, it just needs a different approver, and refusing outright would
        leave the run with no path forward at all.
        """
        from ai_orchestrator.domain.doa import DoaRefusal

        subject_kind = str(request.action_payload.get("subject_kind") or "").strip()
        if not subject_kind:
            # Not every approval is about money. `required_approver_roles` still
            # applies and is still enforced at `decide`; there is no band to consult.
            return
        amount = request.action_payload.get("amount")
        if amount is None:
            raise ValidationError(
                f"the payload declares subject_kind={subject_kind!r} but carries no amount, "
                f"so the DOA matrix has nothing to resolve",
                details={"subject_kind": subject_kind},
            )

        if self._doa is None:
            self._doa = await DoaEnforcer.load(self._session, self._org)
        enforcer = self._doa
        if enforcer.is_empty:
            raise NotImplementedError(
                "this tenant has no delegation-of-authority matrix, so nobody can be "
                "chosen to approve. Run `make seed-process`."
            )

        try:
            decision = enforcer.resolve(
                subject_kind,
                amount,
                agent_autonomy=str(request.action_payload.get("agent_autonomy") or "") or None,
            )
        except DoaRefusal as refusal:
            raise ValidationError(
                f"the delegation-of-authority matrix does not authorise this: {refusal}",
                details={"subject_kind": subject_kind, "amount": str(amount)},
            ) from refusal

        required = frozenset(decision.approver_role_key.split(","))
        if request.required_approver_roles != required:
            logger.info(
                "approval.doa_approver_substituted",
                task_id=request.task_id,
                band=decision.band.code,
                requested=sorted(request.required_approver_roles),
                required=sorted(required),
            )
        request.required_approver_roles = required

    async def get(self, approval_id: str) -> Approval:
        result = await self._session.execute(
            select(Approval).where(
                and_(Approval.id == approval_id, Approval.organization_id == self._org)
            )
        )
        approval = result.scalar_one_or_none()
        if approval is None:
            msg = f"approval not found: {approval_id}"
            raise NotFoundError(msg, resource_type="approval", resource_id=approval_id)
        return approval

    async def decide(
        self,
        approval_id: str,
        *,
        approver: Actor,
        approve: bool,
        note: str = "",
        needs_information: bool = False,
    ) -> ApprovalDecision:
        """Record a human decision.

        Two structural refusals happen before the state machine is consulted:
        an agent can never be the approver, and the requester can never approve
        their own request. Both are the kind of rule that must not live in
        configuration, because configuration is exactly what an attacker with
        write access would change.
        """
        if approver.kind is not ActorType.HUMAN:
            msg = "an agent cannot approve an action; a human approver is required"
            raise AuthorizationError(msg, details={"rule": "APPROVER_MUST_BE_HUMAN"})
        if not approver.is_privileged_human:
            msg = "approver lacks the privilege required to decide this approval"
            raise AuthorizationError(msg, details={"rule": "APPROVER_NOT_PRIVILEGED"})

        approval = await self.get(approval_id)
        if approval.status != ApprovalStatus.PENDING.value:
            msg = (
                f"approval {approval_id} is already {approval.status}; a decided "
                f"approval cannot be re-decided"
            )
            raise ConflictError(msg, details={"status": approval.status})

        if approval.expires_at is not None and approval.expires_at < utcnow():
            approval.status = ApprovalStatus.EXPIRED.value
            approval.updated_at = utcnow()
            await self._session.flush()
            msg = f"approval {approval_id} expired at {approval.expires_at.isoformat()}"
            raise PreconditionError(msg, details={"expired_at": approval.expires_at.isoformat()})

        require_separate_approver(
            requester=Actor(id=approval.requested_by, kind=ActorType(approval.requested_by_type)),
            approver_id=str(approver.id),
            # Constant, and the type checker says so. The check at the top of this
            # method already refused anything that is not a human, so
            # `approver.kind is ActorType.AGENT` is provably false here. Passing
            # the constant rather than the expression keeps
            # `require_separate_approver`'s own guarantee for its other callers
            # without leaving a second, unreachable copy of the rule in this
            # file. Two copies of a security rule is one too many: the next edit
            # to one of them leaves the other looking authoritative.
            approver_is_agent=False,
        )

        target = (
            ApprovalStatus.NEEDS_INFORMATION
            if needs_information
            else (ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED)
        )
        approval.status = target.value
        approval.decision = target.value
        approval.decided_by = str(approver.id)
        approval.decided_at = utcnow()
        approval.decision_note = note
        approval.updated_at = utcnow()
        await self._session.flush()

        if self._on_approved is not None and target is ApprovalStatus.APPROVED:
            # After the write, not before: a lesson drawn from a decision that
            # then failed to record is a lesson about something that did not
            # happen. And guarded, because a failure to learn must never undo a
            # decision a person already made -- that would turn "the platform
            # could not write a skill" into "your approval was rolled back".
            try:
                await self._on_approved(approval)
            except Exception as exc:
                logger.info(
                    "approval.post_approval_failed",
                    approval_id=str(approval.id),
                    error=str(exc),
                )

        return ApprovalDecision(
            approval_id=approval.id,
            status=target,
            decided_by=str(approver.id),
            note=note,
            payload_hash=approval.payload_hash,
            expires_at=approval.expires_at,
        )

    async def expire_stale(self) -> list[str]:
        """Expire approvals nobody answered.

        Run by a scheduled task. Without it a pending approval is a task that
        waits forever, which is the deadlock this platform is supposed to avoid.
        """
        result = await self._session.execute(
            update(Approval)
            .where(
                and_(
                    Approval.organization_id == self._org,
                    Approval.status == ApprovalStatus.PENDING.value,
                    Approval.expires_at.is_not(None),
                    Approval.expires_at < utcnow(),
                )
            )
            .values(status=ApprovalStatus.EXPIRED.value, updated_at=utcnow())
            .returning(Approval.id)
            # 'fetch' updates the identity map. Without it a caller that already
            # holds the object goes on seeing `pending` for a row that is now
            # `expired`, and then blocks a workflow on an approval that has
            # already timed out.
            .execution_options(synchronize_session="fetch")
        )
        return list(result.scalars())

    async def pending_for_task(self, task_id: str) -> Sequence[Approval]:
        result = await self._session.execute(
            select(Approval)
            .where(
                and_(
                    Approval.organization_id == self._org,
                    Approval.task_id == task_id,
                    Approval.status == ApprovalStatus.PENDING.value,
                )
            )
            .order_by(Approval.created_at)
        )
        rows: Sequence[Approval] = result.scalars().all()
        return rows

    async def inbox(
        self,
        *,
        approver_roles: Sequence[str] = (),
        limit: int = 50,
    ) -> Sequence[Approval]:
        """What a human is being asked to decide.

        Expired-but-undecided rows are excluded here even though their status has
        not been flipped yet, because showing an operator a request that can no
        longer be approved is worse than not showing it.
        """
        stmt: Select[Any] = select(Approval).where(
            and_(
                Approval.organization_id == self._org,
                Approval.status == ApprovalStatus.PENDING.value,
                (Approval.expires_at.is_(None)) | (Approval.expires_at > utcnow()),
            )
        )
        if approver_roles:
            # Array overlap: "this approval is addressed to one of my roles".
            # A multi-role approver matches without a join table.
            stmt = stmt.where(Approval.required_approver_roles.op("&&")(list(approver_roles)))
        result = await self._session.execute(stmt.order_by(Approval.created_at).limit(limit))
        rows: Sequence[Approval] = result.scalars().all()
        return rows

    async def listing(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Approval]:
        """Approvals in a named state, asked as a question about the table.

        **This exists because `inbox` could not answer it.** `inbox` narrows to
        `status = pending` in SQL, so the API's `?status=` filter was applied afterwards to
        a list that was already only pending rows. Measured: `GET /approvals?status=approved`
        returned `0 items` on a tenant holding an approval that had just been approved by
        hand, and returned the same shape for every other value. A parameter that is accepted
        and then cannot change the answer is worse than a missing one, because the caller
        concludes the row is gone.

        So the filter goes to the query, and the states are the ones the status column can
        actually hold. An unrecognised state is refused rather than silently matching
        nothing -- an empty list and a typo look identical otherwise, and only one of them
        is a fact.
        """
        stmt: Select[Any] = select(Approval).where(Approval.organization_id == self._org)
        if status is not None:
            known = {v.value for v in ApprovalStatus}
            if status not in known:
                raise ValidationError(
                    f"{status!r} is not a state an approval can be in",
                    details={"known": sorted(known)},
                )
            stmt = stmt.where(Approval.status == status)
        result = await self._session.execute(
            stmt.order_by(Approval.created_at.desc()).limit(limit).offset(offset)
        )
        rows: Sequence[Approval] = result.scalars().all()
        return rows

    async def verify_payload(self, approval_id: str, action_payload: dict[str, Any]) -> None:
        """Confirm the payload about to run is the one that was approved.

        Called immediately before the side effect, not when the approval was
        granted. Anything that could change the payload between the two — a
        summarising model, a template expansion, a re-read of external data — makes
        this the only place that can catch it.
        """
        approval = await self.get(approval_id)
        canonical = json.dumps(
            {
                "action_type": approval.action_type,
                "action_payload": action_payload,
                "task_id": approval.task_id,
                "organization_id": approval.organization_id,
            },
            sort_keys=True,
            default=str,
        )
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if actual != approval.payload_hash:
            msg = (
                f"approval {approval_id} was granted for a different payload; "
                f"refusing to execute the modified action"
            )
            raise AuthorizationError(
                msg,
                details={
                    "rule": "APPROVAL_PAYLOAD_MISMATCH",
                    "approved_hash": approval.payload_hash,
                    "actual_hash": actual,
                },
            )


_SECRET_MARKERS = ("password", "secret", "token", "api_key", "authorization", "ssn", "card")


def _redact_action(payload: dict[str, Any], classification: DataClassification) -> dict[str, Any]:
    """Redact before the action is stored.

    An approval row is read by humans and rendered in an inbox, so a secret in an
    action payload becomes visible in a UI and in an export. Restricted payloads
    are additionally reduced to a shape summary.
    """
    if classification in (DataClassification.RESTRICTED, DataClassification.SECRET):
        return {
            "redacted": True,
            "classification": classification.value,
            "action_keys": sorted(payload),
        }
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if any(marker in key.lower() for marker in _SECRET_MARKERS):
            out[key] = "[redacted]"
        elif isinstance(value, dict):
            out[key] = _redact_action(value, classification)
        else:
            out[key] = value
    return out


__all__ = ["ApprovalDecision", "ApprovalRequest", "ApprovalService", "DoaEnforcer"]
