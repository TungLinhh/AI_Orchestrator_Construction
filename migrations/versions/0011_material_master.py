"""Material master and supplier due diligence.

Built from the corpus rather than from theory. `docs/SUPPLY_CHAIN_CORPUS.md`
records what was measured, and four findings shaped this migration.

**The 12-character material code does not exist in the data.** Tập 1 §4.3
specifies one — `[Hệ]-[Nhóm]-[Loại]-[Số]` across PW/LV/WD/AC/FP, with Procurement
the sole owner and duplicate codes prohibited. A regex scan of 400 workbooks for
code-shaped strings returned zero. What the sheets carry is `Mã Hiệu` =
`BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001`, which is a *drawing* number.

So `materials.code` is nullable. Making it NOT NULL would mean inventing codes for
the whole master — which Tập 1 prohibits, because the codes are Procurement's to
issue — or refusing to record materials we demonstrably have. The uniqueness
index is **partial** (`WHERE code IS NOT NULL`): several NULLs coexist, two
identical codes do not. That is "cấm tạo mã trùng" as a constraint rather than as
a review step somebody remembers.

**`drawing_ref` is not on `materials`.** A material appears on many drawings and
a drawing covers many materials. A single-valued column holding that is the first
material detailed on two drawings overwriting the first. The reference belongs on
the requisition line, which is where the corpus has it.

**`Mã Hiệu` is ambiguous in the source.** In a data row, columns 2 to 5 read
`SSA | | HBG | 2` under a header labelling only `STT`, `Mã Hiệu` and
`Tên vật tư`. The sub-header is not aligned with the columns, so those three
values cannot be mapped to meaning. `materials.raw_cells` holds them verbatim for
a human, because a parser that guessed would be inventing a supplier code.

**The material schedule is a timeline.** Seven dated fields per line — requested,
ordered, expected, actual, expected again, actual again, approved. Six dates
across 13 zones is a lot of state, and the question the sheet answers is "is this
late and by how much", which is a comparison across a timeline rather than a
reading of one field. `material_reconciliations` is therefore a table in the next
tranche, not four columns here.

## The supplier side, from FRM-MO-006A

Tập 3 §1.4 is five groups — legal, financial, capability, commercial, compliance
— producing a Bayesian score out of 100, a colour, an A/B/C classification, and
twelve months of validity. Two modelling decisions follow from that being a
*conclusion*:

* `supplier_risk_flags` is a list of named findings with a severity and a
  `basis`, not a score. The findings are the auditable part, and a score
  recomputed on read is a score nobody can reproduce when a rating is challenged.
* `supplier_assessments` is a *run*, not a column on the supplier. Same reason: a
  challenge has to be able to reproduce the number, including which per-group
  scores produced it.

Tập 1 §5.3's fourth forbidden zone lands here as `is_blocking` — on a risk flag
and on an assessment. It is a column rather than a rule about `severity` or
`rating` so that the freeze is a recorded fact and a second route to it can be
added without a code change.

The table bodies are Alembic's own output, generated rather than retyped, so this
migration and `persistence/supply.py` are the same schema by construction rather
than by review. Regenerate rather than edit.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "material_categories",
    "materials",
    "supplier_assessments",
    "supplier_documents",
    "supplier_risk_flags",

)

APP_ROLE = "ao_app"


def _protect(table: str) -> None:
    """Enable and force RLS, install the isolation policy, grant DML.

    Identical to what 0002 did for the original 44 tables and what 0007 did for
    the first construction tranche, including the `FORCE`. Without `FORCE` the
    owner bypasses the policy, and the owner is the role that ran this migration.
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
    op.create_table('material_categories',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('name_en', sa.String(length=255), server_default='', nullable=False),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('depth', sa.Integer(), server_default='0', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_material_categories_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_material_categories_source_known')),
    sa.CheckConstraint('depth >= 0 AND depth <= 3', name=op.f('ck_material_categories_depth_in_range')),
    sa.ForeignKeyConstraint(['parent_id'], ['material_categories.id'], name=op.f('fk_material_categories_parent_id_material_categories')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_material_categories'))
    )
    op.create_index('uq_material_categories_org_code', 'material_categories', ['organization_id', 'code'], unique=True)
    op.create_table('materials',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=True),
    sa.Column('name_vi', sa.String(length=255), nullable=False),
    sa.Column('name_en', sa.String(length=255), server_default='', nullable=False),
    sa.Column('category_id', sa.String(length=40), nullable=True),
    sa.Column('system_code', sa.String(length=4), nullable=True),
    sa.Column('specification', sa.Text(), server_default='', nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=True),
    sa.Column('reference_rate', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('raw_cells', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_materials_proposal_required_for_agent_source')),
    sa.CheckConstraint("code IS NULL OR code ~ '^(PW|LV|WD|AC|FP)-[A-Z0-9]{2,4}-[A-Z0-9]{2,4}-[0-9]{2,4}$'", name=op.f('ck_materials_code_matches_the_dossier_scheme')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_materials_source_known')),
    sa.CheckConstraint("system_code IS NULL OR system_code IN ('PW','LV','WD','AC','FP')", name=op.f('ck_materials_system_code_known')),
    sa.CheckConstraint('reference_rate IS NULL OR reference_rate >= 0', name=op.f('ck_materials_reference_rate_non_negative')),
    sa.ForeignKeyConstraint(['category_id'], ['material_categories.id'], name=op.f('fk_materials_category_id_material_categories')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_materials'))
    )
    op.create_index('ix_materials_org_category', 'materials', ['organization_id', 'category_id'], unique=False)
    op.create_index('ix_materials_org_name', 'materials', ['organization_id', 'name_vi'], unique=False)
    op.create_index('uq_materials_org_code_when_present', 'materials', ['organization_id', 'code'], unique=True, postgresql_where=sa.text('code IS NOT NULL'))
    op.create_table('supplier_assessments',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.String(length=40), nullable=False),
    sa.Column('assessed_on', sa.Date(), nullable=False),
    sa.Column('assessed_by', sa.String(length=128), nullable=False),
    sa.Column('group_scores', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('total_score', sa.Integer(), server_default='0', nullable=False),
    sa.Column('rating', sa.String(length=128), server_default='C', nullable=False),
    sa.Column('is_blocking', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('valid_for_months', sa.Integer(), server_default='12', nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('conclusion', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_supplier_assessments_proposal_required_for_agent_source')),
    sa.CheckConstraint("NOT is_blocking OR rating = 'C'", name=op.f('ck_supplier_assessments_a_blocking_assessment_is_red')),
    sa.CheckConstraint("rating <> 'C' OR is_blocking", name=op.f('ck_supplier_assessments_a_red_assessment_blocks')),
    sa.CheckConstraint("rating IN ('A','B','C')", name=op.f('ck_supplier_assessments_rating_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_supplier_assessments_source_known')),
    sa.CheckConstraint('total_score >= 0 AND total_score <= 100', name=op.f('ck_supplier_assessments_total_score_in_range')),
    sa.CheckConstraint('valid_for_months > 0', name=op.f('ck_supplier_assessments_valid_for_months_positive')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_supplier_assessments_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_assessments'))
    )
    op.create_index('ix_supplier_assessments_org_blocking', 'supplier_assessments', ['organization_id', 'is_blocking'], unique=False)
    op.create_index('ix_supplier_assessments_org_supplier_recent', 'supplier_assessments', ['organization_id', 'supplier_id', 'assessed_on'], unique=False)
    op.create_table('supplier_risk_flags',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.String(length=40), nullable=False),
    sa.Column('risk_kind', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('severity', sa.String(length=128), server_default='medium', nullable=False),
    sa.Column('is_blocking', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('basis', sa.Text(), server_default='', nullable=False),
    sa.Column('evidence_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('raised_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('raised_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution_note', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_supplier_risk_flags_proposal_required_for_agent_source')),
    sa.CheckConstraint("NOT is_blocking OR severity = 'blocking'", name=op.f('ck_supplier_risk_flags_blocking_is_the_top_severity')),
    sa.CheckConstraint("resolved_at IS NULL OR resolution_note <> ''", name=op.f('ck_supplier_risk_flags_a_resolution_states_why')),
    sa.CheckConstraint("risk_kind IN ('sanctioned','banned_equipment','duplicate_tax_code','abnormal_price','expired_qualification','quality_history','schedule_history','financial_concern','conflicting_interest','document_anomaly')", name=op.f('ck_supplier_risk_flags_risk_kind_known')),
    sa.CheckConstraint("severity IN ('low','medium','high','blocking')", name=op.f('ck_supplier_risk_flags_severity_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_supplier_risk_flags_source_known')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_supplier_risk_flags_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_risk_flags'))
    )
    op.create_index('ix_supplier_risk_flags_org_open_blocking', 'supplier_risk_flags', ['organization_id', 'is_blocking'], unique=False, postgresql_where=sa.text('resolved_at IS NULL'))
    op.create_index('ix_supplier_risk_flags_org_supplier', 'supplier_risk_flags', ['organization_id', 'supplier_id'], unique=False)
    op.create_table('supplier_documents',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.String(length=40), nullable=False),
    sa.Column('document_kind', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('document_id', sa.String(length=40), nullable=True),
    sa.Column('reference_no', sa.String(length=128), server_default='', nullable=False),
    sa.Column('issued_on', sa.Date(), nullable=True),
    sa.Column('valid_from', sa.Date(), nullable=True),
    sa.Column('valid_to', sa.Date(), nullable=True),
    sa.Column('is_verified', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('verified_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_supplier_documents_proposal_required_for_agent_source')),
    sa.CheckConstraint("NOT is_verified OR verified_by <> ''", name=op.f('ck_supplier_documents_a_verified_document_names_its_verifier')),
    sa.CheckConstraint("document_kind IN ('business_registration','tax_code','bank_confirmation','financial_statements','reference_letter','acceptance_record','iso_certificate','sample_certificate','anti_corruption_commitment','origin_declaration')", name=op.f('ck_supplier_documents_document_kind_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_supplier_documents_source_known')),
    sa.CheckConstraint('valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from', name=op.f('ck_supplier_documents_validity_window_is_not_inverted')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_supplier_documents_document_id_documents')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_supplier_documents_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_supplier_documents'))
    )
    op.create_index('ix_supplier_documents_org_expiry', 'supplier_documents', ['organization_id', 'valid_to'], unique=False)
    op.create_index('ix_supplier_documents_org_supplier', 'supplier_documents', ['organization_id', 'supplier_id'], unique=False)



    # RLS last, so every table exists before any policy is created.
    # `transaction_per_migration` is on in `migrations/env.py`, so this either
    # fully applies or fully rolls back; a half-protected tranche is not a
    # reachable state.
    for _table in TENANT_TABLES:
        _protect(_table)


def downgrade() -> None:
    # `DROP TABLE` takes its policies with it, so there is nothing to unprotect
    # explicitly. The drop order below is Alembic's own reverse-dependency
    # order, so children go before parents.

    op.drop_index('ix_supplier_documents_org_supplier', table_name='supplier_documents')
    op.drop_index('ix_supplier_documents_org_expiry', table_name='supplier_documents')
    op.drop_table('supplier_documents')
    op.drop_index('ix_supplier_risk_flags_org_supplier', table_name='supplier_risk_flags')
    op.drop_index('ix_supplier_risk_flags_org_open_blocking', table_name='supplier_risk_flags', postgresql_where=sa.text('resolved_at IS NULL'))
    op.drop_table('supplier_risk_flags')
    op.drop_index('ix_supplier_assessments_org_supplier_recent', table_name='supplier_assessments')
    op.drop_index('ix_supplier_assessments_org_blocking', table_name='supplier_assessments')
    op.drop_table('supplier_assessments')
    op.drop_index('uq_materials_org_code_when_present', table_name='materials', postgresql_where=sa.text('code IS NOT NULL'))
    op.drop_index('ix_materials_org_name', table_name='materials')
    op.drop_index('ix_materials_org_category', table_name='materials')
    op.drop_table('materials')
    op.drop_index('uq_material_categories_org_code', table_name='material_categories')
    op.drop_table('material_categories')

