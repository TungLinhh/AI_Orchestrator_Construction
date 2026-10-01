"""Record how many calls a run actually made, so the ceiling stops being a guess.

F206 said `max_tool_calls` could not be set from a distribution because tool
calls were not recorded per execution. That was half right, and the half that was
wrong matters: **every tool call already writes an `audit_logs` row**, so the data
exists. It is a join and a `GROUP BY` away, and nobody asked that question often
enough to notice it was available.

So the missing thing was not the measurement, it was the measurement being cheap.
A count that costs a join across the audit ledger is a count nobody runs, and a
ceiling set from a count nobody runs is a guess wearing a measurement's clothes.

The count is kept in memory during the run and written **once**, when the
execution row is finished. The obvious alternative -- an `UPDATE executions SET
tool_call_count = tool_call_count + 1` on every tool call -- puts a write on the
hot path of every tool call, inside a transaction that may be about to roll back,
for a number that cannot change its meaning until the run ends anyway. One write
per run instead of one per call, for the same answer.

Measured, and it is the reason this is worth doing rather than tidying:

    qwen3.8-27b:free, real run, real tenant
      24 model calls, 46 tool calls, stopped by the ceiling
      13 tool calls succeeded, 25 failed on a database error
      all 46 were the same tool

Nothing in the data said "this model has lost the thread" until the tool-call
count was next to the model-call count. With `model_usage` alone it is invisible:
one row per model call, and every failure looks like every other failure.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"

TABLE = "executions"

from ai_orchestrator.persistence.models import (  # noqa: E402
    MODEL_CALLS_COMMENT,
    TOOL_CALLS_COMMENT,
)


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column("tool_call_count", sa.Integer(), nullable=True, comment=TOOL_CALLS_COMMENT),
    )
    op.add_column(
        TABLE,
        sa.Column("model_call_count", sa.Integer(), nullable=True, comment=MODEL_CALLS_COMMENT),
    )
    # No backfill, and no default. Rows written before this migration made an
    # unknown number of calls; NULL says "not measured" and 0 would say "made
    # none", which is a different claim and would drag every average down.


def downgrade() -> None:
    op.drop_column(TABLE, "model_call_count")
    op.drop_column(TABLE, "tool_call_count")
