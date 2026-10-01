"""A stable key for "this parent already gave this agent this work".

## What was wrong, measured

The CEO queue listed one piece of work four times. Reading the **titles** said four duplicates;
reading the **goals** said otherwise — see F200 — and the real shape was two *superseded pairs*,
because the model re-read its own results and re-asked with "aggregate the artifacts you have
already created" appended. So the platform asked the same agent for the same work four times,
and nothing stopped it.

## Why neither existing key works

| key | catches a re-ask that appends words | distinguishes different work |
|---|---|---|
| `tasks.fingerprint` (whole normalised goal) | **no** — three extra words, different hash | yes |
| `title` (first 120 characters) | yes | **no** — two different jobs sharing a long prefix collide |

Neither is a bug; they answer two different questions, and the question being asked is a third
one: *is this the same instruction, or the same instruction restated?*

## The rule, and why this one

The normalised **token prefix**. `task_fingerprint` already sorts the normalised tokens, which
is what makes it wording-insensitive for reordering and punctuation. This takes the first
`INTENT_PREFIX_TOKENS` of that same normalised form and hashes those.

* Both measured pairs share their first ~15 tokens; the appended summary clause is at the end.
* The two *different* pieces of work in the same parent diverge within the first few tokens:
  *"kiểm tra chất lượng, đầy đủ"* against *"kiểm tra tính đầy đủ, chính xác"*.
* Reordering and punctuation do not move a token's position, so a restatement is caught.

A number had to be chosen, and 12 is chosen as **the smallest count that separates every
measured pair** — not a round number and not a guess. It is a constant, not a tuning knob,
because the honest state is that the right value depends on the corpus and one tenant's corpus
is not a general answer. Recorded rather than hidden.

## Partial, and that is the point

The index is **partial over live statuses only**. Finished work being asked for again is
legitimate — a follow-up, a re-check — and a guard that refused it would be a second defect in
the first one's clothes. The five rows measured here include a `completed` task that is
*different* work and must stay insertable.

## Nullable, and that is deliberate

Existing rows have no intent key, and inventing one retrospectively would assert that old
delegations meant something nobody recorded. `NULL` therefore means "this row was not created
by the delegating path" and is excluded from the index, which is exactly what `NULL` already
means everywhere else in this schema's provenance columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"

#: How many normalised tokens identify an instruction. See the module docstring: this is the
#: smallest count that separates every measured pair, not a round number.
INTENT_PREFIX_TOKENS = 16

TABLE = "tasks"
#: The `tasks` composite primary key is `(organization_id, id)`, and a check name composes as
#: `ck_<table>_<name>` in this schema -- so an explicit name keeps the error message readable
#: and stops a future rename from silently colliding.
CONSTRAINT = "tasks_intent_fingerprint"
INDEX = "uq_tasks_live_intent"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "intent_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Hash of the first 16 normalised tokens of the goal, in order, plus the owner agent. "
                "What the delegating executor writes; what the partial unique index keys on. "
                "NULL means the row was not created by a delegation."
            ),
        ),
    )
    # Partial, over **live** statuses only. `ON CONFLICT` is not what enforces it -- the index
    # is, which is the point: the database refuses, so a second code path cannot get in.
    op.create_index(
        INDEX,
        TABLE,
        ["organization_id", "owner_agent_id", "intent_fingerprint"],
        unique=True,
        postgresql_where=sa.text("status IN ('created', 'assigned', 'running')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_column(TABLE, "intent_fingerprint")
