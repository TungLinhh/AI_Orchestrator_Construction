"""Ordered workflows persisted as tasks, executions, reviews and outbox events."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime
from ai_orchestrator.application.model_profiles import build_tenant_gateway
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.workflow_schemas import artifact_schema, validate_shape
from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import DEV_DATA_DIR, get_settings
from ai_orchestrator.domain.business_workflow import (
    HIRING,
    RUBRIC_SPEC,
    WORKFLOWS,
    WorkflowStage,
    score_cv,
    validate_match,
)
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, EventType, RunMode
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.hiring_process import JD_SECTIONS
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.integrations.recruitment_mail import RecruitmentMailbox
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import Agent, Approval, AuditLog, Event, ModelUsage, Task
from ai_orchestrator.persistence.repositories.task import ExecutionRepository, TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)
SYSTEM = Actor(id="workflow-controller", kind=ActorType.SYSTEM, display_name="Workflow controller")


def payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def validate_artifact(
    key: str, output: dict[str, Any], brief: dict[str, Any], prior: dict[str, Any]
) -> None:
    """Check actual business evidence as well as contract fields."""
    if key == "jd":
        sections = output["sections"]
        names = set(sections) if isinstance(sections, dict) else {r.get("name") for r in sections}
        if names != set(JD_SECTIONS):
            raise ValueError("JD must contain exactly the six named O-Nexus sections")
        values = (
            list(sections.values())
            if isinstance(sections, dict)
            else [r.get("content") for r in sections]
        )
        if any(not isinstance(v, str) or len(v.strip()) < 20 for v in values):
            raise ValueError("Every JD section needs substantive content")
    elif key == "rubric":
        rows = output["criteria"]
        if len(rows) != len(RUBRIC_SPEC) or {r["key"]: r["max_points"] for r in rows} != dict(
            RUBRIC_SPEC
        ):
            raise ValueError("Rubric keys and weights must equal the proposed specification")
        if output["threshold"] != 70:
            raise ValueError("This run's proposed threshold is 70, not a dossier rule")
    elif key == "scoring":
        cvs = {c["candidate_id"]: c for c in prior["cv_intake"]["cvs"]}
        rows = output["candidates"]
        if len(rows) != len(cvs) or {r["candidate_id"] for r in rows} != set(cvs):
            raise ValueError("Score each received CV exactly once using its SHA-256 candidate_id")
        for row in rows:
            if not all(row.get(k) for k in ("strengths", "gaps", "interview_questions")):
                raise ValueError("Every CV needs strengths, gaps and targeted interview questions")
            try:
                row["score"] = score_cv(row["criteria"], cvs[row["candidate_id"]]["text"])
            except ValueError as exc:
                raise ValueError("Candidate " + row["candidate_id"][:12] + ": " + str(exc)) from exc
            row["source_sha256"] = row["candidate_id"]
            row["recommendation"] = (
                "interview" if row["score"] >= prior["rubric"]["threshold"] else "not_shortlisted"
            )
    elif key == "selection":
        candidate = output["recommended_candidate_id"]
        scored = {c["candidate_id"]: c for c in prior["scoring"]["candidates"]}
        if candidate not in scored or scored[candidate]["score"] < prior["rubric"]["threshold"]:
            raise ValueError("Selection must reference a candidate above the reviewed threshold")
        cv = next(c for c in prior["cv_intake"]["cvs"] if c["candidate_id"] == candidate)
        for interview in ("interview_technical", "interview_hr"):
            if not any(
                r.get("filename") == cv["filename"] and r.get("result") == "pass"
                for r in prior[interview]["transcripts"]
            ):
                raise ValueError(
                    "Recommended candidate requires passing evidence from both interview rounds"
                )
    elif key == "offer":
        if output["candidate_id"] != prior["selection"]["recommended_candidate_id"]:
            raise ValueError("Offer must reference the reviewed selected candidate")
        if (
            not isinstance(output["salary"], (int, float))
            or isinstance(output["salary"], bool)
            or not brief["salary_min"] <= output["salary"] <= brief["salary_max"]
        ):
            raise ValueError("Salary must remain within the boss-approved range")
        if output["start_date"] != brief["start_date"]:
            raise ValueError("Offer start_date must match the supplied date")
    elif key == "plan":
        rows = output["material_plan"]
        expected = {r["material_id"]: r for r in brief["boq"]}
        if len(rows) != len(expected) or {r["material_id"] for r in rows} != set(expected):
            raise ValueError("Material plan must cover every BOQ material exactly once")
        if any(r.get("quantity") != expected[r["material_id"]]["quantity"] for r in rows):
            raise ValueError("Material plan quantities must equal the BOQ")
    elif key == "rfq":
        supplier_ids = {s["supplier_id"] for s in brief["suppliers"]}
        if len(set(output["supplier_ids"])) < 3 or set(output["supplier_ids"]) != supplier_ids:
            raise ValueError("RFQ must cover at least three supplied vendors")
    elif key == "qualification":
        expected = {s["supplier_id"]: s for s in brief["suppliers"]}
        rows = output["suppliers"]
        if len(rows) != len(expected) or {s["supplier_id"] for s in rows} != set(expected):
            raise ValueError("Qualification must cover every supplier")
        for s in rows:
            if s["classification"] not in {"green", "yellow", "red"} or not s.get("reason"):
                raise ValueError("Supplier classification and reason required")
            if not expected[s["supplier_id"]]["legal_valid"] and s["classification"] != "red":
                raise ValueError("Invalid legal credentials must be classified red")
    elif key == "quality":
        quotes = {
            (s["supplier_id"], q["material_id"]): q for s in brief["suppliers"] for q in s["quotes"]
        }
        rows = output["assessments"]
        if len(rows) != len(quotes) or {(r["supplier_id"], r["material_id"]) for r in rows} != set(
            quotes
        ):
            raise ValueError("Quality review must cover every supplier/material pair")
        for r in rows:
            q = quotes[(r["supplier_id"], r["material_id"])]
            if (
                type(r.get("accepted")) is not bool
                or r.get("certificate_ref") != q["certificate_ref"]
            ):
                raise ValueError(
                    "Quality decision needs exact certificate source and boolean accepted"
                )
            if r["accepted"] and (not q["certificate_ref"] or not q["spec_compliant"]):
                raise ValueError("Uncertified/noncompliant material cannot pass quality")
            if not r.get("reason"):
                raise ValueError("Each quality decision needs a reason")
    elif key == "comparison":
        expected = {m["material_id"]: m for m in brief["boq"]}
        quotes = {
            (s["supplier_id"], q["material_id"]): q for s in brief["suppliers"] for q in s["quotes"]
        }
        qualification = {
            s["supplier_id"]: s["classification"] for s in prior["qualification"]["suppliers"]
        }
        quality = {
            (r["supplier_id"], r["material_id"]): r["accepted"]
            for r in prior["quality"]["assessments"]
        }
        awards = output["recommended_awards"]
        if len(awards) != len(expected) or {r["material_id"] for r in awards} != set(expected):
            raise ValueError("Award must cover every BOQ material exactly once")
        for r in awards:
            pair = (r["supplier_id"], r["material_id"])
            if (
                pair not in quotes
                or qualification.get(r["supplier_id"]) == "red"
                or not quality.get(pair)
            ):
                raise ValueError("Cannot award red suppliers or unapproved material quality")
            if (
                r["quantity"] != expected[r["material_id"]]["quantity"]
                or r["unit_price"] != quotes[pair]["unit_price"]
            ):
                raise ValueError("Award quantities/prices must match actual BOQ and quotes")
        output["total_vnd"] = sum(r["quantity"] * r["unit_price"] for r in awards)


async def create_workflow(
    session: AsyncSession,
    org: str,
    kind: str,
    mode: str,
    brief: dict[str, Any],
    reuse_source: str | None = None,
) -> str:
    if kind not in WORKFLOWS or mode not in {"simulation", "live"}:
        raise ValidationError("Unknown workflow or run mode")
    if mode == "simulation" and brief.get("synthetic") is not True:
        raise ValidationError("Simulation requires explicit synthetic inputs")
    if kind == "mep_hiring":
        if (
            not brief.get("position")
            or brief.get("headcount") != 1
            or not isinstance(brief.get("salary_min"), (int, float))
            or not isinstance(brief.get("salary_max"), (int, float))
            or not 0 < brief["salary_min"] <= brief["salary_max"]
        ):
            raise ValidationError(
                "Hiring requires a position, one headcount and a valid salary range"
            )
        if mode == "simulation" and not brief.get("test_cvs"):
            raise ValidationError("Simulation requires actual CV test attachments to send")
        sop_codes = ["ONX-BO-HR-SOP-004"]
    else:
        if not brief.get("boq") or len(brief.get("suppliers", [])) < 3:
            raise ValidationError(
                "Procurement requires a nonempty BOQ and at least three suppliers"
            )
        sop_codes = ["ONX-MO-PRC-SOP-005", "ONX-MO-PRC-SOP-006"]
    sources = []
    for code in sop_codes:
        row = (
            (
                await session.execute(
                    text(
                        (
                            "SELECT code, name_vi, source FROM sop_definitions WHERE "
                            "organization_id=:org AND code=:code"
                        )
                    ),
                    {"org": org, "code": code},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise PreconditionError("Required O-Nexus SOP is not registered: " + code)
        sources.append(dict(row))
    names = {s.owner for s in WORKFLOWS[kind]}
    agents = {
        a.name: a
        for a in (
            await session.execute(
                select(Agent).where(Agent.organization_id == org, Agent.name.in_(names))
            )
        )
        .scalars()
        .all()
    }
    if set(agents) != names:
        raise PreconditionError("Required workflow owner agents are not registered")
    tasks = TaskRepository(session, org)
    root = await tasks.create(
        title="Tuyển kỹ sư MEP đến onboarding"
        if kind == "mep_hiring"
        else "Chuẩn bị mua vật tư và chọn NCC",
        goal=brief.get("boss_brief", "Complete the ordered O-Nexus workflow"),
        task_type="coordination",
        owner_agent_id=agents["Executive Agent"].id,
        allow_parallel=True,
        input={
            "business_workflow": kind,
            "workflow_version": 1,
            "mode": mode,
            "brief": brief,
            "sop_sources": sources,
            "reuse_source": reuse_source,
        },
    )
    previous = None
    for stage in WORKFLOWS[kind]:
        if stage.simulation_only and mode != "simulation":
            continue
        owner = agents[stage.owner]
        child = await tasks.create(
            title=stage.title,
            goal=stage.instruction or stage.title,
            task_type="analysis",
            owner_agent_id=owner.id,
            org_unit_id=owner.org_unit_id,
            parent_task_id=root.id,
            input={"business_workflow": kind, "stage_key": stage.key, "mode": mode},
            expected_output_schema=artifact_schema(stage.key),
        )
        if previous:
            await tasks.add_dependency(task_id=child.id, depends_on_task_id=previous)
        previous = child.id
    await AuditService(session, org).record(
        actor=SYSTEM,
        action="workflow.created",
        resource_type="task",
        resource_id=root.id,
        task_id=root.id,
        context={"kind": kind, "mode": mode, "source_codes": sop_codes},
    )
    return str(root.id)


class BusinessWorkflowService:
    def __init__(
        self,
        db: Database,
        organization_id: str,
        *,
        runtime_factory: Callable[..., Any] | None = None,
        mailbox_factory: Callable[..., Any] = RecruitmentMailbox,
    ) -> None:
        self.db = db
        self.org = organization_id
        self.runtime_factory = runtime_factory
        self.mailbox_factory = mailbox_factory

    async def report(self, root_id: str) -> dict[str, Any]:
        async with self.db.tenant_session(self.org) as session:
            root = await TaskRepository(session, self.org).get(root_id)
            if not root.input.get("business_workflow") or root.parent_task_id:
                raise ValidationError("Task is not a business workflow root")
            rows = (
                (
                    await session.execute(
                        select(Task)
                        .where(Task.organization_id == self.org, Task.parent_task_id == root_id)
                        .order_by(Task.created_at, Task.id)
                    )
                )
                .scalars()
                .all()
            )
            ids = [root_id, *(t.id for t in rows)]
            usage = (
                (
                    await session.execute(
                        select(ModelUsage).where(
                            ModelUsage.organization_id == self.org, ModelUsage.task_id.in_(ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            events = len(
                (
                    await session.execute(
                        select(Event.id).where(
                            Event.organization_id == self.org, Event.subject.in_(ids)
                        )
                    )
                ).all()
            )
            audits = len(
                (
                    await session.execute(
                        select(AuditLog.id).where(
                            AuditLog.organization_id == self.org, AuditLog.task_id.in_(ids)
                        )
                    )
                ).all()
            )
            fake = sum(
                u.provider in {"fake", "scripted", "deterministic", "unit-fake"} for u in usage
            )
            return {
                "id": root.id,
                "kind": root.input["business_workflow"],
                "mode": root.input["mode"],
                "title": root.title,
                "status": root.status,
                "error": root.last_error,
                "summary": root.output or {},
                "sources": root.input["sop_sources"],
                "mail_intake": {
                    "address": get_settings().recruitment_mail_address
                    if get_settings().recruitment_mail_org == self.org
                    else None,
                    "subject": RecruitmentMailbox.subject(root.id),
                }
                if root.input["business_workflow"] == "mep_hiring"
                else None,
                "evidence": {
                    "real_model_calls": len(usage) - fake,
                    "fake_model_calls": fake,
                    "models": sorted({u.model_used for u in usage}),
                    "events": events,
                    "audits": audits,
                },
                "completed": sum(t.status == "completed" for t in rows),
                "total": len(rows),
                "stages": [
                    {
                        "id": t.id,
                        "key": t.input["stage_key"],
                        "title": t.title,
                        "owner_agent_id": t.owner_agent_id,
                        "status": t.status,
                        "output": t.output or {},
                        "error": t.last_error,
                        "reused_from": t.constraints.get("reused_artifact_source"),
                    }
                    for t in rows
                ],
            }

    async def run(self, root_id: str) -> dict[str, Any]:
        # Transaction advisory lock releases on cancellation and process death,
        # including pooled connections. Separate transactions publish each step.
        async with self.db.engine.begin() as lock:
            claimed: bool = (
                await lock.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtext(:org), hashtext(:root))"),
                    {"org": self.org, "root": root_id},
                )
            ).scalar_one()
            if not claimed:
                raise PreconditionError("This workflow is already running")
            report = await self.report(root_id)
            if report["status"] in {"completed", "failed", "canceled", "expired"}:
                return report
            async with self.db.tenant_session(self.org) as session:
                repo = TaskRepository(session, self.org)
                root = await repo.get(root_id)
                kind = root.input["business_workflow"]
                mode = root.input["mode"]
                brief = copy.deepcopy(root.input["brief"])
                reuse_source = root.input.get("reuse_source")
                if root.status == "blocked":
                    await repo.transition(root_id, Transition.UNBLOCK)
                elif root.status == "assigned":
                    await repo.transition(root_id, Transition.BEGIN_WORK)
                execution = await ExecutionRepository(session, self.org).start(
                    task_id=root_id,
                    agent_id=root.owner_agent_id,
                    runtime_adapter="business_workflow",
                    model_profile="workflow-controller",
                    attempt=1
                    + len(await ExecutionRepository(session, self.org).list_for_task(root_id)),
                )
                execution_id = str(execution.id)
            prior: dict[str, Any] = {}
            stage_ids = {s["key"]: s["id"] for s in report["stages"]}
            try:
                for stage in WORKFLOWS[kind]:
                    if stage.key not in stage_ids:
                        continue
                    logger.info(
                        "business_workflow.step", root_id=root_id, stage=stage.key, mode=mode
                    )
                    async with self.db.committing_tenant_session(self.org) as session:
                        repo = TaskRepository(session, self.org)
                        current_root = await repo.get(root_id)
                        if current_root.status in {"canceled", "expired", "failed"}:
                            raise ValueError("Workflow stopped before stage " + stage.key)
                        task = await repo.get(stage_ids[stage.key])
                        if task.status == "completed":
                            prior[stage.key] = copy.deepcopy(task.output or {})
                            continue
                        if task.status in {"failed", "canceled", "expired"}:
                            raise ValueError("Predecessor/stage did not complete: " + stage.key)
                        for predecessor in prior:
                            if (await repo.get(stage_ids[predecessor])).status != "completed":
                                raise ValueError("A prerequisite is no longer completed")
                        stage_brief = brief
                        if stage.kind == "model":
                            stage_brief = {
                                k: v
                                for k, v in brief.items()
                                if k
                                not in {
                                    "test_cvs",
                                    "interview_technical",
                                    "interview_hr",
                                    "offer_acceptance",
                                    "onboarding_evidence",
                                    "delivery",
                                }
                            }
                        task.input = {
                            **task.input,
                            "brief": stage_brief,
                            "prior": copy.deepcopy(prior),
                        }
                        evidence_key = (
                            "onboarding_evidence" if stage.key == "onboarding_setup" else stage.key
                        )
                        needs_input = stage.kind == "input" and stage.key != "brief"
                        needs_input = needs_input or (
                            mode == "live" and stage.key == "onboarding_setup"
                        )
                        if needs_input and not brief.get(evidence_key):
                            if task.status == "assigned":
                                await repo.transition(task.id, Transition.REQUEST_INPUT)
                            await session.commit()
                            await self._pause(
                                root_id, execution_id, "Chờ chứng cứ cho bước " + stage.title
                            )
                            return await self.report(root_id)
                        if stage.kind == "gate":
                            output = await self._gate(session, task, root_id, stage, mode, prior)
                            if output is None:
                                await session.commit()
                                await self._pause(
                                    root_id, execution_id, "Chờ người duyệt bước " + stage.title
                                )
                                return await self.report(root_id)
                        elif stage.kind == "model":
                            gateway = None

                            def validator(
                                result: dict[str, Any], current: WorkflowStage = stage
                            ) -> None:
                                if any(
                                    k not in result or result[k] is None for k in current.fields
                                ):
                                    raise ValueError(
                                        "All declared artifact fields are required: "
                                        + ", ".join(current.fields)
                                    )
                                validate_shape(result, artifact_schema(current.key))
                                validate_artifact(current.key, result, brief, prior)

                            reused = None
                            if reuse_source:
                                source_root = await repo.get(reuse_source)
                                if source_root.input.get("mode") != mode or payload_hash(
                                    source_root.input.get("brief")
                                ) != payload_hash(brief):
                                    raise ValueError(
                                        "Artifact reuse requires identical brief and mode"
                                    )
                                source = (
                                    await session.execute(
                                        select(Task).where(
                                            Task.organization_id == self.org,
                                            Task.parent_task_id == reuse_source,
                                            Task.input["stage_key"].astext == stage.key,
                                            Task.status == "completed",
                                        )
                                    )
                                ).scalar_one_or_none()
                                if source and any(
                                    k in source.input.get("brief", {})
                                    for k in {
                                        "test_cvs",
                                        "interview_technical",
                                        "interview_hr",
                                        "offer_acceptance",
                                        "onboarding_evidence",
                                        "delivery",
                                    }
                                ):
                                    logger.info(
                                        "workflow.artifact_reuse_refused",
                                        stage=stage.key,
                                        source_task=source.id,
                                        reason="Future fixture data was exposed",
                                    )
                                    source = None
                                gates = {s.key for s in WORKFLOWS[kind] if s.kind == "gate"}
                                if source and payload_hash(
                                    {k: v for k, v in prior.items() if k not in gates}
                                ) != payload_hash(
                                    {
                                        k: v
                                        for k, v in source.input.get("prior", {}).items()
                                        if k not in gates
                                    }
                                ):
                                    logger.info(
                                        "workflow.artifact_reuse_refused",
                                        stage=stage.key,
                                        source_task=source.id,
                                        reason="Source inputs changed",
                                    )
                                    source = None
                                if source:
                                    reused = copy.deepcopy(source.output or {})
                                    if stage.key == "comparison":
                                        reused.pop("total_vnd", None)
                                    if stage.key == "scoring":
                                        for candidate in reused.get("candidates", []):
                                            for field in (
                                                "score",
                                                "source_sha256",
                                                "recommendation",
                                            ):
                                                candidate.pop(field, None)
                                            for criterion in candidate.get("criteria", []):
                                                criterion.pop("points", None)
                                    validator(reused)
                                    task.constraints = {
                                        **task.constraints,
                                        "reused_artifact_source": source.id,
                                    }
                                    await repo.transition(task.id, Transition.BEGIN_WORK)
                                    await self._complete_mechanical(
                                        session, task, reused, "verified_artifact_reuse"
                                    )
                                    await AuditService(session, self.org).record(
                                        actor=SYSTEM,
                                        action="workflow.artifact.reused",
                                        resource_type="task",
                                        resource_id=task.id,
                                        task_id=task.id,
                                        context={
                                            "source_task_id": source.id,
                                            "source_root_id": reuse_source,
                                            "artifact_hash": payload_hash(reused),
                                        },
                                    )
                                    await session.commit()
                                    prior[stage.key] = reused
                                    continue
                            if self.runtime_factory:
                                runtime = self.runtime_factory(stage, validator)
                            else:
                                if get_settings().model_provider_default in {
                                    "fake",
                                    "scripted",
                                    "deterministic",
                                    "none",
                                    "null",
                                }:
                                    raise ValueError(
                                        "Business acceptance requires "
                                        "an explicitly configured real model"
                                    )
                                gateway = await build_tenant_gateway(self.org, session=session)

                                validation_prior = copy.deepcopy(prior)

                                def candidate_validator(
                                    cv: dict[str, Any],
                                    artifact: dict[str, Any],
                                    stage_prior: dict[str, Any] = validation_prior,
                                ) -> None:
                                    single_prior = {**stage_prior, "cv_intake": {"cvs": [cv]}}
                                    validate_shape(artifact, artifact_schema("scoring"))
                                    validate_artifact("scoring", artifact, brief, single_prior)

                                runtime = WorkflowEvidenceRuntime(
                                    gateway,
                                    validator,
                                    candidate_validator if stage.key == "scoring" else None,
                                )

                            async def checkpoint_usage(
                                task_id: str = task.id,
                                stage_key: str = stage.key,
                                task_repo: TaskRepository = repo,
                            ) -> None:
                                await task_repo.emit(
                                    EventType.TASK_UPDATED,
                                    subject=task_id,
                                    data={
                                        "task_id": task_id,
                                        "model_call_recorded": True,
                                        "stage_key": stage_key,
                                    },
                                )
                                await session.commit()
                                await self.db.bind_tenant(session, self.org)

                            try:
                                outcome = await TaskExecutionService(
                                    session,
                                    self.org,
                                    runtime=runtime,
                                    run_mode=RunMode.LIVE,
                                    auto_approve=False,
                                    model_usage_checkpoint=checkpoint_usage,
                                ).execute_task(task.id)
                            finally:
                                if gateway:
                                    await gateway.aclose()
                            if outcome.status.value != "completed":
                                await session.commit()
                                raise ValueError(
                                    "Model stage did not complete: "
                                    + stage.key
                                    + " ("
                                    + outcome.status.value
                                    + ")"
                                )
                            await session.refresh(task)
                            output = task.output or {}
                        else:
                            input_resumed = task.status == "waiting_for_input"
                            if input_resumed:
                                await repo.transition(task.id, Transition.PROVIDE_INPUT)
                            if task.status == "running" and not input_resumed:
                                raise ValueError(
                                    (
                                        "Interrupted action requires "
                                        "evidence reconciliation before "
                                        "retry: "
                                    )
                                    + stage.key
                                )
                            if not input_resumed:
                                await repo.transition(task.id, Transition.BEGIN_WORK)
                            await session.commit()
                            await self.db.bind_tenant(session, self.org)
                            try:
                                output = await self._mechanical(root_id, stage, mode, brief, prior)
                            except Exception as exc:
                                await repo.transition(
                                    task.id,
                                    Transition.FAIL,
                                    error="Step refused: " + str(exc),
                                    failure_category="workflow_evidence",
                                )
                                await session.commit()
                                raise
                        if output.get("awaiting_cv"):
                            task.output = output
                            await repo.transition(task.id, Transition.REQUEST_INPUT)
                            poll = await ExecutionRepository(session, self.org).start(
                                task_id=task.id,
                                agent_id=task.owner_agent_id,
                                runtime_adapter="workflow_mail_read",
                                model_profile="no-model-mechanical",
                            )
                            await ExecutionRepository(session, self.org).finish(
                                poll.id,
                                status="blocked",
                                summary="Mailbox checked read-only; waiting for CVs",
                                decision_record={"count": output["count"], "readonly": True},
                            )
                            await AuditService(session, self.org).record(
                                actor=SYSTEM,
                                action="workflow.cv.poll",
                                resource_type="task",
                                resource_id=task.id,
                                task_id=task.id,
                                context={"count": output["count"], "readonly": True},
                            )
                            await session.commit()
                            await self._pause(
                                root_id,
                                execution_id,
                                "Chờ CV; hệ thống sẽ kiểm tra lại hộp thư theo mã đợt",
                            )
                            return await self.report(root_id)
                        if stage.kind != "model":
                            await self._complete_mechanical(session, task, output, stage.kind)
                        await session.commit()
                        prior[stage.key] = copy.deepcopy(output)
                async with self.db.tenant_session(self.org) as session:
                    repo = TaskRepository(session, self.org)
                    root = await repo.get(root_id)
                    summary = prior["close"]
                    root.output = summary
                    await repo.transition(root_id, Transition.COMPLETE)
                    await ExecutionRepository(session, self.org).finish(
                        execution_id,
                        status="completed",
                        summary=summary["summary"],
                        decision_record={"mode": mode, "completed_steps": list(prior)},
                    )
                    await AuditService(session, self.org).record(
                        actor=SYSTEM,
                        action="workflow.completed",
                        resource_type="task",
                        resource_id=root_id,
                        task_id=root_id,
                        context={
                            "mode": mode,
                            "completed_steps": len(prior),
                            "artifact_hash": payload_hash(summary),
                        },
                    )
            except BaseException as exc:
                reason = (
                    "Interrupted workflow" if isinstance(exc, asyncio.CancelledError) else str(exc)
                )
                async with self.db.tenant_session(self.org) as session:
                    repo = TaskRepository(session, self.org)
                    root = await repo.get(root_id)
                    if root.status not in {"completed", "failed", "canceled", "expired"}:
                        await repo.transition(
                            root_id,
                            Transition.FAIL,
                            error=reason,
                            failure_category="workflow_evidence",
                        )
                    await ExecutionRepository(session, self.org).finish(
                        execution_id,
                        status="failed",
                        error_category="workflow_evidence",
                        error_message=reason,
                    )
                async with self.db.tenant_session(self.org) as session:
                    repo = TaskRepository(session, self.org)
                    for tid, status in (await repo.subtree_statuses(root_id)).items():
                        if status not in {"completed", "failed", "canceled", "expired"}:
                            await repo.transition(tid, Transition.CANCEL)
                    from ai_orchestrator.persistence.models import Execution

                    unfinished = (
                        (
                            await session.execute(
                                select(Execution).where(
                                    Execution.organization_id == self.org,
                                    Execution.task_id.in_(list(stage_ids.values())),
                                    Execution.status == "running",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for row in unfinished:
                        await ExecutionRepository(session, self.org).finish(
                            row.id,
                            status="failed",
                            error_category="workflow_evidence",
                            error_message=reason[:2000],
                        )
                        await AuditService(session, self.org).record(
                            actor=SYSTEM,
                            action="workflow.execution.interrupted",
                            resource_type="execution",
                            resource_id=row.id,
                            task_id=row.task_id,
                            context={"root": root_id, "reason": reason[:200]},
                        )
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise
            return await self.report(root_id)

    async def _pause(self, root: str, execution: str, reason: str) -> None:
        async with self.db.tenant_session(self.org) as session:
            task = await TaskRepository(session, self.org).get(root)
            task.output = {"summary": reason, "mode": task.input["mode"]}
            await TaskRepository(session, self.org).transition(root, Transition.BLOCK)
            await ExecutionRepository(session, self.org).finish(
                execution, status="blocked", summary=reason
            )

    async def _gate(
        self,
        session: AsyncSession,
        task: Task,
        root: str,
        stage: WorkflowStage,
        mode: str,
        prior: dict[str, Any],
    ) -> dict[str, Any] | None:
        repo = TaskRepository(session, self.org)
        reviewed = copy.deepcopy(next(reversed(prior.values())))
        snapshot = {"workflow_root": root, "stage_key": stage.key, "artifact": reviewed}
        if mode == "simulation":
            if task.status != "assigned":
                raise ValueError("Simulated gate must not consume a real pending approval")
            await repo.transition(task.id, Transition.BEGIN_WORK)
            output = {
                "decision": "simulated_approved",
                "label": "Phê duyệt mô phỏng",
                "mode": "simulation",
                "human_decision": False,
                "review_role": stage.owner,
                "artifact_hash": payload_hash(reviewed),
                "recorded_at": utcnow().isoformat(),
                "conditions": (
                    "Chỉ sử dụng cho dữ liệu và môi trường thử; không cho phép hành động pr"
                    "oduction."
                ),
            }
            await AuditService(session, self.org).record(
                actor=SYSTEM,
                action="workflow.review.simulated",
                resource_type="task",
                resource_id=task.id,
                task_id=task.id,
                outcome="simulated",
                context={
                    "root": root,
                    "stage": stage.key,
                    "artifact_hash": output["artifact_hash"],
                    "human_decision": False,
                },
            )
            return dict(output)
        service = ApprovalService(session, self.org)
        approvals = (
            (
                await session.execute(
                    select(Approval)
                    .where(Approval.organization_id == self.org, Approval.task_id == task.id)
                    .order_by(Approval.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        if not approvals:
            await service.create(
                ApprovalRequest(
                    organization_id=self.org,
                    action_type="workflow.review",
                    action_payload=snapshot,
                    requested_by=task.owner_agent_id or "workflow-controller",
                    requested_by_type=ActorType.AGENT,
                    task_id=task.id,
                    reason=stage.title + ": duyệt đúng sản phẩm trước khi tiếp tục",
                    ttl_seconds=7 * 24 * 3600,
                )
            )
            await repo.transition(task.id, Transition.REQUEST_APPROVAL)
            return None
        approval = approvals[0]
        if approval.status == "approved":
            await service.verify_payload(approval.id, snapshot)
            await repo.transition(task.id, Transition.APPROVAL_GRANTED)
            return {
                "decision": "approved",
                "mode": "live",
                "human_decision": True,
                "approval_id": approval.id,
                "decided_by": approval.decided_by,
                "artifact_hash": payload_hash(reviewed),
                "note": approval.decision_note,
            }
        if approval.status in {"rejected", "expired"}:
            await repo.transition(
                task.id, Transition.APPROVAL_REJECTED, error="Human approval refused or expired"
            )
            raise ValueError("Human approval refused or expired")
        return None

    async def _complete_mechanical(
        self, session: AsyncSession, task: Task, output: dict[str, Any], kind: str
    ) -> None:
        task.output = output
        execution = await ExecutionRepository(session, self.org).start(
            task_id=task.id,
            agent_id=task.owner_agent_id,
            runtime_adapter="workflow_" + kind,
            model_profile="no-model-mechanical",
        )
        await ExecutionRepository(session, self.org).finish(
            execution.id,
            status="completed",
            summary=task.title,
            decision_record={
                "kind": kind,
                "mode": task.input["mode"],
                "artifact_hash": payload_hash(output),
            },
        )
        await TaskRepository(session, self.org).transition(task.id, Transition.COMPLETE)
        await AuditService(session, self.org).record(
            actor=SYSTEM,
            action="workflow.step.completed",
            resource_type="task",
            resource_id=task.id,
            task_id=task.id,
            context={
                "kind": kind,
                "mode": task.input["mode"],
                "artifact_hash": payload_hash(output),
            },
        )

    async def _mechanical(
        self,
        root: str,
        stage: WorkflowStage,
        mode: str,
        brief: dict[str, Any],
        prior: dict[str, Any],
    ) -> dict[str, Any]:
        if stage.key == "brief":
            return {
                "boss_brief": brief["boss_brief"],
                "position": brief.get("position"),
                "headcount": brief.get("headcount"),
                "salary_range": [brief.get("salary_min"), brief.get("salary_max")],
                "mode": mode,
                "synthetic": brief.get("synthetic", False),
            }
        if stage.kind == "mail_send":
            return await asyncio.to_thread(
                self.mailbox_factory(self.org).send_tests, root, brief["test_cvs"]
            )
        if stage.kind == "mail_read":
            mailbox = self.mailbox_factory(self.org)
            output = {}
            for attempt in range(6):
                output = await asyncio.to_thread(mailbox.read_cvs, root)
                expected = {r["sha256"] for r in prior.get("test_mail", {}).get("sent", [])}
                got = {cv["sha256"] for cv in output["cvs"]}
                if (
                    got
                    and (mode != "simulation" or got == expected)
                    and len(got) >= brief.get("minimum_cvs", 1)
                ):
                    if mode == "simulation" and any(
                        cv.get("synthetic") is not True for cv in output["cvs"]
                    ):
                        raise ValueError("Simulation cannot consume actual applicant CVs")
                    return dict(output)
                if mode == "live":
                    return {**output, "awaiting_cv": True, "observed_at": utcnow().isoformat()}
                if attempt < 5:
                    await asyncio.sleep(5)
            raise ValueError("No complete CV intake from this run's actual mailbox messages")
        if stage.key in {"interview_technical", "interview_hr", "offer_acceptance"}:
            record = copy.deepcopy(brief.get(stage.key))
            if not record or not record.get("source"):
                raise ValueError("Evidence input is missing: " + stage.key)
            if mode == "simulation" and record.get("synthetic") is not True:
                raise ValueError("Synthetic interview/acceptance evidence required in simulation")
            if mode == "live" and record.get("synthetic"):
                raise ValueError("Live run cannot use synthetic interview/acceptance evidence")
            if stage.key == "offer_acceptance":
                if record.get("accepted") is not True:
                    raise ValueError("Offer has not been accepted")
                if mode == "live" and record.get("offer_hash") != payload_hash(prior["offer"]):
                    raise ValueError("Acceptance must identify the exact reviewed offer hash")
                record["offer_hash"] = payload_hash(prior["offer"])
                record["candidate_id"] = prior["selection"]["recommended_candidate_id"]
            return dict(record)
        if stage.kind == "onboard":
            if mode != "simulation":
                record = brief.get("onboarding_evidence")
                if (
                    not record
                    or record.get("synthetic")
                    or not all(
                        record.get(k)
                        for k in (
                            "hr_documents",
                            "safety_induction",
                            "role_briefing",
                            "access_verified",
                            "mentor_handover",
                        )
                    )
                ):
                    raise ValueError("Actual completed onboarding evidence is required")
                return copy.deepcopy(record)
            folder = DEV_DATA_DIR / "recruitment" / self.org / root / "onboarding"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            candidate = prior["selection"]["recommended_candidate_id"]
            actions = []
            workspace = folder / "workspace"
            workspace.mkdir(mode=0o700, exist_ok=True)
            induction = workspace / "safety-induction.md"
            induction.write_text(
                "Sandbox MEP induction: isolation; permit-to-work; working at height; "
                "emergency contacts.\nSynthetic participant acknowledgement is a test fixture, "
                "not real attendance.",
                encoding="utf-8",
            )
            probe = workspace / "role-access-probe.txt"
            probe.write_text(candidate, encoding="utf-8")
            if probe.read_text() != candidate:
                raise ValueError("Sandbox workspace read/write verification failed")
            records = {
                "hr_documents": prior["offer"],
                "safety_induction": {
                    "material": (
                        "Isolation, permit-to-work, working-at-height and emergency contact ind"
                        "uction in the synthetic sandbox"
                    ),
                    "acknowledgement": "fixture:day-one-safety-ack",
                },
                "role_briefing": prior["jd"],
                "access_verified": {
                    "account": "sandbox-mep-" + candidate[:12],
                    "scope": "staging MEP training workspace only",
                    "production_access": False,
                    "workspace_read_write_verified": True,
                    "workspace_id": root + ":" + candidate,
                    "account_provisioned": False,
                },
                "mentor_handover": prior["onboarding_plan"],
            }
            for name, record in records.items():
                file = folder / (name + ".json")
                file.write_text(
                    json.dumps(
                        {"synthetic": True, "candidate_id": candidate, "record": record},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                file.chmod(0o600)
                check = json.loads(file.read_text())
                if check["candidate_id"] != candidate:
                    raise ValueError("Onboarding sandbox readback failed")
                actions.append(
                    {
                        "action": name,
                        "status": "completed_in_sandbox",
                        "synthetic": True,
                        "artifact_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        "readback_verified": True,
                    }
                )
            return {
                "candidate_id": candidate,
                "actions": actions,
                "synthetic": True,
                "day_one": "sandbox_complete",
                "future_reviews": {
                    "day_30": "scheduled",
                    "day_60": "scheduled",
                    "day_90": "scheduled",
                },
                "production_access": False,
            }
        if stage.key == "po":
            return {
                "po_id": "DRAFT-" + root,
                "status": "draft_not_issued",
                "lines": copy.deepcopy(prior["comparison"]["recommended_awards"]),
                "quality_review_hash": payload_hash(prior["material_review"]),
                "award_review_hash": payload_hash(prior["award_review"]),
                "synthetic": mode == "simulation",
            }
        if stage.key == "delivery":
            source = copy.deepcopy(brief.get("delivery", {}))
            if mode == "simulation":
                if source.get("synthetic") is not True:
                    raise ValueError("Synthetic warehouse/invoice fixture is required")
                pairs = {(r["supplier_id"], r["material_id"]) for r in prior["po"]["lines"]}
                grn = [
                    r
                    for r in source.get("grn_lines", [])
                    if (r["supplier_id"], r["material_id"]) in pairs
                ]
                invoice = [
                    r
                    for r in source.get("invoice_lines", [])
                    if (r["supplier_id"], r["material_id"]) in pairs
                ]
                if len(grn) != len(pairs) or len(invoice) != len(pairs):
                    raise ValueError("Independent sandbox warehouse/invoice records are missing")
                return {
                    "synthetic": True,
                    "source": source["source"],
                    "grn_id": "TEST-GRN-" + root,
                    "invoice_id": "TEST-INVOICE-" + root,
                    "grn": grn,
                    "invoice": invoice,
                    "external_delivery_claimed": False,
                }
            if source.get("synthetic") or not source.get("grn") or not source.get("invoice"):
                raise ValueError("Actual GRN and invoice evidence required")
            return dict(source)
        if stage.kind == "match":
            total = validate_match(
                prior["po"]["lines"], prior["delivery"]["grn"], prior["delivery"]["invoice"]
            )
            return {
                "matched": True,
                "total_vnd": total,
                "line_count": len(prior["po"]["lines"]),
                "po_hash": payload_hash(prior["po"]),
                "grn_hash": payload_hash(prior["delivery"]["grn"]),
                "invoice_hash": payload_hash(prior["delivery"]["invoice"]),
                "payment_executed": False,
                "synthetic": mode == "simulation",
            }
        if stage.kind == "close":
            required = [
                s.key
                for s in (HIRING if "cv_intake" in prior else WORKFLOWS["procurement"])
                if s.key != "close" and (mode == "simulation" or not s.simulation_only)
            ]
            if set(prior) != set(required):
                raise ValueError("Cannot close workflow with missing stages")
            summary = (
                (
                    "Đã hoàn thành toàn bộ workflow trong môi trường thử; mọi cổng người du"
                    "yệt đều là phê duyệt mô phỏng."
                )
                if mode == "simulation"
                else "Đã hoàn thành workflow với chứng cứ và quyết định phê duyệt thật."
            )
            result = {
                "summary": summary,
                "mode": mode,
                "synthetic": mode == "simulation",
                "completed_stages": [*required, "close"],
                "stage_evidence_hashes": {key: payload_hash(value) for key, value in prior.items()},
                "simulated_reviews": sum(
                    v.get("decision") == "simulated_approved" for v in prior.values()
                ),
                "human_approvals": sum(v.get("human_decision") is True for v in prior.values()),
            }
            if "scoring" in prior:
                result.update(
                    {
                        "candidates": prior["scoring"]["candidates"],
                        "selection": prior["selection"],
                        "offer": prior["offer"],
                        "onboarding": prior["onboarding_setup"],
                        "onboarding_plan": prior["onboarding_plan"],
                        "future_day_30_60_90_completed": False,
                    }
                )
            else:
                result.update(
                    {
                        "selected_materials_and_suppliers": prior["po"]["lines"],
                        "total_vnd": prior["three_way_match"]["total_vnd"],
                        "three_way_match": prior["three_way_match"],
                        "po_status": "draft_not_issued",
                        "payment_executed": False,
                    }
                )
            return result
        raise ValueError("Unknown mechanical stage: " + stage.key)
