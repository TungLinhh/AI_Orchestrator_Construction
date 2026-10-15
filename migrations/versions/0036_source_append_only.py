"""Override default table grants for append-only source and artifact records."""

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None
TABLES = ("source_files", "source_manifests", "source_bindings", "source_artifacts")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON "{table}" FROM ao_app')


def downgrade() -> None:
    for table in TABLES:
        op.execute(f'GRANT UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON "{table}" TO ao_app')
