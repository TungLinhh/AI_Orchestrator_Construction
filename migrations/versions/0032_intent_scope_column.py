"""The duplicate-intent scope becomes a column, because an expression index cannot be one.

## What 0031 did, and why this file exists

0031 keyed the duplicate index on `COALESCE(parent_task_id, '')` so that two identical
goals from **different parents** would not collide, while a re-ask inside one parent still
would. That is the right rule, and the index had to be built with `CREATE UNIQUE INDEX ... (
organization_id, owner_agent_id, COALESCE(parent_task_id, ''), intent_fingerprint)`.

**Which the ORM cannot express.** `make model-sync` generated

```python
Index(
    "uq_tasks_live_intent",
    "organization_id",
    "owner_agent_id",
    "COALESCE(parent_task_id",
    "''::character",
    "intent_fingerprint",
    unique=True,
    postgresql_ops=["varying)"],
    ...
)
```

— the generator split the expression on its commas. And that is not merely ugly: SQLAlchemy
refuses it, so **the model module would not import at all**:

```
sqlalchemy.exc.ConstraintColumnNotFoundError: Can't create Index on table 'tasks':
no column named 'COALESCE(parent_task_id' is present.
```

`test_schema_matches_models` was the instrument that noticed, which is the one job it has.

## The fix is to stop computing it

`intent_scope` holds the value the expression was computing, written by the same code that
writes `intent_fingerprint`. The index is then four ordinary columns, `model-sync`
regenerates it correctly, and the drift test can compare it — which is the whole point of
that test, and an expression index silently exempt from it is worse than no index.

**Why a column and not a wider window or a looser rule.** Both of those were tried and
measured:

* a wider token window still collided on the *run marker* the platform appends, because two
  otherwise-identical goals differ only after it (F272);
* dropping the dedup entirely would let a model re-ask forever, and the first thing that
  broke when it did was a fan-out cap that stopped meaning anything.

A column says what it says, can be indexed, can be inspected in a query, and can be
backfilled — none of which an expression index can do.

## Backfill

`COALESCE(parent_task_id, '')`, from the same 0031 rule. Idempotent: re-running writes the
same values, and the unique index is dropped before the rewrite and recreated after, so the
intermediate state cannot violate it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

TABLE = "tasks"
INDEX = "uq_tasks_live_intent"
COLUMN = "intent_scope"


def upgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.add_column(
        TABLE,
        sa.Column(
            COLUMN,
            sa.String(length=40),
            nullable=False,
            server_default="",
            comment=(
                "The request this task belongs to, for duplicate detection: "
                "parent_task_id, or '' for a root. Computed rather than derived at "
                "index time because an expression index cannot be declared on the ORM, "
                "and a rule the drift test cannot see is a rule nobody verifies. "
                "See 0031 and 0032."
            ),
        ),
    )
    # The rewrite runs with the index gone, so the intermediate state cannot violate it.
    op.execute(
        sa.text(
            f'UPDATE "{TABLE}" SET {COLUMN} = COALESCE(parent_task_id, \'\')'
            f" WHERE {COLUMN} <> COALESCE(parent_task_id, '')"
        )
    )
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX {INDEX} ON {TABLE} "
            f"(organization_id, owner_agent_id, {COLUMN}, intent_fingerprint) "
            "WHERE status IN ('created', 'assigned', 'running')"
        )
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, COLUMN)
    op.create_index(
        INDEX,
        TABLE,
        ["organization_id", "owner_agent_id", "intent_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('created', 'assigned', 'running')"),
    )