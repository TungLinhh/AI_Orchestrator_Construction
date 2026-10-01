"""Identify a progress reading by where it is, not by what it says.

Migration `0018`. One column, and the unique key rebuilt around it.

## What was wrong, measured over the corpus rather than over one file

`progress_snapshots` is keyed on `(organization_id, report_ref, section_label,
line_label)`. That key was a measured correction once — `0015` widened it from
`(report_ref, line_label)` because the real file numbers each system heading's
activities from 1, so `1` under `Zone A` and `1` under `Hệ thống cấp nước` collided.

**The measurement behind `0015` covered 34 activities. The corpus has 465.**

    candidate key                              sheets it collides on
    (section_label, line_label)                            3
    (section_label, line_label, line_no)                   1
    (section_label, line_label, work_description)          1
    all four together                                       1

The one sheet that survives none of them is `TĐ Hạ Tầng.xlsx :: TĐ INF`: 68 readings
of which **five keys appear twice**, identical in section, line, line number and work
description. The same activity is listed twice in one sheet. No key built from what a
reading *says* can separate those, because they say the same thing.

So the identity is positional: `source_row`, the row's absolute position in the sheet.
That is unique by construction, it is a fact about the file rather than a derivation
from it, and it is the only identity that survives a corpus which repeats itself.

## A duplicate here is a fact, not an error

`TĐ Hạ Tầng.xlsx` lists the same activity twice. That is either two zones with the same
name or a copy-paste in the source. The reader **records both** rather than refusing the
second, because:

* Refusing it would silently lose a line of somebody's schedule.
* The duplicate is visible in the data — two rows at two row numbers — and a reader
  comparing them can decide.

That is the same call as `actual_updated`: record the fact, let the reader see it, and
refuse only what cannot be represented at all.

## `source_row` is NOT NULL

Every existing reading has a row, so a nullable column with a NULL fallback would put
`NULL` into a unique key — and in Postgres `NULL`s are distinct from each other, so the
constraint would silently stop applying to exactly the rows that needed it. `NOT NULL`
is what makes the key an identity rather than a suggestion.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-29 14:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "progress_snapshots",
        sa.Column(
            "source_row",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    # Backfilled from the position each reading was written at, which is the only
    # ordering the table itself preserves — `created_at` is a per-row clock read and
    # two rows in one report can share it to the microsecond on a fast machine.
    op.execute(
        """
        WITH ordered AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY organization_id, report_ref
                       ORDER BY created_at, id
                   ) - 1 AS position
            FROM progress_snapshots
        )
        UPDATE progress_snapshots p
        SET source_row = ordered.position
        FROM ordered
        WHERE p.id = ordered.id
        """
    )
    op.alter_column(
        "progress_snapshots",
        "source_row",
        nullable=False,
        server_default="0",
    )
    # The old key, replaced rather than kept: it is wrong for 3 of the 16 progress
    # sheets in the corpus, and a unique constraint that is right most of the time is
    # worse than none, because a write that fails for no visible reason looks like a
    # bug in the writer.
    op.drop_index(
        "uq_progress_snapshots_org_report_line", table_name="progress_snapshots"
    )
    op.create_index(
        "uq_progress_snapshots_org_report_row",
        "progress_snapshots",
        ["organization_id", "report_ref", "source_row"],
        unique=True,
    )
    # `section_label` and `line_label` stay, and stay indexed together, because
    # "show me this system's activities" is the query a PM actually runs and dropping
    # the index to save a few kilobytes would be the wrong trade.
    op.create_index(
        "ix_progress_snapshots_org_section_line",
        "progress_snapshots",
        ["organization_id", "report_ref", "section_label", "line_label"],
    )


def downgrade() -> None:
    op.drop_index("ix_progress_snapshots_org_section_line", table_name="progress_snapshots")
    op.drop_index("uq_progress_snapshots_org_report_row", table_name="progress_snapshots")
    op.drop_column("progress_snapshots", "source_row")
    op.create_index(
        "uq_progress_snapshots_org_report_line",
        "progress_snapshots",
        ["organization_id", "report_ref", "section_label", "line_label"],
        unique=True,
    )
