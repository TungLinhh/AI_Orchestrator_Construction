"""Run a task in this process, when there is no worker to run it.

## Why this exists

`POST /tasks` dispatches a workflow to Temporal. Temporal is not running here, so the task
is committed and nothing picks it up -- and `_start_workflow_for` says so honestly in the
response's `workflow.reason`. That honesty is right and it is also a dead end: the page
ignored the field, showed "Queued.", and the task sat at `created` for ever. So the product
had a Run button that queued work nothing would ever do.

This module is the missing half. It drives `TaskExecutionService.execute_task` -- **the same
call the Temporal activity makes** -- on a background task, so the request returns at once
and the page can watch the run.

## The number that shapes the whole design

Measured, on the free model, for a coordination task that delegates to three agents:

    644.2s   67,136 tokens   status=completed

**Ten minutes and sixty-seven thousand tokens.** A synchronous `POST /tasks/{id}/run`
would time out in every reverse proxy worth the name, and a person who pressed it and saw
a spinner for ten minutes would conclude the product was broken. So the run is in the
background, its cost is stated on the button, and the page watches rather than waits.

Which is the honest shape for a platform where a real model does the work: **the run is long,
so the interface has to show it running.** A spinner that finishes instantly is a lie about
how long the work takes, and a page that pretends otherwise is worse than one that admits
the wait.

## The state lives in the database, not in this process

Progress is read from `executions`, which the real executor writes. A registry in memory
would answer "is it running" and nothing else -- and it would answer wrongly the moment the
server restarted. The only thing this module holds in memory is the set of task ids it has
*started*, and that exists for one reason: **to refuse a second run of the same task while
one is in flight**, which is the difference between a retry and a duplicate bill.

## Why no broker, no queue, no polling table

A run is one asyncio task against one database row. A durable queue for that is
infrastructure with a failure mode of its own, and this platform already has the durable
path (`workflow_runs` + Temporal) for when the brokers are up. This is the in-process
fallback, and it is labelled as one at every call site.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.errors import ConflictError, NotFoundError
from ai_orchestrator.persistence.session import Database

logger = logging.getLogger("ai_orchestrator.local_runner")

#: Measured on the free model for a three-way coordination delegation. Stated on the
#: button, because a person who presses Run deserves to know what it costs before, not
#: after.
MEASURED_SECONDS = 644
MEASURED_TOKENS = 67_136

#: Task ids this process has started and not yet finished. **In memory on purpose** -- it
#: is the double-run guard and nothing else. Losing it on a restart is safe: a restart
#: loses the run too.
_in_flight: set[tuple[str, str]] = set()

#: The asyncio tasks, so the server can be shut down without orphaning a run mid-write.
_running: dict[tuple[str, str], asyncio.Task[Any]] = {}


@dataclass(slots=True)
class RunHandle:
    """What the caller gets back, immediately. It is a handle, not a result."""

    task_id: str
    agent_id: str
    started: bool
    reason: str = ""
    already_running: bool = False
    estimate_seconds: int = MEASURED_SECONDS
    estimate_tokens: int = MEASURED_TOKENS

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "started": self.started,
            "reason": self.reason,
            "already_running": self.already_running,
            "estimate_seconds": self.estimate_seconds,
            "estimate_tokens": self.estimate_tokens,
        }


#: Is anything already running for this task, **according to the database**?
#:
#: The in-memory set catches a double press in one process. This catches a task that a
#: *different* process started, and a task the worker picked up -- neither of which the
#: in-memory set can see, and both of which are the ways a person gets billed twice.
_IS_RUNNING = """
SELECT count(*) FROM executions
WHERE organization_id = CAST(:o AS varchar(64)) AND task_id = :t
  AND status IN ('running', 'pending', 'started')
