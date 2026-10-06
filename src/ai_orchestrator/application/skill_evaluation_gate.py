"""Publication checks for feedback lessons, using persisted evaluation evidence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.application.business_workflow import payload_hash
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.persistence.models import AuditLog, ModelUsage, SkillVersion, Task


async def evaluated_feedback(
    session: AsyncSession, org: str, version: SkillVersion, expert_source: str | None
) -> dict[str, Any]:
    receipt = (version.test_results or {}).get("paired_evaluation") or {}
    if not expert_source or len(expert_source.strip()) < 5:
        raise PreconditionError("Feedback lessons require a human expert corpus review source")
    if (
        receipt.get("passed") is not True
        or receipt.get("runtime") != "live"
        or receipt.get("rounds", 0) < 2
        or receipt.get("instructions_hash") != payload_hash(version.instructions)
        or not receipt.get("agent_id")
    ):
        raise PreconditionError(
            "Feedback lesson needs passing repeated live baseline/holdout evaluation"
        )
    recorded = await session.scalar(
        select(AuditLog.id).where(
            AuditLog.organization_id == org,
            AuditLog.action == "evaluation.paired",
            AuditLog.resource_id == version.id,
            AuditLog.context["receipt_hash"].astext == payload_hash(receipt),
        )
    )
    if not recorded:
        raise PreconditionError("Paired evaluation receipt has no matching audit record")
    rows = receipt.get("tasks", [])
    if not rows or {r["split"] for r in rows} != {"train", "holdout"}:
        raise PreconditionError("Both training and independent holdout evidence are required")
    expected_pairs: dict[tuple[int, str], set[str]] = {}
    seen_tasks: set[str] = set()
    for row in rows:
        if row["task_id"] in seen_tasks:
            raise PreconditionError("Evaluation tasks cannot be reused across pairs")
        seen_tasks.add(row["task_id"])
        expected_pairs.setdefault((row["round"], row["case_id"]), set()).add(row["arm"])
        task = await session.scalar(
            select(Task).where(Task.organization_id == org, Task.id == row["task_id"])
        )
        if (
            task is None
            or task.status != "completed"
            or task.owner_agent_id != receipt["agent_id"]
            or task.input.get("evaluation_case") != row["case_id"]
            or task.constraints.get("evaluation_corpus") != receipt["corpus_sha256"]
            or payload_hash(task.output) != row["output_hash"]
        ):
            raise PreconditionError("Evaluation task evidence changed or belongs to another scope")
        expected_candidate = version.id if row["arm"] == "candidate" else None
        if task.input.get("feedback_candidate_version") != expected_candidate:
            raise PreconditionError("Evaluation did not execute the claimed baseline/candidate")
        usage = await session.scalar(
            select(ModelUsage.id).where(
                ModelUsage.organization_id == org,
                ModelUsage.task_id == task.id,
                ModelUsage.provider == "openrouter",
                ModelUsage.model_used.like("%:free"),
                ModelUsage.status == "ok",
            )
        )
        if not usage:
            raise PreconditionError("Every evaluated task requires an actual free model response")
    if any(arms != {"baseline", "candidate"} for arms in expected_pairs.values()):
        raise PreconditionError("Each case requires independent baseline and candidate tasks")
    if {key[0] for key in expected_pairs} != set(range(receipt["rounds"])):
        raise PreconditionError("Missing evaluation rounds")
    return {"passed": True, "paired_evaluation": receipt}
