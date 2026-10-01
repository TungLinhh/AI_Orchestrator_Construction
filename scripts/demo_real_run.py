"""Does the organisation organise itself? Run it and see.

Takes a goal from a human, gives it to the executive agent, lets the *real*
model decide, and prints what actually happened: which delegations were proposed,
which the platform accepted, and what the departments did with their share.

The point is that every line is measured rather than assumed. The previous
version of this script handed the task to a scripted runtime and reported
"0 delegations", which was true and was the whole problem.

    uv run python scripts/demo_real_run.py --goal "..." --model dots

**What this script does not prove.** It calls `TaskExecutionService.execute_task`
directly, in-process. That is the *execution* path, and it is a real one: the real
model, the real gates, the real delegation, the real child tasks. It is **not** the
*orchestration* path — no Temporal workflow, no retry policy, no durable timer.

For a long time this script was the project's evidence that the platform orchestrates,
and what it was actually evidence of is that the platform *executes*. The difference
stayed invisible because the worker had never once started (F67), so there was nothing
to be tested against.

To exercise the durable path, start the worker and use the API:

    make dev-worker
    POST /api/v1/tasks {"task_type": "coordination", "start_workflow": true}

and watch it on `GET /api/v1/ui`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.agent_runtime import PydanticAIRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.procedure import describe
from ai_orchestrator.models.gateway import ModelGateway
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import build_providers_from_settings
from ai_orchestrator.persistence.models import (
    Agent,
    Delegation,
    ModelUsage,
    Organization,
    Task,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import configure_logging

DEFAULT_GOAL = (
    "Lên đơn mua hàng cho phòng Marketing: 5 chiếc laptop và 10kg giấy A4. "
    "Cần trình duyệt của ban điều hành trước khi đặt hàng."
)


def _gateway() -> ModelGateway:
    gw = ModelGateway()
    for provider in build_providers_from_settings(get_settings()).values():
        gw.register_provider(provider)
    for profile in default_profiles().values():
        gw.register_profile(profile)
    return gw


async def run(goal: str, *, profile: str, depth: int) -> int:
    settings = get_settings()
    configure_logging(settings)
    db = Database.from_settings()
    slug = settings.seed_organization_slug

    async with db.session() as lookup:
        org = (
            await lookup.execute(select(Organization).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if org is None:
            print(f"no organisation with slug {slug!r}; run `make seed --reset`")
            await db.dispose()
            return 1
        org_id = str(org.id)
        org_name = str(org.name)

    print(f"organisation : {org_name}")
    print(f"model profile: {profile}")
    print(f"goal         : {goal}\n")

    # The runtime under test is the real one: PydanticAI's agent loop with our
    # gateway underneath, so the privacy filter, the budget pre-flight and the
    # recorded fallback all apply to every call the model makes.
    runtime = PydanticAIRuntime()

    async with db.tenant_session(org_id) as session:
        agents = {str(a.name): str(a.id) for a in (await session.execute(select(Agent))).scalars()}
        print(f"agents ({len(agents)}): {', '.join(sorted(agents))}\n")

        tasks = TaskRepository(session, org_id)
        service = TaskExecutionService(session, org_id, runtime=runtime)

        # A unique goal per run. The platform refuses a task whose intent hash
        # matches a live one, which is the deduplication guarantee working — and
        # it also means a demo run twice in a row blocks on the second attempt,
        # which reads as a hang rather than as a correct refusal.
        run_goal = f"{goal}\n\n[run {uuid.uuid4().hex[:8]}]"
        top = await tasks.create(
            title="Executive goal",
            goal=run_goal,
            # `coordination`, not `analysis`: this is a request to decompose, and
            # the type is what makes the platform hold the Executive to actually
            # delegating instead of quietly doing the work itself.
            task_type="coordination",
            requester_type="human",
        )
        await tasks.assign(top.id, agents["Executive Agent"])

        outcome = await service.execute_task(top.id, agent_id=agents["Executive Agent"])
        print("--- the executive's run --------------------------------------")
        print(f"status     : {outcome.status.value}")
        print(f"summary    : {outcome.summary[:160]!r}")
        proposals = [(p.kind, p.target_agent_id or p.tool_name) for p in outcome.proposed_actions]
        print(f"proposals  : {proposals or 'none'}")

        # Which model actually answered. The `primary` profile lists a
        # deterministic provider as its second candidate, so a real-model timeout
        # quietly swaps in a canned one — and every line above would still look
        # like a result. This is the difference between "the model delegated" and
        # "something answered, and here is what".
        served = (
            await session.execute(
                select(ModelUsage.model_used, ModelUsage.provider).where(
                    ModelUsage.organization_id == org_id
                )
            )
        ).all()
        models = sorted({f"{provider}/{model}" for model, provider in served})
        print(f"model used : {', '.join(models) or 'nothing was recorded'}")

        # The shape of the work and how often this agent has done it. Printed
        # because the repetition gate is the precondition for the self-improvement
        # loop, and a gate nobody can see is a gate nobody can trust.
        print(
            f"procedure  : {describe(outcome.procedure_fingerprint or '')}"
            f"  (done {outcome.prior_repetitions} time(s) before this run)"
        )

        # Scoped to this run, not the tenant. The tenant count grows with every run
        # ever executed, so `delegations recorded: 3` on the third run reads as
        # "this run delegated three times" — which is how a run that delegated
        # exactly once looked like it had a duplication problem that had already
        # been fixed.
        delegations = (
            (
                await session.execute(
                    select(Delegation)
                    .where(
                        Delegation.organization_id == org_id,
                        Delegation.parent_task_id == top.id,
                    )
                    .order_by(Delegation.created_at)
                )
            )
            .scalars()
            .all()
        )
        print(f"\ndelegations from this goal: {len(delegations)}")
        for d in delegations:
            target = next(
                (n for n, i in agents.items() if i == d.target_agent_id), d.target_agent_id
            )
            print(f"  -> {target:<20} [{d.status}]  {d.objective[:56]!r}")

        # The second half of the question, and it is printed on *both* paths.
        # "did the company delegate?" and "did the company finish?" are different
        # questions, and a demo that only answers the first reads as though a
        # failed run were a successful one.
        open_children = [
            str(t.id)
            for t in await all_child_tasks(session, org_id, top.id)
            if t.status not in {"completed", "failed", "canceled"}
        ]
        print(
            f"\nchildren still open: {len(open_children)}"
            + ("" if open_children else "  (the company finished the goal)")
        )

        if not delegations:
            print(
                "\nNo delegation. The model finished the goal itself, or proposed\n"
                "something the platform refused. Both are visible above."
            )
            await db.dispose()
            return 0

        # Both facts, because together they are the whole story. A run can
        # delegate *and* still fail — the parent is only allowed to complete once
        # its children are done, so a goal that has just been decomposed is
        # legitimately not finished. Reporting only one of the two makes each
        # look like a bug.
        if outcome.status.value != "completed":
            print(
                "\nThe Executive delegated and then did not complete. That is the "
                "state\nmachine working: a parent with open children cannot report "
                "itself\nfinished. The children below are the rest of the work."
            )

        # Run the children, breadth-first. They already exist — the delegation
        # executor creates the child task in the same transaction as the
        # delegation row, so a delegation with no task cannot happen. This loop
        # therefore only *executes* what the organisation already decided.
        frontier = [str(t.id) for t in await all_child_tasks(session, org_id, top.id)]
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            for child_id in frontier:
                child = await tasks.get(child_id)
                if child.status in {"completed", "failed", "canceled"}:
                    continue
                child_outcome = await service.execute_task(child_id, agent_id=child.owner_agent_id)
                target = next((n for n, i in agents.items() if i == child.owner_agent_id), "?")
                print(
                    f"  L{level} {target:<20} {child_outcome.status.value:<12} "
                    f"{child_outcome.summary[:64]!r}"
                )
                next_frontier.extend(
                    str(t.id) for t in await all_child_tasks(session, org_id, child_id)
                )
            frontier = next_frontier
            if not frontier:
                break

    await db.dispose()
    return 0


async def all_child_tasks(session, org_id: str, parent_id: str) -> list:
    """Direct children of a task, oldest first."""
    from sqlalchemy import select as _select

    return list(
        (
            await session.execute(
                _select(Task)
                .where(Task.organization_id == org_id, Task.parent_task_id == parent_id)
                .order_by(Task.created_at)
            )
        )
        .scalars()
        .all()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--profile", default="primary")
    parser.add_argument("--depth", type=int, default=2)
    args = parser.parse_args()
    return asyncio.run(run(args.goal, profile=args.profile, depth=args.depth))


if __name__ == "__main__":
    raise SystemExit(main())
