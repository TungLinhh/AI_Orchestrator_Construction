"""Durable native commands and external-action evidence, isolated per tenant."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_commands",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), nullable=False),
        sa.Column("root_task_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("requested_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("settled_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(40), nullable=False, server_default=sa.text("'pending'")),
        sa.Column(
            "feedback", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "root_task_id"], ["tasks.organization_id", "tasks.id"]
        ),
        sa.UniqueConstraint(
            "organization_id", "root_task_id", name="uq_workflow_commands_org_root"
        ),
        sa.CheckConstraint(
            "requested_seq >= settled_seq AND settled_seq >= 0", name="sequence_valid"
        ),
        sa.CheckConstraint("kind IN ('business_workflow', 'agent_workflow')", name="kind_known"),
    )
    op.create_index(
        "ix_workflow_commands_organization_id_state",
        "workflow_commands",
        ["organization_id", "state"],
    )
    op.create_table(
        "connector_actions",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("organization_id", sa.String(40), nullable=False),
        sa.Column("root_task_id", sa.String(40), nullable=False),
        sa.Column("stage_task_id", sa.String(40), nullable=False),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(40), nullable=False, server_default=sa.text("'prepared'")),
        sa.Column(
            "receipt", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "root_task_id"], ["tasks.organization_id", "tasks.id"]
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "stage_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_connector_actions_stage_task",
        ),
        sa.UniqueConstraint("organization_id", "action_key", name="uq_connector_actions_org_key"),
        sa.CheckConstraint(
            "state IN ('prepared', 'sending', 'confirmed', 'unknown')", name="state_known"
        ),
    )
    for table in ("workflow_commands", "connector_actions"):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            "USING (organization_id = current_setting('app.current_tenant', true)) "
            "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
        )
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO ao_app')


def downgrade() -> None:
    op.drop_table("connector_actions")
    op.drop_table("workflow_commands")
