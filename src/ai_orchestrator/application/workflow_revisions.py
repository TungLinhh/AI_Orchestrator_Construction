"""Hash-bound human review before a changed brief becomes a new campaign."""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.application.business_workflow import create_workflow, payload_hash
from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.business_workflow import WORKFLOWS
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.domain.workflow_revision import HiringBrief, brief_diff
from ai_orchestrator.persistence.models import Task
from ai_orchestrator.persistence.repositories.task import TaskRepository


class WorkflowRevisions:
    def __init__(self, session: AsyncSession, org: str) -> None:
        self.session, self.org = session, org
        self.tasks = TaskRepository(session, org)

    async def propose(
        self, root_id: str, brief: HiringBrief, expected_hash: str, reason: str, actor: Actor
    ) -> dict[str, Any]:
        root = await self.tasks.get(root_id)
        if root.parent_task_id or root.input.get("business_workflow") != "mep_hiring":
            raise ValidationError("Structured hiring revision requires a hiring campaign")
        if payload_hash(root.input["brief"]) != expected_hash:
            raise PreconditionError("The source brief changed; reload before proposing a revision")
        changes = brief.model_dump(mode="json")
        diff = brief_diff(root.input["brief"], changes)
        if not diff:
            raise ValidationError("Change at least one brief field")
        snapshot = {
            "source_root": root_id,
            "source_hash": expected_hash,
            "mode": root.input["mode"],
            "brief": changes,
            "reason": reason,
            "diff": diff,
            "affected_stages": [
                stage.key
                for stage in WORKFLOWS["mep_hiring"]
                if not stage.simulation_only or root.input["mode"] == "simulation"
            ],
            "source_policy": "Fresh campaign; new interviews, acceptance and onboarding evidence",
            "permission_policy": "Same mode and human gates; source campaign remains unchanged",
        }
        draft = await self.tasks.create(
            title="Review recruitment revision: " + root.title,
            goal=reason,
            task_type="analysis",
            owner_agent_id=root.owner_agent_id,
            input={"workflow_revision_for": root_id},
        )
        draft.output = snapshot
        await self.tasks.transition(draft.id, Transition.BEGIN_WORK)
        await self.tasks.transition(draft.id, Transition.REQUEST_APPROVAL)
        approval = await ApprovalService(self.session, self.org).create(
            ApprovalRequest(
                organization_id=self.org,
                action_type="workflow.revision",
                action_payload={
                    "draft_id": draft.id,
                    "snapshot_hash": payload_hash(snapshot),
                    "revision": snapshot,
                },
                requested_by=str(actor.id),
                requested_by_type=actor.kind,
                task_id=draft.id,
                reason="Review the brief diff and new campaign before applying it",
            )
        )
        await AuditService(self.session, self.org).record(
            actor=actor,
            action="workflow.revision.proposed",
            resource_type="task",
            resource_id=draft.id,
            task_id=draft.id,
            context={"source_root": root_id, "snapshot_hash": payload_hash(snapshot)},
        )
        return {"id": draft.id, "approval_id": approval.id, "snapshot": snapshot}

    async def apply(self, approval_id: str, actor: Actor) -> dict[str, Any]:
        approvals = ApprovalService(self.session, self.org)
        approval = await approvals.get(approval_id)
        if approval.status != "approved":
            raise PreconditionError("A human must approve this revision before applying it")
        draft = await self.session.scalar(
            select(Task)
            .where(Task.organization_id == self.org, Task.id == approval.task_id)
            .with_for_update()
        )
        if (
            not draft
            or not draft.input.get("workflow_revision_for")
            or approval.action_type != "workflow.revision"
        ):
            raise ValidationError("Not a campaign revision review")
        snapshot = copy.deepcopy(draft.output)
        destination = snapshot.pop("destination_root", None)
        await approvals.verify_payload(
            approval_id,
            {"draft_id": draft.id, "snapshot_hash": payload_hash(snapshot), "revision": snapshot},
        )
        if destination:
            return {"id": destination, "revision_of": snapshot["source_root"], "started": False}
        source = await self.tasks.get(snapshot["source_root"])
        if payload_hash(source.input["brief"]) != snapshot["source_hash"]:
            raise PreconditionError("Source evidence changed since revision review")
        # Fresh inputs and gates. Interview/acceptance/onboarding evidence is never inherited.
        brief = {
            k: copy.deepcopy(v)
            for k, v in source.input["brief"].items()
            if k
            not in {
                "interview_technical",
                "interview_hr",
                "offer_acceptance",
                "onboarding_evidence",
            }
        }
        brief.update(snapshot["brief"])
        root_id = await create_workflow(
            self.session, self.org, "mep_hiring", snapshot["mode"], brief
        )
        replacement = await self.tasks.get(root_id)
        replacement.input = {
            **replacement.input,
            "revision_of": source.id,
            "revision_approval_id": approval.id,
        }
        draft.output = {**snapshot, "destination_root": root_id}
        await self.tasks.transition(draft.id, Transition.APPROVAL_GRANTED)
        await self.tasks.transition(draft.id, Transition.COMPLETE)
        await AuditService(self.session, self.org).record(
            actor=actor,
            action="workflow.revision.applied",
            resource_type="task",
            resource_id=root_id,
            task_id=root_id,
            context={"source_root": source.id, "approval_id": approval.id},
        )
        return {"id": root_id, "revision_of": source.id, "started": False}
