"""Construction domain, tranche 1: clients, projects, zones, WBS, milestones.

The first slice of the business tables, and deliberately the least interesting
one. Nothing here needs a language model, and that is the point of starting
here: the spine has to be right before any agent is allowed to propose a change
to it.

Tenancy is applied in this file, not in 0002
    0002 discovered tenant tables by querying `information_schema`, which was
    correct when the schema was 44 tables and wrong the moment a new one was
    added. RLS here is written out explicitly, table by table, so a reviewer can
    see that `zones` is protected without reading 0002 and hoping. A table
    missing from `TENANT_TABLES` is a table this migration forgot, and
    `tests/unit/test_construction_schema.py` fails on exactly that.

Every business row carries provenance
    `source` and `proposal_id`, with a check constraint making the claim and its
    evidence agree. This is the platform's central promise — an AI service emits
    a proposal, never a write — and it lives in the database rather than in
    application code, so it holds for raw SQL, for a service nobody has written
    yet, and for a mistake.

Quantity and unit are separate columns
    `wbs_items.quantity` is NUMERIC and `wbs_items.unit_code` is half of a
    composite foreign key over `(organization_id, unit_code)`. There is no column
    in which "12.5m" can be stored, and no way for one tenant's line to point at
    another tenant's unit.

The table bodies are Alembic's own output, generated rather than retyped, so the
migration and `persistence/construction.py` are the same schema by construction
rather than by review. Regenerate rather than edit: this file is `models.py`
translated, and hand-editing the two is how they stop agreeing.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates. Drives RLS and grants, and is the list
#: the schema test asserts against, so "protected" and "created" cannot drift
#: apart. `units_dictionary` is here despite not looking like a business table:
#: it is organisation-scoped, so a policy on it is meaningful.
TENANT_TABLES = (
    "clients",
    "client_contacts",
    "projects",
    "project_roles",
    "project_phases",
    "units_dictionary",
    "zones",
    "wbs",
    "wbs_items",
    "milestones",
)

APP_ROLE = "ao_app"


def _protect(table: str) -> None:
    """Enable and force RLS, install the isolation policy, grant DML.

    Identical to what 0002 did for the original 44 tables, including the
    `FORCE`. Without `FORCE` the owner bypasses the policy, and the owner is the
    role that ran this migration.
    """
    op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
    op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
    op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
    op.execute(
        f'CREATE POLICY tenant_isolation ON "{table}" '
        "USING (organization_id = current_setting('app.current_tenant', true)) "
        "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
    )
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO {APP_ROLE}')


def upgrade() -> None:
    op.create_table('clients',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('short_name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('tax_code', sa.String(length=128), server_default='', nullable=False),
    sa.Column('address', sa.Text(), server_default='', nullable=False),
    sa.Column('city', sa.String(length=128), server_default='', nullable=False),
    sa.Column('country', sa.String(length=128), server_default='VN', nullable=False),
    sa.Column('email', sa.String(length=128), server_default='', nullable=False),
    sa.Column('phone', sa.String(length=128), server_default='', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='active', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_clients_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_clients_source_known')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clients'))
    )
    op.create_index('ix_clients_org_code', 'clients', ['organization_id', 'code'], unique=True)
    op.create_index('ix_clients_org_name', 'clients', ['organization_id', 'name'], unique=False)
    op.create_table('units_dictionary',
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=128), server_default='', nullable=False),
    sa.Column('name_en', sa.String(length=128), server_default='', nullable=False),
    sa.Column('dimension', sa.String(length=128), server_default='count', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_units_dictionary_proposal_required_for_agent_source')),
    sa.CheckConstraint("dimension IN ('length','area','volume','count','time','mass','lump')", name=op.f('ck_units_dictionary_dimension_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_units_dictionary_source_known')),
    sa.PrimaryKeyConstraint('organization_id', 'code', name=op.f('pk_units_dictionary'))
    )
    op.create_index('ix_units_dictionary_org_code', 'units_dictionary', ['organization_id', 'code'], unique=False)
    op.create_table('client_contacts',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('full_name', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=128), server_default='', nullable=False),
    sa.Column('email', sa.String(length=128), server_default='', nullable=False),
    sa.Column('phone', sa.String(length=128), server_default='', nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_client_contacts_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_client_contacts_source_known')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], name=op.f('fk_client_contacts_client_id_clients')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_client_contacts'))
    )
    op.create_index('ix_client_contacts_org_client', 'client_contacts', ['organization_id', 'client_id'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=True),
    sa.Column('project_type', sa.String(length=128), server_default='construction', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='active', nullable=False),
    sa.Column('contract_value', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('address', sa.Text(), server_default='', nullable=False),
    sa.Column('timezone', sa.String(length=128), server_default='Asia/Ho_Chi_Minh', nullable=False),
    sa.Column('planned_start', sa.Date(), nullable=True),
    sa.Column('planned_completion', sa.Date(), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_projects_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_projects_source_known')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], name=op.f('fk_projects_client_id_clients')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_projects'))
    )
    op.create_index('ix_projects_org_code', 'projects', ['organization_id', 'code'], unique=True)
    op.create_index('ix_projects_org_status', 'projects', ['organization_id', 'status'], unique=False)
    op.create_table('project_phases',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('planned_start', sa.Date(), nullable=True),
    sa.Column('planned_end', sa.Date(), nullable=True),
    sa.Column('actual_start', sa.Date(), nullable=True),
    sa.Column('actual_end', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=128), server_default='planned', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_project_phases_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_project_phases_source_known')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_phases_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_phases'))
    )
    op.create_index('ix_project_phases_org_project', 'project_phases', ['organization_id', 'project_id', 'sequence'], unique=False)
    op.create_table('project_roles',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('role_key', sa.String(length=128), nullable=False),
    sa.Column('person_name', sa.String(length=255), nullable=False),
    sa.Column('person_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('is_primary', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('fallback_sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_project_roles_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_project_roles_source_known')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_roles_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_roles'))
    )
    op.create_index('ix_project_roles_org_project', 'project_roles', ['organization_id', 'project_id'], unique=False)
    op.create_index('ix_project_roles_org_role_key', 'project_roles', ['organization_id', 'role_key', 'project_id'], unique=False)
    op.create_table('wbs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_leaf', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_wbs_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_wbs_source_known')),
    sa.ForeignKeyConstraint(['parent_id'], ['wbs.id'], name=op.f('fk_wbs_parent_id_wbs')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_wbs_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wbs'))
    )
    op.create_index('ix_wbs_org_project', 'wbs', ['organization_id', 'project_id', 'sequence'], unique=False)
    op.create_index('ix_wbs_org_project_code', 'wbs', ['organization_id', 'project_id', 'code'], unique=True)
    op.create_table('zones',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('parent_zone_id', sa.String(length=40), nullable=True),
    sa.Column('area_m2', sa.Numeric(precision=18, scale=2), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_zones_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_zones_source_known')),
    sa.ForeignKeyConstraint(['parent_zone_id'], ['zones.id'], name=op.f('fk_zones_parent_zone_id_zones')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_zones_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_zones'))
    )
    op.create_index('ix_zones_org_project', 'zones', ['organization_id', 'project_id'], unique=False)
    op.create_index('ix_zones_org_project_code', 'zones', ['organization_id', 'project_id', 'code'], unique=True)
    op.create_table('milestones',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('wbs_id', sa.String(length=40), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=128), server_default='internal', nullable=False),
    sa.Column('gate_code', sa.String(length=128), server_default='', nullable=False),
    sa.Column('baseline_date', sa.Date(), nullable=True),
    sa.Column('forecast_date', sa.Date(), nullable=True),
    sa.Column('actual_date', sa.Date(), nullable=True),
    sa.Column('status', sa.String(length=128), server_default='pending', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_milestones_proposal_required_for_agent_source')),
    sa.CheckConstraint("kind IN ('contractual','internal','gate')", name=op.f('ck_milestones_milestone_kind_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_milestones_source_known')),
    sa.CheckConstraint("status IN ('pending','at_risk','achieved','missed')", name=op.f('ck_milestones_milestone_status_known')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_milestones_project_id_projects')),
    sa.ForeignKeyConstraint(['wbs_id'], ['wbs.id'], name=op.f('fk_milestones_wbs_id_wbs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_milestones'))
    )
    op.create_index('ix_milestones_org_gate', 'milestones', ['organization_id', 'gate_code'], unique=False)
    op.create_index('ix_milestones_org_project', 'milestones', ['organization_id', 'project_id'], unique=False)
    op.create_table('wbs_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('wbs_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('unit_rate', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('extraction_confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_wbs_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_wbs_items_source_known')),
    sa.CheckConstraint('extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)', name=op.f('ck_wbs_items_confidence_in_range')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_wbs_items_quantity_non_negative')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_wbs_items_organization_id_units_dictionary')),
    sa.ForeignKeyConstraint(['wbs_id'], ['wbs.id'], name=op.f('fk_wbs_items_wbs_id_wbs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_wbs_items'))
    )
    op.create_index('ix_wbs_items_org_wbs', 'wbs_items', ['organization_id', 'wbs_id'], unique=False)
    op.alter_column('tasks', 'procedure_fingerprint',
               existing_type=sa.VARCHAR(length=64),
               comment="Hash of the tool sequence and argument *shape*. Distinct from `fingerprint`, which hashes the goal's wording: two tasks can be different requests and the same procedure.",
               existing_nullable=True)



    # RLS last, so every table exists before any policy is created.
    # `transaction_per_migration` is on in `env.py`, so this either fully applies
    # or fully rolls back; a half-protected tranche is not a reachable state.
    for _table in TENANT_TABLES:
        _protect(_table)


def downgrade() -> None:
    # `DROP TABLE` takes its policies with it, so there is nothing to unprotect
    # explicitly. The drop order below is Alembic's reverse-dependency order, so
    # children go before parents.

    op.alter_column('tasks', 'procedure_fingerprint',
               existing_type=sa.VARCHAR(length=64),
               comment=None,
               existing_comment="Hash of the tool sequence and argument *shape*. Distinct from `fingerprint`, which hashes the goal's wording: two tasks can be different requests and the same procedure.",
               existing_nullable=True)
    op.drop_index('ix_wbs_items_org_wbs', table_name='wbs_items')
    op.drop_table('wbs_items')
    op.drop_index('ix_milestones_org_project', table_name='milestones')
    op.drop_index('ix_milestones_org_gate', table_name='milestones')
    op.drop_table('milestones')
    op.drop_index('ix_zones_org_project_code', table_name='zones')
    op.drop_index('ix_zones_org_project', table_name='zones')
    op.drop_table('zones')
    op.drop_index('ix_wbs_org_project_code', table_name='wbs')
    op.drop_index('ix_wbs_org_project', table_name='wbs')
    op.drop_table('wbs')
    op.drop_index('ix_project_roles_org_role_key', table_name='project_roles')
    op.drop_index('ix_project_roles_org_project', table_name='project_roles')
    op.drop_table('project_roles')
    op.drop_index('ix_project_phases_org_project', table_name='project_phases')
    op.drop_table('project_phases')
    op.drop_index('ix_projects_org_status', table_name='projects')
    op.drop_index('ix_projects_org_code', table_name='projects')
    op.drop_table('projects')
    op.drop_index('ix_client_contacts_org_client', table_name='client_contacts')
    op.drop_table('client_contacts')
    op.drop_index('ix_units_dictionary_org_code', table_name='units_dictionary')
    op.drop_table('units_dictionary')
    op.drop_index('ix_clients_org_name', table_name='clients')
    op.drop_index('ix_clients_org_code', table_name='clients')
    op.drop_table('clients')

