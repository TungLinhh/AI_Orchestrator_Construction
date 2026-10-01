"""Attach a progress reading to the WBS node that contains it.

Migration `0017`. One column and one composite foreign key, and neither is optional.

## What was missing

`wbs` is the work breakdown: `code`, `name`, `parent_id`, `sequence`, `is_leaf`. It has
**no date columns**, deliberately — a breakdown is a *structure*, and a structure is
not a schedule. The schedule lives in `progress_snapshots`, one row per activity per
report.

But nothing joined them. `progress_snapshots` carried `wbs_item_id`, which is a foreign
key to `wbs_items` — the **priced BOQ line** — and not to `wbs`. The corpus's progress
activities have a window and a duration and no quantity and no rate, so there is nothing
to put them in `wbs_items`: writing them there would mean inventing a quantity and a
unit rate for 110 activities, which is a fabrication wearing a number.

So the two halves of the construction record sat in tables with no path between them.
"Which zone is late?" was unanswerable, and that is the first question a PM workspace
exists to answer.

## The column is nullable, and that is a decision

Existing rows have no WBS node to point at, and a sheet read before this migration
cannot be re-derived without re-reading the file. So `wbs_id` is nullable rather than
backfilled with a guess.

The alternative — refusing to read a sheet into a project that has no WBS — would have
made this migration a precondition for Phase 1's work, which is a much larger change
than the problem warrants. A reading with no node attached is a true reading of the
file, just an unplaced one, and `readings` in the reader already distinguishes exactly
that case.

## Composite, not bare

`(organization_id, wbs_id)` -> `(wbs.organization_id, wbs.id)`, like every other
new-table foreign key in `0016`. `wbs.project_id` itself is still a bare FK — that is
the 135-pointer gap in `PRODUCT_GAP.md` §8a, fixed on its own schedule — but a
*new* column has no excuse, and this one crosses exactly the boundary a bare FK would
get wrong: a reading in tenant A attached to a work package in tenant B would be a row
that RLS can never follow.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-29 09:30:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A composite foreign key needs the referenced pair to be unique, and `wbs.id` being
    # unique on its own is not the same statement -- Postgres needs
    # `UNIQUE (organization_id, id)` literally present.
    op.create_index("uq_wbs_org_id", "wbs", ["organization_id", "id"], unique=True)

    op.add_column(
        "progress_snapshots",
        sa.Column("wbs_id", sa.String(length=40), nullable=True),
    )
    op.create_foreign_key(
        "fk_progress_snapshots_wbs",
        "progress_snapshots",
        "wbs",
        ["organization_id", "wbs_id"],
        ["organization_id", "id"],
    )
    # The column every PM query starts from: "show me this work package's readings".
    # Without it the table has two halves and a foreign key between them that nothing
    # can traverse in the useful direction.
    op.create_index(
        "ix_progress_snapshots_org_wbs", "progress_snapshots", ["organization_id", "wbs_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_progress_snapshots_org_wbs", table_name="progress_snapshots")
    op.drop_constraint("fk_progress_snapshots_wbs", "progress_snapshots", type_="foreignkey")
    op.drop_column("progress_snapshots", "wbs_id")
    op.drop_index("uq_wbs_org_id", table_name="wbs")
