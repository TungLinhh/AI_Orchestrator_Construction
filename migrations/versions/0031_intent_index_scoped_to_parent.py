"""Scope the duplicate-intent index to the request that raised it.

## Why this exists

`uq_tasks_live_intent` was

```sql
UNIQUE (organization_id, owner_agent_id, intent_fingerprint)
```

**per agent, for the whole tenant.** So a department could hold exactly one live task of a
given kind, ever, and every new piece of work that normalised to the same prefix was
refused:

```
an equivalent task is already active: tsk_01m40w7z787mxdzwqqy8f4v754.
It was created concurrently, and the colleague that owns it has it.
```

Measured consequence, reported as *"a task cannot be done when there is another task going
— there should be multiple agents working at the same time and not blocking each other's
work"*. The index was the mechanism. A unique index over an unbounded scope is a queue
that admits one of anything.

## The rule it should have encoded

**Duplicate detection is about a model re-asking, and re-asking happens inside one
request.** The question `uq_tasks_live_intent` answers is *"has this parent already handed
this agent this work?"* — and that question is only meaningful with the parent in it.

* Same parent, same agent, same wording → **duplicate.** Refused. This is the case the
  index was built for and it still works.
* Different parent, same agent, same wording → **two pieces of work.** Two goals given on
  different days are two things to do, and refusing the second is the bug.

`parent_task_id` is nullable and a root task has none, so the key has to say what a root
should be compared against. Two candidates, and the first one tried was wrong:

* `COALESCE(parent_task_id, id)` — **wrong.** It makes every root task its own bucket, so
  two identical root goals never collide. That breaks the case the index was built for:
  pressing Run twice with the same goal must be refused, and
  `test_a_refused_duplicate_leaves_the_callers_work_intact` caught it immediately. It also
  makes root tasks exempt, and the platform's root tasks are the ones a person submits.
* `COALESCE(parent_task_id, '')` — **right.** Every root shares one bucket, so identical
  root goals still collide with each other, while any parent separates its own subtree.

`id` can never be the empty string, so `''` is not ambiguous with a real parent.

## Also: the fingerprints on disk were written by the old rule

`intent_fingerprint` changed from "the first 16 tokens" to "the whole goal"
(`domain/delegation.py`, F272). Every row written before that carries a key that no
longer matches what the code would compute, so the index would refuse a re-run of work it
should allow and allow one it should refuse — silently, and only for old rows.

They are cleared rather than recomputed. Recomputing needs each row's `goal`, `task_type`
and owner, all of which are on the row, so it is possible; but the *correct* value for a
row whose goal has since been rewritten is unknowable, and a row whose fingerprint is
stale-but-plausible is worse than one with none. A task with `NULL` is simply not deduped,
which is the fail-open direction and the right one for an anti-duplicate guard.

## Downgrade

Restores the old column list and the old predicates. It does **not** restore the old
fingerprints: they were cleared, and the hash cannot be inverted. This is recorded in the
function's docstring rather than hidden, because a migration that silently loses data in
one direction is the F178 shape.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

TABLE = "tasks"
INDEX = "uq_tasks_live_intent"
#: Over live statuses only: a finished task must not reserve its intent for ever, which was
#: the F249/F262 lesson twice over.
LIVE = sa.text("status IN ('created', 'assigned', 'running')")


def upgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.execute(
        sa.text(f'UPDATE "{TABLE}" SET intent_fingerprint = NULL WHERE intent_fingerprint IS NOT NULL')
    )
    # **Built with `CREATE UNIQUE INDEX`, not `op.create_index`.** The key is
    # `COALESCE(parent_task_id, '')` — an *expression*, not a column name — and
    # `op.create_index` passes its column list straight to Alembic, which rejects a
    # `TextClause` there with `TypeError: 'TextClause' object is not iterable`. The SQL is
    # written out so the expression can be there at all.
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} "
            "(organization_id, owner_agent_id, COALESCE(parent_task_id, ''), "
            "intent_fingerprint) WHERE status IN ('created', 'assigned', 'running')"
        )
    )


def downgrade() -> None:
    """Restores the old column list. **Does not restore the fingerprints** — they were
    cleared and a hash cannot be inverted. Rows stay `NULL`, which leaves them undeduped
    rather than wrongly deduped."""
    op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX}"))
    op.create_index(
        INDEX,
        TABLE,
        # The old shape, with real column names: no expression, so `op.create_index`
        # accepts it. The fingerprints themselves are **not** restored — see the module
        # docstring and this function's own.
        ["organization_id", "owner_agent_id", "intent_fingerprint"],
        unique=True,
        postgresql_where=LIVE,
    )