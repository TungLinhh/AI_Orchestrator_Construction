"""Close executions stranded on a task that has since reached a terminal state.

## Why this is a script and not part of the reaper

The reaper's own condition is lease-based, and the lease is written inside the run's
transaction and never committed before the run ends (F190), so nothing can see it while the
run is in flight. That makes the lease-based sweep unsatisfiable in practice -- it runs, finds
nothing, and reports success.

This one needs no lease and no clock, so it needs no scheduler: **a run cannot still be in
flight on a task that has finished.** A task in `completed`, `failed`, `cancelled` or
`blocked` got there *because* a run finished, so an execution still marked `running` on it is
a fact about the past, not a timeout to be guessed at.

## What was wrong before this existed

Measured: **11 executions** sat at `running` in the development tenant, the oldest from
27 September, on tasks that had reached `failed`. `local_runner`'s double-run guard counts
exactly those, so pressing Run answered *"1 execution(s) are already running for this task,
so something else is working on it"* -- and would have answered the same thing forever, about
work nobody was doing and never would be. A message that says someone is on it, when nobody
ever will be, is worse than no message: acting on it means waiting.

## Idempotent by construction

The `WHERE` only matches rows still marked `running` on a terminal task, so a second run
finds nothing. That is what lets it sit in `make page` -- a maintenance step that is safe to
run on every start, rather than something that needs a cron somebody forgot.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.application.fleet_view import STUCK_AFTER_SECONDS  # noqa: E402
from ai_orchestrator.application.task_reaper import (  # noqa: E402
    abandon_unclaimed_tasks,
    reap_stranded_executions,
)
from ai_orchestrator.approvals.service import ApprovalService  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402


async def sweep(organization_id: str | None) -> int:
    db = Database.from_settings(use_admin_role=True)
    try:
        if organization_id is None:
            async with db.session() as session:
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
        async with db.tenant_session(organization_id) as session:
            now = dt.datetime.now(tz=dt.UTC)
            closed = await reap_stranded_executions(
                session, organization_id=organization_id, now=now
            )
            # **Tasks nobody ever claimed.**
            #
            # Measured 2026-10-03: 106 tasks `assigned`, 0 `running`, and a person
            # pressing Run with nothing happening. The queue was not draining because
            # nothing drained it -- a task created over HTTP is committed, and with no
            # Temporal server and no worker nothing ever claims it. It sat `assigned` for
            # ever, counted in "waiting on you", and was work no agent would ever do.
            #
            # Failing them is the only way the register becomes a statement about the
            # organisation rather than a record of what was abandoned. Same sweep, same
            # window the fleet view uses for a stranded box, so the console and the sweep
            # cannot disagree about what counts as nobody working on it.
            abandoned = await abandon_unclaimed_tasks(
                session, organization_id=organization_id, now=now
            )
            # Approvals nobody answered, expired.
            #
            # `ApprovalService.expire_stale` has existed the whole time and its
            # docstring says "Run by a scheduled task" -- there is no scheduled
            # task. So every approval past its deadline stayed `pending` in the
            # table, appeared in the queue as if it could still be decided, and
            # refused when it was: two `hr.open_headcount` requests a day old, both
            # offering an Approve button, both answering "expired at
            # 2026-09-29T15:46:06" on click.
            #
            # It belongs here rather than in a new script: this is the same job --
            # state that is a fact about the clock rather than a decision, left
            # behind because nobody was on the hook for it -- and `make page`
            # already runs this on every start.
            expired = await ApprovalService(session, organization_id).expire_stale()
            await session.commit()
    finally:
        await db.dispose()

    # Read back rather than trusting the return value. A maintenance step that reports a
    # change it then rolled back is the F178 shape, and this script is the kind that gets
    # believed.
    async with Database.from_settings().tenant_session(organization_id) as session:
        still = (
            await session.execute(
                text(
                    "SELECT count(*) FROM executions e "
                    "JOIN tasks t ON t.id = e.task_id AND t.organization_id = e.organization_id "
                    "WHERE e.organization_id = CAST(:o AS varchar(64)) AND e.status = 'running' "
                    "  AND t.status IN ('completed', 'failed', 'cancelled', 'blocked')"
                ),
                {"o": organization_id},
            )
        ).scalar()
    # Đọc lại thay vì tin giá trị trả về. Lỗi F178: một bước bảo trì báo cáo thay đổi
    # nó vừa rollback, và script này đúng loại được tin.
    async with Database.from_settings().tenant_session(organization_id) as session:
        still_open = (
            await session.execute(
                text(
                    "SELECT count(*) FROM tasks WHERE organization_id = CAST(:o AS"
                    " varchar(64)) AND status IN ('created', 'assigned', 'running')"
                    " AND updated_at < :since"
                ),
                {
                    "o": organization_id,
                    "since": dt.datetime.now(tz=dt.UTC) - dt.timedelta(seconds=STUCK_AFTER_SECONDS),
                },
            )
        ).scalar()
    print(
        f"  org {organization_id}: closed {len(closed)} stranded execution(s), "
        f"expired {len(expired)} unanswered approval(s), "
        f"abandoned {len(abandoned)} unclaimed task(s)"
    )
    if still_open:
        print(f"  FAILED: {still_open} unclaimed task(s) are still open after writing")
        return 1
    if still:
        print(f"  FAILED: {still} still stranded after writing")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    args = parser.parse_args()
    return asyncio.run(sweep(args.org))


if __name__ == "__main__":
    raise SystemExit(main())
