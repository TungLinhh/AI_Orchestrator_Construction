"""Repair four points of drift between the ORM models and the shipped schema.

This migration exists because running `alembic revision --autogenerate` for the
construction domain produced four `alter_column` calls against tables that were
written weeks earlier and had nothing to do with construction. That is the only
evidence anyone had: nothing in the repository compared the models to the
migrated schema, so the two were free to disagree indefinitely and every test
still passed, because every test used the models.

That is the failure mode this file is named for. A test asserting a column
exists passes on a model that says it exists. The model is the claim; the schema
is the fact. `tests/integration/test_schema_matches_models.py` now compares them
directly, and this migration is the first thing that comparison caught.

All four fixes are safe and all four move the schema *towards* the model except
where the model is the bug:

`model_profiles.max_classification` VARCHAR(64) -> VARCHAR(128)
    A widening. The model was widened when the API started reporting the
    classification ceiling and migration 0003 was never regenerated. Widening a
    varchar cannot lose data, so this is free.

`quarantined_proposals.findings` / `evidence_task_ids` JSON -> JSONB
    Migration 0005 wrote `sa.JSON()`; every other JSON column in the schema is
    JSONB. JSONB is binary and indexable, JSON is text. Converting is lossless
    because both store the same document. The fix makes quarantine consistent
    with the other 20-odd JSON columns rather than the reverse.

`quarantined_proposals.organization_id` — the model was wrong
    The migration created it NOT NULL; the model declared it nullable. Here the
    model loses. An org-scoped table with a nullable `organization_id` is a
    table whose RLS predicate compares NULL to the tenant GUC, which is never
    true, so the row would be invisible to its own tenant while still consuming
    storage and still appearing in owner-role reports. The model is corrected in
    `models.py` alongside this migration; neither alone is the fix.

`tasks.procedure_fingerprint` — drop a comment
    0004 attached a comment explaining that this hashes the *shape* of the work
    and is distinct from `fingerprint`, which hashes the wording. The model did
    not carry it, so autogenerate proposed deleting the only place that
    explanation existed. The comment moves into the model instead.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FINGERPRINT_COMMENT = (
    "Hash of the tool sequence and argument *shape*. Distinct from `fingerprint`, "
    "which hashes the goal's wording: two tasks can be different requests and the "
    "same procedure."
)


def upgrade() -> None:
    op.alter_column(
        "model_profiles",
        "max_classification",
        existing_type=sa.VARCHAR(length=64),
        type_=sa.String(length=128),
        existing_nullable=False,
        existing_server_default=sa.text("'restricted'::character varying"),
    )
    op.alter_column(
        "quarantined_proposals",
        "findings",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
    )
    op.alter_column(
        "quarantined_proposals",
        "evidence_task_ids",
        existing_type=postgresql.JSON(astext_type=sa.Text()),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=False,
    )
    # Deliberately absent: `quarantined_proposals.organization_id`. The schema is
    # already NOT NULL and that is correct; the model is fixed in `models.py`.
    # Emitting a `nullable=True` here would trade a real isolation hole for a
    # cosmetic one, purely to make autogenerate's diff come out empty.
    op.alter_column(
        "tasks",
        "procedure_fingerprint",
        existing_type=sa.VARCHAR(length=64),
        comment=None,
        existing_comment=FINGERPRINT_COMMENT,
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "tasks",
        "procedure_fingerprint",
        existing_type=sa.VARCHAR(length=64),
        comment=FINGERPRINT_COMMENT,
        existing_nullable=True,
    )
    op.alter_column(
        "quarantined_proposals",
        "evidence_task_ids",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
    )
    op.alter_column(
        "quarantined_proposals",
        "findings",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=postgresql.JSON(astext_type=sa.Text()),
        existing_nullable=False,
    )
    op.alter_column(
        "model_profiles",
        "max_classification",
        existing_type=sa.String(length=128),
        type_=sa.VARCHAR(length=64),
        existing_nullable=False,
        existing_server_default=sa.text("'restricted'::character varying"),
    )
