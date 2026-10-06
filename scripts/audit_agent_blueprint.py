"""Read-only audit of approved agent setup; this does not certify artifact quality."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from ai_orchestrator.application.agent_blueprints import AgentBlueprintService
from ai_orchestrator.persistence.models import (
    Agent,
    AgentToolBinding,
    Approval,
    Execution,
    ModelUsage,
    Task,
    TaskDependency,
    Tool,
)
from ai_orchestrator.persistence.session import Database


async def audit(org: str, draft_id: str, output: Path) -> None:
    database = Database.from_settings()
    checks: list[str] = []

    def require(ok: bool, label: str) -> None:
        if not ok:
            raise ValueError("Setup audit refused: " + label)
        checks.append(label)

    try:
        async with database.tenant_session(org) as session:
            service = AgentBlueprintService(session, org)
            draft = await service.get(draft_id)
            result = draft.output.get("provisioned")
            require(bool(result), "draft actually provisioned")
            root = await service.tasks.get(result["workflow_id"])
            agent = await session.get(Agent, result["agent_id"])
            require(
                agent is not None and agent.autonomy_level == "l2_parent_review",
                "agent retains human-review autonomy",
            )
            review = await session.get(Approval, agent.metadata_["approval_id"])
            require(
                review is not None and review.status == "approved" and bool(review.decided_by),
                "recorded operator decision",
            )
            await service.approvals.verify_payload(review.id, service.payload(draft))
            checks.append("current revision matches approved payload hash")
            require(root.input["plan"] == draft.output["plan"], "workflow uses reviewed plan")
            rows = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.organization_id == org, Task.parent_task_id == root.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            children = {row.id: row for row in rows}
            ordered: list[str] = []
            for step, ids in zip(root.input["plan"]["steps"], root.input["step_ids"], strict=True):
                child = children[ids["id"]]
                require(
                    child.owner_agent_id == agent.id and child.input["stage_key"] == step["key"],
                    step["key"] + ": assigned stage identity",
                )
                require(
                    child.goal == step["key"] + ": " + step["instructions"],
                    step["key"] + ": reviewed instructions",
                )
                require(
                    child.expected_output_schema["required"] == step["output_fields"],
                    step["key"] + ": reviewed output contract",
                )
                ordered.append(child.id)
                require(
                    bool(ids["review_id"]) == step["human_review"],
                    step["key"] + ": reviewed human gate",
                )
                if ids["review_id"]:
                    gate = children[ids["review_id"]]
                    require(
                        gate.owner_agent_id == agent.id and gate.input["agent_workflow"],
                        step["key"] + ": owned review task",
                    )
                    ordered.append(gate.id)
            require(set(ordered) == set(children), "no missing or extra workflow tasks")
            edges = (
                (
                    await session.execute(
                        select(TaskDependency).where(
                            TaskDependency.organization_id == org,
                            TaskDependency.task_id.in_(ordered),
                        )
                    )
                )
                .scalars()
                .all()
            )
            expected_edges = {(ordered[i], ordered[i - 1]) for i in range(1, len(ordered))}
            require(
                {(e.task_id, e.depends_on_task_id) for e in edges} == expected_edges
                and len(edges) == len(expected_edges),
                "every review gates the next stage",
            )
            bindings = (
                await session.execute(
                    select(AgentToolBinding, Tool)
                    .join(Tool, Tool.id == AgentToolBinding.tool_id)
                    .where(
                        AgentToolBinding.organization_id == org,
                        Tool.organization_id == org,
                        AgentToolBinding.agent_id == agent.id,
                    )
                )
            ).all()
            require(
                all(b.max_risk == "read_only" and t.risk_level == "read_only" for b, t in bindings),
                "tools remain read-only",
            )
            require(
                {b.tool_id for b, _ in bindings}
                == {t["tool_id"] for t in draft.input["config"]["tools"]},
                "reviewed tool set",
            )
            calls = (
                (
                    await session.execute(
                        select(ModelUsage).where(
                            ModelUsage.organization_id == org, ModelUsage.task_id == draft_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            require(any(c.status == "ok" for c in calls), "real successful draft model response")
            require(
                all(
                    c.provider == "openrouter"
                    and c.model_used.endswith(":free")
                    and c.cost_usd == 0
                    for c in calls
                ),
                "free real models only",
            )
            executions = (
                (
                    await session.execute(
                        select(Execution).where(
                            Execution.organization_id == org, Execution.task_id.in_(ordered)
                        )
                    )
                )
                .scalars()
                .all()
            )
            report = {
                "draft_id": draft_id,
                "root_id": root.id,
                "scope": "agent_setup_only",
                "passed": True,
                "checks": checks,
                "workflow_status": root.status,
                "stage_count": len(root.input["step_ids"]),
                "review_count": sum(bool(s["review_id"]) for s in root.input["step_ids"]),
                "execution_count": len(executions),
                "completed_stages": sum(
                    t.status == "completed"
                    for t in rows
                    if t.input.get("stage_key") in {s["key"] for s in root.input["plan"]["steps"]}
                ),
                "artifact_quality_certified": False,
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text, json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"PASS setup only: {len(checks)} checks; workflow {report['workflow_status']}; "
            f"{report['completed_stages']} completed stages"
        )
    finally:
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--draft", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(audit(args.org, args.draft, args.output))
