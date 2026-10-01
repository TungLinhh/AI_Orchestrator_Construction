"""Cancel the two superseded children, and write down why.

## What the measurement found, and how it corrected the earlier claim

An earlier pass reported "four duplicate child tasks, same title, same owner agent, same
status", and the plan was to delete three of them. **That was wrong, and the error is worth
recording because the method was wrong, not the arithmetic.**

`title` is `objective[:120]` — a truncation. Every objective longer than 120 characters has the
same first 120 characters, so the column an operator reads makes different work look identical.
The duplicate conclusion came entirely from that column.

Read against the **full goal** instead, and there is not one exact duplicate among the five:

    [1] -> [2]  [1] is an exact PREFIX of [2]; the extra text is
                 ", tổng hợp kết quả từ các artifact đã tạo"
    [3] -> [4]  [3] is an exact PREFIX of [4]; the extra text is
                 ". Tổng hợp kết quả từ các artifact đã tạo trước đó."
    [5]          similarity 0.27 to everything: a different task, and it is **completed**

So there are **two superseded pairs**, not four copies. The parent was run four times, and each
time the model re-read its own results and re-asked with "aggregate the artifacts you have
already created" appended. That is the behaviour the in-memory intent set was written for.

## Which one survives

**[2] and [4].** Each is a strict superset of its predecessor, so keeping the earlier one throws
away the instruction that says to aggregate the artifacts. And **[5] corroborates that** rather
than merely being compatible with it: the roll-up task that actually completed opens with
*"tổng hợp và báo cáo kết quả kiểm tra ... từ các artifact đã tạo trước đó"* — it needed the
aggregation, and it is the later wording that produced it. **The completed task is evidence for
which wording was the real instruction.**

## Cancel, never delete

Two reasons, and the second is the one that matters:

* **[1] and [3] hold no state.** No children, no delegations, no executions, no approvals,
  no model calls, no spend. Cancelling them loses nothing, because there is nothing there.
* **But the rows are the evidence.** That the platform asked the same agent for the same work
  four times is a fact about the platform, and it is recorded *only* by these rows. Deleting
  them would remove the record of the defect while leaving the defect. `assigned -> cancelled`
  keeps the row, is a legal transition, and makes the queue honest: one open task per piece of
  work, and a cancelled one that says why.

There is no `cancelled_reason` column on `tasks` — only `last_error`, which would be a lie here
because nothing failed. So the reason goes in the **audit log**, which is where the platform
keeps why something happened, and the state change goes through the state machine rather than
through an `UPDATE`.
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.audit.service import AuditService  # noqa: E402
from ai_orchestrator.domain.authority import Actor  # noqa: E402
from ai_orchestrator.domain.enums import ActorType  # noqa: E402
from ai_orchestrator.domain.ids import OrganizationId  # noqa: E402
from ai_orchestrator.domain.state_machines import Transition  # noqa: E402
from ai_orchestrator.persistence.repositories.task import TaskRepository  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402

ORG = "org_01m3h7j45b6cj3jnphq2ggjteq"
PARENT = "tsk_01m3m0gq71488f2c77hc4e0gbm"

#: The two superseded tasks and the one that supersedes each. Stated explicitly rather than
#: derived from a rule, because this is a **decision about meaning** -- "these two are the same
#: work and this one says it better" -- and a rule that inferred it would be making that call
#: invisibly. The evidence for each pair is in the module docstring.
SUPERSEDED = {
    "tsk_01m3m0hh6xt87b1kt1dc4egvpa": (
        "tsk_01m3m0yeavw6e3ke029ema3z6b",
        'the same instruction with ", tổng hợp kết quả từ các artifact đã tạo" appended',
    ),
    "tsk_01m3m0z3d4gkxk9kqjbsvxchss": (
        "tsk_01m3m0zhgt19dw0pkrst92h02n",
        'the same instruction with ". Tổng hợp kết quả từ các artifact đã tạo trước đó." appended',
    ),
}


async def main() -> int:
    db = Database.from_settings()
    try:
        async with db.tenant_session(ORG) as session:
            tasks = TaskRepository(session, ORG)
            audit = AuditService(session, ORG)
            actor = Actor(
                id="dev:no-auth",
                kind=ActorType.HUMAN,
                display_name="Local operator (authentication off)",
                organization_id=OrganizationId(ORG),
                is_privileged_human=True,
            )
            for task_id, (kept_id, extra) in SUPERSEDED.items():
                before = await tasks.get(task_id)
                if before.status == "cancelled":
                    print(f"  {task_id[:24]} already cancelled; nothing to do")
                    continue
                reason = (
                    f"superseded: {kept_id} carries the same instruction with {extra}. "
                    f"Kept because the completed roll-up task "
                    f"(tsk_01m3m110z3zvf0m39p0as4tcmg) needed the aggregation this wording "
                    f"adds. Cancelled rather than deleted, because this row is the record that "
                    f"the platform asked the same agent for the same work four times."
                )
                await tasks.transition(task_id, Transition.CANCEL)
                await audit.record(
                    actor=actor,
                    action="task.supersede",
                    resource_type="task",
                    resource_id=task_id,
                    task_id=task_id,
                    outcome="success",
                    context={"superseded_by": kept_id, "reason": reason},
                )
                print(f"  cancelled {task_id[:24]} -> superseded by {kept_id[:24]}")
            await session.commit()

        # Read back. A cleanup that reports a change it then rolled back is the F178 shape, and
        # this one is rewriting someone's queue.
        async with db.tenant_session(ORG) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT status, count(*) AS n FROM tasks "
                            "WHERE organization_id=CAST(:o AS varchar(64)) "
                            "AND parent_task_id=CAST(:p AS varchar(64)) GROUP BY 1 ORDER BY 1"
                        ),
                        {"o": ORG, "p": PARENT},
                    )
                )
                .mappings()
                .all()
            )
            audited = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM audit_logs "
                        "WHERE organization_id=CAST(:o AS varchar(64)) "
                        "AND action='task.supersede'"
                    ),
                    {"o": ORG},
                )
            ).scalar_one()
    finally:
        await db.dispose()

    print("  the parent's children, by state:")
    for r in rows:
        print(f"    {r['status']:10} {r['n']}")
    print(f"  {audited} audit row(s) written saying why")
    live = sum(r["n"] for r in rows if r["status"] in ("assigned", "running", "created"))
    return 0 if live == 2 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
