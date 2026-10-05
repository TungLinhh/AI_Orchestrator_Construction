"""Save the previous binding before publishing; revert a falsified lesson automatically.

The supported falsifier is explicit and executable: a later run using this version
fails its output contract. Provider and infrastructure failures do not falsify it.
The receipt and binding change share the caller's transaction. Reversion compares
the pinned version, so a late result cannot overwrite a newer publication.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import ConflictError
from ai_orchestrator.domain.review import assess_output, required_keys
from ai_orchestrator.persistence.models import (
    AgentSkillBinding,
    Execution,
    Skill,
    SkillVersion,
    Task,
)

FALSIFIER = (
    "Revert if a later execution using this version fails its declared output contract, "
    "including missing keys, empty output or placeholders."
)


async def prepare_publication(
    session: AsyncSession, org: str, skill: Skill, version: SkillVersion
) -> dict[str, Any] | None:
    """Persist the rollback receipt while the skill row is locked, before it goes live."""
    evidence = dict(version.derived_from or {})
    agent_id = evidence.get("agent_id")
    if not agent_id:
        return None
    await session.execute(
        select(Skill.id).where(Skill.organization_id == org, Skill.id == skill.id).with_for_update()
    )
    await session.refresh(version)
    evidence = dict(version.derived_from or {})
    if evidence.get("reverted_by_task"):
        raise ConflictError("this lesson was falsified and reverted; propose a corrected version")
    if evidence.get("revert_receipt"):
        return dict(evidence["revert_receipt"])
    binding = (
        await session.execute(
            select(AgentSkillBinding)
            .where(
                AgentSkillBinding.organization_id == org,
                AgentSkillBinding.agent_id == str(agent_id),
                AgentSkillBinding.skill_id == skill.id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    receipt = {
        "agent_id": str(agent_id),
        "previous_version_id": str(binding.skill_version_id)
        if binding and binding.skill_version_id
        else None,
        "previous_enabled": bool(binding and binding.is_enabled),
        "previous_constraints": dict(binding.constraints or {}) if binding else {},
        "falsifier_kind": "output_contract",
        "falsifier": FALSIFIER,
    }
    version.derived_from = {**evidence, "revert_receipt": receipt}
    await session.flush()
    return receipt


async def revert_falsified_skills(
    session: AsyncSession, org: str, task: Task, execution_id: str | None
) -> list[str]:
    """Use the execution's actual skill stamps, never whatever is currently latest."""
    if not execution_id:
        return []
    execution = (
        await session.execute(
            select(Execution).where(Execution.organization_id == org, Execution.id == execution_id)
        )
    ).scalar_one_or_none()
    if execution is None or not execution.skill_versions:
        return []
    if task.status == "failed":
        falsified = task.failure_category == "output_contract_unmet"
    elif task.status == "completed" and required_keys(task.expected_output_schema):
        falsified = not assess_output(
            output=task.output, expected_output_schema=task.expected_output_schema
        ).ok
    else:
        falsified = False
    if not falsified:
        return []
    reverted = []
    for skill_id, version_number in sorted(execution.skill_versions.items()):
        await session.execute(
            select(Skill.id)
            .where(Skill.organization_id == org, Skill.id == skill_id)
            .with_for_update()
        )
        version = (
            await session.execute(
                select(SkillVersion).where(
                    SkillVersion.organization_id == org,
                    SkillVersion.skill_id == skill_id,
                    SkillVersion.version == str(version_number),
                )
            )
        ).scalar_one_or_none()
        if version is None:
            continue
        evidence = dict(version.derived_from or {})
        receipt = evidence.get("revert_receipt") or {}
        if not receipt or receipt.get("falsifier_kind") != "output_contract":
            continue
        if evidence.get("reverted_by_task"):
            continue
        binding = (
            await session.execute(
                select(AgentSkillBinding)
                .where(
                    AgentSkillBinding.organization_id == org,
                    AgentSkillBinding.agent_id == str(execution.agent_id),
                    AgentSkillBinding.skill_id == skill_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if binding is None or binding.skill_version_id != version.id:
            # A newer lesson is already live. Preserve that writer's decision.
            continue
        binding.skill_version_id = receipt.get("previous_version_id")
        binding.is_enabled = bool(receipt.get("previous_enabled"))
        binding.constraints = dict(receipt.get("previous_constraints") or {})
        version.is_published = False
        version.derived_from = {**evidence, "reverted_by_task": str(task.id)}
        await AuditService(session, org).record(
            actor=Actor(id="system:learning", kind=ActorType.SYSTEM),
            action="skill.automatically_reverted",
            resource_type="skill_version",
            resource_id=str(version.id),
            task_id=str(task.id),
            execution_id=execution_id,
            context={"falsifier": FALSIFIER, "restored_version_id": binding.skill_version_id},
        )
        reverted.append(str(version.id))
    await session.flush()
    return reverted
