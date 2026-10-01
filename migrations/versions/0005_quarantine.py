"""The migration for quarantine: a table that is never read by the load path.

`SELF_IMPROVEMENT.md` §8 records the deliberate difference from Hermes. There, a
scanned-and-quarantined skill is left on disk and merely hidden from the index. Here,
**the row is not written at all**, because a quarantined row in the database is one
query away from being used.

So quarantine cannot be a `status` column on the proposals table. It has to be a
different table, holding a record of the *verdict* and the *evidence* but never the
*payload*, and the proposal loader must not read it.

The payload is the dangerous part. An operator investigating a quarantine needs to
know which procedure it was about and what tripped it, and can reconstruct the text
from the cited runs if a human being genuinely needs to see it. Writing the
proposed text into a table that an operator dashboard will eventually query would
put the payload back into reach, one report away — which is the failure this table
exists to make impossible.

Revision ID: 0005
Revives: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "quarantined_proposals"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        # What it was about, and what tripped it. Never what it *said*: the
        # column is deliberately absent and its absence is the design.
        sa.Column("procedure_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("proposed_by", sa.String(length=255), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("evidence_task_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quarantined_proposals")),
    )
    # Tenant-scoped like everything else. `organizations` is deliberately not a
    # foreign key: the quarantine record must outlive the organisation, or an
    # operator loses the only trace that something was attempted.
    op.create_index(
        "ix_quarantined_proposals_org_created",
        TABLE,
        ["organization_id", "created_at"],
        unique=False,
    )

    # RLS, applied here rather than by editing 0002.
    #
    # A tenant-scoped table without a policy is a table every tenant can read, and
    # the isolation test would have caught it — but only *after* the migration had
    # shipped to a database, which is the wrong order to find out. Quarantine
    # records name which procedure was proposed and by which model, which is
    # another tenant's business.
    #
    # `FORCE` as everywhere else, so the table owner is subject to it too.
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {TABLE}_tenant_isolation ON {TABLE} "
        "USING (organization_id = current_setting('app.current_tenant', true)) "
        "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO ao_app")


def downgrade() -> None:
    op.drop_index("ix_quarantined_proposals_org_created", table_name=TABLE)
    op.drop_table(TABLE)
