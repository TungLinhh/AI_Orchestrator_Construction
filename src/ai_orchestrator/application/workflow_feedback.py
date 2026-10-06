"""Pause, clarify and revise without rewriting finished evidence or approvals."""

from __future__ import annotations

import copy
import json
from typing import Any

from sqlalchemy import select, text

from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime
from ai_orchestrator.application.model_profiles import build_tenant_gateway
from ai_orchestrator.application.task_attempt import TaskAttemptRunner
from ai_orchestrator.application.workflow_commands import command_for, enqueue
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.business_workflow import WORKFLOWS
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.hiring_process import JD_SECTIONS
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.domain.workflow_feedback import validate_feedback
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Agent, Execution, Skill, SkillVersion, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database

FEEDBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["understanding", "questions", "plan", "skill_lesson"],
    "properties": {
        "understanding": {"type": "string", "minLength": 10},
        "questions": {"type": "array", "maxItems": 5, "items": {"type": "string", "minLength": 5}},
        "plan": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["stage_key", "action", "owner", "acceptance"],
                "properties": {
                    "stage_key": {"type": "string", "minLength": 1},
                    "action": {"type": "string", "minLength": 5},
                    "owner": {"type": "string", "minLength": 2},
                    "acceptance": {"type": "string", "minLength": 5},
                },
            },
        },
        "skill_lesson": {"type": "string", "minLength": 10},
    },
}


async def request_feedback(
    session: Any, org: str, root_id: str, message: str, actor: Actor
) -> WorkflowKind:
    root = await TaskRepository(session, org).get(root_id)
    if root.parent_task_id or root.status in {"completed", "failed", "canceled", "expired"}:
        raise PreconditionError("Feedback requires an open workflow root")
    kind = (
        WorkflowKind.BUSINESS
        if root.input.get("business_workflow")
        else WorkflowKind.AGENT
        if root.input.get("agent_workflow")
        else None
    )
    if kind is None:
        raise ValidationError("Use the workflow root to interrupt its agents")
    existing = await command_for(session, org, root_id, lock=True)
    if existing and existing.paused and existing.feedback.get("state") not in {None, "failed"}:
        raise PreconditionError("Finish the current feedback discussion before starting another")
    command = await enqueue(session, org, root_id, kind)
    command.paused = True
    command.feedback = {
        "state": "requested",
        "message": message,
        "requested_by": str(actor.id),
        "requested_at": utcnow().isoformat(),
        "revision": command.requested_seq,
        "assessment_task_id": None,
    }
    await AuditService(session, org).record(
        actor=actor,
        action="workflow.feedback.requested",
        resource_type="task",
        resource_id=root_id,
        task_id=root_id,
        context={"revision": command.requested_seq, "message": message},
    )
    return kind


