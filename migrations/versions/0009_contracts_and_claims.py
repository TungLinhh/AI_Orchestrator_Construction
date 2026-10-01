"""Contracts, variations, claims, and the supplier master.

The signed agreement and everything that happens to it after signature. This
tranche closes the chain `0008` left open: a `bid` now has something to become,
and `projects.contract_value` — documented as agreeing with the signed contract
— finally has a contract to agree with.

Three decisions here are the reason this is a module rather than seven tables.

No `change_orders` table
A variation and a change order are the same object before and after pricing.
Modelling them as two tables guarantees a period in which the instruction lives
in one and its priced form in the other, with nothing joining them. One table,
distinguished by `status`.

Claimed and approved, both, for value and for time
`value_claimed`/`value_approved` and `days_claimed`/`days_approved`. **The gap
between them is the money.** A variation instructed in writing and valued at
nothing is a receivable that appears in no report unless somebody is looking for
it, and it is the most common way margin disappears on a project.
`instructed-but-unvalued` is a single indexed query against this table.

`claim_events` is append-only, because a claim is not a row, it is a chronology
Contracts are usually lost on procedure rather than merit: notice late, assessment
never issued, correspondence never filed. A `claims` row with four date columns
cannot represent "the letter went out on the 4th, chased on the 19th, assessed on
the 2nd". Events are added and never edited, because a rewritable chronology
proves nothing.

One `contracts` table with a party direction
A client contract and a supplier contract are the same kind of document with the
same clauses, milestones and variation machinery. Splitting them duplicates every
column and makes every report a union. A check constraint requires **exactly one**
of `client_id` and `supplier_id`, so "a contract with nobody" and "a contract with
both" are refused rather than filtered out downstream.

`contract_clauses` is what was **read**; `contracts` is what was **agreed**
The Contract Intelligence service writes clauses here, with a text excerpt and a
mandatory confidence for agent-written rows. It does not write
`contracts.retention_pct`. That is the platform's one rule: an agent emits a
proposal, a person confirms, and a person's confirmation is what lands on the
contract record. So the terms reports and gates compute on are columns, and the
provenance of how they were read lives next to the sentence they were read from.

`contracts` enforces what a *status* means, not just which values are legal
`status='signed'` requires a `signed_at`; `status='approved'` on a supplier
requires current qualification papers; a payment amount may only sit on a
payment milestone. All three would otherwise be states a row reaches by accident,
and "approved" is exactly the word that makes somebody stop looking.

The table bodies are Alembic's own output, generated rather than retyped, so this
migration and `persistence/contracts.py` are the same schema by construction
rather than by review. Regenerate rather than edit.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "claim_events",
    "contract_claims",
    "contract_clauses",
    "contract_milestones",
    "contract_variations",
    "contracts",
    "suppliers",

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
    op.create_table('suppliers',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('short_name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('tax_code', sa.String(length=128), server_default='', nullable=False),
    sa.Column('category', sa.String(length=128), server_default='materials', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='prospect', nullable=False),
    sa.Column('address', sa.Text(), server_default='', nullable=False),
    sa.Column('country', sa.String(length=128), server_default='VN', nullable=False),
    sa.Column('email', sa.String(length=128), server_default='', nullable=False),
    sa.Column('phone', sa.String(length=128), server_default='', nullable=False),
    sa.Column('contact_name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('payment_terms_days', sa.Numeric(precision=8, scale=0), server_default='30', nullable=False),
    sa.Column('approved_bank_account_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('approved_bank_account_last4', sa.String(length=4), server_default='', nullable=False),
    sa.Column('qualification_expires_on', sa.Date(), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_suppliers_proposal_required_for_agent_source')),
    sa.CheckConstraint("category IN ('materials','equipment','subcontractor','services','labour','transport','other')", name=op.f('ck_suppliers_category_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_suppliers_source_known')),
    sa.CheckConstraint("status <> 'approved' OR (qualification_expires_on IS NOT NULL     AND qualification_expires_on >= CURRENT_DATE)", name=op.f('ck_suppliers_an_approved_supplier_is_qualified')),
    sa.CheckConstraint("status IN ('prospect','under_review','approved','conditional','suspended','rejected','blacklisted')", name=op.f('ck_suppliers_status_known')),
    sa.CheckConstraint('payment_terms_days >= 0', name=op.f('ck_suppliers_payment_terms_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_suppliers'))
    )
    op.create_index('ix_suppliers_org_category', 'suppliers', ['organization_id', 'category'], unique=False)
    op.create_index('ix_suppliers_org_expiry', 'suppliers', ['organization_id', 'qualification_expires_on'], unique=False)
    op.create_index('ix_suppliers_org_status', 'suppliers', ['organization_id', 'status'], unique=False)
    op.create_index('uq_suppliers_org_code', 'suppliers', ['organization_id', 'code'], unique=True)
    op.create_table('contracts',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=True),
    sa.Column('client_id', sa.String(length=40), nullable=True),
    sa.Column('supplier_id', sa.String(length=40), nullable=True),
    sa.Column('party_role', sa.String(length=128), server_default='employer', nullable=False),
    sa.Column('contract_type', sa.String(length=128), server_default='works', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('title', sa.String(length=255), server_default='', nullable=False),
    sa.Column('signed_at', sa.Date(), nullable=True),
    sa.Column('effective_from', sa.Date(), nullable=True),
    sa.Column('effective_to', sa.Date(), nullable=True),
    sa.Column('contract_value', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('retention_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('liquidated_damages_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('advance_payment_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('payment_terms_days', sa.Numeric(precision=8, scale=0), nullable=True),
    sa.Column('warranty_months', sa.Numeric(precision=8, scale=0), nullable=True),
    sa.Column('document_id', sa.String(length=40), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_contracts_proposal_required_for_agent_source')),
    sa.CheckConstraint("contract_type IN ('works','supply','services','framework','consultancy')", name=op.f('ck_contracts_contract_type_known')),
    sa.CheckConstraint("party_role IN ('employer','supplier','subcontractor','consultant')", name=op.f('ck_contracts_party_role_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_contracts_source_known')),
    sa.CheckConstraint("status IN ('draft','for_review','approved','signed','active','suspended','terminated','completed','closed')", name=op.f('ck_contracts_status_known')),
    sa.CheckConstraint("status NOT IN ('signed','active','completed','closed') OR signed_at IS NOT NULL", name=op.f('ck_contracts_signed_requires_a_date')),
    sa.CheckConstraint('(client_id IS NULL) <> (supplier_id IS NULL)', name=op.f('ck_contracts_exactly_one_counterparty')),
    sa.CheckConstraint('COALESCE(retention_pct, 0) + COALESCE(advance_payment_pct, 0) <= 100', name=op.f('ck_contracts_retention_plus_advance_within_contract_value')),
    sa.CheckConstraint('advance_payment_pct IS NULL OR (advance_payment_pct >= 0 AND advance_payment_pct <= 100)', name=op.f('ck_contracts_advance_payment_in_range')),
    sa.CheckConstraint('liquidated_damages_pct IS NULL OR (liquidated_damages_pct >= 0 AND liquidated_damages_pct <= 100)', name=op.f('ck_contracts_liquidated_damages_in_range')),
    sa.CheckConstraint('payment_terms_days IS NULL OR payment_terms_days >= 0', name=op.f('ck_contracts_payment_terms_non_negative')),
    sa.CheckConstraint('retention_pct IS NULL OR (retention_pct >= 0 AND retention_pct <= 100)', name=op.f('ck_contracts_retention_in_range')),
    sa.CheckConstraint('warranty_months IS NULL OR warranty_months >= 0', name=op.f('ck_contracts_warranty_non_negative')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], name=op.f('fk_contracts_client_id_clients')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_contracts_document_id_documents')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_contracts_project_id_projects')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_contracts_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contracts'))
    )
    op.create_index('ix_contracts_org_client', 'contracts', ['organization_id', 'client_id'], unique=False)
    op.create_index('ix_contracts_org_project', 'contracts', ['organization_id', 'project_id'], unique=False)
    op.create_index('ix_contracts_org_status', 'contracts', ['organization_id', 'status'], unique=False)
    op.create_index('ix_contracts_org_supplier', 'contracts', ['organization_id', 'supplier_id'], unique=False)
    op.create_index('uq_contracts_org_code', 'contracts', ['organization_id', 'code'], unique=True)
    op.create_table('contract_clauses',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('contract_id', sa.String(length=40), nullable=False),
    sa.Column('category', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('text_excerpt', sa.Text(), server_default='', nullable=False),
    sa.Column('extracted_value', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('is_favourable', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('deviation_score', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('status', sa.String(length=128), server_default='extracted', nullable=False),
    sa.Column('source_page', sa.Numeric(precision=6, scale=0), nullable=True),
    sa.Column('extraction_confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('reviewed_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('review_note', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_contract_clauses_proposal_required_for_agent_source')),
    sa.CheckConstraint("category IN ('payment','retention','penalty','warranty','liability','scope','termination','force_majeure','dispute','other')", name=op.f('ck_contract_clauses_category_known')),
    sa.CheckConstraint("source <> 'agent_proposal' OR extraction_confidence IS NOT NULL", name=op.f('ck_contract_clauses_agent_row_needs_a_confidence')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_contract_clauses_source_known')),
    sa.CheckConstraint("status <> 'confirmed' OR (reviewed_by <> '' AND reviewed_at IS NOT NULL)", name=op.f('ck_contract_clauses_a_confirmed_clause_names_its_reviewer')),
    sa.CheckConstraint("status IN ('extracted','confirmed','rejected')", name=op.f('ck_contract_clauses_status_known')),
    sa.CheckConstraint('deviation_score IS NULL OR (deviation_score >= 0 AND deviation_score <= 100)', name=op.f('ck_contract_clauses_deviation_score_in_range')),
    sa.CheckConstraint('extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)', name=op.f('ck_contract_clauses_confidence_in_range')),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], name=op.f('fk_contract_clauses_contract_id_contracts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_clauses'))
    )
    op.create_index('ix_contract_clauses_org_category', 'contract_clauses', ['organization_id', 'category'], unique=False)
    op.create_index('ix_contract_clauses_org_contract', 'contract_clauses', ['organization_id', 'contract_id'], unique=False)
    op.create_index('ix_contract_clauses_org_review_queue', 'contract_clauses', ['organization_id', 'status', 'extraction_confidence'], unique=False)
    op.create_table('contract_milestones',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('contract_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='pending', nullable=False),
    sa.Column('sequence', sa.Numeric(precision=6, scale=0), server_default='0', nullable=False),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('achieved_date', sa.Date(), nullable=True),
    sa.Column('amount', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(kind = 'payment') = (amount IS NOT NULL)", name=op.f('ck_contract_milestones_amount_only_on_payment_milestones')),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_contract_milestones_proposal_required_for_agent_source')),
    sa.CheckConstraint("kind IN ('payment','delivery','progress','acceptance','warranty','mobilisation','demobilisation','other')", name=op.f('ck_contract_milestones_kind_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_contract_milestones_source_known')),
    sa.CheckConstraint("status IN ('pending','due','achieved','overdue','waived')", name=op.f('ck_contract_milestones_status_known')),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], name=op.f('fk_contract_milestones_contract_id_contracts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_milestones'))
    )
    op.create_index('ix_contract_milestones_org_due', 'contract_milestones', ['organization_id', 'contract_id', 'due_date'], unique=False)
    op.create_index('ix_contract_milestones_org_kind', 'contract_milestones', ['organization_id', 'kind', 'status'], unique=False)
    op.create_index('uq_contract_milestones_org_code', 'contract_milestones', ['organization_id', 'contract_id', 'code'], unique=True)
    op.create_table('contract_variations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('contract_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('variation_type', sa.String(length=128), server_default='scope', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='proposed', nullable=False),
    sa.Column('originator', sa.String(length=128), server_default='employer', nullable=False),
    sa.Column('instructed_at', sa.Date(), nullable=True),
    sa.Column('raised_at', sa.Date(), nullable=True),
    sa.Column('settled_at', sa.Date(), nullable=True),
    sa.Column('value_claimed', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('value_approved', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('days_claimed', sa.Numeric(precision=8, scale=0), server_default='0', nullable=False),
    sa.Column('days_approved', sa.Numeric(precision=8, scale=0), server_default='0', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_contract_variations_proposal_required_for_agent_source')),
    sa.CheckConstraint("originator IN ('employer','contractor')", name=op.f('ck_contract_variations_originator_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_contract_variations_source_known')),
    sa.CheckConstraint("status IN ('proposed','instructed','assessed','approved','rejected','executed','valued','settled')", name=op.f('ck_contract_variations_status_known')),
    sa.CheckConstraint("status NOT IN ('instructed','assessed','approved','executed','valued','settled') OR instructed_at IS NOT NULL", name=op.f('ck_contract_variations_a_privileged_variation_was_instructed')),
    sa.CheckConstraint("variation_type IN ('quantity','scope','design','price','time','omission')", name=op.f('ck_contract_variations_variation_type_known')),
    sa.CheckConstraint('days_claimed >= 0 AND days_approved >= 0', name=op.f('ck_contract_variations_days_non_negative')),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], name=op.f('fk_contract_variations_contract_id_contracts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_variations'))
    )
    op.create_index('ix_contract_variations_org_status', 'contract_variations', ['organization_id', 'contract_id', 'status'], unique=False)
    op.create_index('uq_contract_variations_org_code', 'contract_variations', ['organization_id', 'contract_id', 'code'], unique=True)
    op.create_table('contract_claims',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('contract_id', sa.String(length=40), nullable=False),
    sa.Column('variation_id', sa.String(length=40), nullable=True),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('claim_type', sa.String(length=128), server_default='both', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('event_date', sa.Date(), nullable=True),
    sa.Column('raised_at', sa.Date(), nullable=True),
    sa.Column('notified_at', sa.Date(), nullable=True),
    sa.Column('assessed_at', sa.Date(), nullable=True),
    sa.Column('settled_at', sa.Date(), nullable=True),
    sa.Column('amount_claimed', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('amount_awarded', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('days_claimed', sa.Numeric(precision=8, scale=0), server_default='0', nullable=False),
    sa.Column('days_awarded', sa.Numeric(precision=8, scale=0), server_default='0', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_contract_claims_proposal_required_for_agent_source')),
    sa.CheckConstraint("claim_type IN ('time','money','both','defect','delay','other')", name=op.f('ck_contract_claims_claim_type_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_contract_claims_source_known')),
    sa.CheckConstraint("status = 'draft' OR notified_at IS NOT NULL", name=op.f('ck_contract_claims_a_submitted_claim_was_notified')),
    sa.CheckConstraint("status IN ('draft','notified','assessed','submitted','accepted','rejected','settled','withdrawn')", name=op.f('ck_contract_claims_status_known')),
    sa.CheckConstraint('days_claimed >= 0 AND days_awarded >= 0', name=op.f('ck_contract_claims_days_non_negative')),
    sa.CheckConstraint('notified_at IS NULL OR event_date IS NULL OR notified_at >= event_date', name=op.f('ck_contract_claims_notice_is_not_before_the_event')),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], name=op.f('fk_contract_claims_contract_id_contracts')),
    sa.ForeignKeyConstraint(['variation_id'], ['contract_variations.id'], name=op.f('fk_contract_claims_variation_id_contract_variations')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_contract_claims'))
    )
    op.create_index('ix_contract_claims_org_status', 'contract_claims', ['organization_id', 'contract_id', 'status'], unique=False)
    op.create_index('ix_contract_claims_org_unnotified', 'contract_claims', ['organization_id', 'notified_at', 'event_date'], unique=False)
    op.create_index('uq_contract_claims_org_code', 'contract_claims', ['organization_id', 'contract_id', 'code'], unique=True)
    op.create_table('claim_events',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('claim_id', sa.String(length=40), nullable=False),
    sa.Column('event_type', sa.String(length=128), server_default='note', nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('title', sa.String(length=255), server_default='', nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('evidence_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('recorded_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_claim_events_proposal_required_for_agent_source')),
    sa.CheckConstraint("event_type IN ('notice','correspondence','meeting','assessment','submission','decision','payment','note')", name=op.f('ck_claim_events_event_type_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_claim_events_source_known')),
    sa.ForeignKeyConstraint(['claim_id'], ['contract_claims.id'], name=op.f('fk_claim_events_claim_id_contract_claims')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_claim_events'))
    )
    op.create_index('ix_claim_events_org_claim_occurred', 'claim_events', ['organization_id', 'claim_id', 'occurred_at'], unique=False)
    op.create_index('ix_claim_events_org_type', 'claim_events', ['organization_id', 'event_type'], unique=False)



    # RLS last, so every table exists before any policy is created.
    # `transaction_per_migration` is on in `migrations/env.py`, so this either
    # fully applies or fully rolls back; a half-protected tranche is not a
    # reachable state.
    for _table in TENANT_TABLES:
        _protect(_table)

    # `claim_events` is the same shape of table as `audit_logs`: a chronology that
    # can be edited is not evidence of anything. Migration 0002 revoked UPDATE and
    # DELETE from the audit ledger for exactly that reason, and this does the same
    # for the claim chronology.
    #
    # The module docstring claimed append-only and this is what makes it true.
    # The alternative — trusting the application never to update an event — is a
    # convention, and a convention is exactly what a dispute is argued about:
    # "your chronology shows the notice on the 4th, our copy shows the 9th" is
    # settled by whether the row can be changed after the fact.
    op.execute("REVOKE UPDATE, DELETE ON claim_events FROM ao_app")
    op.execute("GRANT SELECT, INSERT ON claim_events TO ao_app")


def downgrade() -> None:
    # `DROP TABLE` takes its policies with it, so there is nothing to unprotect
    # explicitly. The drop order below is Alembic's own reverse-dependency
    # order, so children go before parents.

    op.drop_index('ix_claim_events_org_type', table_name='claim_events')
    op.drop_index('ix_claim_events_org_claim_occurred', table_name='claim_events')
    op.drop_table('claim_events')
    op.drop_index('uq_contract_claims_org_code', table_name='contract_claims')
    op.drop_index('ix_contract_claims_org_unnotified', table_name='contract_claims')
    op.drop_index('ix_contract_claims_org_status', table_name='contract_claims')
    op.drop_table('contract_claims')
    op.drop_index('uq_contract_variations_org_code', table_name='contract_variations')
    op.drop_index('ix_contract_variations_org_status', table_name='contract_variations')
    op.drop_table('contract_variations')
    op.drop_index('uq_contract_milestones_org_code', table_name='contract_milestones')
    op.drop_index('ix_contract_milestones_org_kind', table_name='contract_milestones')
    op.drop_index('ix_contract_milestones_org_due', table_name='contract_milestones')
    op.drop_table('contract_milestones')
    op.drop_index('ix_contract_clauses_org_review_queue', table_name='contract_clauses')
    op.drop_index('ix_contract_clauses_org_contract', table_name='contract_clauses')
    op.drop_index('ix_contract_clauses_org_category', table_name='contract_clauses')
    op.drop_table('contract_clauses')
    op.drop_index('uq_contracts_org_code', table_name='contracts')
    op.drop_index('ix_contracts_org_supplier', table_name='contracts')
    op.drop_index('ix_contracts_org_status', table_name='contracts')
    op.drop_index('ix_contracts_org_project', table_name='contracts')
    op.drop_index('ix_contracts_org_client', table_name='contracts')
    op.drop_table('contracts')
    op.drop_index('uq_suppliers_org_code', table_name='suppliers')
    op.drop_index('ix_suppliers_org_status', table_name='suppliers')
    op.drop_index('ix_suppliers_org_expiry', table_name='suppliers')
    op.drop_index('ix_suppliers_org_category', table_name='suppliers')
    op.drop_table('suppliers')

