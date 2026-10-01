"""Run one request down three tiers and print what happened, in order.

Not a test. The question this answers is "does the hierarchy actually work, or
does the suite merely say so?", and the honest way to answer it is to hand a
task to the top of the organisation and read the log.

What it prints, in the order it happened:

  * every task, with its owner and status, at each tier;
  * every delegation, from whom to whom;
  * every approval request, and whether anyone was actually asked;
  * the reports that came back up from below.

The runtime is the scripted one, so the shape is exercised without a bill. The
fan-out, the hierarchy limits, the approval and the reports are all the real
code paths -- substituting the real model changes which sentence comes out, not
how many rows appear.

The run mode is LIVE and that is not incidental. The gateway refuses, in
simulation, to execute any tool with an effect outside the platform, and a
delegation is exactly that: it creates a task and gives it an owner. Running
the demonstration in simulation would show a refusal, and would have looked like
the hierarchy being broken rather than a demonstration asking for the wrong
thing.

Refuses to run against a tenant that already has an open chain with the same
intent, because a demonstration that quietly creates a second one leaves two
answers to the same question.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import (
    Agent,
    Approval,
    AuditLog,
    Delegation,
    Organization,
    Task,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database

GOAL = "Draft the interview score sheet and the panel schedule for the QA engineer role"


class _TiersRuntime(ScriptedRuntime):
    """Delegates down, and asks a colleague before deciding.

    Both are optional in a real run, and both are what a manager actually does:
    check with someone before committing, then hand the work to whoever can do
    it. Scripted here so the demonstration costs nothing and always produces the
    same shape to read.
    """

    name = "tiers"

    #: What this tier hands down. It differs per tier on purpose: the
    #: duplicate guard refuses a second task with the same intent while the
    #: first is still open, so a demonstration that passes the same objective
    #: down two tiers is refused at the second one and looks like a broken
    #: hierarchy rather than a guard doing its job.
    objective = "produce the score sheet and the panel schedule"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        # A colleague with no one below them has nobody to hand anything to, and
        # the platform refuses to let them invent one. That refusal is the
        # hierarchy working; the run finishes instead.
        if execute_tool is not None:
            self._acted = True
            if getattr(context, "actor", None) and context.delegate_options:
                # Ask whoever looks least loaded, then delegate to them. The
                # roster is read, not guessed: this is the whole point of
                # putting the workload in the prompt.
                colleague = min(
                    context.delegate_options, key=lambda o: (o.active_tasks, o.agent_name)
                )
                await execute_tool(
                    tool_name="ask_agent",
                    arguments={
                        "agent_name": colleague.agent_name,
                        "question": (
                            "Am I right that the panel and the score sheet are one "
                            "piece of work rather than two?"
                        ),
                    },
                )
                await execute_tool(
                    tool_name="delegate_to_agent",
                    arguments={
                        "agent_name": colleague.agent_name,
                        "objective": self.objective,
                    },
                )
        return await super().execute(task, context, execute_tool=None, **kwargs)


class _ApprovingRuntime(_TiersRuntime):
    """The top tier, which needs a person before it commits.

    Deliberately separate from the tiers that just delegate: a demonstration in
    which every agent happily proceeds shows nothing about the human in the
    loop, and the question the switch exists to answer -- "does the platform
    really stop and ask?" -- is only answered by a run that stops.
    """

    name = "needs_approval"

    async def execute(self, task, context, **kwargs):  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

        return AgentResult(
            status=AgentResultStatus.NEEDS_APPROVAL,
            summary="the panel schedule names external assessors; I need a person to agree",
            execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
            task_id=str(task.task_id),
        )


class _OfficeRuntime(_TiersRuntime):
    """The office tier: hands the work to a department.

    Separate from the executive on purpose. One runtime cannot both run at the
    top and run below it, and a demonstration that shows the same agent twice
    proves the shape of a delegation rather than the shape of a hierarchy.
    """

    name = "office"
    objective = "build the panel schedule from the agreed score sheet"


def _bar() -> None:
    print("─" * 78)


async def main(org_id: str) -> int:
    db = Database.from_settings()
    async with db.tenant_session(org_id) as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one()
        print(f"Organisation: {org.name}")

        # Refuse rather than create a second demonstration of the same question.
        existing = list(
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
        if existing:
            print(f"  {len(existing)} open task(s) already exist in this tenant.")
            print("  Running a demonstration on top would leave two answers to the")
            print("  same question. Clear them first (make clear-demo) or use a new tenant.")
            return 1

        ceo = (
            await session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Executive Agent"
                )
            )
        ).scalar_one()
        tasks = TaskRepository(session, org_id)

        before = await _counts(session, org_id)
        print(f"\nGoal: {GOAL}\n")
        _bar()

        # The office and the department agent, so the demo can place three tiers
        # in the tree rather than asking the seed to grow one.
        back_office = (
            await session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Back Office Agent"
                )
            )
        ).scalar_one()

        root = await tasks.create(
            title="prepare the QA engineer hiring pack",
            goal=GOAL,
            task_type="coordination",
            requester_type="human",
        )
        await tasks.assign(root.id, ceo.id)
        print("1. Task handed to the top of the organisation")
        print(f"   {root.id}  →  {ceo.name}\n")

        service = TaskExecutionService(
            session=session,
            organization_id=org_id,
            runtime=_TiersRuntime(),
            run_mode=RunMode.LIVE,
            # Off by default, and this demonstration reports whichever way it
            # is set rather than pretending the run was unattended.
            auto_approve=get_settings().approval_auto_approve,
        )
        await service.execute_task(root.id)
        await session.flush()

        # Tier two runs for real. The executive only creates the child; nobody
        # executes it, so a demonstration that stopped here would show a queue
        # rather than an organisation working.
        children = list(
            (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == org_id, Task.parent_task_id == str(root.id)
                    )
                )
            )
            .scalars()
            .all()
        )
        # Every open generation runs, not just the first, so a chain that has
        # stalled at tier two is finished rather than reported as finished work.
        frontier = children
        while frontier:
            nxt = []
            for child in frontier:
                if child.status in ("completed", "failed", "canceled", "expired"):
                    continue
                office = TaskExecutionService(
                    session=session,
                    organization_id=org_id,
                    runtime=_OfficeRuntime(),
                    run_mode=RunMode.LIVE,
                    auto_approve=get_settings().approval_auto_approve,
                )
                await office.execute_task(child.id)
                await session.flush()
                fresh = (
                    await session.execute(select(Task).where(Task.id == child.id))
                ).scalar_one()
                nxt.extend(
                    (
                        await session.execute(
                            select(Task).where(
                                Task.organization_id == org_id,
                                Task.parent_task_id == str(child.id),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                del fresh
            frontier = nxt
        await session.flush()

        rows = await _tier_table(session, org_id)
        print("2. What exists now\n")
        print(f"   {'task':<30} {'owner':<18} {'status':<14} tier")
        for task_id, owner, status, tier in rows:
            print(f"   {task_id[:28]:<30} {owner[:16]:<18} {status:<14} {tier}")
        print()

        delegations = list(
            (await session.execute(select(Delegation).where(Delegation.organization_id == org_id)))
            .scalars()
            .all()
        )
        agents_by_id = {
            a.id: a.name
            for a in (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
            .scalars()
            .all()
        }
        print(f"3. Delegations recorded: {len(delegations)}")
        for d in delegations:
            src = agents_by_id.get(d.source_agent_id or "", "?")
            dst = agents_by_id.get(d.target_agent_id or "", "?")
            print(f"   {src} → {dst}  (depth {d.depth}, {d.status})")
            print(f"        objective: {(d.objective or '')[:56]}")
        print()

        approvals = list(
            (await session.execute(select(Approval).where(Approval.organization_id == org_id)))
            .scalars()
            .all()
        )
        print(f"4. Approval requests: {len(approvals)}")
        if not approvals:
            print("   none — this run needed nobody's permission")
        for a in approvals:
            asked_by = "a person" if a.status == "pending" else (a.decided_by or "?")
            print(f"   {a.action_type:<16} {a.status:<10} reason: {a.reason[:44]}")
            print(f"   {'':>16} decided by: {asked_by}")
        print()

        # The same run twice, with the switch in both positions. Printing one
        # run's outcome tells the reader what happened; running it twice tells
        # them what the switch does, which is the thing that has to be believed.
        _bar()
        print("4b. Human in the loop\n")
        for n, switch in enumerate((False, True)):
            # The titles differ on purpose: the duplicate guard refuses a second
            # task with the same intent while the first is open, so two identical
            # tasks would be refused at the second one and the switch would never
            # be exercised. That refusal is correct -- these are two different
            # requests, not the same one twice.
            ask = await tasks.create(
                title=f"release the external assessor panel (run {n + 1})",
                goal=f"release the external assessor panel, attempt {n + 1}",
                task_type="coordination",
                requester_type="human",
            )
            await tasks.assign(ask.id, back_office.id)
            gate = TaskExecutionService(
                session=session,
                organization_id=org_id,
                runtime=_ApprovingRuntime(),
                run_mode=RunMode.LIVE,
                auto_approve=switch,
            )
            await gate.execute_task(ask.id)
            await session.flush()
            rows = await _approvals_for(session, org_id, str(ask.id))
            state = rows[0].status if rows else "no request was made"
            note = (rows[0].decision_note or "")[:44] if rows else ""
            print(f"   auto-approve={switch!s:<5}  request: {state:<10} {note}")
        print()

        after = await _counts(session, org_id)
        print("5. Rows written by the run")
        print(f"   tasks       {before['tasks']:>4} → {after['tasks']:<4}")
        print(f"   delegations {before['delegations']:>4} → {after['delegations']:<4}")
        print(f"   audit rows  {before['audit']:>4} → {after['audit']:<4}")
        print(f"   approvals   {before['approvals']:>4} → {after['approvals']:<4}")
        _bar()
        print("Open the page to watch it, or read the rows directly:")
        print("  SELECT title, status FROM tasks WHERE organization_id =", f"'{org_id}';")
    await db.dispose()
    return 0


async def _approvals_for(session, org_id: str, task_id: str) -> list[Approval]:
    return list(
        (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == org_id, Approval.task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )


async def _counts(session, org_id: str) -> dict[str, int]:
    from sqlalchemy import func

    out = {}
    for key, model in (
        ("tasks", Task),
        ("delegations", Delegation),
        ("approvals", Approval),
        ("audit", AuditLog),
    ):
        out[key] = int(
            (
                await session.execute(
                    select(func.count()).select_from(model).where(model.organization_id == org_id)
                )
            ).scalar_one()
        )
    return out


async def _tier_table(session, org_id: str) -> list[tuple[str, str, str, str]]:
    """Tasks with their owner and how deep they sit.

    Depth is measured by following `parent_task_id`, because a task's tier is a
    fact about where it came from, not a field anyone maintains.
    """
    tasks = list(
        (
            await session.execute(
                select(Task).where(Task.organization_id == org_id).order_by(Task.created_at)
            )
        )
        .scalars()
        .all()
    )
    agents = {
        a.id: a.name
        for a in (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
        .scalars()
        .all()
    }
    parents = {t.id: t.parent_task_id for t in tasks}
    depths: dict[str, int] = {}
    for t in tasks:
        d, cur = 0, t.id
        while parents.get(cur):
            cur = parents[cur]  # type: ignore[assignment]
            d += 1
            if d > 10:
                break
        depths[t.id] = d
    return [
        (t.title, agents.get(t.owner_agent_id or "", "unassigned"), t.status, f"L{d}")
        for d, t in ((depths[t.id], t) for t in tasks)
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="organization id")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.org)))