class WorkflowFeedbackService:
    def __init__(self, db: Database, org: str, *, runtime: Any = None) -> None:
        self.db, self.org, self.runtime = db, org, runtime

    async def assess(self, root_id: str) -> None:
        gateway = None
        async with self.db.tenant_session(self.org) as session:
            command = await command_for(session, self.org, root_id, lock=True)
            if (
                not command
                or not command.paused
                or command.feedback.get("state") not in {"requested", "thinking"}
            ):
                return
            feedback = copy.deepcopy(command.feedback)
            root = await TaskRepository(session, self.org).get(root_id)
            catalog = [
                {"key": step.key, "title": step.title, "owner": step.owner, "kind": step.kind}
                for step in WORKFLOWS.get(str(root.input.get("business_workflow", "")), ())
            ]
            if not catalog:
                for step in root.input.get("plan", {}).get("steps", []):
                    catalog.append({"key": step["key"], "title": step["title"], "kind": "model"})
                    if step.get("human_review"):
                        catalog.append({"key": step["key"] + "_review", "kind": "gate"})
            stage_keys = [step["key"] for step in catalog]
            owners = set(
                await session.scalars(select(Agent.name).where(Agent.organization_id == self.org))
            ) | {"HR", "Design", "Procurement", "QA/QC", "Boss", "CEO"}
            if root.status in {"canceled", "expired"}:
                return
            assessment_id = feedback.get("assessment_task_id")
            if assessment_id:
                previous_task = await TaskRepository(session, self.org).get(assessment_id)
                if previous_task.status in {"running", "blocked"}:
                    # This adapter only drafts artifacts. Preserve the old ledger
                    # and create a new read-only assessment after crash, never
                    # replay an external connector or erase billed usage.
                    async with self.db.engine.begin() as assessment_lock:
                        acquired = await assessment_lock.scalar(
                            text(
                                "SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:key))"
                            ),
                            {"org": self.org, "key": "task-attempt:" + assessment_id},
                        )
                        if not acquired:
                            raise PreconditionError("The feedback assessment is still running")
                        executions = (
                            await session.scalars(
                                select(Execution).where(
                                    Execution.organization_id == self.org,
                                    Execution.task_id == assessment_id,
                                    Execution.status == "running",
                                )
                            )
                        ).all()
                        for execution in executions:
                            execution.status, execution.finished_at = "failed", utcnow()
                            execution.error_category = "readonly_feedback_interrupted"
                        await TaskRepository(session, self.org).transition(
                            assessment_id, Transition.CANCEL
                        )
                        feedback["recovered_assessments"] = [
                            *feedback.get("recovered_assessments", []),
                            assessment_id,
                        ]
                        assessment_id = None
            if not assessment_id:
                children = (
                    await session.scalars(
                        select(Task).where(
                            Task.organization_id == self.org,
                            Task.parent_task_id == root_id,
                        )
                    )
                ).all()
                completed = [t for t in children if t.status == "completed"]
                states = {t.input.get("stage_key"): t.status for t in children}
                schema = copy.deepcopy(FEEDBACK_SCHEMA)
                schema["properties"]["plan"]["items"]["properties"]["stage_key"]["enum"] = (
                    stage_keys
                )
                assessment = await TaskRepository(session, self.org).create(
                    title="Phân tích feedback: " + root.title,
                    goal=(
                        "Read operator feedback as a change request. Explain understanding, use"
                        " previous answers to resolve questions; ask only unanswered critical q"
                        "uestions (maximum five), and propose a concrete plan with an owner and"
                        " measurable acceptance for every step. Do not assume any approval, inv"
                        "ent completed evidence, or expand connector permissions. Propose one e"
                        "vidence-based skill lesson; it is a draft for later evaluation, not a "
                        "published skill."
                        " Use known_brief, SOP sources and the actual stage catalog; never ask"
                        " again for facts already supplied. Keep actions within this workflow."
                        " Actual offer delivery, payments and production accounts are unavailable;"
                        " describe drafts or required human evidence instead. Keep the plan brief."
                    ),
                    task_type="analysis",
                    owner_agent_id=root.owner_agent_id,
                    expected_output_schema=schema,
                    input={
                        "stage_key": "operator_feedback",
                        "workflow_feedback_for": root_id,
                        "brief": {
                            "original_goal": root.goal,
                            "known_brief": {
                                key: value
                                for key, value in root.input.get("brief", {}).items()
                                if key
                                not in {
                                    "test_cvs",
                                    "interview_technical",
                                    "interview_hr",
                                    "offer_acceptance",
                                    "onboarding_evidence",
                                    "delivery",
                                }
                            },
                            "sop_sources": root.input.get("sop_sources", []),
                            "approved_plan": root.input.get("plan", {}),
                            "execution_mode": root.input.get("mode", "approved_agent_plan"),
                            "jd_sections": list(JD_SECTIONS)
                            if root.input.get("business_workflow") == "mep_hiring"
                            else [],
                            "mailbox_managed_by_connector": root.input.get("business_workflow")
                            == "mep_hiring",
                            "allowed_owners": sorted(owners),
                            "stage_catalog": [
                                {**step, "status": states.get(step["key"], "not_started")}
                                for step in catalog
                            ],
                            "connector_scope": {
                                "actual_offer_delivery": False,
                                "production_accounts": False,
                                "payment": False,
                            },
                            "feedback": feedback["message"],
                            "answers": feedback.get("answers", ""),
                            "previous_assessment": feedback.get("previous_assessment", {}),
                            "answer_history": feedback.get("history", []),
                            "completed_artifacts": [
                                {
                                    "id": t.id,
                                    "title": t.title,
                                    "preview_json": json.dumps(t.output, ensure_ascii=False)[
                                        :12000
                                    ],
                                    "preview_truncated": len(
                                        json.dumps(t.output, ensure_ascii=False)
                                    )
                                    > 12000,
                                }
                                for t in completed[-6:]
                            ],
                        },
                    },
                )
                assessment_id = assessment.id
                feedback["assessment_task_id"] = assessment_id
            feedback["state"] = "thinking"
            command.feedback = feedback
        try:
            runtime = self.runtime
            if runtime is None:
                async with self.db.tenant_session(self.org) as session:
                    gateway = await build_tenant_gateway(self.org, session=session)

                def validate(output: dict[str, Any]) -> None:
                    validate_feedback(
                        output,
                        owners,
                        stage_keys,
                        {s["key"] for s in catalog if s["kind"] == "gate"},
                    )

                runtime = WorkflowEvidenceRuntime(gateway, validate)
                runtime.output_token_limit = 6000
                runtime.system_instruction_suffix = (
                    "\nFeedback discussion contract: the operator's latest answers are binding"
                    " clarifications, not an earlier model proposal. First reconcile each"
                    " previous question against those answers and known_brief. State the"
                    " resolved facts in understanding. Do not repeat resolved questions, even"
                    " paraphrased. Ask only critical facts genuinely absent from BOTH answers"
                    " and known_brief. Questions may be empty: awaiting operator confirmation"
                    " is sufficient. Missing interview/approval evidence is a future workflow"
                    " gate, not a reason to ask the same brief questions again."
                    " Plans must follow the provided stage catalog: draft before review,"
                    " review before acceptance, then onboarding. Use Vietnamese, and describe"
                    " future human actions as required evidence, not agent capabilities."
                    " Each planned action must carry its exact stage_key and appear in catalog"
                    " order. Do not ask whether a recorded pending gate has been approved: its"
                    " recorded status already answers that; include that review in the plan."
                    " JD sections are the provided jd_sections, not the six scoring criteria."
                    " Propose focused changes to this workflow; its full stage tree already"
                    " exists, so do not bundle all future steps into one action to fit a limit."
                    " A gate requires a HUMAN role owner (HR, Design, Procurement, QA/QC,"
                    " Boss or CEO), never an Agent. Mailbox configuration is managed by its"
                    " connector and is not a missing recruitment-brief fact."
                )
            outcome = await TaskAttemptRunner(self.db, self.org, runtime=runtime).execute_task(
                assessment_id
            )
            async with self.db.tenant_session(self.org) as session:
                command = await command_for(session, self.org, root_id, lock=True)
                assert command is not None
                assessment = await TaskRepository(session, self.org).get(assessment_id)
                feedback = copy.deepcopy(command.feedback)
                if outcome.status.value != "completed":
                    feedback.update(state="failed", error=outcome.summary)
                else:
                    feedback.update(
                        state="awaiting_confirmation",
                        assessment=assessment.output,
                        lesson_status="proposed",
                        lesson_evidence_task_id=assessment.id,
                    )
                command.feedback = feedback
        except Exception as exc:
            async with self.db.tenant_session(self.org) as session:
                command = await command_for(session, self.org, root_id, lock=True)
                if command:
                    command.feedback = {
                        **command.feedback,
                        "state": "failed",
                        "error": type(exc).__name__ + ": " + str(exc)[:500],
                    }
            raise
        finally:
            if gateway:
                await gateway.aclose()

    async def confirm(
        self, root_id: str, actor: Actor, *, revision: int, answers: str, new_revision: bool
    ) -> tuple[str, WorkflowKind]:
        from ai_orchestrator.application.business_workflow import create_workflow

        async with self.db.tenant_session(self.org) as session:
            command = await command_for(session, self.org, root_id, lock=True)
            if (
                not command
                or not command.paused
                or command.feedback.get("state") != "awaiting_confirmation"
                or command.feedback.get("revision") != revision
            ):
                raise PreconditionError("Reload the feedback discussion before confirming")
            feedback = copy.deepcopy(command.feedback)
            if feedback["assessment"].get("questions"):
                raise ValidationError("Send answers for model review before confirming the plan")
            reviewed_answers = feedback.get("answers", "")
            if answers.strip() and answers.strip() != reviewed_answers.strip():
                raise ValidationError("Send new corrections for model review before confirming")
            answers = reviewed_answers
            root = await TaskRepository(session, self.org).get(root_id)
            if root.status in {"completed", "failed", "canceled", "expired"}:
                raise PreconditionError("This workflow is no longer open")
            kind = WorkflowKind(command.kind)
            context = {
                "message": feedback["message"],
                "answers": answers,
                "answer_history": feedback.get("history", []),
                "understanding": feedback["assessment"]["understanding"],
                "plan": feedback["assessment"]["plan"],
                "assessment_task_id": feedback["assessment_task_id"],
                "confirmed_by": str(actor.id),
            }
            destination = root_id
            if new_revision:
                if kind is not WorkflowKind.BUSINESS:
                    raise ValidationError("Revise an agent operating plan in its blueprint editor")
                brief = copy.deepcopy(root.input["brief"])
                brief["boss_brief"] += (
                    "\nFeedback confirmed by operator: "
                    + feedback["message"]
                    + "\nAnswers: "
                    + answers
                )
                destination = await create_workflow(
                    session, self.org, root.input["business_workflow"], root.input["mode"], brief
                )
                replacement = await TaskRepository(session, self.org).get(destination)
                replacement.input = {
                    **replacement.input,
                    "feedback_context": context,
                    "revision_of": root_id,
                }
            else:
                root.input = {**root.input, "feedback_context": context}
            skill = Skill(
                id=make_id("skl"),
                organization_id=self.org,
                name="Feedback lesson " + feedback["assessment_task_id"],
                description="Candidate lesson from an operator-confirmed feedback plan",
                governance_state="experimental",
                requires_approval=True,
                tags=["feedback", "candidate"],
            )
            session.add(skill)
            await session.flush()
            version = SkillVersion(
                id=make_id("skv"),
                organization_id=self.org,
                skill_id=skill.id,
                version="1",
                instructions=feedback["assessment"]["skill_lesson"],
                derived_from={
                    "workflow_root": root_id,
                    "assessment_task_id": feedback["assessment_task_id"],
                    "feedback_revision": revision,
                    "confirmed_by": str(actor.id),
                    "lesson_scope": "proposal_only",
                },
                test_results={},
                is_published=False,
            )
            session.add(version)
            feedback.update(
                skill_id=skill.id,
                skill_version_id=version.id,
                lesson_status="candidate_unpublished",
            )
            feedback.update(
                state="applied",
                answers=answers,
                destination_root_id=destination,
                confirmed_by=str(actor.id),
            )
            command.feedback = feedback
            # A revision keeps the old campaign paused and immutable.
            command.paused = new_revision
            command.settled_seq = command.requested_seq
            command.state = "superseded" if new_revision else "waiting"
            await enqueue(session, self.org, destination, kind)
            await AuditService(session, self.org).record(
                actor=actor,
                action="workflow.feedback.confirmed",
                resource_type="task",
                resource_id=root_id,
                task_id=root_id,
                context={
                    "revision": revision,
                    "destination": destination,
                    "new_revision": new_revision,
                    "answers": answers,
                    "assessment_task_id": feedback["assessment_task_id"],
                },
            )
            return destination, kind

    async def answer(
        self, root_id: str, actor: Actor, *, revision: int, answers: str
    ) -> WorkflowKind:
        async with self.db.tenant_session(self.org) as session:
            command = await command_for(session, self.org, root_id, lock=True)
            if (
                not command
                or not command.paused
                or command.feedback.get("state") != "awaiting_confirmation"
                or command.feedback.get("revision") != revision
            ):
                raise PreconditionError("Reload before answering the current feedback questions")
            feedback = copy.deepcopy(command.feedback)
            kind = WorkflowKind(command.kind)
            command = await enqueue(session, self.org, root_id, kind)
            feedback.update(
                state="requested",
                answers=answers,
                previous_assessment=feedback.get("assessment", {}),
                assessment_task_id=None,
                revision=command.requested_seq,
                history=[
                    *feedback.get("history", []),
                    {
                        "assessment_task_id": feedback["assessment_task_id"],
                        "answers": answers,
                        "actor": str(actor.id),
                    },
                ],
            )
            feedback.pop("assessment", None)
            command.feedback = feedback
            await AuditService(session, self.org).record(
                actor=actor,
                action="workflow.feedback.answered",
                resource_type="task",
                resource_id=root_id,
                task_id=root_id,
                context={"revision": feedback["revision"], "answers": answers},
            )
            return kind
