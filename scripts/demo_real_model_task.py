"""One task, one real model, and what it actually did.

The suite cannot answer this: `tests/conftest.py` pins the provider to `fake`
for determinism and so the suite cannot spend money or reach a shared broker. So
"the hierarchy works with a real model" is not a claim the suite can make, and
this script makes it instead.

Prints, in the order it happened:
  * which model answered, and how many calls it took;
  * what it proposed — tool calls, delegations, consultations;
  * the final status, and the refusal if it was refused.

Refuses to run if there is open work in the tenant, for the same reason the
hierarchy demo does: a demonstration on top of existing work answers a different
question than the one it was written to answer.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select, text

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import (
    Agent,
    AuditLog,
    Delegation,
    ModelUsage,
    Organization,
    Task,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database

GOAL = (
    "You have no knowledge of this organisation. Use internal_database_query to "
    "look up tasks whose status is not completed or canceled, then report how "
    "many are open and name any whose deadline is in the past. Do not answer "
    "from memory -- query first."
)


async def main(org_id: str, model_profile: str) -> int:
    settings = get_settings()
    if not settings.openrouter_api_key.get_secret_value():
        print("  No OPENROUTER_API_KEY: set it in .secrets/runtime.env.")
        return 1

    db = Database.from_settings()
    async with db.tenant_session(org_id) as session:
        # A model call is a *network wait* inside a database transaction, and
        # `statement_timeout` (30s on this cluster) fires on the wait rather than
        # on a slow statement. The first run of this script died with
        # `canceling statement due to statement timeout` on the INSERT that
        # created the task -- not on anything expensive, just on the transaction
        # being open while a free model thought for 40 seconds.
        #
        # Raised for this session only, and restored after. The alternative --
        # committing the task before the run -- would change what the
        # demonstration shows, because the interesting part is the run and its
        # writes inside one transaction.
        await session.execute(text("SET LOCAL statement_timeout = '180s'"))

        org = (
            await session.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one()

        open_work = list(
            (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == org_id,
                        Task.status.notin_(["canceled", "failed", "expired"]),
                    )
                )
            )
            .scalars()
            .all()
        )
        if open_work:
            print(f"  {len(open_work)} open task(s) already exist here.")
            print("  Run on a clean tenant, or clear first (make clear-demo).")
            return 1

        finance = (
            await session.execute(
                select(Agent).where(Agent.organization_id == org_id, Agent.name == "Finance Agent")
            )
        ).scalar_one()
        # The profile decides which model answers. Printed, because "the real
        # model ran" is a claim a reader cannot check from a status.
        finance.model_profile = model_profile
        await session.flush()

        tasks = TaskRepository(session, org_id)
        root = await tasks.create(
            title="month-end finance summary",
            goal=GOAL,
            task_type="analysis",
            requester_type="human",
        )
        await tasks.assign(root.id, finance.id)

        print(f"\n{org.name}")
        print(f"Agent:  {finance.name}  (profile: {model_profile})")
        print(f"Goal:   {GOAL}\n")
        print("Running against the real provider. This costs a real model call.\n")

        # `PydanticAIRuntime` takes no constructor arguments: everything a run
        # needs is already in the `AgentContext` the control plane assembles.
        # Constructing it "properly" with a provider and an organisation is
        # guessing at an interface this adapter does not have.
        from ai_orchestrator.agent_runtime import PydanticAIRuntime

        runtime = PydanticAIRuntime()
        service = TaskExecutionService(
            session=session,
            organization_id=org_id,
            runtime=runtime,
            run_mode=RunMode.LIVE,
            auto_approve=get_settings().approval_auto_approve,
        )
        outcome = await service.execute_task(root.id)
        await session.flush()

        print("─" * 78)
        print(f"status   : {outcome.status}")
        print(f"summary  : {(outcome.summary or '')[:150]}")
        print(f"cost     : {outcome.cost_usd:.6f} USD   tokens: {outcome.tokens}")
        if outcome.blocked_reason:
            print(f"blocked  : {outcome.blocked_reason[:150]}")
        if outcome.failure_category:
            print(f"category : {outcome.failure_category}")

        usage = list(
            (
                await session.execute(
                    select(ModelUsage).where(
                        ModelUsage.organization_id == org_id, ModelUsage.task_id == str(root.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        print(f"\nmodel calls: {len(usage)}")
        for u in usage:
            print(f"  {u.model_used}  in={u.input_tokens} out={u.output_tokens}")

        tools = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == org_id,
                        AuditLog.task_id == str(root.id),
                        AuditLog.resource_type == "tool",
                    )
                )
            )
            .scalars()
            .all()
        )
        print(f"\ntool calls: {len(tools)}")
        for t in tools:
            print(f"  {t.resource_id}  outcome={t.outcome}  {t.policy_reason or ''}")

        delegations = list(
            (
                await session.execute(
                    select(Delegation).where(
                        Delegation.organization_id == org_id,
                        Delegation.parent_task_id == str(root.id),
                    )
                )
            )
            .scalars()
            .all()
        )
        print(f"\ndelegations: {len(delegations)}")
        print("─" * 78)
    await db.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--profile", default="primary")
    args = parser.parse_args()
    os.environ.setdefault("AO_MODEL_PROVIDER_DEFAULT", "openrouter")
    raise SystemExit(asyncio.run(main(args.org, args.profile)))
