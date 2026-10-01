"""Say **which** agent asked for a task, not merely that an agent did.

## The gap

`tasks.requester_id` is a foreign key to **`users`**, and the only other column about the
requester is `requester_type`. So the pair can say *"an agent asked for this"* and carry **no
way to say which one**: an agent's id is not a `users` row, and writing one raised

    ForeignKeyViolationError: ... violates foreign key constraint on table "tasks"

Found by seeding the hiring chain, where every stage below the first is asked for by an agent.

## Why not one of the three easy answers

* `requester_type='human'` with a user id — **a lie**. A machine asked.
* An agent id in a free-text column — unqueryable, uncheckable, and a second place where
  "who asked" lives, which is how one vocabulary becomes two.
* Leave it `NULL` and record the agent in `input` — **what was done in the interim**, and it
  works, but it is a JSON key a `WHERE` cannot use. A column the query planner can read beats a
  key it cannot.

## Additive, so both writers stay honest

`requester_id` keeps pointing at `users` and is left `NULL` for agent requests. The new column
is a **second, agent-shaped** answer rather than a second meaning for the first one: a human
requester is in `requester_id`, an agent requester is in `requester_agent_id`, and neither
column is ever asked to hold the other's value. That is why this is additive and not a
migration of the existing data — **no historical row is re-interpreted**, which is the failure
mode that makes a schema change to a provenance column dangerous.

## The constraint forbids confusion; it does not require the new column

A `CHECK` in the first version read:

```sql
(requester_type <> 'agent' OR requester_agent_id IS NOT NULL)
AND (requester_type = 'agent'  OR requester_agent_id IS NULL)
```

and the upgrade **failed on the development database**: `ForeignKeyViolationError` no —
`CheckViolationError`, on **113 rows** that carry `requester_type='agent'` and predate the
column.

Which is the constraint being right and the migration being wrong. Requiring the new column on
historical rows means either a backfill that **guesses which agent asked** — inventing a fact —
or a constraint that refuses to apply to data that is already true. So the requirement half is
dropped and only the prohibition half is kept:

> a requester that is **not** an agent may not name an agent.

That still catches the real mistake — an agent's id written into `requester_id` alongside
`requester_type='human'`, which is the confusion the column exists to end — and it lets the 113
rows keep saying what is true of them: an agent asked, and the platform did not record which.
The gap stays visible in the data rather than being papered over with a guess.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"

TABLE = "tasks"
CONSTRAINT = "requester_kind_matches"
INDEX = "ix_tasks_requester_agent"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "requester_agent_id",
            sa.String(length=64),
            nullable=True,
            comment=(
                "The agent that asked for this task, when an agent did. Separate from "
                "requester_id, which is a foreign key to users and can only hold a person. "
                "A human requester goes in requester_id; an agent requester goes here."
            ),
        ),
    )
    # The rule is a constraint, not a check in the repository. A check in code is a rule the
    # next writer does not read; a `CHECK` is refused by the database whatever wrote the row.
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        # One direction only, and deliberately: see the docstring. Forbidding a **non-agent**
        # requester from naming an agent is the mistake worth preventing; requiring the new
        # column on rows that predate it would mean guessing who asked.
        "requester_type = 'agent' OR requester_agent_id IS NULL",
    )
    op.create_index(
        INDEX,
        TABLE,
        ["organization_id", "requester_agent_id"],
        postgresql_where=sa.text("requester_agent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, "requester_agent_id")
