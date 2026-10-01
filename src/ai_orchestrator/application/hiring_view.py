"""The hiring process, read for a person watching it happen.

## What this is for, and what it refuses to do

A person opening this page is asking four questions, in this order:

1. **Where is it now?** Which stage, who holds it, and is it moving or stuck.
2. **What is waiting on a person?** Every gate, with the question being asked -- because a gate
   showing only an action is a gate that gets clicked.
3. **What has been produced?** Each stage's artefacts, so the process is visibly doing work
   rather than visibly progressing.
4. **Did it follow the SOP?** Which SOP code it is implementing, and which deliverable of that
   SOP's title each stage belongs to.

**It does not judge.** There is no verdict here, no "on track" and no score: the platform can
report what happened and cannot say whether the recruitment is going well, because nobody has
told it what well looks like for a vacancy. The `jd_reference` and the rubric levels come from
the dossier, so the *process* is checkable; the *outcome* is not, and inventing a progress
percentage would be the one dishonest number on the page.

## Read from tasks, not from a parallel table

The stages are `tasks` rows carrying `input.stage_key` and `input.request_key`, because that is
where the executor's state already lives: a stage that is blocked, running, failed or waiting
for approval is a task in a state, and keeping a second copy would be a second thing to go
stale. Read, never write -- this module decides nothing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.hiring_process import (
    DEMO_REQUEST,
    JD_SECTIONS,
    RUBRIC,
    SOP_CODE,
    SOP_TITLE,
    STAGES_BY_KEY,
)

#: Live states a stage can be in, and what each means to the person watching. Named here
#: because "assigned" on its own says nothing about whether anybody picked it up.
_STAGE_STATE: dict[str, str] = {
    "created": "chờ giao",
    "queued": "đang chờ",
    "assigned": "đã giao, chưa chạy",
    "running": "đang chạy",
    "waiting_approval": "chờ người duyệt",
    "blocked": "bị chặn",
    "completed": "xong",
    "failed": "thất bại",
    "cancelled": "đã hủy",
}

_STAGES = """
SELECT t.id, t.title, t.status, t.goal, t.created_at, t.completed_at, t.last_error,
       t.owner_agent_id, t.input, t.output, t.attempt_count,
       a.name AS agent_name,
       (SELECT count(*) FROM task_dependencies d
         WHERE d.task_id = t.id AND d.depends_on_task_id <> t.id) AS deps,
       (SELECT count(*) FROM task_dependencies d
         JOIN tasks p ON p.id = d.depends_on_task_id AND p.organization_id = d.organization_id
         WHERE d.task_id = t.id AND p.status NOT IN ('completed','failed','cancelled','blocked')
       ) AS blocking
FROM tasks t
LEFT JOIN agents a ON a.id = t.owner_agent_id AND a.organization_id = t.organization_id
WHERE t.organization_id = CAST(:o AS varchar(64)) AND t.input->>'request_key' = :k
ORDER BY (t.input->>'stage_n')::int
"""

_APPROVALS = """
SELECT task_id, status, decision, decision_note, decided_by, created_at, decided_at,
       action_type, effect_class, risk_level, reason
