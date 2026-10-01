"""Turn a playbook SOP into a task, and run it.

`application/playbook.py` knows *what* each procedure is. This knows how to hand
one to the organisation: find the department that owns it, give the chief a goal
that says which procedure and which control point, declare what a finished answer
must contain, and hand the rest to the pipeline.

**Why the contract is the whole point.** Every SOP in the dossier has a sentence
saying what must not happen -- a payment without a 3-way match, a Gate passed on a
verbal assurance, a supplier appointed without an assessment. An agent handed that
sentence as prose and asked for "a report" will produce a report. Declared
required keys turn the sentence into something the office can refuse.

**Why the AI-forbidden zones are enforced twice.** The SOP carries the zone, the
contract avoids asking for a decision, and `work_review` will reject an answer
that claims one. One of the three would be a promise; three is a gate.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.application.playbook import (
    AGENT_BY_DEPARTMENT,
    OFFICE_OF,
    PLAYBOOK,
    PlaybookSop,
    unassigned,
)
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.persistence.models import Agent
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


def build_goal(sop: PlaybookSop) -> str:
    """The chief's brief.

    Carries the procedure's own identity, its steps and its control point, because
    an office that only knows the goal has to guess which procedure it is running
    and what "done" means. The steps go in `input` as well; they are repeated here
    because the goal is what a human reads on the page and a reader should not have
    to open a second panel to learn what the control point is.
    """
    steps = "\n".join(f"  {i}. {s}" for i, s in enumerate(sop.steps, start=1))
    forbidden = ""
    if sop.forbidden:
        forbidden = (
            "\n\nVÙNG CẤM AI: việc này chỉ được chuẩn bị và đề xuất. "
            "Quyết định cuối cùng do con người quyết — dù kết quả của bạn có vẻ rõ ràng."
        )
    return (
        f"[{sop.code}] {sop.name}\n\n"
        f"Phòng ban chịu trách nhiệm: {sop.department}\n"
        f"Điểm kiểm soát then chốt: {sop.control_point}\n\n"
        f"Các bước:\n{steps}\n\n"
        f"Phải trả về các trường: {', '.join(sop.required)}"
        f"{forbidden}"
    )


async def create_sop_task(db: Database, org_id: str, sop: PlaybookSop) -> str:
    """The root task for one SOP, owned by the chief.

    **Refuses an SOP with no owning department.** Creating the task anyway would
    produce a task nobody can be routed to, and the pipeline would then report a
    `planning_error` about an agent rather than about the real problem, which is
    that this organisation has no department for this procedure. The dossier has
    eighteen departments and this build has six; saying which procedures that
    leaves uncovered is the point of the catalogue.
    """
    if not sop.runnable:
        msg = (
            f"{sop.code} ({sop.name}) has no owning department in this "
            f"organisation, so it cannot be handed to anyone. {sop.note}"
        )
        raise ValidationError(msg, details={"sop_code": sop.code, "department": None})
    async with db.committing_tenant_session(org_id) as session:
        chief = (
            await session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Executive Agent"
                )
            )
        ).scalar_one()
        root = await TaskRepository(session, org_id).create(
            title=f"[{sop.code}] {sop.name}"[:255],
            goal=build_goal(sop),
            task_type="coordination",
            requester_type="human",
            owner_agent_id=chief.id,
            expected_output_schema={"required": list(sop.required)},
            input={
                **sop.as_task_input(),
                "owning_department": agent_for_sop(sop),
                "owning_office": OFFICE_OF.get(sop.department or "", ""),
            },
        )
        await session.commit()
        return str(root.id)


def agent_for_sop(sop: PlaybookSop) -> str:
    """The agent that should end up doing this, named for the routing facts."""
    if not sop.department:
        return ""
    return AGENT_BY_DEPARTMENT[sop.department]


async def main(org_id: str, only: str | None, max_exec: int) -> int:
    settings = get_settings()
    sops = PLAYBOOK if not only else tuple(s for s in PLAYBOOK if s.code == only)
    if not sops:
        print(f"no SOP with that code. Try: {', '.join(s.code for s in PLAYBOOK[:3])}…")
        return 2

    print(f"\nprovider: {settings.model_provider_default}\n")
    db = Database.from_settings()
    failures = 0
    try:
        for sop in sops:
            if not sop.runnable:
                print(f"  --  {sop.code}  chưa có phòng ban chịu trách nhiệm")
                print(f"      {sop.note.splitlines()[0]}")
                continue
            root_id = await create_sop_task(db, org_id, sop)
            outcome = await run_pipeline(
                db, org_id, root_id, max_executions=max_exec, run_mode=RunMode.LIVE
            )
            mark = "OK  " if outcome.finished else "FAIL"
            if not outcome.finished:
                failures += 1
            print(
                f"  {mark} {sop.code}  {sop.department:<12} "
                f"executions={len(outcome.steps)} "
                f"accepted={outcome.reviews_accepted} rerun={outcome.reviews_rerun}"
            )
            for step in outcome.steps:
                print(f"        {'  ' * step.depth}{step.owner:<20} {step.status}")
    finally:
        await db.dispose()

    blocked = unassigned()
    print("\n" + "-" * 72)
    print(f"{len(sops) - failures}/{len(sops)} SOP chạy tới khi phòng ban xong việc")
    if blocked:
        print(f"{len(blocked)} SOP chưa có phòng ban trong tổ chức 6 phòng:")
        for sop in blocked:
            print(f"  - {sop.code} {sop.name}")
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--sop", help="one SOP code; all runnable SOPs when omitted")
    ap.add_argument("--max-executions", type=int, default=60)
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.org, args.sop, args.max_executions)))
