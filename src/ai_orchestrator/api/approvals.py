"""Approval endpoints — the human side of human-in-the-loop.

Deciding an approval does two things, and doing only the first is the classic
failure: it records the decision *and* signals the waiting workflow. A recorded
decision with no signal leaves the workflow blocked forever, and an operator has
no way to tell that from a workflow that is still thinking.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.api.health import bump
from ai_orchestrator.approvals import ApprovalService
from ai_orchestrator.domain.human_exceptions import approval_class
from ai_orchestrator.persistence.models import Approval
from ai_orchestrator.security.auth import ensure_local_operator

router = APIRouter(tags=["approvals"])


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(default="", max_length=4000)
    #: `needs_information` sends the task back to the agent rather than failing it,
    #: which is usually the right answer when a human has a question.
    needs_information: bool = False


def _approval_dict(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "task_id": approval.task_id,
        "execution_id": approval.execution_id,
        "action_type": approval.action_type,
        "action_payload": approval.action_payload,
        "effect_class": approval.effect_class,
        "risk_level": approval.risk_level,
        "reason": approval.reason,
        "exception_class": approval_class(
            action_type=approval.action_type,
            effect=approval.effect_class,
            payload=approval.action_payload or {},
        ).value,
        "requested_by": approval.requested_by,
        "requested_by_type": approval.requested_by_type,
        "required_approver_roles": approval.required_approver_roles,
        "status": approval.status,
        "decision": approval.decision,
        "decision_note": approval.decision_note,
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "workflow_id": approval.workflow_id,
        "created_at": approval.created_at.isoformat(),
    }


@router.get("/approvals")
async def list_approvals(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Approvals in a named state, or all of them.

    `status=pending` is the operator's inbox; anything else asks the table. The filter used
    to be applied in Python to a list `inbox` had already narrowed to pending rows, so
    **every value except `pending` returned the same empty answer** -- measured on a tenant
    holding an approval that had just been approved. The filter now reaches the query, and an
    unrecognised state is refused rather than quietly matching nothing.

    Expired-but-undecided rows are still excluded from `pending`, because showing a request
    that can no longer be approved is worse than not showing it.
    """
    service = ApprovalService(ctx.session, ctx.organization_id)
    approvals = await service.listing(status=status_filter, limit=limit, offset=offset)
    return {
        "items": [_approval_dict(a) for a in approvals],
        "limit": limit,
        "offset": offset,
        "returned": len(approvals),
        "total": await service.count(status=status_filter),
    }


