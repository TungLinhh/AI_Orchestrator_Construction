"""Run one real task, with a real model, and print what the agents did.

    python scripts/run_demo_task.py
    python scripts/run_demo_task.py --goal "..." --type coordination

## Why this exists

You asked where the agents were, and the honest answer was: **nine registered, sixty-five
recorded model calls, and not one of them a real model.** Every `model_profiles` row in
the tenant pointed at `provider: "deterministic"` — the scripted fake the test suite uses
— so nothing an agent did in development ever came out of a language model.

`make seed-free-model` fixes the profile. This fixes the other half: a task created over
HTTP sits at `created` forever, because the thing that picks it up is a Temporal worker
and the brokers are not running. So this drives `TaskExecutionService.execute_task`
directly, in-process, which is the same code the worker calls.

## It runs a **coordination** task on purpose

A `coordination` task that does the work itself is **failed by the platform**, on
purpose:

    a coordination task completed without delegating: the agent had 8 agents it could
    have handed work to and did the work itself

24 of the 47 tasks in the seeded history failed exactly that way, and that is the
separation-of-duties rule working rather than a bug. So a demo that expects a
coordination task to succeed without a real model and a real delegation path is a demo
that is guaranteed to look broken. The goal below is phrased to hand work off, and the
script reports what the delegation tree actually is — including when it is empty, which
is a finding and not a failure of the script.

## What it prints

The task's states, the agents that touched it, every delegation with its depth and
status, and the outcome. Everything is read back from the database rather than from the
executor's return value, so what you see is what is stored.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# A free real model, and no brokers. The task is driven in-process, so Temporal and NATS
# being off is the point rather than a limitation.
os.environ.setdefault("AO_MODEL_PROVIDER_DEFAULT", "openrouter")
os.environ.setdefault("AO_TEMPORAL_ENABLED", "false")
os.environ.setdefault("AO_NATS_ENABLED", "false")

from sqlalchemy import text

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.worker_runtime import build_runtime

DEFAULT_GOAL = (
    "Produce a weekly progress report for BÃI TRÀM ESTATES: which work packages are "
    "behind schedule, and which have a plan but no recorded actual. Hand the schedule "
    "analysis to a department rather than doing it yourself."
)


async def run(*, goal: str, task_type: str, agent_name: str, budget: float) -> int:

    admin = Database.from_settings(use_admin_role=True)
    async with admin.engine.connect() as conn:
        org = (
            await conn.execute(text("SELECT id FROM organizations ORDER BY created_at, id LIMIT 1"))
        ).scalar()
    if org is None:
        print("no organization; run `make seed` first.", file=sys.stderr)
        return 1

    runtime = build_runtime()
    database = Database.from_settings()
    try:
        async with database.tenant_session(org) as session:
            agent = (
                (
                    await session.execute(
                        text(
                            "SELECT id, name, model_profile FROM agents"
                            " WHERE organization_id = CAST(:o AS varchar(40))"
                            "   AND (name = :n OR name = 'Executive Agent') LIMIT 1"
                        ),
                        {"o": org, "n": agent_name},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if agent is None:
                print(f"no agent named {agent_name!r}", file=sys.stderr)
                return 1

            tasks = TaskRepository(session, org)
            # Reuse an equivalent task that is still open, rather than colliding with it.
            #
            # `TaskRepository.create` refuses an equivalent *active* task by fingerprint,
            # which is right — two agents doing the same work at once is waste. But a task
            # created over HTTP sits at `created` forever when no worker is running, and
            # it then blocks the demo of the very thing it was created to demonstrate.
            # So: adopt it, run it, and say which one was adopted.
            task_id = await _open_equivalent(session, org, goal, task_type)
            if task_id is not None:
                print(f"  reusing    {task_id} — an identical task is already open and no")
                print("             worker is running, so it has been sitting at `created`.")
            else:
                created = await tasks.create(
                    title=goal[:60],
                    goal=goal,
                    task_type=task_type,
                    requester_type="human",
                )
                task_id = str(created.id)
            await tasks.assign(task_id, agent["id"])
            # No commit here. `tenant_session` wraps `session.begin()`, so a commit
            # inside the block ends the transaction and the next statement raises
            # "Can't operate on closed transaction" -- F102, third appearance. The
            # context manager commits on the way out, and nothing after this point
            # needs to be visible to a second connection.

            print(f"  task      {task_id}  ({task_type})")
            print(f"  agent     {agent['name']}  profile={agent['model_profile']}")
            print("  running…\n")

            service = TaskExecutionService(session, org, runtime=runtime)
            outcome = await service.execute_task(task_id, agent_id=agent["id"])

            await _report(session, org, task_id, outcome, budget)
    finally:
        await database.engine.dispose()
        await admin.engine.dispose()
    return 0


async def _open_equivalent(session: Any, org: str, goal: str, task_type: str) -> str | None:
    """An open task with the same goal, if there is one. `None` to create a new one."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM tasks WHERE organization_id = CAST(:o AS varchar(40))"
                "   AND goal = :g AND task_type = :t"
                "   AND status IN ('created', 'pending', 'assigned', 'running')"
                " ORDER BY created_at LIMIT 1"
            ),
            {"o": org, "g": goal, "t": task_type},
        )
    ).first()
    return str(row[0]) if row else None


