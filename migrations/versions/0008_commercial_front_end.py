"""Commercial front end: opportunity, tender, bid.

The part of the business that decides whether a contract will exist. Tranche 1
started at `projects`, which assumes a signed contract; this tranche is upstream
of that assumption.

Two decisions here are about how an AI service's output gets reviewed, which is
the first time that question appears in the schema.

`TenderRequirement` keeps `source_page`, `source_span` and `extraction_confidence`
A tender has a hundred requirements and a human can check forty in an afternoon.
The ones that will disqualify the bid are the ones that did not look strange, so
the reviewer has to be comparing the model's answer against the sentence it came
from rather than against their memory of the document. `source_span` holds the
clipped text the model actually read: enough to judge, not enough to reconstruct
the document, and deliberately not a hash — a hash would make the comparison
impossible.

Confidence is **mandatory** for a model-written row, by check constraint
(`agent_row_needs_a_confidence`). The review queue is ordered by it, so a row
without one cannot be placed in that queue: it looks recorded and will not be
read. The reverse is deliberately not required — a human-entered requirement has
no model confidence, and inventing one would poison the number the whole review
process sorts by.

A rejected extraction is kept, not deleted
`status='rejected'` with `review_note`. A deleted wrong answer is a wrong answer
the next run will produce again, because nothing survived to correct it.

`BidItem` is a separate table from `WbsItem`
A bid is a position taken before any WBS exists. One table with a nullable
`bid_id` would mean every query carries a `WHERE bid_id IS NOT NULL` that
somebody eventually forgets. On award the lines are copied across, and that
copy is an auditable event rather than a mutation.

Money is `NUMERIC`; units are referenced through a composite foreign key over
`(organization_id, unit_code)`, so a rate cannot be quoted against a unit
belonging to another tenant. Same rule as `wbs_items`, same reason: a rate
applied to the wrong unit is a wrong number that nothing downstream can catch.

`Bids.margin_pct` is allowed far below -100
A bid priced at a -140% margin is a number the gate must be able to *store* in
order to refuse it. Clamping the column would hide exactly the bids that should
never be submitted.

The table bodies are Alembic's own output, generated rather than retyped, so this
migration and `persistence/commercial.py` are the same schema by construction
rather than by review. Regenerate rather than edit.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "bid_items",
    "bids",
    "opportunities",
    "tender_documents",
    "tender_requirements",
    "tenders",

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
    op.create_table('opportunities',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('project_type', sa.String(length=128), server_default='construction', nullable=False),
    sa.Column('stage', sa.String(length=128), server_default='lead', nullable=False),
    sa.Column('expected_value', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('probability_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('owner_name', sa.String(length=128), server_default='', nullable=False),
    sa.Column('bid_deadline', sa.Date(), nullable=True),
    sa.Column('lead_source', sa.String(length=128), server_default='', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_opportunities_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_opportunities_source_known')),
    sa.CheckConstraint("stage IN ('lead','qualified','tender_received','bidding','submitted','won','lost','abandoned')", name=op.f('ck_opportunities_stage_known')),
    sa.CheckConstraint('probability_pct IS NULL OR (probability_pct >= 0 AND probability_pct <= 100)', name=op.f('ck_opportunities_probability_in_range')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], name=op.f('fk_opportunities_client_id_clients')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_opportunities'))
    )
    op.create_index('ix_opportunities_org_client', 'opportunities', ['organization_id', 'client_id'], unique=False)
    op.create_index('ix_opportunities_org_code', 'opportunities', ['organization_id', 'code'], unique=True)
    op.create_index('ix_opportunities_org_deadline', 'opportunities', ['organization_id', 'bid_deadline'], unique=False)
    op.create_index('ix_opportunities_org_stage', 'opportunities', ['organization_id', 'stage'], unique=False)
    op.create_table('tenders',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('opportunity_id', sa.String(length=40), nullable=True),
    sa.Column('client_id', sa.String(length=40), nullable=False),
    sa.Column('reference_no', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='received', nullable=False),
    sa.Column('received_at', sa.Date(), nullable=True),
    sa.Column('closing_at', sa.Date(), nullable=True),
    sa.Column('language', sa.String(length=128), server_default='vi', nullable=False),
    sa.Column('contract_value', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_tenders_proposal_required_for_agent_source')),
    sa.CheckConstraint("language IN ('vi','en','both')", name=op.f('ck_tenders_language_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_tenders_source_known')),
    sa.CheckConstraint("status IN ('received','under_review','extracted','priced','submitted','withdrawn')", name=op.f('ck_tenders_status_known')),
    sa.ForeignKeyConstraint(['client_id'], ['clients.id'], name=op.f('fk_tenders_client_id_clients')),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], name=op.f('fk_tenders_opportunity_id_opportunities')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tenders'))
    )
    op.create_index('ix_tenders_org_closing', 'tenders', ['organization_id', 'closing_at'], unique=False)
    op.create_index('ix_tenders_org_opportunity', 'tenders', ['organization_id', 'opportunity_id'], unique=False)
    op.create_index('ix_tenders_org_reference', 'tenders', ['organization_id', 'reference_no'], unique=True)
    op.create_index('ix_tenders_org_status', 'tenders', ['organization_id', 'status'], unique=False)
    op.create_table('bids',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('opportunity_id', sa.String(length=40), nullable=True),
    sa.Column('tender_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('bid_value', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('margin_amount', sa.Numeric(precision=18, scale=6), nullable=True),
    sa.Column('margin_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decision_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_bids_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_bids_source_known')),
    sa.CheckConstraint("status IN ('draft','priced','internal_approved','submitted','won','lost','withdrawn')", name=op.f('ck_bids_status_known')),
    sa.ForeignKeyConstraint(['opportunity_id'], ['opportunities.id'], name=op.f('fk_bids_opportunity_id_opportunities')),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_bids_tender_id_tenders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bids'))
    )
    op.create_index('ix_bids_org_code', 'bids', ['organization_id', 'code'], unique=True)
    op.create_index('ix_bids_org_status', 'bids', ['organization_id', 'status'], unique=False)
    op.create_index('ix_bids_org_tender', 'bids', ['organization_id', 'tender_id'], unique=False)
    op.create_table('tender_documents',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('tender_id', sa.String(length=40), nullable=False),
    sa.Column('document_id', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('extraction_status', sa.String(length=128), server_default='pending', nullable=False),
    sa.Column('page_count', sa.Integer(), nullable=True),
    sa.Column('is_scanned', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('extracted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_tender_documents_proposal_required_for_agent_source')),
    sa.CheckConstraint("extraction_status IN ('pending','classified','extracted','needs_review','failed')", name=op.f('ck_tender_documents_extraction_status_known')),
    sa.CheckConstraint("role IN ('invitation','scope_of_work','bill_of_quantities','drawings','evaluation_criteria','contract_draft','qualification','submission_form','other')", name=op.f('ck_tender_documents_role_known')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_tender_documents_source_known')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_tender_documents_document_id_documents')),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_tender_documents_tender_id_tenders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tender_documents'))
    )
    op.create_index('ix_tender_documents_org_status', 'tender_documents', ['organization_id', 'extraction_status'], unique=False)
    op.create_index('ix_tender_documents_org_tender', 'tender_documents', ['organization_id', 'tender_id'], unique=False)
    op.create_index('uq_tender_documents_org_tender_doc', 'tender_documents', ['organization_id', 'tender_id', 'document_id'], unique=True)
    op.create_table('bid_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('bid_id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('unit_rate', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('is_optional', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('extraction_confidence', sa.Numeric(precision=5, scale=4), nullable=True),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_bid_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source <> 'agent_proposal' OR extraction_confidence IS NOT NULL", name=op.f('ck_bid_items_agent_row_needs_a_confidence')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_bid_items_source_known')),
    sa.CheckConstraint('extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)', name=op.f('ck_bid_items_confidence_in_range')),
    sa.CheckConstraint('quantity >= 0', name=op.f('ck_bid_items_quantity_non_negative')),
    sa.ForeignKeyConstraint(['bid_id'], ['bids.id'], name=op.f('fk_bid_items_bid_id_bids')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_bid_items_organization_id_units_dictionary')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bid_items'))
    )
    op.create_index('uq_bid_items_org_bid_code', 'bid_items', ['organization_id', 'bid_id', 'code'], unique=True)
    op.create_table('tender_requirements',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('tender_id', sa.String(length=40), nullable=False),
    sa.Column('tender_document_id', sa.String(length=40), nullable=True),
    sa.Column('kind', sa.String(length=128), server_default='other', nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('detail', sa.Text(), server_default='', nullable=False),
    sa.Column('is_mandatory', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('severity', sa.String(length=128), server_default='medium', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='extracted', nullable=False),
    sa.Column('source_page', sa.Integer(), nullable=True),
    sa.Column('source_span', sa.Text(), server_default='', nullable=False),
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
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_tender_requirements_proposal_required_for_agent_source')),
    sa.CheckConstraint("kind IN ('scope','qualification','commercial','technical','schedule','warranty','penalty','other')", name=op.f('ck_tender_requirements_kind_known')),
    sa.CheckConstraint("severity IN ('informational','low','medium','high','critical')", name=op.f('ck_tender_requirements_severity_known')),
    sa.CheckConstraint("source <> 'agent_proposal' OR extraction_confidence IS NOT NULL", name=op.f('ck_tender_requirements_agent_row_needs_a_confidence')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_tender_requirements_source_known')),
    sa.CheckConstraint("status IN ('extracted','confirmed','rejected')", name=op.f('ck_tender_requirements_status_known')),
    sa.CheckConstraint('extraction_confidence IS NULL OR (extraction_confidence >= 0 AND extraction_confidence <= 1)', name=op.f('ck_tender_requirements_confidence_in_range')),
    sa.ForeignKeyConstraint(['tender_document_id'], ['tender_documents.id'], name=op.f('fk_tender_requirements_tender_document_id_tender_documents')),
    sa.ForeignKeyConstraint(['tender_id'], ['tenders.id'], name=op.f('fk_tender_requirements_tender_id_tenders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tender_requirements'))
    )
    op.create_index('ix_tender_requirements_org_mandatory', 'tender_requirements', ['organization_id', 'tender_id', 'is_mandatory'], unique=False)
    op.create_index('ix_tender_requirements_org_review_queue', 'tender_requirements', ['organization_id', 'status', 'extraction_confidence'], unique=False)
    op.create_index('ix_tender_requirements_org_tender', 'tender_requirements', ['organization_id', 'tender_id'], unique=False)



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

    op.drop_index('ix_tender_requirements_org_tender', table_name='tender_requirements')
    op.drop_index('ix_tender_requirements_org_review_queue', table_name='tender_requirements')
    op.drop_index('ix_tender_requirements_org_mandatory', table_name='tender_requirements')
    op.drop_table('tender_requirements')
    op.drop_index('uq_bid_items_org_bid_code', table_name='bid_items')
    op.drop_table('bid_items')
    op.drop_index('uq_tender_documents_org_tender_doc', table_name='tender_documents')
    op.drop_index('ix_tender_documents_org_tender', table_name='tender_documents')
    op.drop_index('ix_tender_documents_org_status', table_name='tender_documents')
    op.drop_table('tender_documents')
    op.drop_index('ix_bids_org_tender', table_name='bids')
    op.drop_index('ix_bids_org_status', table_name='bids')
    op.drop_index('ix_bids_org_code', table_name='bids')
    op.drop_table('bids')
    op.drop_index('ix_tenders_org_status', table_name='tenders')
    op.drop_index('ix_tenders_org_reference', table_name='tenders')
    op.drop_index('ix_tenders_org_opportunity', table_name='tenders')
    op.drop_index('ix_tenders_org_closing', table_name='tenders')
    op.drop_table('tenders')
    op.drop_index('ix_opportunities_org_stage', table_name='opportunities')
    op.drop_index('ix_opportunities_org_deadline', table_name='opportunities')
    op.drop_index('ix_opportunities_org_code', table_name='opportunities')
    op.drop_index('ix_opportunities_org_client', table_name='opportunities')
    op.drop_table('opportunities')

