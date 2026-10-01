"""What actually happens when a task reaches the real organisation?

Not a test — a demonstration, so the answer to "have the departments done any
work yet?" comes from running the platform rather than from reading the suite.

Prints, for one task handed to the Executive Agent:
  * what the runtime proposed;
  * whether any delegation was recorded;
  * how many tasks and audit rows exist afterwards.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.persistence.models import Agent, AuditLog, Delegation, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


async def main() -> int:
    db = Database.from_settings()

    # `organizations` is the one table without row-level security, so an unbound
    # session can read it. Everything else needs a binding — and reading `agents`
    # without one returns zero rows, which is the isolation working rather than
    # an empty company. Demonstrated below, because it surprises people.
    # By slug, not `limit(1)`. An arbitrary organisation is the wrong answer the
    # moment there is more than one — and a demo that silently demonstrates an
    # empty tenant is worse than one that refuses to run.
    from ai_orchestrator.config.settings import get_settings

    slug = get_settings().seed_organization_slug
    async with db.session() as lookup:
        org = (
            await lookup.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        unbound_agents = int(await lookup.scalar(select(func.count()).select_from(Agent)) or 0)
    if org is None:
        print(f"no organisation with slug {slug!r}; run `make seed --reset`")
        await db.dispose()
        return 1
    org_id = str(org.id)

    print(f"organisation   : {org.name}")
    print(f"agents unbound : {unbound_agents}  <- row-level security, not an empty company")

    async with db.tenant_session(org_id) as session:
        agents = {str(a.name): str(a.id) for a in (await session.execute(select(Agent))).scalars()}
        print(f"agents bound   : {len(agents)} -> {', '.join(sorted(agents))}\n")

        tasks = TaskRepository(session, org_id)
        service = TaskExecutionService(session, org_id, runtime=ScriptedRuntime())

        before = {
            "tasks": await session.scalar(
                select(func.count()).select_from(Task).where(Task.organization_id == org_id)
            ),
            "delegations": await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(Delegation.organization_id == org_id)
            ),
        }

        task = await tasks.create(
            title="Board pack",
            goal=(
                "Prepare the Q3 board pack: research the market with Marketing, "
                "assess risk with Risk, and summarise the numbers with Finance."
            ),
            task_type="research",
            requester_type="human",
        )
        await tasks.assign(task.id, agents["Executive Agent"])
        print(f"task         : {task.id}")
        print(f"owner        : {agents['Executive Agent']} (Executive Agent)")
        print("goal         : three departments named in one sentence\n")

        outcome = await service.execute_task(task.id, agent_id=agents["Executive Agent"])

        after = {
            "tasks": await session.scalar(
                select(func.count()).select_from(Task).where(Task.organization_id == org_id)
            ),
            "delegations": await session.scalar(
                select(func.count())
                .select_from(Delegation)
                .where(Delegation.organization_id == org_id)
            ),
        }
        rows = (
            (
                await session.execute(
                    select(AuditLog.action).where(
                        AuditLog.organization_id == org_id, AuditLog.task_id == task.id
                    )
                )
            )
            .scalars()
            .all()
        )

    print("--- what happened -------------------------------------------")
    print(f"status         : {outcome.status.value}")
    print(f"summary        : {outcome.summary[:70]!r}")
    print(f"proposals      : {[p.kind for p in outcome.proposed_actions] or 'none'}")
    print(f"delegations    : {before['delegations']} -> {after['delegations']}")
    print(f"tasks          : {before['tasks']} -> {after['tasks']}")
    print(f"audit for task : {rows or 'none'}")

    delegated = after["delegations"] > before["delegations"]
    print()
    if delegated:
        print("The organisation decomposed the goal by itself.")
    else:
        print("The organisation did NOT decompose the goal.")
        print("The Executive Agent completed the task on its own: no delegation")
        print("was proposed, and none would be acted on if it were.")
    await db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
