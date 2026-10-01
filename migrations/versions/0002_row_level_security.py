"""Row-level security and a least-privilege application role.

Two things happen here that the initial schema could not do on its own:

1. RLS is enabled and *forced* on every tenant-scoped table, with one uniform
   isolation policy. Without `FORCE`, the table owner bypasses every policy —
   and the owner is the role the migrations run as.

2. A separate `ao_app` role is created for the running application, granted DML
   but not ownership and not BYPASSRLS. The point is that a bug in the
   application cannot escalate to "read every tenant's data": the role does not
   hold that capability, and the RLS predicate is evaluated for it.

The owner role keeps BYPASSRLS on purpose: migrations, backups and the seed
script legitimately need cross-tenant access.
`tests/integration/test_tenant_isolation.py` asserts which grants the app role
does and does not hold.

Revision ID: 0002
Revises: e16621b0d3d9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "e16621b0d3d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables with no organization_id. RLS on these is meaningless: a policy would
#: have no column to compare against, so it is skipped rather than faked.
GLOBAL_TABLES = ("consumer_offsets", "organizations")

#: The audit ledger. Append-only: the application gets no UPDATE/DELETE, so a
#: compromised agent can record what it did and cannot erase it.
LEDGER_TABLE = "audit_logs"


def _tenant_tables(conn: sa.Connection) -> list[str]:
    rows = conn.execute(
        sa.text(
            """
            SELECT table_name FROM information_schema.columns
            WHERE table_schema = 'public' AND column_name = 'organization_id'
            ORDER BY table_name
            """
        )
    ).scalars()
    return [t for t in rows if t not in GLOBAL_TABLES]


def upgrade() -> None:
    conn = op.get_bind()
    tables = _tenant_tables(conn)

    for table in tables:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        # Without FORCE the owner bypasses the policy, and the owner is the role
        # that applied this migration. FORCE closes that hole.
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.execute(
            f'CREATE POLICY tenant_isolation ON "{table}" '
            "USING (organization_id = current_setting('app.current_tenant', true)) "
            "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
        )

    # ---------------------------------------------------------- app roles ----
    # NOSUPERUSER and NOBYPASSRLS are the load-bearing parts. A role with
    # BYPASSRLS silently ignores every policy above.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ao_app') "
        "THEN CREATE ROLE ao_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "NOBYPASSRLS NOINHERIT; END IF; END $$;"
    )
    # ao_backup exists because `pg_dump` cannot read rows that FORCE ROW LEVEL
    # SECURITY otherwise hides. It is never used by the application.
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='ao_backup') "
        "THEN CREATE ROLE ao_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        "BYPASSRLS NOINHERIT; END IF; END $$;"
    )

    op.execute("GRANT USAGE ON SCHEMA public TO ao_app")
    for table in [*tables, *GLOBAL_TABLES]:
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO ao_app')
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO ao_app")
    op.execute("GRANT USAGE, SELECT ON SEQUENCE audit_log_seq TO ao_app")

    op.execute(f"REVOKE UPDATE, DELETE ON {LEDGER_TABLE} FROM ao_app")
    op.execute(f"GRANT SELECT, INSERT ON {LEDGER_TABLE} TO ao_app")

    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ao_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ao_app"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO ao_backup")


def downgrade() -> None:
    conn = op.get_bind()
    for table in _tenant_tables(conn):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON SCHEMA public FROM ao_app")
    op.execute("REVOKE ALL ON SCHEMA public FROM ao_backup")
    op.execute("DROP ROLE IF EXISTS ao_app")
    op.execute("DROP ROLE IF EXISTS ao_backup")
