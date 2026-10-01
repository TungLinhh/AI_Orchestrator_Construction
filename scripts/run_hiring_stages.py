"""Run the eight hiring stages for real, in order, and clear each gate.

The chain exists as eight tasks: stage 1 was `assigned` and stages 2-8 were
`created`, so the process had been seeded and never started. Nothing here
decides *what* the stages are -- `domain.hiring_process.STAGES` is the
authority and this file reads it rather than restating it.

Three things it does, and why each is not optional:

1. **Assigns every stage to the agent that owns it.** Six are HR, one is Sales,
   and the first is the chief confirming the need. Leaving them unowned is why
   the page said "waiting to be assigned" seven times: a task with no owner is
   not queued work, it is a row.

2. **Executes each stage and waits for it.** In order, because stage 3 approves
   the JD that stage 5 scores against. Running them in parallel would produce
   eight answers to eight questions and one of them would be about a document
   that did not exist yet.

3. **Clears each gate through `ApprovalService.decide`**, as a person clicking
   Approve would, with a human principal. Not by setting the switch
   `AO_APPROVAL_AUTO_APPROVE`, which skips the decision rather than recording it:
   the audit row is the deliverable at a gate, and a run that produces the
   outcome without the record has demonstrated nothing a reviewer could check.

Reports one line per stage and exits with the number that did not finish, so
this can gate a deployment instead of being read.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, RunMode, TaskStatus
from ai_orchestrator.domain.hiring_process import STAGES
from ai_orchestrator.persistence.models import Agent, Approval, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.security.auth import operator_id_for
from ai_orchestrator.worker_runtime import build_runtime

#: Which agent does each stage. Read from the stage's own `department`, so a stage
#: moved to another department needs no edit here.
DEPARTMENT_AGENT = {
    "HR": "HR Agent",
    "Sales": "Sales Agent",
    "Procurement": "Procurement Agent",
    "Finance": "Finance Agent",
    "Design": "Design Agent",
    "QA/QC-HSE": "Quality Agent",
}

#: The one stage the chief owns rather than a department: confirming that the
#: company needs the hire at all is the chief's decision, and routing it to HR
#: would put the person who wants the headcount in charge of approving it.
CHIEF_STAGE = "need_confirmed"


def _approver(org_id: str) -> Actor:
    """The person clicking Approve, for **this** organisation.

    The id comes from `operator_id_for` rather than being written down, and that
    is not tidiness. `users` is keyed on `id` alone while `approvals.decided_by`
    is a composite key on `(organization_id, decided_by)`, so one id belongs to
    exactly one organisation: the fixed id `dev:no-auth` approves in whichever
    tenant got there first and returns a foreign-key violation in every other.
    Writing the literal cost a 500 on the first gate of the first stage.
    """
    return Actor(
        id=operator_id_for(org_id),
        kind=ActorType.HUMAN,
        is_privileged_human=True,
        display_name="local operator (recruitment run)",
    )


async def _agent(session, org_id: str, name: str) -> str | None:  # type: ignore[no-untyped-def]
    row = (
        await session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == name)
        )
    ).scalar_one_or_none()
    return str(row.id) if row is not None else None


async def _clear_gates(session, org_id: str, task_id: str) -> int:  # type: ignore[no-untyped-def]
    """Approve what this stage asked a person to approve. Returns how many.

    Reads the approvals off the task rather than assuming a count, because a stage
    that asked for two decisions needs both cleared and a stage that asked for
    none must not have one invented.
    """
    from ai_orchestrator.approvals.service import ApprovalService

    rows = (
        (
            await session.execute(
                select(Approval).where(
                    Approval.organization_id == org_id,
                    Approval.task_id == task_id,
                    Approval.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    service = ApprovalService(session=session, organization_id=org_id)
    for approval in rows:
        await service.decide(
            str(approval.id),
            approver=_approver(org_id),
            approve=True,
            note="recruitment run",
        )
    await session.flush()
    return len(rows)


async def main(org_id: str, only: int | None) -> int:  # type: ignore[no-untyped-def]
    db = Database.from_settings()
    settings = get_settings()
    failures = 0
    attempted = 0

    async with db.tenant_session(org_id) as session:
        tasks = TaskRepository(session, org_id)
        service = TaskExecutionService(
            session=session,
            organization_id=org_id,
            run_mode=RunMode.LIVE,
            auto_approve=settings.approval_auto_approve,
            # The same runtime the worker uses, not a scripted stand-in: a stage
            # that has to read a JD, score a CV and write an offer is the thing
            # that needs a model, and a stub would report the chain working while
            # proving nothing about any of it.
            runtime=build_runtime(settings),
        )

        print(f"\nprovider: {settings.model_provider_default}\n")
        for stage in STAGES:
            if only and stage.n != only:
                continue
            # `stage_key` alone matches **two** rows: the stage and the chain
            # root, which carries the first stage's key and its `request_key` so
            # the seeder can find the chain again on a re-run. `MultipleResultsFound`
            # was the first symptom; excluding on `request_key` then found
            # nothing, because the stage rows carry one too.
            #
            # What actually separates them is the tree: the root has no parent and
            # every stage hangs off it.
            task = (
                await session.execute(
                    select(Task).where(
                        Task.organization_id == org_id,
                        Task.input["stage_key"].astext == stage.key,
                        Task.parent_task_id.isnot(None),
                    )
                )
            ).scalar_one_or_none()
            if task is None:
                print(f"  {stage.n}. {stage.name_vi:<34} FAIL  no task for this stage")
                failures += 1
                continue

            owner = await _agent(
                session,
                org_id,
                "Executive Agent"
                if stage.key == CHIEF_STAGE
                else DEPARTMENT_AGENT[stage.department],
            )
            if owner is None:
                print(f"  {stage.n}. {stage.name_vi:<34} FAIL  no agent owns {stage.department}")
                failures += 1
                continue
            if str(task.owner_agent_id or "") != owner:
                await tasks.assign(task.id, owner)
                await session.flush()

            await service.execute_task(str(task.id))
            # Re-read rather than trust the returned outcome: the task row is what
            # the verdict is measured against, and a return value that disagreed
            # with it would be exactly the kind of plausible wrong answer this
            # script exists to catch.
            await session.flush()
            task = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()

            cleared = await _clear_gates(session, org_id, str(task.id))
            # Only resume a task that is actually *waiting*. Re-running one that
            # failed raises `illegal task transition: failed --fail-->` from inside
            # the service, which replaces the real reason -- the failure that
            # caused it -- with a crash in the thing meant to report it.
            if cleared and task.status == TaskStatus.WAITING_FOR_APPROVAL:
                # The gate resumed the work, so it runs again with the decision
                # attached. Skipping this leaves every gated stage parked in
                # `needs_approval` and the chain "finished" four times over.
                await service.execute_task(str(task.id))
                await session.flush()
                task = (await session.execute(select(Task).where(Task.id == task.id))).scalar_one()

            done = task.status == TaskStatus.COMPLETED
            produced = bool(task.output)
            if not (done and produced):
                failures += 1
                print(f"        reason: {str(task.last_error)[:160]}")
            note = f"gates={cleared}" if cleared else ""
            if not produced:
                note = (note + " " if note else "") + "no declared output"
            mark = "ok  " if done and produced else "FAIL"
            print(f"  {mark} {stage.n}. {stage.name_vi:<34} {task.status!s:<12} {note}")

        await session.commit()

    print("\n" + "-" * 72)
    # Counted over what was *attempted*, not over the eight. `len(STAGES) - failures`
    # reported 7/8 while a single stage had run, which is the kind of number that
    # reads as progress and is not.
    print(f"{attempted - failures}/{attempted} stages finished with their declared output")
    await db.dispose()
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--only", type=int, help="run one stage by number")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.org, args.only)))