FROM approvals
WHERE organization_id = CAST(:o AS varchar(64)) AND status = 'pending'
ORDER BY created_at
"""


async def hiring_process(conn: ReadConnection, *, organization_id: str) -> dict[str, Any]:
    """The whole process as a person would describe it, in order."""
    rows = (
        (await conn.execute(text(_STAGES), {"o": organization_id, "k": "hire:procurement-head"}))
        .mappings()
        .all()
    )
    pending = (await conn.execute(text(_APPROVALS), {"o": organization_id})).mappings().all()

    # **Pending approvals are matched against the task the view actually shows.** The first
    # version matched against every task carrying the request key, so after stage 1 was
    # retried the page said *"waiting on you at stage 1"* while stage 1 was finished -- the
    # pending approval belonged to the attempt that had been replaced. A gate that points at a
    # superseded attempt is worse than a gate that is not shown: clicking it would decide
    # something nobody is waiting for.
    by_task: dict[str, list[Any]] = {}
    for a in pending:
        by_task.setdefault(str(a["task_id"]), []).append(a)
    shown = {str(r["id"]) for r in rows}
    pending = [a for a in pending if str(a["task_id"]) in shown]
    by_task = {k: v for k, v in by_task.items() if k in shown}

    # **One row per stage, not one per task.** Running stage 1 for real produced a `failed`
    # task, and retrying it -- correctly -- created a *second* task carrying the same
    # `stage_key`. The first version of this view listed both, so the UI showed two stages
    # numbered 1 and the process looked longer than it is.
    #
    # A retry is a second **attempt** at a stage, not a second stage. So the newest task per
    # `stage_key` is the stage, and the count of the others is reported beside it -- which is
    # also more useful than hiding them, because "this stage took two attempts" is something an
    # operator wants to know.
    newest: dict[str, Any] = {}
    attempts: dict[str, int] = {}
    for row in rows:
        key = str((row["input"] or {}).get("stage_key") or "")
        if not key:
            continue
        attempts[key] = attempts.get(key, 0) + 1
        seen = newest.get(key)
        if seen is None or str(row["created_at"]) > str(seen["created_at"]):
            newest[key] = row
    rows = [newest[k] for k in sorted(newest, key=lambda k: int(STAGES_BY_KEY[k].n))]

    stages: list[dict[str, Any]] = []
    for row in rows:
        stage = STAGES_BY_KEY.get(str((row["input"] or {}).get("stage_key")))
        if stage is None:
            continue
        approvals = by_task.get(str(row["id"]), [])
        output = row["output"] or {}
        stages.append(
            {
                "key": stage.key,
                "n": stage.n,
                "name_vi": stage.name_vi,
                "office": stage.office,
                "department": stage.department,
                "agent": row["agent_name"] or stage.agent_name,
                "state": _STAGE_STATE.get(str(row["status"]), str(row["status"])),
                "task_id": str(row["id"]),
                "task_status": str(row["status"]),
                "produces": stage.produces.split(","),
                "produced": sorted(output.keys()) if output else [],
                "question": stage.approval_question,
                "gate": stage.approval,
                "deliverable": stage.deliverable,
                "waiting_on_you": bool(approvals),
                "approval_question": approvals[0]["reason"] if approvals else "",
                "error": row["last_error"],
                "attempt": int(row["attempt_count"] or 0),
                # How many tasks have been created for this stage. 1 is normal; more means the
                # stage failed and was retried, which is worth saying rather than hiding.
                "attempts": attempts.get(stage.key, 1),
            }
        )

    done = [s for s in stages if s["task_status"] == "completed"]
    on_you = [s for s in stages if s["waiting_on_you"]]
    # **Not a percentage.** A count of stages finished, and a count of stages waiting on a
    # person, are facts. "42% complete" would imply the stages are equally weighted and that
    # the process has a known shape -- neither of which is true, and one of them (the shape) is
    # the thing an operator is supposed to be able to change.
    return {
        "sop_code": SOP_CODE,
        "sop_title": SOP_TITLE,
        "position": DEMO_REQUEST.position,
        "office": DEMO_REQUEST.office,
        "department": DEMO_REQUEST.department,
        "headcount": DEMO_REQUEST.headcount,
        "jd_reference": DEMO_REQUEST.jd_reference,
        "jd_sections": list(JD_SECTIONS),
        "rubric": [{"level": n, "weight": w} for n, w in RUBRIC],
        "candidates": [dict(c) for c in DEMO_REQUEST.candidates],
        "stages": stages,
        "stage_count": len(stages),
        "done_count": len(done),
        "waiting_on_you": [s["n"] for s in on_you],
        "current": next(
            (s for s in stages if s["task_status"] not in ("completed", "cancelled")),
            stages[-1] if stages else None,
        ),
        "approved_deliverables": sorted(
            {s["deliverable"] for s in stages if s["task_status"] == "completed"}
        ),
    }


__all__ = ["hiring_process"]
