"""Immutable tenant sources, task manifests and downloadable artifacts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_files",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), nullable=False),
        sa.Column("org_unit_id", sa.String(40)),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("uploaded_by", sa.String(120), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("extraction", pg.JSONB(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_source_files_org_id"),
        sa.CheckConstraint("byte_count > 0 AND byte_count <= 10485760", name="size_bounded"),
        sa.CheckConstraint(
            "state IN ('ready', 'needs_review', 'unsupported', 'failed')", name="state_known"
        ),
        sa.CheckConstraint("origin IN ('upload', 'mock')", name="origin_known"),
        sa.CheckConstraint(
            "classification IN ('public','internal','confidential','restricted','secret')",
            name="classification_known",
        ),
    )
    op.create_index(
        "ix_source_files_organization_id_created_at",
        "source_files",
        ["organization_id", "created_at"],
    )
    op.create_table(
        "source_manifests",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), nullable=False),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("selection", sa.String(20), nullable=False),
        sa.Column("snapshot", pg.JSONB(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"], ["tasks.organization_id", "tasks.id"]
        ),
        sa.UniqueConstraint("organization_id", "id", name="uq_source_manifests_org_id"),
        sa.UniqueConstraint("organization_id", "task_id", name="uq_source_manifests_org_task"),
        sa.CheckConstraint("selection IN ('sources', 'mock')", name="selection_known"),
    )
    op.create_table(
        "source_bindings",
        sa.Column("organization_id", sa.String(40), primary_key=True),
        sa.Column("manifest_id", sa.String(40), primary_key=True),
        sa.Column("file_id", sa.String(40), primary_key=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "manifest_id"],
            ["source_manifests.organization_id", "source_manifests.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "file_id"], ["source_files.organization_id", "source_files.id"]
        ),
    )
    op.create_table(
        "source_artifacts",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), nullable=False),
        sa.Column("task_id", sa.String(40), nullable=False),
        sa.Column("manifest_id", sa.String(40), nullable=False),
        sa.Column("format", sa.String(12), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"], ["tasks.organization_id", "tasks.id"]
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "manifest_id"],
            ["source_manifests.organization_id", "source_manifests.id"],
        ),
        sa.UniqueConstraint(
            "organization_id", "task_id", "format", name="uq_source_artifacts_org_task_format"
        ),
    )
    for table in ("source_files", "source_manifests", "source_bindings", "source_artifacts"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        unit = (
            " AND app_unit_visible(org_unit_id)"
            if table == "source_files"
            else " AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = task_id AND t.organization_id = "
            + table
            + ".organization_id)"
            if table in {"source_manifests", "source_artifacts"}
            else ""
        )
        predicate = "organization_id = current_setting('app.current_tenant', true)" + unit
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        )
        # Sources and manifests are append-only even to the application role.
        op.execute(f'GRANT SELECT, INSERT ON "{table}" TO ao_app')


def downgrade() -> None:
    for table in ("source_artifacts", "source_bindings", "source_manifests", "source_files"):
        op.drop_table(table)