async def _report(session: Any, org: str, task_id: str, outcome: Any, budget: float) -> None:
    """Read everything back from the database, not from the return value."""
    task = (
        (
            await session.execute(
                text(
                    "SELECT status, failure_category, "
                    "  left(coalesce(last_error,''),200) AS last_error"
                    " FROM tasks WHERE organization_id = CAST(:o AS varchar(40))"
                    " AND id = CAST(:i AS varchar(40))"
                ),
                {"o": org, "i": task_id},
            )
        )
        .mappings()
        .one()
    )

    print("  --- what happened ---")
    print(f"  status     {task['status']}")
    if task["failure_category"]:
        print(f"  category   {task['failure_category']}")
    if task["last_error"]:
        print(f"  reason     {task['last_error']}")

    hops = (
        (
            await session.execute(
                text(
                    "SELECT d.depth, d.status, d.objective, a.name AS target"
                    " FROM delegations d LEFT JOIN agents a ON a.id = d.target_agent_id"
                    " WHERE d.organization_id = CAST(:o AS varchar(40))"
                    "   AND (d.parent_task_id = CAST(:i AS varchar(40))"
                    "        OR d.child_task_id = CAST(:i AS varchar(40)))"
                    " ORDER BY d.depth, d.created_at"
                ),
                {"o": org, "i": task_id},
            )
        )
        .mappings()
        .all()
    )
    if hops:
        print(f"\n  --- delegation tree ({len(hops)} hop(s)) ---")
        for h in hops:
            print(f"    {'  ' * (h['depth'] or 0)}└ {h['target'] or '?'}  [{h['status']}]")
            print(f"    {'  ' * (h['depth'] or 0)}  {str(h['objective'])[:88]}")
    else:
        print("\n  --- delegation tree ---")
        print("    empty. A coordination task that does the work itself is *failed* on")
        print("    purpose, so an empty tree on a coordination task is a finding rather")
        print("    than a crash: the agent chose to answer rather than hand off.")

    used = (
        await session.execute(
            text(
                "SELECT model_used, provider, count(*), sum(coalesce(cost_usd,0))"
                " FROM model_usage WHERE organization_id = CAST(:o AS varchar(40))"
                "   AND task_id = CAST(:i AS varchar(40)) GROUP BY 1,2"
            ),
            {"o": org, "i": task_id},
        )
    ).all()
    if used:
        print("\n  --- model calls ---")
        for model, provider, n, cost in used:
            print(f"    {n} x {provider}/{model}  ${float(cost or 0):.4f}")
    else:
        print("\n  --- model calls ---")
        print("    none recorded for this task.")

    budget_note = f" (budget ${budget:.2f})" if budget else ""
    print(f"\n  outcome: {getattr(outcome, 'summary', None) or 'no summary returned'}{budget_note}")
    print("\n  Open the console:  make page  ->  #/console")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument(
        "--type",
        default="coordination",
        dest="task_type",
        choices=("coordination", "analysis", "research"),
    )
    parser.add_argument("--agent", default="Executive Agent", dest="agent_name")
    parser.add_argument("--budget", type=float, default=0.0)
    args = parser.parse_args(argv)
    try:
        return asyncio.run(
            run(
                goal=args.goal,
                task_type=args.task_type,
                agent_name=args.agent_name,
                budget=args.budget,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
