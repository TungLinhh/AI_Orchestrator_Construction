"""The hiring request, as tasks the platform can actually run.

## What this creates, and what it deliberately does not

A **dependency chain**, one task per stage, with the dossier's own dependencies. The point is
that the chain is not decoration: each stage's task carries its stage's `produces` in
`expected_output_schema`, its SOP code in `input`, and its approval gate in `input`, so the
agent working a stage is told what it is producing and where a person decides, without anyone
having to re-describe the process in a prompt.

**The stages are not wired to each other by a workflow.** The chain is expressed as
`task_dependencies` — stage *n* depends on stage *n-1* — which the executor already honours
(`unsatisfied_dependencies` refuses a run whose dependency has not finished). That is the
honest level of orchestration available: the durable engine is Temporal, it is not running, and
claiming a workflow here would be claiming something the product does not do.

## The one row that is hand-written, and why it is exactly one

Every task is created through `TaskRepository.create`, so every one gets its fingerprint, its
dedup key, its events and its timestamps from the same code the product uses. **The single
exception is the first approval.** There is no public writer for a *pending* approval — the
executor creates one when a run asks for a decision — so a demo cannot start without either
running the first stage or writing the row.

The choice is to write it, and to say so, because the alternative is worse: an approval created
by a *fake failed run* would put a fabricated outcome into the audit trail, and a fabricated
approver into a signed column. The row below carries `source='human'`, names no approver, and
says in `input` that it was written by this script. A person clicking Approve is then recorded
as `dev:no-auth` — the truth about authentication being off.

## Convergence, not only addition

Re-running this must not add a second chain. The script finds an existing root by its title and
its input's `request_key`, and **reconciles** the stage tasks against it, reporting what it
corrected. A provisioning step that only ever adds is not idempotent, and an idempotent step
that only ever reports is lying about one of the two (F146).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.domain.errors import ConflictError  # noqa: E402
from ai_orchestrator.domain.hiring_process import (  # noqa: E402
    APPROVAL_OUTCOMES,
    DEMO_REQUEST,
    JD_SECTIONS,
    RUBRIC,
    SOP_CODE,
    STAGES,
)
from ai_orchestrator.domain.ids import new_ulid  # noqa: E402
from ai_orchestrator.persistence.repositories.task import TaskRepository  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402

#: Stable per request, so re-running finds the same chain rather than making another.
REQUEST_KEY = "hire:procurement-head"


def _stage_goal(stage, request) -> str:
    """What the agent is asked to do at this stage.

    Written as a sentence with the deliverable named, because the model reads this and nothing
    else. The stage's `produces` is repeated in it deliberately: an agent that finishes without
    producing the named artefact has not done the stage, and saying so in the instruction is
    cheaper than discovering it at the approval.
    """
    lines = [
        f"[{SOP_CODE} · giai đoạn {stage.n}/{len(STAGES)}] {stage.name_vi}.",
        "",
        f"Yêu cầu tuyển: {request.position} — khối {request.office}, "
        f"phòng ban {request.department}.",
        f"Cần tạo: {stage.produces}.",
    ]
    if stage.key == "jd_and_rubric":
        lines += [
            "",
            "JD phải theo 6 hạng mục: " + " · ".join(JD_SECTIONS) + ".",
            "Rubric chấm điểm 3 mức: "
            + ", ".join(f"{n} = {int(w * 100)}%" for n, w in RUBRIC)
            + ". Mỗi tiêu chí phải kèm 1 dòng căn cứ.",
            f"Tham chiếu JD có sẵn trong hồ sơ: {request.jd_reference}.",
        ]
    if stage.key == "cv_screening":
        lines += ["", "Ứng viên trong hồ sơ (mô phỏng, không gửi cho ai):"]
        for c in request.candidates:
            lines.append(f"  - {c['name']} · {c['years']} · hiện tại: {c['current']} · {c['note']}")
    if stage.key == "scored":
        lines += [
            "",
            "Chấm từng ứng viên theo rubric đã duyệt, mỗi tiêu chí ghi 1 dòng căn cứ, "
            "và xếp hạng cuối cùng.",
        ]
    if stage.key == "offer":
        lines += [
            "",
            "Soạn offer để ký. **Không gửi cho ai** — đây là bản nháp để người duyệt xem.",
        ]
    if stage.key == "onboarding":
        lines += [
            "",
            "Hoàn thiện onboarding cho ứng viên đã được duyệt: hợp đồng, bộ hồ sơ nhân viên, "
            "các bước bàn giao phòng ban.",
        ]
    if stage.approval:
        lines += [
            "",
            f"Giai đoạn này CẦN NGƯỜI DUYỆT: {stage.approval_question}",
            f"Các kết quả hợp lệ: {', '.join(APPROVAL_OUTCOMES)}.",
            "Hãy dừng và đưa ra quyết định cần duyệt thay vì tự quyết.",
        ]
    return "\n".join(lines)


async def _root_for(session, org: str) -> str | None:
    row = (
        await session.execute(
            text(
                "SELECT id FROM tasks WHERE organization_id=CAST(:o AS varchar(64)) "
                "AND task_type='coordination' AND input->>'request_key' = :k "
                "ORDER BY created_at LIMIT 1"
            ),
            {"o": org, "k": REQUEST_KEY},
        )
    ).scalar_one_or_none()
    return str(row) if row else None


async def provision(organization_id: str | None) -> int:
    request = DEMO_REQUEST
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.session() as session:
            if organization_id is None:
                organization_id = (
                    await session.execute(
                        text(
                            "SELECT organization_id FROM sop_definitions "
                            " GROUP BY organization_id ORDER BY count(*) DESC LIMIT 1"
                        )
                    )
                ).scalar()
            if organization_id is None:
                print("  no organization holds the dossier catalogue; run `make ingest` first")
                return 1
        org = str(organization_id)
        print(f"  org            : {org}")
        print(f"  SOP            : {SOP_CODE} — {request.position}")

        async with db.tenant_session(org) as session:
            tasks = TaskRepository(session, org)
            existing_root = await _root_for(session, org)
            created, reused = 0, 0

            # --- stage 1: the root, owned by the chief -----------------------------------
            if existing_root:
                root_id = existing_root
                reused += 1
            else:
                # **`input` on the root, or the seeder does not converge.** `_root_for` looks
                # for `input->>'request_key'`, and the first version passed none, so a re-run
                # would have created a second chain and reported the stages as "already
                # present" while silently duplicating the thing they hang from. Found by
                # re-running it.
                # **Convergence, not only addition.** The first version let `create`'s
                # duplicate refusal escape, and a re-run then died with a `ConflictError`
                # rather than reconciling -- the F146 defect, in a script whose whole job is to
                # be re-runnable. The refusal here is *correct*: an active task with the same
                # fingerprint already exists, which is exactly the root being looked for. So it
                # is adopted and labelled, and the run reports what it adopted.
                try:
                    root = await tasks.create(
                        title=request.title,
                        goal=_stage_goal(STAGES[0], request),
                        # `execution`, not `coordination`. Measured by running it: a
                        # coordination task that produces a decision instead of delegating is
                        # failed by `coordination_may_complete`, correctly, and the task type
                        # was the thing that was wrong. The decomposition of this work is
                        # already eight rows below this one.
                        task_type="execution",
                        requester_type="human",
                        input={
                            "stage_key": STAGES[0].key,
                            "stage_n": STAGES[0].n,
                            "request_key": REQUEST_KEY,
                            "sop_code": SOP_CODE,
                            "office": STAGES[0].office,
                            "department": STAGES[0].department,
                            "produces": STAGES[0].produces,
                            "deliverable": STAGES[0].deliverable,
                            "approval": STAGES[0].approval,
                            "approval_question": STAGES[0].approval_question,
                        },
                        expected_output_schema={"produces": STAGES[0].produces},
                    )
                    root_id = str(root.id)
                    created += 1
                except ConflictError as exc:
                    adopted = (exc.details or {}).get("existing_task_id")
                    if not adopted:
                        raise
                    root_id = str(adopted)
                    reused += 1
                    # Label it, so the *next* run finds it by key rather than by collision.
                    await session.execute(
                        text(
                            "UPDATE tasks SET input = input || CAST(:i AS jsonb) "
                            "WHERE id=CAST(:t AS varchar(64)) "
                            "  AND organization_id=CAST(:o AS varchar(64))"
                        ),
                        {
                            "i": json.dumps(
                                {
                                    "request_key": REQUEST_KEY,
                                    "stage_key": STAGES[0].key,
                                    "stage_n": STAGES[0].n,
                                    "sop_code": SOP_CODE,
                                }
                            ),
                            "t": root_id,
                            "o": org,
                        },
                    )
                    print(f"  = adopted the existing root {root_id} and labelled it")
            chief = await _agent(session, org, STAGES[0].agent_name)
            if chief:
                await tasks.assign(root_id, chief)
                await tasks.set_output(root_id, {})

            # --- stages 2..8, each depending on the one before ----------------------------
            previous = root_id
            stage_ids: dict[str, str] = {STAGES[0].key: root_id}
            for stage in STAGES[1:]:
                goal = _stage_goal(stage, request)
                found = (
                    await session.execute(
                        text(
                            "SELECT id FROM tasks WHERE organization_id=CAST(:o AS varchar(64)) "
                            "AND input->>'stage_key' = :k ORDER BY created_at LIMIT 1"
                        ),
                        {"o": org, "k": stage.key},
                    )
                ).scalar_one_or_none()
                if found:
                    task_id = str(found)
                    reused += 1
                else:
                    # `requester_id` is **NULL**, and that is not an oversight.
                    #
                    # Measured: `tasks.requester_id` is a foreign key to **`users`**, and the
                    # only other column about the requester is `requester_type`. So the pair
                    # can say "an agent asked for this" and carry no way to say *which* agent --
                    # an agent's id is not a `users` row, and writing one raised
                    # `ForeignKeyViolationError`.
                    #
                    # The alternative was `requester_type='human'` with a user id, which would
                    # be a lie: a machine asked. So the type is honest, the id is absent because
                    # the column cannot hold one, and the agent that asked is in `input` and in
                    # the `delegations` row that made it. Recorded as F202; the fix is a
                    # `requester_agent_id` column, which is a schema change.
                    row = await tasks.create(
                        title=f"{stage.n}. {stage.name_vi}",
                        goal=goal,
                        task_type="execution",
                        parent_task_id=None,
                        requester_type="agent",
                        # **Both columns, each holding what it can hold.** `requester_id` is a
                        # foreign key to `users` and can only ever hold a person -- measured,
                        # when writing an agent's id there raised `ForeignKeyViolationError`.
                        # `requester_agent_id` (migration 0027) is the agent-shaped answer, and
                        # a `CHECK` forbids a non-agent requester from naming one, so the two
                        # can never be confused.
                        requester_id=None,
                        requester_agent_id=chief,
                        input={
                            "stage_key": stage.key,
                            "stage_n": stage.n,
                            "request_key": REQUEST_KEY,
                            "sop_code": SOP_CODE,
                            "office": stage.office,
                            "department": stage.department,
                            "produces": stage.produces,
                            "deliverable": stage.deliverable,
                            "approval": stage.approval,
                            "approval_question": stage.approval_question,
                            "jd_sections": list(JD_SECTIONS),
                            "rubric": {n: w for n, w in RUBRIC},
                            "candidates": list(request.candidates)
                            if stage.key == "cv_screening"
                            else [],
                        },
                        expected_output_schema={"produces": stage.produces},
                    )
                    task_id = str(row.id)
                    created += 1
                stage_ids[stage.key] = task_id
                # The chain. One dependency per stage, and the executor already refuses a run
                # whose dependency has not finished -- so the ordering is enforced, not implied.
                await session.execute(
                    text(
                        "INSERT INTO task_dependencies (id, organization_id, task_id, "
                        "depends_on_task_id, dependency_type) "
                        "SELECT :i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), "
                        "CAST(:d AS varchar(64)), 'finish_to_start' "
                        "WHERE NOT EXISTS (SELECT 1 FROM task_dependencies x "
                        "  WHERE x.organization_id=CAST(:o AS varchar(64)) "
                        "  AND x.task_id=CAST(:t AS varchar(64)) "
                        "  AND x.depends_on_task_id=CAST(:d AS varchar(64)))"
                    ),
                    {"i": f"tdep_{new_ulid()}", "o": org, "t": task_id, "d": previous},
                )
                previous = task_id

            # --- the one hand-written row: the opening approval ---------------------------
            from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
            from ai_orchestrator.domain.enums import ActorType, EffectClass, RiskLevel

            approvals = ApprovalService(session, org)
            pending = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM approvals "
                        "WHERE organization_id=CAST(:o AS varchar(64)) "
                        "  AND task_id=CAST(:t AS varchar(64)) AND status='pending'"
                    ),
                    {"o": org, "t": root_id},
                )
            ).scalar_one()
            if not pending:
                await approvals.create(
                    ApprovalRequest(
                        organization_id=org,
                        action_type="hr.open_headcount",
                        action_payload={
                            "position": request.position,
                            "office": request.office,
                            "department": request.department,
                            "headcount": request.headcount,
                            "sop_code": SOP_CODE,
                        },
                        requested_by=chief or "dev:no-auth",
                        requested_by_type=ActorType.AGENT,
                        effect_class=EffectClass.MUTATE_INTERNAL,
                        risk_level=RiskLevel.MEDIUM,
                        reason=STAGES[0].approval_question,
                        task_id=root_id,
                    )
                )
                print(
                    "  + a pending approval on the first stage "
                    f"({STAGES[0].approval}): {STAGES[0].approval_question}"
                )
            await session.commit()

        # --- read back. A seeder that reports a row it rolled back is F178, and this one
        #     writes an approval somebody is meant to click. ---------------------------------
        async with db.tenant_session(org) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT t.input->>'stage_n' AS n, t.status, t.title, "
                            "       (SELECT count(*) FROM task_dependencies d "
                            "         WHERE d.task_id = t.id) AS deps "
                            "FROM tasks t WHERE t.organization_id=CAST(:o AS varchar(64)) "
                            "  AND t.input->>'request_key' = :k "
                            "ORDER BY t.input->>'stage_n' NULLS FIRST, t.created_at"
                        ),
                        {"o": org, "k": REQUEST_KEY},
                    )
                )
                .mappings()
                .all()
            )
            appr = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM approvals "
                        "WHERE organization_id=CAST(:o AS varchar(64)) "
                        "  AND status='pending' AND action_type='hr.open_headcount'"
                    ),
                    {"o": org},
                )
            ).scalar_one()
    finally:
        await db.dispose()

    print(f"  wrote {created}, already present {reused}")
    for r in rows:
        n = r["n"] or "1"
        print(f"    {n:>2}. {r['status']:10} {r['title'][:48]:50} deps={r['deps']}")
    print(f"  {appr} pending approval(s) to answer")
    return 0


async def _agent(session, org: str, name: str) -> str | None:
    got = (
        await session.execute(
            text("SELECT id FROM agents WHERE organization_id=CAST(:o AS varchar(64)) AND name=:n"),
            {"o": org, "n": name},
        )
    ).scalar_one_or_none()
    return str(got) if got else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    args = parser.parse_args()
    return asyncio.run(provision(args.org))


if __name__ == "__main__":
    raise SystemExit(main())