"""


async def start(
    conn: ReadConnection,
    *,
    organization_id: str,
    task_id: str,
    agent_id: str | None = None,
) -> RunHandle:
    """Start the run and return at once. **Never waits for the work.**

    Refuses a second run of a task that is already in flight, and says which of the two
    guards caught it. A refusal here is the cheapest thing in the platform: 67,000 tokens
    is a real bill and a duplicate is a real one.
    """
    key = (organization_id, task_id)
    if key in _in_flight:
        return RunHandle(
            task_id=task_id,
            agent_id=agent_id or "",
            started=False,
            reason="this task is already running in this process",
            already_running=True,
        )

    task = (
        (
            await conn.execute(
                text(
                    "SELECT id, status, owner_agent_id, title, input FROM tasks "
                    " WHERE organization_id = CAST(:o AS varchar(64)) AND id = :t"
                ),
                {"o": organization_id, "t": task_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if task is None:
        raise NotFoundError(f"no task {task_id}")

    task_input = task["input"] or {}
    if any(
        task_input.get(flag)
        for flag in ("business_workflow", "agent_workflow", "agent_blueprint_draft")
    ):
        return RunHandle(
            task_id=task_id,
            agent_id=str(agent_id or task["owner_agent_id"] or ""),
            started=False,
            reason="Run this task through its workflow controller",
        )

    remote = int(
        (await conn.execute(text(_IS_RUNNING), {"o": organization_id, "t": task_id})).scalar_one()
    )
    if remote:
        return RunHandle(
            task_id=task_id,
            agent_id=agent_id or "",
            started=False,
            reason=f"{remote} execution(s) are already running for this task, so something "
            "else is working on it",
            already_running=True,
        )

    # A **terminal** task is refused rather than run. Measured and then confirmed: pressing
    # Run on a task the executor had already completed returned `started: true`, and the
    # executor then re-read its own result and returned in a second -- so the API claimed
    # to have started 67,000 tokens of work and did nothing. A handle that says "started"
    # must mean work began.
    if task["status"] in ("completed", "failed", "cancelled", "blocked"):
        return RunHandle(
            task_id=task_id,
            agent_id=str(agent_id or task["owner_agent_id"] or ""),
            started=False,
            reason=f"this task already finished as {task['status']}. A run would re-read "
            "its own result and spend nothing, so the button would claim to have started "
            "work that never began",
        )

    chosen = agent_id or task["owner_agent_id"]
    if not chosen:
        raise ConflictError(
            "this task has no agent, so there is nothing to run it with. Assign one first."
        )

    _in_flight.add(key)
    _running[key] = asyncio.create_task(
        _run(organization_id, task_id, str(chosen)),
        name=f"local-run-{task_id}",
    )
    logger.info("local run started task=%s agent=%s", task_id, chosen)
    return RunHandle(task_id=task_id, agent_id=str(chosen), started=True)


async def _run(organization_id: str, task_id: str, agent_id: str) -> None:
    """The work, on its own connection, off the request's transaction.

    A **new** `Database` and a **new** session, because the caller's session is inside a
    request-scoped transaction that has already returned by the time this runs, and
    `tenant_session` wraps `session.begin()` (F102's third appearance: committing inside it
    ends it permanently).
    """
    from ai_orchestrator.application.pipeline import run_pipeline

    key = (organization_id, task_id)
    db = Database.from_settings()
    try:
        outcome = await run_pipeline(db, organization_id, task_id, root_agent_id=agent_id)
        logger.info("local goal finished task=%s outcome=%s", task_id, outcome.summary())
    except Exception:
        logger.exception("local run failed task=%s", task_id)
    finally:
        _in_flight.discard(key)
        _running.pop(key, None)
        with contextlib.suppress(Exception):
            await db.dispose()


async def shutdown() -> None:
    """Let in-flight runs finish, for a server that is going away cleanly.

    A run cancelled halfway leaves a task in `assigned` with no execution and no worker --
    the exact state this module exists to escape. Waiting is better than cancelling, and the
    caller is a shutdown path where a few more seconds is available.
    """
    for key, task in list(_running.items()):
        logger.info("waiting for in-flight run %s", key[1])
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task
    _running.clear()
    _in_flight.clear()


async def continue_goal(organization_id: str, task_id: str) -> RunHandle:
    """After the decision commits, wake the whole goal through its real parent chain."""
    db = Database.from_settings()
    try:
        async with db.tenant_session(organization_id) as session:
            root_id: str = (
                await session.execute(
                    text("""
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_task_id FROM tasks
                    WHERE organization_id = :org AND id = :task
                    UNION
                    SELECT t.id, t.parent_task_id FROM tasks t
                    JOIN ancestors a ON a.parent_task_id = t.id
                    WHERE t.organization_id = :org
                )
                SELECT id FROM ancestors WHERE parent_task_id IS NULL
            """),
                    {"org": organization_id, "task": task_id},
                )
            ).scalar_one()
            return await start(session, organization_id=organization_id, task_id=str(root_id))
    finally:
        await db.dispose()


async def is_running(organization_id: str, task_id: str) -> bool:
    """Whether a run is in flight, from the database or from this process."""
    if (organization_id, task_id) in _in_flight:
        return True
    db = Database.from_settings()
    try:
        async with db.session() as session:
            count = int(
                (
                    await session.execute(
                        text(_IS_RUNNING),
                        {"o": organization_id, "t": task_id},
                    )
                ).scalar_one()
            )
    finally:
        await db.dispose()
    return count > 0


__all__ = ["MEASURED_SECONDS", "MEASURED_TOKENS", "RunHandle", "is_running", "shutdown", "start"]
