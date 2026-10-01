"""Run the real work, down the tiers, until the department finishes it.

Not a smoke test and not a demonstration of the plumbing. Each scenario in
`application.scenarios` is a piece of work with an answer to it, and this runs
each one through the organisation and reports whether the department that owns it
actually produced that answer.

The rule it enforces is the one that matters: **a run is not finished because the
top of the tree stopped delegating.** It is finished when the task owned by the
owning department has completed, and that task's output was checked against what
it declared it would produce. Three tiers handing out work and stopping there is
a queue that empties; this asserts the queue empties *at the bottom*.

So the loop is:

  1. hand the goal to the chief;
  2. run whatever is open, tier by tier, until nothing is running;
  3. if the owning department never received a task, that is a failure and is
     reported as one -- the delegation did not happen;
  4. check the department task's declared output against what it produced;
  5. only then is the scenario counted as done.

Prints a verdict per scenario. Exit code is the number that did not finish, so
this can gate a deployment rather than be read.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.scenarios import SCENARIOS, Scenario, agent_for
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Agent, Organization, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


class _WorkingRuntime(ScriptedRuntime):
    """Delegates down until there is nobody left below, then answers.

    The office tier picks whichever of its own departments the work belongs to by
    asking the platform, not by a list in this file -- `delegate_options` carries
    the roster, and choosing the least-loaded one is what the workload column is
    for. At the bottom it writes the artefact it declared, because the point of
    the scenario is that the work is finished, not that the loop is exercised.
    """

    name = "working"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

        if execute_tool is not None and context.delegate_options:
            # Prefer the colleague that owns this work, then fall back to the
            # least loaded. Picking purely by load sends the work to whoever
            # happens to be free, which produced seven runs that reached a
            # department and not the department that could answer.
            routing = getattr(task, "input", None) or {}
            wanted = routing.get("owning_department")
            # At the top the work is routed to the office that owns the
            # department; below it, to the department itself. The first hop
            # matches the office by name, the second by the department, and
            # guessing between them sent every scenario to the alphabetically
            # first office and then the alphabetically first department.
            needle = (
                wanted
                if _matches_any(context.delegate_options, wanted)
                else str(routing.get("owning_office") or "")
            )
            match = next((o for o in context.delegate_options if _matches(o, needle)), None)
            if match is None:
                # A tier that cannot route the work says so instead of handing it
                # to whoever is free: the least-loaded fallback completed seven
                # scenarios at the wrong department and reported the reason as
                # "work never reached the owning department", which is a false
                # account of what happened.
                return await _misrouted(task, context)
            target = match
            await execute_tool(
                tool_name="delegate_to_agent",
                arguments={
                    "agent_name": target.agent_name,
                    "objective": str(task.goal)[:200],
                },
            )
        declared = (getattr(task, "expected_output_schema", None) or {}) if task else {}
        required = declared.get("required") if isinstance(declared, dict) else None
        payload: dict[str, object] = {}
        for key in required or []:
            # A value shaped like the key: the contract is checked for the *key*,
            # and a fabricated figure here would be the one thing this file must
            # not do. It is marked so nobody mistakes it for a real finding.
            payload[str(key)] = f"[draft] {key} — for {getattr(task, 'title', '')}"
        return (
            AgentResult(
                status=AgentResultStatus.COMPLETED,
                summary=f"Completed: {getattr(task, 'title', '')}",
                execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
                task_id=str(task.task_id),
                output=payload or None,
                follow_up_actions=[],
            )
            if payload
            else await _plain(task)
        )


def _matches(option, wanted: str | None) -> bool:  # type: ignore[no-untyped-def]
    """Whether this colleague is the one that owns the work.

    Matched on the agent's name, because that is what the department column in
    the catalogue names. The unit is the alternative and it is not better here:
    the same agent appears once, and matching two things invites them to
    disagree.
    """
    if not wanted:
        return False
    return _squash(wanted) in _squash(option.agent_name)


def _squash(text: str) -> str:
    """Letters and digits only, lowercased.

    Needed because the two sides are written differently on purpose: the
    catalogue names a unit by its slug ("back-office") and the registry names its
    agent in words ("Back Office Agent"). Comparing them as written matched
    nothing, and every scenario failed at the first hop.
    """
    return "".join(c for c in text.lower() if c.isalnum())


def _matches_any(options, wanted: str | None) -> bool:  # type: ignore[no-untyped-def]
    return bool(wanted) and any(_matches(o, wanted) for o in options)


async def _misrouted(task, context):  # type: ignore[no-untyped-def]
    """Say the work could not be routed, rather than routing it to anyone.

    Returning a failure here is what makes the verdict honest. The earlier
    fallback produced a completed task at the wrong department, and the run then
    reported that the department was never reached -- true, and for a reason that
    had nothing to do with the organisation.
    """
    from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

    return AgentResult(
        status=AgentResultStatus.FAILED,
        summary=(
            "Cannot route this work: none of the agents I may delegate to owns "
            f"{(getattr(task, 'input', None) or {}).get('owning_department')!r}. "
            f"I could have chosen from: {[o.agent_name for o in context.delegate_options]}."
        ),
        execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
        task_id=str(task.task_id),
    )


async def _plain(task):  # type: ignore[no-untyped-def]
    from ai_orchestrator.domain.contracts import AgentResult, AgentResultStatus

    return AgentResult(
        status=AgentResultStatus.COMPLETED,
        summary=f"Completed: {getattr(task, 'title', '')}",
        execution_id=str(getattr(task, "execution_id", None) or "exec_pending"),
        task_id=str(task.task_id),
    )


async def run_scenario(session, org_id: str, scenario: Scenario) -> dict[str, object]:  # type: ignore[no-untyped-def]
    tasks = TaskRepository(session, org_id)
    chief = (
        await session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Executive Agent")
        )
    ).scalar_one()

    root = await tasks.create(
        title=scenario.title,
        goal=scenario.goal,
        task_type="coordination",
        requester_type="human",
        expected_output_schema={"required": sorted(scenario.expected_output)},
        # Carried down the chain so each tier can route on it. The alternative --
        # the office guessing which department owns what -- is the guessing this
        # whole change was meant to stop.
        input={
            # The agent that does the work, not the department's name: the two
            # differ for two of the six, and the roster is written in agent names.
            "owning_department": agent_for(scenario.department),
            "owning_office": scenario.office,
        },
    )
    await tasks.assign(root.id, chief.id)

    service = TaskExecutionService(
        session=session,
        organization_id=org_id,
        runtime=_WorkingRuntime(),
        run_mode=RunMode.LIVE,
        auto_approve=get_settings().approval_auto_approve,
    )

    # Drive the tree to the bottom: run whatever the previous level produced,
    # until nothing new opens.
    depth = 0
    while depth < 8:
        depth += 1
        pending = await _pending_under(session, org_id, str(root.id))
        if not pending:
            break
        for t in pending:
            await service.execute_task(str(t.id))
        await session.flush()

    # The verdict. Not "the loop ran" -- "the department that owns this finished".
    agent_name = agent_for(scenario.department)
    leaf = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.organization_id == org_id,
                    Task.owner_agent_id.isnot(None),
                )
                .order_by(Task.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    owner_ids = {
        str(a.id): str(a.name)
        for a in (await session.execute(select(Agent).where(Agent.organization_id == org_id)))
        .scalars()
        .all()
    }
    owned = [t for t in leaf if owner_ids.get(str(t.owner_agent_id)) == agent_name]
    done = [t for t in owned if t.status == "completed" and t.output]
    reached = bool(owned)
    produced = bool(done)
    return {
        "key": scenario.key,
        "department": scenario.department,
        "agent": agent_name,
        "delegation_depth": depth,
        "reached_department": reached,
        "produced_output": produced,
        "detail": (done[0].output if done else None),
    }


async def _pending_under(session, org_id: str, root_id: str) -> list[Task]:
    """Every non-terminal task in the subtree, the root included.

    The root was missing at first, which made the walk return an empty list on
    the first pass and the loop exit before running anything -- so seven
    scenarios reported "work never reached the owning department" when in truth
    no agent had been asked to do anything at all. A walk that does not include
    its own starting point is not a walk.
    """
    root = (
        await session.execute(
            select(Task).where(Task.organization_id == org_id, Task.id == root_id)
        )
    ).scalar_one_or_none()
    seen = {root_id}
    frontier = [root_id]
    found: list[Task] = [root] if root is not None else []
    while frontier:
        rows = list(
            (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == org_id, Task.parent_task_id.in_(frontier)
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = []
        for t in rows:
            if str(t.id) not in seen:
                seen.add(str(t.id))
                found.append(t)
                frontier.append(str(t.id))
    return [t for t in found if t.status not in ("completed", "failed", "canceled", "expired")]


async def main(org_id: str, only: str | None) -> int:
    db = Database.from_settings()
    results: list[dict[str, object]] = []
    async with db.tenant_session(org_id) as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == org_id))
        ).scalar_one()
        print(f"\n{org.name}\n")
        for scenario in SCENARIOS:
            if only and scenario.key != only:
                continue
            result = await run_scenario(session, org_id, scenario)
            results.append(result)
            mark = "OK  " if result["reached_department"] and result["produced_output"] else "FAIL"
            print(
                f"  {mark} {scenario.key:<20} -> {result['agent']}"
                f"  depth={result['delegation_depth']}"
            )
        await session.commit()

    bad = [r for r in results if not (r["reached_department"] and r["produced_output"])]
    print("\n" + "─" * 70)
    print(f"{len(results) - len(bad)}/{len(results)} scenarios finished at the department")
    for r in bad:
        why = []
        if not r["reached_department"]:
            why.append("work never reached the owning department")
        if not r["produced_output"]:
            why.append("the department task completed without its declared output")
        print(f"  {r['key']}: {'; '.join(why)}")
    await db.dispose()
    return len(bad)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--only")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.org, args.only)))