@router.get("/approvals/stats")
async def approval_stats(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """What an operator needs to see the governance working: how much is waiting,
    and how long people take to answer.

    The wait-time histogram is the interesting one. A rising queue is a signal
    that the approval gate is faster than the work arriving, which is a staffing
    problem the platform cannot solve and an operator must.
    """
    from sqlalchemy import func, select

    rows = (
        await ctx.session.execute(
            select(Approval.status, func.count())
            .where(Approval.organization_id == ctx.organization_id)
            .group_by(Approval.status)
        )
    ).all()
    counts = {status: count for status, count in rows}

    decided = (
        await ctx.session.execute(
            select(
                func.avg(func.extract("epoch", Approval.decided_at - Approval.created_at))
            ).where(
                Approval.organization_id == ctx.organization_id,
                Approval.decided_at.is_not(None),
            )
        )
    ).scalar_one_or_none()

    return {
        "counts": counts,
        "pending": counts.get("pending", 0),
        "mean_decision_seconds": float(decided) if decided is not None else None,
    }


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    service = ApprovalService(ctx.session, ctx.organization_id)
    return _approval_dict(await service.get(approval_id))


async def _decide(
    approval_id: str, body: ApprovalDecisionRequest, ctx: ApiContext, *, approve: bool
) -> dict[str, Any]:
    # The approver has to be a real `users` row before a decision can be recorded
    # against it, and with authentication off that row does not exist in every
    # tenant. Without this, Approve returned a 500 in any organisation other than
    # the first -- a button that looked available and could not work, which is
    # the state the UI is least able to explain.
    #
    # Done here rather than in authentication, because this is the only write
    # that needs the row, and provisioning a user on every read would put a
    # write on the path of every request to fix one endpoint.
    if ctx.actor.is_privileged_human:
        await ensure_local_operator(ctx.session, ctx.organization_id)
    service = ApprovalService(ctx.session, ctx.organization_id)
    requested = await service.get(approval_id)
    if requested.action_type == "agent.provision":
        from ai_orchestrator.application.agent_blueprints import AgentBlueprintService

        blueprints = AgentBlueprintService(ctx.session, ctx.organization_id)
        draft = await blueprints.get(str(requested.task_id), lock=True)
        if requested.action_payload != blueprints.payload(draft):
            from ai_orchestrator.domain.errors import PreconditionError

            raise PreconditionError("Blueprint changed; review the latest revision")
    decision = await service.decide(
        approval_id,
        approver=ctx.actor,
        approve=approve,
        note=body.note,
        needs_information=body.needs_information,
    )
    bump("approvals_decided_total")

    # Signal the waiting workflow. Recorded without a signal means a task blocked
    # forever, and the operator has no way to distinguish that from a workflow
    # that is still working.
    approval = await service.get(approval_id)
    signalled = False
    if approval.workflow_id and not body.needs_information:
        from ai_orchestrator.workflows.client import signal_approval_decision

        signalled = await signal_approval_decision(
            approval.workflow_id,
            approval_id=approval_id,
            approved=approve,
            decided_by=str(ctx.actor.id),
            note=body.note,
        )
    result: dict[str, Any] = {
        "approval_id": decision.approval_id,
        "status": decision.status.value,
        "decided_by": decision.decided_by,
        "workflow_id": approval.workflow_id,
        "workflow_signalled": signalled,
    }
    if approval.task_id and approval.action_type == "task.continue" and not approval.workflow_id:
        from ai_orchestrator.application.local_runner import continue_goal
        from ai_orchestrator.domain.state_machines import Transition
        from ai_orchestrator.persistence.repositories.task import TaskRepository

        if approve:
            await service.verify_payload(approval_id, approval.action_payload or {})
        elif not body.needs_information:
            await TaskRepository(ctx.session, ctx.organization_id).transition(
                str(approval.task_id),
                Transition.APPROVAL_REJECTED,
                error=body.note or "A human refused the requested review",
                failure_category="approval_rejected",
            )
        if not body.needs_information:
            # A worker must see the committed decision, not race this request.
            task_id = str(approval.task_id)
            await ctx.session.commit()
            result["local_run"] = (await continue_goal(ctx.organization_id, task_id)).as_dict()
    if approve and not body.needs_information and approval.action_type == "agent.provision":
        result["provisioned"] = await blueprints.provision(approval_id, ctx.actor)
    if approval.action_type == "workflow.revision" and not body.needs_information:
        if approve:
            from ai_orchestrator.application.workflow_revisions import WorkflowRevisions

            result["revision"] = await WorkflowRevisions(ctx.session, ctx.organization_id).apply(
                approval_id, ctx.actor
            )
        else:
            from ai_orchestrator.domain.state_machines import Transition
            from ai_orchestrator.persistence.repositories.task import TaskRepository

            await TaskRepository(ctx.session, ctx.organization_id).transition(
                str(approval.task_id),
                Transition.APPROVAL_REJECTED,
                error=body.note or "Campaign revision rejected",
            )
    return result


@router.post("/approvals/{approval_id}/approve")
async def approve(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Approve. The decision is bound to a hash of the payload, verified
    immediately before the side effect runs."""
    approval = await ApprovalService(ctx.session, ctx.organization_id).get(approval_id)
    action_type = approval.action_type
    payload = dict(approval.action_payload or {})
    result = await _decide(approval_id, body, ctx, approve=True)
    await _resume_controller(action_type, payload, result, request, ctx)
    return result


async def _resume_controller(
    action_type: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    request: Request,
    ctx: ApiContext,
) -> None:
    from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind

    if result["status"] not in {"approved", "rejected"}:
        return
    if action_type == "agent.workflow.review":
        result["workflow_started"] = await request.app.state.workflow_drivers.submit(
            ctx.organization_id, payload["root_id"], WorkflowKind.AGENT, session=ctx.session
        )
    elif action_type == "workflow.review":
        root = payload["workflow_root"]
        started = await request.app.state.workflow_drivers.submit(
            ctx.organization_id, root, WorkflowKind.BUSINESS, session=ctx.session
        )
        result["workflow_run"] = {"id": root, "started": started, "already_running": not started}


@router.post("/approvals/{approval_id}/reject")
async def reject(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    approval = await ApprovalService(ctx.session, ctx.organization_id).get(approval_id)
    action_type = approval.action_type
    payload = dict(approval.action_payload or {})
    result = await _decide(approval_id, body, ctx, approve=False)
    await _resume_controller(action_type, payload, result, request, ctx)
    return result


@router.post("/approvals/{approval_id}/request-information")
async def request_information(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Ask the agent a question instead of deciding.

    Usually the right answer when a human is unsure: a rejection ends the task,
    whereas a question lets the agent gather what is missing and come back.
    """
    approval = await ApprovalService(ctx.session, ctx.organization_id).get(approval_id)
    action_type, payload = approval.action_type, dict(approval.action_payload or {})
    result = await _decide(
        approval_id, body.model_copy(update={"needs_information": True}), ctx, approve=False
    )
    if action_type in {"workflow.review", "agent.workflow.review"}:
        from ai_orchestrator.application.workflow_feedback import request_feedback

        root = payload.get("workflow_root") or payload.get("root_id")
        if not isinstance(root, str):
            from ai_orchestrator.domain.errors import ValidationError

            raise ValidationError("Review payload does not identify its workflow root")
        kind = await request_feedback(
            ctx.session,
            ctx.organization_id,
            root,
            body.note or "Please clarify the evidence and proposed workflow decision",
            ctx.actor,
        )
        # Feedback and the human decision share the same transaction.
        await ctx.session.commit()
        await request.app.state.workflow_drivers.cancel(ctx.organization_id, root)
        request.app.state.workflow_drivers.start(ctx.organization_id, root, kind, persist=False)
        result["feedback_root"] = root
    return result


@router.get("/tasks/{task_id}/approvals")
async def approvals_for_task(
    task_id: str, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    service = ApprovalService(ctx.session, ctx.organization_id)
    items = await service.pending_for_task(task_id)
    return {"items": [_approval_dict(a) for a in items], "count": len(items)}


__all__ = ["router"]
