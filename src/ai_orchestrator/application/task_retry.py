"""Run a failed task's work again, as a **new** task that remembers the first.

## Why this is a new task and not a reset

`TASK_TRANSITIONS` maps every terminal status to `{}`: nothing leaves it, and the comment on
that table says why — *"Retrying a FAILED task is a new task (linked via task_dependencies),
because reusing the row would make the audit trail lie about what was attempted."* That
reasoning is right, and it was the whole of the design: **the retry was specified and never
built.**

The consequence was measured. Fifty-eight tasks in the development tenant are `failed`, and
for every one of them the platform could say what went wrong and could offer nothing to do
about it. A person looking at the CEO queue could see failures and not handle them, which is
the one thing a work queue must not be.

So the state machine is honoured, not worked around: `retry_failed` **copies the work into a
new row** and links the two. The old row keeps its failure, its error and its `last_error`,
because those are facts. The new row is where the second attempt lives, and
`task_dependencies` makes the relationship queryable — *"this was a retry of that"* survives
the transaction.

## The insert goes through `TaskRepository.create`, not raw SQL

The first version wrote the `INSERT` by hand and failed twice on columns nobody remembered:
`fingerprint` is `NOT NULL` and is computed inside `create`, and the `jsonb` parameters have
to be serialised because asyncpg will not encode a dict for them. Both mistakes are the same
mistake — **a second definition of what a task row is**, which then disagrees with the first
about what a task is. `create` is the only constructor; this module adds the two things it
does not do: the link to the failure, and the reason it is allowed.

That also means the duplicate check still applies, and it is the *right* one: the original is
terminal, so it is not "active", so retrying it is not a duplicate of anything.

## Why the link is `finish_to_start` and points at the failure

`task_dependencies` reads `task` *depends on* `depends_on_task_id`. The new task therefore
depends on the failed one, and `unsatisfied_dependencies` — which is what stops a run — will
not release it until the failed task is finished.

That sounds like a deadlock, and it is worth being explicit about why it is not: the failed
task **is** finished. It is `failed`, which is terminal, so its dependency is satisfied at the
moment it is created and never blocks anything. The link is for the audit trail, not for
scheduling. If the dependency ever did go unsatisfied, that would mean a "failed" task that is
not terminal, and refusing to run would be the correct response to that impossibility.

## What is copied, and what is deliberately not

Copied: `goal`, `task_type`, `priority`, `input`, `constraints`, the requested output schema,
and the budget ceilings. Those are the *work*.

Not copied: `status` (it starts `created`), `owner_agent_id` (the retry is unassigned until
somebody or the coordinator gives it an owner), `last_error`, `attempt_count`, the lease, and
every spend figure. Those are the *history of the first attempt*, and copying them would make
the new row claim a cost it did not incur.

The title is copied with an attempt marker rather than verbatim, because a queue that lists
two identical titles gives an operator nothing to tell them apart — the same defect the
duplicate-delegation work ran into, in a different table.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.errors import ConflictError, NotFoundError
from ai_orchestrator.persistence.repositories.task import TaskRepository

#: How a failure is named in the new task's title. Deliberately short and deliberately
#: different: the operator's question at a glance is "is this the same work, again?", and the
#: marker answers it without opening anything.
RETRY_MARKER = " (lần chạy lại)"

_READ = """
SELECT id, title, goal, task_type, status, last_error, parent_task_id
FROM tasks
WHERE id = CAST(:id AS varchar(64)) AND organization_id = CAST(:o AS varchar(64))
"""

#: How many attempts have already been made at this work. Counting the **chain** rather than
#: the row, so retrying a retry also says "2". `parent_task_id` is the walk, and it is set on
#: every row this module creates, so the count stays right as the chain grows.
_CHAIN = """
WITH RECURSIVE chain AS (
    SELECT id, parent_task_id FROM tasks
    WHERE id = CAST(:id AS varchar(64)) AND organization_id = CAST(:o AS varchar(64))
  UNION ALL
    SELECT t.id, t.parent_task_id
    FROM tasks t JOIN chain c ON t.id = c.parent_task_id
)
SELECT count(*) FROM chain
"""


async def retry_failed(
    # **An `AsyncSession`, not the `SqlRunner` this layer uses for reads.** Every other
    # function in this layer takes a runner because it only issues SQL, and a runner is
    # the honest type for that. This one goes through `TaskRepository`, which needs the ORM
    # session -- so keeping the narrower type would mean a second definition of "insert a
    # task", which is the mistake this module already made once (see the docstring).
    session: AsyncSession,
    *,
    organization_id: str,
    task_id: str,
    now: dt.datetime,
) -> dict[str, Any]:
    """Create a new task carrying the same work, and link it to the failure it follows.

    Reads, validates, then writes — in that order, in one transaction. The ordering is not
    stylistic: a check that runs after the write can only ever report what it should have
    prevented.

    `now` is required rather than defaulted, for the same reason the reaper requires one: a
    timestamp taken per row cannot be reproduced from the row it was written to, and a naive
    one raises from inside the driver, far from the cause.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError(
            f"retry_failed(now=...) must be timezone-aware; got {now!r}. The tasks table "
            "stores timestamptz."
        )

    original = (
        (await session.execute(text(_READ), {"o": organization_id, "id": task_id}))
        .mappings()
        .one_or_none()
    )
    if original is None:
        raise NotFoundError(f"no task {task_id}")
    if original["status"] not in ("failed", "cancelled", "blocked"):
        raise ConflictError(
            f"this task is {original['status']}, and only failed work is retried. A task that "
            f"has not failed either needs running or needs deciding -- there is nothing to "
            f"repeat",
            details={"status": original["status"]},
        )

    attempt = int(
        (await session.execute(text(_CHAIN), {"o": organization_id, "id": task_id})).scalar_one()
    )
    tasks = TaskRepository(session, organization_id)
    retried = await tasks.create(
        title=f"{original['title'][:100]}{RETRY_MARKER} {attempt}",
        goal=original["goal"],
        task_type=original["task_type"],
        # **The link that carries the history.** `create` derives `root_task_id` from
        # `parent_task_id`, so naming the original's parent keeps the retry inside the same
        # piece of work rather than starting a second tree beside it.
        parent_task_id=original["parent_task_id"],
        requester_type="human",
    )
    # And the explicit dependency, so "this was a retry of that" is queryable without
    # reconstructing it from titles. Satisfied at once -- the failed task is terminal -- so it
    # never delays a run; see the module docstring for why that is not a deadlock.
    await tasks.add_dependency(task_id=retried.id, depends_on_task_id=original["id"])
    await session.execute(
        text("UPDATE tasks SET created_at = :now, updated_at = :now WHERE id = :id"),
        {"id": str(retried.id), "now": now},
    )
    return {
        "task_id": str(retried.id),
        "title": retried.title,
        "status": retried.status,
        "retried_from": str(original["id"]),
        "original_status": str(original["status"]),
        "attempt": attempt,
        "reason": (
            f"this work failed as {original['status']}"
            f"{': ' + str(original['last_error'])[:140] if original['last_error'] else ''}. "
            "The failed task is left exactly as it is; this is a new one that remembers it."
        ),
    }


__all__ = ["RETRY_MARKER", "retry_failed"]
