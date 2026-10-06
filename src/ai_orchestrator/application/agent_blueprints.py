"""Free-model authoring, hash-bound human review and atomic agent setup.

Drafts and instantiated workflows use the existing task/execution/event/audit
ledger. Seed SOPs remain reference data; generated plans are versioned artifacts.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime
from ai_orchestrator.application.business_workflow import payload_hash
from ai_orchestrator.application.model_profiles import build_tenant_gateway
from ai_orchestrator.application.playbook import PLAYBOOK
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.workflow_lifecycle import workflow_run
from ai_orchestrator.application.workflow_schemas import validate_shape
from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.agent_blueprint import AgentBlueprint
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, EventType
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.models.gateway import ModelGateway
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import (
    Agent,
    AgentSkillBinding,
    AgentToolBinding,
    Approval,
    SkillVersion,
    Task,
    Tool,
)
from ai_orchestrator.persistence.process import SopDefinition
from ai_orchestrator.persistence.repositories.organization import (
    AgentCapabilityRepository,
    AgentDefinitionRepository,
    AgentRepository,
    OrgUnitRepository,
)
from ai_orchestrator.persistence.repositories.task import ExecutionRepository, TaskRepository
from ai_orchestrator.persistence.session import Database

SYSTEM = Actor(id="agent-blueprint-controller", kind=ActorType.SYSTEM)
DEPARTMENTS = {
    "hr": "HR",
    "procurement": "PRC",
    "design": "DES",
    "qa": "QA",
    "finance": "FIN",
    "sales": "SAL",
}
PLAYBOOK_DEPARTMENTS = {
    "hr": "HR",
    "procurement": "Procurement",
    "design": "Design",
    "qa": "QA/QC-HSE",
    "finance": "Finance",
    "sales": "Sales",
}
FREE_PROFILE = "agent-blueprint-free"


def configure_free_profile(gateway: ModelGateway, source: str) -> None:
    profile = gateway.profiles.get(source)
    if profile is None:
        raise ValidationError("Unknown model profile")
    candidates = tuple(
        c
        for c in profile.candidates
        if c.provider == "openrouter"
        and c.model.endswith(":free")
        and c.pricing.input_per_mtok == 0
        and c.pricing.output_per_mtok == 0
    )
    if not candidates:
        raise PreconditionError("This profile has no explicitly free real model")
    # No paid or scripted fallback can escape this separate profile.
    gateway.register_profile(
        replace(profile, name=FREE_PROFILE, candidates=candidates, fallback_profile=None)
    )


def blueprint_tool_schema() -> dict[str, Any]:
    """Inline local references and encode named inputs as explicit tool fields.

    The persisted plan uses a keyed map. Tool providers can require properties
    on every object, so the wire representation is a list of key/description pairs.
    """
    schema = AgentBlueprint.model_json_schema()
    definitions = schema.get("$defs", {})

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(v) for v in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(definitions[value["$ref"].rsplit("/", 1)[-1]])
        return {k: inline(v) for k, v in value.items() if k != "$defs"}

    result = inline(schema)
    result["properties"]["required_inputs"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,47}$"},
                "description": {"type": "string", "minLength": 1},
            },
            "required": ["key", "description"],
            "additionalProperties": False,
        },
    }
    return dict(result)


class BlueprintRuntime(WorkflowEvidenceRuntime):
    name = "agent_blueprint"
    output_token_limit = 12000

    def __init__(self, gateway: Any, validator: Any, *, checkpoint: Any = None) -> None:
        super().__init__(gateway, validator)
        self.checkpoint = checkpoint

    async def execute(self, task: Any, context: Any, **kwargs: Any) -> Any:
        if self.checkpoint:
            await self.checkpoint()
        if task.input.get("agent_blueprint_draft"):
            task = task.model_copy(update={"expected_output_schema": blueprint_tool_schema()})
        return await super().execute(
            task, context.model_copy(update={"model_profile": FREE_PROFILE}), **kwargs
        )


class AgentBlueprintService:
    def __init__(
        self, session: AsyncSession, organization_id: str, database: Database | None = None
    ) -> None:
        self.session = session
        self.org = organization_id
        self.database = database
        self.tasks = TaskRepository(session, organization_id)
        self.approvals = ApprovalService(session, organization_id)

    async def _commit(self) -> None:
        if self.database is None:
            raise PreconditionError("A committing tenant session is required for model runs")
        await self.session.commit()
        await self.database.bind_tenant(self.session, self.org)

    async def _audit(
        self,
        action: str,
        task: Task,
        *,
        actor: Actor = SYSTEM,
        approval_id: str | None = None,
        context: Any = None,
    ) -> None:
        await AuditService(self.session, self.org).record(
            actor=actor,
            action=action,
            resource_type="agent_blueprint",
            resource_id=task.id,
            task_id=task.id,
            approval_id=approval_id,
            context=context or {},
        )

    async def draft(self, config: dict[str, Any]) -> Task:
        department = config["department"]
        if department not in DEPARTMENTS:
            raise ValidationError("Select an enabled department; IT development is deferred")
        definition = await AgentDefinitionRepository(self.session, self.org).get(
            config["definition_id"]
        )
        agents = AgentRepository(self.session, self.org)
        owner = (
            await self.session.execute(
                select(Agent)
                .where(
                    Agent.organization_id == self.org,
                    Agent.role_id == definition.role_id,
                    Agent.definition_id == definition.id,
                    Agent.lifecycle_status == "active",
                )
                .order_by(Agent.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if owner is None or owner.definition_id != definition.id:
            raise PreconditionError("The selected role needs an active source agent")
        unit = await OrgUnitRepository(self.session, self.org).get(str(owner.org_unit_id))
        if unit.slug != department:
            raise ValidationError("The definition role does not belong to the selected department")
        if config.get("org_unit_id") and config["org_unit_id"] != owner.org_unit_id:
            raise ValidationError("The new agent must belong to the source department unit")
        config["org_unit_id"] = owner.org_unit_id
        if config.get("parent_agent_id"):
            await agents.get(config["parent_agent_id"])
        else:
            config["parent_agent_id"] = owner.parent_agent_id
        sources = (
            (
                await self.session.execute(
                    select(SopDefinition)
                    .where(
                        SopDefinition.organization_id == self.org,
                        or_(
                            SopDefinition.department == DEPARTMENTS[department],
                            SopDefinition.code.in_(
                                [
                                    sop.code
                                    for sop in PLAYBOOK
                                    if sop.department == PLAYBOOK_DEPARTMENTS[department]
                                ]
                            ),
                        ),
                    )
                    .order_by(SopDefinition.code)
                )
            )
            .scalars()
            .all()
        )
        if not sources:
            raise PreconditionError("This department has no source SOP catalogue")
        config["source_agent_id"] = owner.id
        # Only the source agent's enabled, published skills and read-only tools.
        skill_rows = (
            await self.session.execute(
                select(AgentSkillBinding, SkillVersion)
                .join(SkillVersion, SkillVersion.id == AgentSkillBinding.skill_version_id)
                .where(
                    AgentSkillBinding.organization_id == self.org,
                    SkillVersion.organization_id == self.org,
                    AgentSkillBinding.agent_id == owner.id,
                    AgentSkillBinding.is_enabled.is_(True),
                    SkillVersion.is_published.is_(True),
                )
            )
        ).all()
        tool_rows = (
            await self.session.execute(
                select(AgentToolBinding, Tool)
                .join(Tool, Tool.id == AgentToolBinding.tool_id)
                .where(
                    AgentToolBinding.organization_id == self.org,
                    Tool.organization_id == self.org,
                    AgentToolBinding.agent_id == owner.id,
                    AgentToolBinding.is_enabled.is_(True),
                    Tool.risk_level == "read_only",
                )
            )
        ).all()
        config["skills"] = [{"skill_id": b.skill_id, "version_id": v.id} for b, v in skill_rows]
        config["tools"] = [{"tool_id": t.id, "name": t.name} for _, t in tool_rows]
        playbook = {s.code: s for s in PLAYBOOK}
        config["source_sops"] = [
            {
                "code": s.code,
                "name": s.name_vi,
                **(
                    {
                        "source_steps": [
                            {"ref": s.code + "#" + str(i), "instruction": text}
                            for i, text in enumerate(playbook[s.code].steps, 1)
                        ],
                        "control_point": playbook[s.code].control_point,
                        "required_artifacts": list(playbook[s.code].required),
                        "forbidden_zone": playbook[s.code].forbidden,
                        "dossier_department": playbook[s.code].dossier_department,
                        "mapped_department": playbook[s.code].department,
                        "mapping_note": playbook[s.code].note,
                    }
                    if s.code in playbook
                    else {}
                ),
            }
            for s in sources
        ]
        task = await self.tasks.create(
            title="Agent blueprint: " + config["name"][:150],
            goal="Soạn đầy đủ trách nhiệm và các bước vận hành của phòng ban theo TẤT CẢ SOP "
            "được cung cấp. Phát triển brief của Boss thành kế hoạch cụ thể, chỉnh văn phong "
            "tiếng Việt chuyên nghiệp. Mỗi bước có đầu vào, đầu ra, điều kiện nghiệm thu và "
            "người duyệt. Không giao agent quyền quyết định tuyển dụng, an toàn, chi tiền "
            "hay phát hành ra ngoài. Mỗi source_steps.ref phải xuất hiện trong source_step_refs "
            "của một bước phù hợp; bao phủ mọi bước SOP và điểm kiểm soát, không chỉ nhắc mã SOP. "
            "Tách mỗi sản phẩm, người duyệt và hành động khác nhau thành bước cụ thể. "
            "required_inputs chỉ là tài liệu nguồn bên ngoài, tuyệt đối không yêu cầu JD, rubric, "
            "điểm, shortlist, báo cáo hay checklist mà chính workflow phải tạo. input_keys trỏ "
            "nguồn bên ngoài hoặc output_fields của bước trước, không được trỏ output tương lai. "
            "Sản phẩm bước trước được cung cấp tự động qua prior. "
            "Chỉ soạn dự thảo "
            "và đánh giá bằng chứng nguồn, không tuyên bố đã gửi mail/phỏng vấn/chi tiền "
            "hay cấp quyền. "
            "Thiếu chứng cứ ghi chưa xác minh và yêu cầu người cung cấp. "
            "Bước cuối human_review=true. " + config["mandate"],
            task_type="analysis",
            owner_agent_id=owner.id,
            input={
                "agent_blueprint_draft": True,
                "config": config,
                "source_instructions": definition.system_instructions,
            },
            expected_output_schema=AgentBlueprint.model_json_schema(),
            budget_limit_tokens=96000,
            budget_limit_usd=0.01,
        )
        await self._commit()
        gateway = await build_tenant_gateway(self.org, session=self.session)
        try:
            configure_free_profile(gateway, config["model_profile"])
            codes = {s.code for s in sources}

            def validate(output: dict[str, Any]) -> None:
                inputs = output.get("required_inputs")
                if isinstance(inputs, list):
                    named = {item["key"]: item["description"] for item in inputs}
                    if len(named) != len(inputs):
                        raise ValueError("Required input keys must be unique")
                    output["required_inputs"] = named
                AgentBlueprint.model_validate(output).validate_sources(
                    codes, self.source_steps(config)
                )

            runtime = BlueprintRuntime(gateway, validate, checkpoint=self._commit)
            await TaskExecutionService(
                self.session,
                self.org,
                runtime=runtime,
                auto_approve=False,
                model_usage_checkpoint=self._commit,
            ).execute_task(task.id)
        except Exception:
            if task.status not in {"completed", "failed", "canceled"}:
                await self.tasks.transition(
                    task.id, Transition.FAIL, error="Blueprint model preparation failed"
                )
            await self._commit()
            raise
        finally:
            await gateway.aclose()
        await self.session.refresh(task)
        if task.status == "completed":
            task.output = {"revision": 1, "plan": copy.deepcopy(task.output), "provisioned": None}
            await self._audit("agent.blueprint.drafted", task)
        return task

    async def get(self, task_id: str, *, lock: bool = False) -> Task:
        stmt = select(Task).where(Task.id == task_id, Task.organization_id == self.org)
        if lock:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None or not row.input.get("agent_blueprint_draft"):
            raise ValidationError("Agent blueprint not found in this organization")
        return row

    @staticmethod
    def source_steps(config: dict[str, Any]) -> set[str]:
        return {
            step["ref"] for sop in config["source_sops"] for step in sop.get("source_steps", [])
        }

    def payload(self, draft: Task) -> dict[str, Any]:
        return {
            "draft_id": draft.id,
            "revision": draft.output["revision"],
            "config": copy.deepcopy(draft.input["config"]),
            "plan": copy.deepcopy(draft.output["plan"]),
        }

    async def edit(self, task_id: str, plan: AgentBlueprint, actor: Actor, revision: int) -> Task:
        task = await self.get(task_id, lock=True)
        if task.status != "completed" or task.output.get("provisioned"):
            raise PreconditionError("Only an unprovisioned completed draft may be edited")
        if revision != task.output["revision"]:
            raise PreconditionError("The blueprint revision changed; reload before editing")
        try:
            plan.validate_sources(
                {s["code"] for s in task.input["config"]["source_sops"]},
                self.source_steps(task.input["config"]),
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        # Editing invalidates older pending reviews without inventing a human decision.
        for approval in await self.approvals.pending_for_task(task_id):
            if approval.action_type == "agent.provision":
                approval.status = "expired"
                approval.expires_at = utcnow()
                await self.approvals._announce(approval, EventType.APPROVAL_EXPIRED)
        task.output = {
            "revision": task.output["revision"] + 1,
            "plan": plan.model_dump(),
            "provisioned": None,
        }
        await self._audit(
            "agent.blueprint.edited",
            task,
            actor=actor,
            context={"revision": task.output["revision"]},
        )
        return task

    async def submit(self, task_id: str) -> Approval:
        task = await self.get(task_id, lock=True)
        if task.status != "completed" or task.output.get("provisioned"):
            raise PreconditionError("Complete and review the draft before submitting")
        try:
            AgentBlueprint.model_validate(task.output["plan"]).validate_sources(
                {s["code"] for s in task.input["config"]["source_sops"]},
                self.source_steps(task.input["config"]),
            )
        except ValueError as exc:
            raise ValidationError("Correct the draft before submitting: " + str(exc)) from exc
        payload = self.payload(task)
        for pending in await self.approvals.pending_for_task(task_id):
            if pending.action_type == "agent.provision" and pending.action_payload == payload:
                return pending
        row = await self.approvals.create(
            ApprovalRequest(
                organization_id=self.org,
                action_type="agent.provision",
                action_payload=payload,
                requested_by=SYSTEM.id,
                requested_by_type=ActorType.SYSTEM,
                task_id=task_id,
                ttl_seconds=86400,
                reason="CEO/Boss duyệt bản thiết kế agent và toàn bộ workflow: "
                + task.input["config"]["name"],
            )
        )
        await self._audit(
            "agent.blueprint.submitted",
            task,
            approval_id=row.id,
            context={"revision": task.output["revision"], "hash": payload_hash(payload)},
        )
        return row

    async def provision(self, approval_id: str, actor: Actor) -> dict[str, Any]:
        approval = await self.approvals.get(approval_id)
        if approval.action_type != "agent.provision" or not approval.task_id:
            raise ValidationError("Not an agent provisioning approval")
        if approval.status != "approved":
            raise PreconditionError("Agent setup requires an approved human decision")
        draft = await self.get(approval.task_id, lock=True)
        payload = self.payload(draft)
        await self.approvals.verify_payload(approval_id, payload)
        if draft.output.get("provisioned"):
            return dict(draft.output["provisioned"])
        config = payload["config"]
        plan = AgentBlueprint.model_validate(payload["plan"])
        plan.validate_sources({s["code"] for s in config["source_sops"]}, self.source_steps(config))
        base = await AgentDefinitionRepository(self.session, self.org).get(config["definition_id"])
        # Validate resources again at the moment of setup, not only when drafting.
        for skill in config["skills"]:
            version = (
                await self.session.execute(
                    select(SkillVersion).where(
                        SkillVersion.organization_id == self.org,
                        SkillVersion.id == skill["version_id"],
                        SkillVersion.skill_id == skill["skill_id"],
                        SkillVersion.is_published.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if version is None:
                raise PreconditionError("A reviewed skill version is no longer published")
        for tool in config["tools"]:
            found = (
                await self.session.execute(
                    select(Tool).where(
                        Tool.organization_id == self.org,
                        Tool.id == tool["tool_id"],
                        Tool.risk_level == "read_only",
                    )
                )
            ).scalar_one_or_none()
            if found is None:
                raise PreconditionError("A reviewed tool is no longer read-only")
        definition = await AgentDefinitionRepository(self.session, self.org).create(
            name=config["name"] + " / " + draft.id[-8:],
            role_id=base.role_id,
            system_instructions=base.system_instructions + "\n\n" + plan.system_instructions,
            model_profile=config["model_profile"],
            allowed_task_types=["analysis"],
            behavior_config={
                **base.behavior_config,
                "approved_blueprint": plan.model_dump(),
                "blueprint_draft_id": draft.id,
                "approval_id": approval_id,
            },
            memory_policy=base.memory_policy,
            escalation_policy=base.escalation_policy,
            created_by=str(actor.id),
        )
        agents = AgentRepository(self.session, self.org)
        agent = await agents.create(
            name=config["name"],
            role_id=base.role_id,
            definition_id=definition.id,
            org_unit_id=config["org_unit_id"],
            parent_agent_id=config["parent_agent_id"],
            description=plan.description,
            model_profile=config["model_profile"],
            autonomy_level="l2_parent_review",
            budget_limit_tokens=96000,
            budget_limit_usd=0.01,
            metadata={"blueprint_draft_id": draft.id, "approval_id": approval_id},
        )
        caps = AgentCapabilityRepository(self.session, self.org)
        for skill in config["skills"]:
            await caps.bind_skill(
                agent_id=agent.id,
                skill_id=skill["skill_id"],
                skill_version_id=skill["version_id"],
                granted_by=str(actor.id),
            )
        for tool in config["tools"]:
            await caps.bind_tool(
                agent_id=agent.id,
                tool_id=tool["tool_id"],
                max_risk="read_only",
                granted_by=str(actor.id),
            )
        await agents.transition(agent.id, Transition.ACTIVATE)
        await agents.transition(agent.id, Transition.ACTIVATE)
        root = await self.tasks.create(
            title="Workflow: " + config["name"],
            goal=agent.id + ": " + config["mandate"],
            task_type="analysis",
            owner_agent_id=agent.id,
            input={
                "agent_workflow": True,
                "draft_id": draft.id,
                "plan": plan.model_dump(),
                "inputs": {},
                "step_ids": [],
            },
        )
        previous = None
        rows = []
        for step in plan.steps:
            schema = {
                "type": "object",
                "properties": {k: {"type": "string", "minLength": 1} for k in step.output_fields},
                "required": step.output_fields,
                "additionalProperties": False,
            }
            child = await self.tasks.create(
                title=step.title,
                goal=step.key + ": " + step.instructions,
                task_type="analysis",
                owner_agent_id=agent.id,
                parent_task_id=root.id,
                input={"agent_workflow": True, "stage_key": step.key},
                expected_output_schema=schema,
                budget_limit_tokens=96000,
                budget_limit_usd=0.01,
            )
            if previous:
                await self.tasks.add_dependency(task_id=child.id, depends_on_task_id=previous)
            previous = child.id
            rows.append({"id": child.id, "key": step.key, "review_id": None})
            if step.human_review:
                gate = await self.tasks.create(
                    title="Review: " + step.title,
                    goal="Người duyệt xác nhận sản phẩm của bước " + step.title,
                    task_type="analysis",
                    owner_agent_id=agent.id,
                    parent_task_id=root.id,
                    input={"agent_workflow": True, "stage_key": step.key + "_review"},
                )
                await self.tasks.add_dependency(task_id=gate.id, depends_on_task_id=previous)
                rows[-1]["review_id"] = gate.id
                previous = gate.id
        root.input = {**root.input, "step_ids": rows}
        await self.tasks.transition(root.id, Transition.REQUEST_INPUT)
        result = {
            "agent_id": agent.id,
            "definition_id": definition.id,
            "workflow_id": root.id,
            "task_ids": [r["id"] for r in rows],
            "review_task_ids": [r["review_id"] for r in rows if r["review_id"]],
            "ready": True,
            "waiting_for": "real_inputs",
        }
        draft.output = {**draft.output, "provisioned": result}
        await self._audit(
            "agent.blueprint.provisioned",
            draft,
            actor=actor,
            approval_id=approval_id,
            context=result,
        )
        return result

    async def report(self, task_id: str) -> dict[str, Any]:
        draft = await self.get(task_id)
        approvals = (
            (
                await self.session.execute(
                    select(Approval)
                    .where(
                        Approval.organization_id == self.org,
                        Approval.task_id == task_id,
                        Approval.action_type == "agent.provision",
                    )
                    .order_by(Approval.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return {
            "id": draft.id,
            "status": draft.status,
            "error": draft.last_error,
            "config": draft.input["config"],
            "output": draft.output,
            "approvals": [{"id": a.id, "status": a.status} for a in approvals],
        }

    async def _cancel_remaining(self, root_id: str) -> None:
        rows = (
            (
                await self.session.execute(
                    select(Task).where(
                        Task.organization_id == self.org, Task.parent_task_id == root_id
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            if row.status not in {"completed", "failed", "canceled", "expired"}:
                await self.tasks.transition(
                    row.id, Transition.CANCEL, error="Stopped after workflow failure"
                )

    async def run(self, root_id: str) -> dict[str, Any]:
        if self.database is None:
            raise PreconditionError("A committing tenant session is required for model runs")
        # Publish caller-supplied inputs before the recovery connection reads them.
        await self._commit()
        async with workflow_run(self.database, self.org, root_id, WorkflowKind.AGENT):
            # Refresh only controller checkpoints, not unrelated caller objects.
            await self.session.execute(
                select(Task)
                .where(
                    Task.organization_id == self.org,
                    (Task.id == root_id) | (Task.parent_task_id == root_id),
                )
                .execution_options(populate_existing=True)
            )
            try:
                return await self._run_steps(root_id)
            except BaseException:
                # Release stage row locks before recovery uses a fresh transaction.
                await self.session.rollback()
                await self.database.bind_tenant(self.session, self.org)
                raise

    async def _run_steps(self, root_id: str) -> dict[str, Any]:
        root = await self.tasks.get(root_id)
        if not root.input.get("agent_workflow") or root.parent_task_id:
            raise ValidationError("Not an agent workflow root")
        if root.status in {"completed", "failed", "canceled", "expired"}:
            return {"id": root.id, "status": root.status}
        plan = AgentBlueprint.model_validate(root.input["plan"])
        if root.status == "assigned":
            await self.tasks.transition(root.id, Transition.BEGIN_WORK)
        elif root.status == "blocked":
            await self.tasks.transition(root.id, Transition.UNBLOCK)
        prior: dict[str, Any] = {}
        for step, ids in zip(plan.steps, root.input["step_ids"], strict=True):
            await self.session.refresh(root)
            if root.status == "canceled":
                return {"id": root.id, "status": root.status}
            child = await self.tasks.get(ids["id"])
            if child.status not in {"completed", "assigned", "blocked"}:
                raise PreconditionError(
                    "A workflow step failed or was interrupted; inspect its log"
                )
            if child.status != "completed":
                missing = [
                    key
                    for key in step.input_keys
                    if key in plan.required_inputs and not root.input["inputs"].get(key)
                ]
                if missing:
                    if root.status != "waiting_for_input":
                        await self.tasks.transition(root.id, Transition.REQUEST_INPUT)
                    await self._commit()
                    return {
                        "id": root.id,
                        "status": root.status,
                        "waiting_step": step.key,
                        "missing_inputs": missing,
                    }
                if root.status == "waiting_for_input":
                    await self.tasks.transition(root.id, Transition.PROVIDE_INPUT)
                child.input = {
                    **child.input,
                    "inputs": {
                        key: root.input["inputs"][key]
                        if key in root.input["inputs"]
                        else next(
                            artifact[key]
                            for artifact in reversed(list(prior.values()))
                            if key in artifact
                        )
                        for key in step.input_keys
                    },
                    "operator_feedback": root.input.get("feedback_context", {}),
                    "prior": copy.deepcopy(prior),
                    "acceptance_criteria": step.acceptance_criteria,
                    "source_sop_codes": step.source_sop_codes,
                }
                gateway = await build_tenant_gateway(self.org, session=self.session)
                try:
                    agent = await AgentRepository(self.session, self.org).get(
                        str(root.owner_agent_id)
                    )
                    configure_free_profile(gateway, agent.model_profile)
                    schema = child.expected_output_schema or {}

                    def validate_stage(
                        out: dict[str, Any], schema: dict[str, Any] = schema
                    ) -> None:
                        validate_shape(out, schema)
                        if any(not value.strip() for value in out.values()):
                            raise ValueError(
                                "Every output field must contain a substantive artifact"
                            )

                    runtime = BlueprintRuntime(gateway, validate_stage, checkpoint=self._commit)
                    outcome = await TaskExecutionService(
                        self.session,
                        self.org,
                        runtime=runtime,
                        auto_approve=False,
                        model_usage_checkpoint=self._commit,
                    ).execute_task(
                        child.id,
                        attempt=1
                        + len(
                            await ExecutionRepository(self.session, self.org).list_for_task(
                                child.id
                            )
                        ),
                    )
                finally:
                    await gateway.aclose()
                await self.session.refresh(root)
                if root.status == "canceled":
                    return {"id": root.id, "status": root.status}
                if outcome.status.value != "completed":
                    await self.tasks.transition(
                        root.id,
                        Transition.FAIL,
                        error="Agent workflow step did not complete: " + step.key,
                    )
                    await self._cancel_remaining(root.id)
                    await self._commit()
                    return {"id": root.id, "status": root.status, "failed_step": step.key}
                await self.session.refresh(child)
            prior[step.key] = copy.deepcopy(child.output)
            if ids["review_id"]:
                gate = await self.tasks.get(ids["review_id"])
                snapshot = {
                    "root_id": root.id,
                    "step_id": child.id,
                    "output": copy.deepcopy(child.output),
                }
                reviews = (
                    (
                        await self.session.execute(
                            select(Approval)
                            .where(
                                Approval.organization_id == self.org,
                                Approval.task_id == gate.id,
                                Approval.action_type == "agent.workflow.review",
                            )
                            .order_by(Approval.created_at.desc())
                            .limit(1)
                        )
                    )
                    .scalars()
                    .all()
                )
                if (
                    reviews
                    and reviews[0].status == "pending"
                    and reviews[0].expires_at
                    and reviews[0].expires_at < utcnow()
                ):
                    reviews[0].status = "expired"
                    await self.approvals._announce(reviews[0], EventType.APPROVAL_EXPIRED)
                if gate.status == "completed":
                    if not reviews or reviews[0].status != "approved":
                        raise PreconditionError("A completed review has no human approval")
                    await self.approvals.verify_payload(reviews[0].id, snapshot)
                if gate.status != "completed":
                    if not reviews:
                        await self.approvals.create(
                            ApprovalRequest(
                                organization_id=self.org,
                                action_type="agent.workflow.review",
                                action_payload=snapshot,
                                requested_by=str(child.owner_agent_id),
                                task_id=gate.id,
                                ttl_seconds=86400,
                                reason="Duyệt sản phẩm: " + step.title,
                            )
                        )
                        await self.tasks.transition(gate.id, Transition.REQUEST_APPROVAL)
                    elif reviews[0].status == "approved":
                        await self.approvals.verify_payload(reviews[0].id, snapshot)
                        await self.tasks.transition(gate.id, Transition.APPROVAL_GRANTED)
                        gate.output = {
                            "approval_id": reviews[0].id,
                            "reviewed_output_hash": payload_hash(child.output),
                        }
                        await self.tasks.transition(gate.id, Transition.COMPLETE)
                        await self._audit(
                            "agent.workflow.reviewed", gate, approval_id=reviews[0].id
                        )
                    elif reviews[0].status in {"rejected", "expired"}:
                        await self.tasks.transition(
                            gate.id,
                            Transition.APPROVAL_REJECTED,
                            error="Human review rejected or expired",
                        )
                        await self.tasks.transition(
                            root.id,
                            Transition.FAIL,
                            error="Human review rejected or expired: " + step.key,
                        )
                        await self._cancel_remaining(root.id)
                        await self._commit()
                        return {
                            "id": root.id,
                            "status": root.status,
                            "review_status": reviews[0].status,
                        }
                    elif reviews[0].status == "needs_information":
                        return {
                            "id": root.id,
                            "status": "waiting_for_review",
                            "review_status": "needs_information",
                        }
                    if gate.status != "completed":
                        if root.status == "running":
                            await self.tasks.transition(root.id, Transition.BLOCK)
                        await self._commit()
                        return {
                            "id": root.id,
                            "status": root.status,
                            "approval_id": (await self.approvals.pending_for_task(gate.id))[0].id,
                        }
            await self._commit()
        await self.session.refresh(root)
        if root.status == "canceled":
            return {"id": root.id, "status": root.status}
        root.output = {"stages": prior, "all_required_reviews": "approved"}
        await self.tasks.transition(root.id, Transition.COMPLETE)
        await self._audit("agent.workflow.completed", root)
        await self._commit()
        return {"id": root.id, "status": root.status}
