"""Add `tasks.procedure_fingerprint` — the shape of the work, not its wording.

`tasks.fingerprint` answers "have I been asked this before?", which is what
deduplication needs and what repetition detection must not use: two purchase
requisitions worded differently are two tasks and one procedure. Without a
separate column there is nowhere to put the second answer, and the alternative —
reusing the task hash — produces a system that never detects a repeat, which looks
exactly like a system where nothing repeats.

Nullable, deliberately. A task that ran before this column existed, or one whose
trace was never written, has no procedure to record, and inventing one would put a
hash in a column that claims to describe a real observation. A null reads as "not
observed", which is the truth.

The index is on `(organization_id, owner_agent_id, procedure_fingerprint)`, which
is the shape of the repetition query: "has this agent done this shape of work
before?". A count over the whole organisation would answer a different and much
broader question.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "tasks"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "procedure_fingerprint",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Hash of the tool sequence and argument *shape*. Distinct from "
                "`fingerprint`, which hashes the goal's wording: two tasks can be "
                "different requests and the same procedure."
            ),
        ),
    )
    op.create_index(
        "ix_tasks_org_owner_procedure",
        TABLE,
        ["organization_id", "owner_agent_id", "procedure_fingerprint"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_org_owner_procedure", table_name=TABLE)
    op.drop_column(TABLE, "procedure_fingerprint")
