"""Requisition to goods receipt: the chain that makes the 3-way match arithmetic.

Ten tables, and the structural decision that holds them together is one sentence:
**the documents are referentially connected.** A receipt names a purchase order, a
purchase order names a quotation, a quotation names a requisition, and every
line traces back to the requisition line it answers. The 3-way match is then a
join of rows that cannot be wrong independently, rather than three numbers
somebody typed into a form.

The corpus's own payment form (`FRM-BO-002A`) asks for "số PO; số GRN; số hóa
đơn" as free text and says in the same row that the system fills in the match.
This migration is what "fills in" means.

Four measurements shaped the columns, and each of them would have been got wrong
by a design that had not opened the files.

**Deliveries and payments happen in installments.** The payment form carries
"Lần thanh toán/giao hàng: Đợt 2" — round 2. A contract is not delivered once and
paid once; it is drawn down in rounds, each with its own quantity and its own
retention calculation. `purchase_orders.installment_count`,
`current_installment` and `goods_receipts.installment_no` exist for that. Modelled
as one quantity and one receipt, a contract would look right for round one and be
wrong for every round after it.

**Origin and brand are separate values.** The GRN sheet has `Nhãn hiệu/Brandname`
and `Xuất xứ/C/O` as two columns; the price appendix collapses them into one
"Nhà sản xuất / Xuất xứ". The sheet is right and the appendix is lossy, so
`brand` and `origin_country` are separate columns here. A receipt recording
"Cooper / UK" as one string cannot answer "is this locally manufactured", which
is a compliance question and occasionally a tariff one.

**There are no material codes in the corpus.** A scan of 400 workbooks for
code-shaped strings returned only `IP20`, `4000K`, `1F`-`8F` and `T2`-`T7` — an
ingress rating, a colour temperature, floor numbers and type marks. Every line
here therefore references a material by id, never by a code typed in a
spreadsheet, and the `Mã Hiệu` drawing reference sits on `rfq_items` where the
corpus has it: on the line, not on the material.

**Units are inconsistently cased.** `Bộ` 17 times and `bộ` 7; `Cái` 34 and `cái`
6. So every unit references `units_dictionary` by its ASCII code and the corpus's
Vietnamese label is mapped at ingest. Copying it would make `Bộ` and `bộ` two
units and a quantity reconciliation would fail on a capital letter.

## Two deliberately copied numbers

`purchase_orders.retention_pct` copies the contract's rate rather than
referencing it. This is the one place in the schema a number is knowingly
duplicated, and the reason is that the retention actually withheld is a term of
*this order as issued*. A contract amended after the order was placed must not
retroactively change what was withheld, and the corpus's own amendment sheet
("Đề nghị điều chỉnh nội dung hợp đồng") shows those amendments happening.

`quotations.market_variance_pct` is stored rather than recomputed because Tập 3
§1.4 requires an explanation for a quote more than 15% from market, and an
explanation attached to a number nobody kept is not reviewable. The check
`a_large_variance_states_why` makes the explanation mandatory above the
dossier's own threshold.

## What the acceptance rule reaches outside procurement

`goods_receipts` cannot reach `accepted` without quality documents attached, a QA
sign-off **and** an HSE confirmation. Tập 3 §1.5 requires all three for
subcontract deliveries. Three separate booleans rather than one
`documents_attached`, because the last two are sign-offs from two different
departments and a single flag would let either substitute for the other.

The table bodies are Alembic's own output, generated rather than retyped, so this
migration and `persistence/procurement.py` are the same schema by construction.
Regenerate rather than edit.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every table this migration creates, derived from the file rather than typed
#: in, so "protected" and "created" cannot drift apart. Every one of these is
#: organisation-scoped, including the ones that do not look like business tables
#: such as a controlled vocabulary: a policy on those is meaningful too.
TENANT_TABLES = (
    "goods_receipts",
    "material_reconciliations",
    "po_items",
    "purchase_orders",
    "quotation_items",
    "quotations",
    "receipt_checks",
    "receipt_items",
    "rfq_items",
    "rfqs",

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
    op.create_table('rfqs',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('title', sa.String(length=255), server_default='', nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('contract_id', sa.String(length=40), nullable=True),
    sa.Column('system_code', sa.String(length=4), nullable=True),
    sa.Column('zone_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('package_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('requested_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('requested_on', sa.Date(), nullable=True),
    sa.Column('required_on', sa.Date(), nullable=True),
    sa.Column('is_long_lead', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_rfqs_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_rfqs_source_known')),
    sa.CheckConstraint("status IN ('draft','issued','partially_quoted','quoted','closed','cancelled')", name=op.f('ck_rfqs_status_known')),
    sa.CheckConstraint("system_code IS NULL OR system_code IN ('PW','LV','WD','AC','FP')", name=op.f('ck_rfqs_system_code_known')),
    sa.ForeignKeyConstraint(['contract_id'], ['contracts.id'], name=op.f('fk_rfqs_contract_id_contracts')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_rfqs_project_id_projects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rfqs'))
    )
    op.create_index('ix_rfqs_org_long_lead', 'rfqs', ['organization_id', 'is_long_lead'], unique=False)
    op.create_index('ix_rfqs_org_project', 'rfqs', ['organization_id', 'project_id', 'status'], unique=False)
    op.create_index('uq_rfqs_org_code', 'rfqs', ['organization_id', 'code'], unique=True)
    op.create_table('quotations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('rfq_id', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='received', nullable=False),
    sa.Column('quoted_on', sa.Date(), nullable=True),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('includes_tax', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('tax_rate_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('payment_terms_days', sa.Numeric(precision=8, scale=0), nullable=True),
    sa.Column('market_variance_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('market_variance_note', sa.Text(), server_default='', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_quotations_proposal_required_for_agent_source')),
    sa.CheckConstraint("market_variance_pct IS NULL OR abs(market_variance_pct) <= 15.0 OR market_variance_note <> ''", name=op.f('ck_quotations_a_large_variance_states_why')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_quotations_source_known')),
    sa.CheckConstraint("status IN ('received','under_review','accepted','rejected','withdrawn')", name=op.f('ck_quotations_status_known')),
    sa.CheckConstraint('market_variance_pct IS NULL OR abs(market_variance_pct) <= 100', name=op.f('ck_quotations_variance_in_range')),
    sa.CheckConstraint('valid_until IS NULL OR quoted_on IS NULL OR valid_until >= quoted_on', name=op.f('ck_quotations_validity_window_is_not_inverted')),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfqs.id'], name=op.f('fk_quotations_rfq_id_rfqs')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_quotations_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quotations'))
    )
    op.create_index('ix_quotations_org_rfq', 'quotations', ['organization_id', 'rfq_id'], unique=False)
    op.create_index('ix_quotations_org_supplier', 'quotations', ['organization_id', 'supplier_id'], unique=False)
    op.create_index('uq_quotations_org_code', 'quotations', ['organization_id', 'code'], unique=True)
    op.create_table('rfq_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('rfq_id', sa.String(length=40), nullable=False),
    sa.Column('material_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('drawing_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('line_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_rfq_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_rfq_items_source_known')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_rfq_items_quantity_positive')),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], name=op.f('fk_rfq_items_material_id_materials')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_rfq_items_organization_id_units_dictionary')),
    sa.ForeignKeyConstraint(['rfq_id'], ['rfqs.id'], name=op.f('fk_rfq_items_rfq_id_rfqs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_rfq_items'))
    )
    op.create_index('ix_rfq_items_org_rfq', 'rfq_items', ['organization_id', 'rfq_id'], unique=False)
    op.create_index('uq_rfq_items_org_rfq_line', 'rfq_items', ['organization_id', 'rfq_id', 'line_no'], unique=True)
    op.create_table('purchase_orders',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('quotation_id', sa.String(length=40), nullable=False),
    sa.Column('project_id', sa.String(length=40), nullable=False),
    sa.Column('supplier_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('issued_on', sa.Date(), nullable=True),
    sa.Column('required_on', sa.Date(), nullable=True),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('subtotal', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('retention_pct', sa.Numeric(precision=6, scale=3), nullable=True),
    sa.Column('retention_amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('installment_count', sa.Integer(), server_default='1', nullable=False),
    sa.Column('current_installment', sa.Integer(), server_default='1', nullable=False),
    sa.Column('approved_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_purchase_orders_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_purchase_orders_source_known')),
    sa.CheckConstraint("status IN ('draft','pending_approval','approved','issued','partially_received','received','cancelled','closed')", name=op.f('ck_purchase_orders_status_known')),
    sa.CheckConstraint("status NOT IN ('issued','partially_received','received','closed') OR approved_by <> ''", name=op.f('ck_purchase_orders_an_issued_order_names_its_approver')),
    sa.CheckConstraint('current_installment <= installment_count', name=op.f('ck_purchase_orders_installment_within_the_order')),
    sa.CheckConstraint('current_installment >= 1', name=op.f('ck_purchase_orders_installment_positive')),
    sa.CheckConstraint('installment_count >= 1', name=op.f('ck_purchase_orders_installment_count_positive')),
    sa.CheckConstraint('retention_amount >= 0', name=op.f('ck_purchase_orders_retention_non_negative')),
    sa.CheckConstraint('retention_pct IS NULL OR (retention_pct >= 0 AND retention_pct <= 100)', name=op.f('ck_purchase_orders_retention_in_range')),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_purchase_orders_project_id_projects')),
    sa.ForeignKeyConstraint(['quotation_id'], ['quotations.id'], name=op.f('fk_purchase_orders_quotation_id_quotations')),
    sa.ForeignKeyConstraint(['supplier_id'], ['suppliers.id'], name=op.f('fk_purchase_orders_supplier_id_suppliers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_purchase_orders'))
    )
    op.create_index('ix_purchase_orders_org_project', 'purchase_orders', ['organization_id', 'project_id', 'status'], unique=False)
    op.create_index('ix_purchase_orders_org_quotation', 'purchase_orders', ['organization_id', 'quotation_id'], unique=False)
    op.create_index('ix_purchase_orders_org_supplier', 'purchase_orders', ['organization_id', 'supplier_id'], unique=False)
    op.create_index('uq_purchase_orders_org_code', 'purchase_orders', ['organization_id', 'code'], unique=True)
    op.create_table('quotation_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('quotation_id', sa.String(length=40), nullable=False),
    sa.Column('rfq_item_id', sa.String(length=40), nullable=False),
    sa.Column('material_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('brand', sa.String(length=128), server_default='', nullable=False),
    sa.Column('origin_country', sa.String(length=128), server_default='', nullable=False),
    sa.Column('manufacturer', sa.String(length=128), server_default='', nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('unit_rate', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('lead_time_days', sa.Numeric(precision=8, scale=0), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('line_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_quotation_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_quotation_items_source_known')),
    sa.CheckConstraint('lead_time_days IS NULL OR lead_time_days >= 0', name=op.f('ck_quotation_items_lead_time_non_negative')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_quotation_items_quantity_positive')),
    sa.CheckConstraint('unit_rate >= 0', name=op.f('ck_quotation_items_unit_rate_non_negative')),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], name=op.f('fk_quotation_items_material_id_materials')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_quotation_items_organization_id_units_dictionary')),
    sa.ForeignKeyConstraint(['quotation_id'], ['quotations.id'], name=op.f('fk_quotation_items_quotation_id_quotations')),
    sa.ForeignKeyConstraint(['rfq_item_id'], ['rfq_items.id'], name=op.f('fk_quotation_items_rfq_item_id_rfq_items')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_quotation_items'))
    )
    op.create_index('ix_quotation_items_org_quotation', 'quotation_items', ['organization_id', 'quotation_id'], unique=False)
    op.create_index('ix_quotation_items_org_rfq_item', 'quotation_items', ['organization_id', 'rfq_item_id'], unique=False)
    op.create_index('uq_quotation_items_org_quotation_line', 'quotation_items', ['organization_id', 'quotation_id', 'line_no'], unique=True)
    op.create_table('goods_receipts',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('code', sa.String(length=128), nullable=False),
    sa.Column('po_id', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=128), server_default='draft', nullable=False),
    sa.Column('installment_no', sa.Integer(), server_default='1', nullable=False),
    sa.Column('received_on', sa.Date(), nullable=True),
    sa.Column('received_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('supplier_delivery_note', sa.String(length=128), server_default='', nullable=False),
    sa.Column('quality_documents_attached', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('qa_accepted', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('hse_confirmed', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_goods_receipts_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_goods_receipts_source_known')),
    sa.CheckConstraint("status <> 'accepted' OR (quality_documents_attached AND qa_accepted AND hse_confirmed)", name=op.f('ck_goods_receipts_acceptance_requires_quality_and_qa_hse_signoff')),
    sa.CheckConstraint("status <> 'accepted' OR accepted_at IS NOT NULL", name=op.f('ck_goods_receipts_an_accepted_receipt_says_when')),
    sa.CheckConstraint("status IN ('draft','under_inspection','accepted','rejected','cancelled')", name=op.f('ck_goods_receipts_status_known')),
    sa.CheckConstraint('installment_no >= 1', name=op.f('ck_goods_receipts_installment_positive')),
    sa.ForeignKeyConstraint(['po_id'], ['purchase_orders.id'], name=op.f('fk_goods_receipts_po_id_purchase_orders')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_goods_receipts'))
    )
    op.create_index('ix_goods_receipts_org_po', 'goods_receipts', ['organization_id', 'po_id'], unique=False)
    op.create_index('ix_goods_receipts_org_received', 'goods_receipts', ['organization_id', 'received_on'], unique=False)
    op.create_index('uq_goods_receipts_org_code', 'goods_receipts', ['organization_id', 'code'], unique=True)
    op.create_table('po_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('purchase_order_id', sa.String(length=40), nullable=False),
    sa.Column('quotation_item_id', sa.String(length=40), nullable=False),
    sa.Column('material_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('ordered_quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('received_quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('unit_rate', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=6), server_default='0', nullable=False),
    sa.Column('currency_code', sa.String(length=128), server_default='VND', nullable=False),
    sa.Column('line_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('notes', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_po_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_po_items_source_known')),
    sa.CheckConstraint('ordered_quantity > 0', name=op.f('ck_po_items_ordered_quantity_positive')),
    sa.CheckConstraint('received_quantity >= 0 AND received_quantity <= ordered_quantity', name=op.f('ck_po_items_received_within_ordered')),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], name=op.f('fk_po_items_material_id_materials')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_po_items_organization_id_units_dictionary')),
    sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name=op.f('fk_po_items_purchase_order_id_purchase_orders')),
    sa.ForeignKeyConstraint(['quotation_item_id'], ['quotation_items.id'], name=op.f('fk_po_items_quotation_item_id_quotation_items')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_po_items'))
    )
    op.create_index('ix_po_items_org_po', 'po_items', ['organization_id', 'purchase_order_id'], unique=False)
    op.create_index('uq_po_items_org_po_line', 'po_items', ['organization_id', 'purchase_order_id', 'line_no'], unique=True)
    op.create_table('material_reconciliations',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('po_item_id', sa.String(length=40), nullable=False),
    sa.Column('step', sa.String(length=128), nullable=False),
    sa.Column('planned_on', sa.Date(), nullable=True),
    sa.Column('actual_on', sa.Date(), nullable=True),
    sa.Column('zone_ref', sa.String(length=128), server_default='', nullable=False),
    sa.Column('owner_role_key', sa.String(length=128), server_default='', nullable=False),
    sa.Column('note', sa.Text(), server_default='', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_material_reconciliations_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_material_reconciliations_source_known')),
    sa.CheckConstraint("step <> 'actual_delivery_2nd' OR actual_on IS NULL OR planned_on IS NOT NULL", name=op.f('ck_material_reconciliations_a_retry_presumes_an_original')),
    sa.CheckConstraint("step IN ('requested_by_client','ordered_by_procurement','expected_delivery','actual_delivery','expected_delivery_2nd','actual_delivery_2nd','approved')", name=op.f('ck_material_reconciliations_step_known')),
    sa.ForeignKeyConstraint(['po_item_id'], ['po_items.id'], name=op.f('fk_material_reconciliations_po_item_id_po_items')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_material_reconciliations'))
    )
    op.create_index('ix_material_reconciliations_org_late', 'material_reconciliations', ['organization_id', 'step', 'planned_on'], unique=False)
    op.create_index('uq_material_reconciliations_org_step', 'material_reconciliations', ['organization_id', 'po_item_id', 'step'], unique=True)
    op.create_table('receipt_checks',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('goods_receipt_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('method', sa.Text(), server_default='', nullable=False),
    sa.Column('result', sa.Text(), server_default='', nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=True),
    sa.Column('checked_by', sa.String(length=128), server_default='', nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('remark', sa.Text(), server_default='', nullable=False),
    sa.Column('sequence', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_receipt_checks_proposal_required_for_agent_source')),
    sa.CheckConstraint("passed IS NOT FALSE OR remark <> ''", name=op.f('ck_receipt_checks_a_failed_check_says_why')),
    sa.CheckConstraint("passed IS NOT NULL OR checked_by = ''", name=op.f('ck_receipt_checks_an_unchecked_line_names_nobody')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_receipt_checks_source_known')),
    sa.CheckConstraint('passed IS NULL OR checked_at IS NOT NULL', name=op.f('ck_receipt_checks_a_performed_check_says_when')),
    sa.ForeignKeyConstraint(['goods_receipt_id'], ['goods_receipts.id'], name=op.f('fk_receipt_checks_goods_receipt_id_goods_receipts')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receipt_checks'))
    )
    op.create_index('ix_receipt_checks_org_receipt', 'receipt_checks', ['organization_id', 'goods_receipt_id', 'sequence'], unique=False)
    op.create_table('receipt_items',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('goods_receipt_id', sa.String(length=40), nullable=False),
    sa.Column('po_item_id', sa.String(length=40), nullable=False),
    sa.Column('material_id', sa.String(length=40), nullable=False),
    sa.Column('description', sa.Text(), server_default='', nullable=False),
    sa.Column('brand', sa.String(length=128), server_default='', nullable=False),
    sa.Column('origin_country', sa.String(length=128), server_default='', nullable=False),
    sa.Column('unit_code', sa.String(length=128), nullable=False),
    sa.Column('received_quantity', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('variance_reason', sa.String(length=128), nullable=True),
    sa.Column('variance_note', sa.Text(), server_default='', nullable=False),
    sa.Column('line_no', sa.Integer(), server_default='0', nullable=False),
    sa.Column('organization_id', sa.String(length=40), nullable=False),
    sa.Column('source', sa.String(length=128), server_default='human', nullable=False),
    sa.Column('source_actor', sa.String(length=128), server_default='', nullable=False),
    sa.Column('proposal_id', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(source = 'agent_proposal') = (proposal_id IS NOT NULL)", name=op.f('ck_receipt_items_proposal_required_for_agent_source')),
    sa.CheckConstraint("source IN ('human','agent_proposal','import','system')", name=op.f('ck_receipt_items_source_known')),
    sa.CheckConstraint("variance_reason IS NULL OR variance_reason IN ('price','quantity','quality','document')", name=op.f('ck_receipt_items_variance_reason_known')),
    sa.CheckConstraint('received_quantity > 0', name=op.f('ck_receipt_items_received_quantity_positive')),
    sa.ForeignKeyConstraint(['goods_receipt_id'], ['goods_receipts.id'], name=op.f('fk_receipt_items_goods_receipt_id_goods_receipts')),
    sa.ForeignKeyConstraint(['material_id'], ['materials.id'], name=op.f('fk_receipt_items_material_id_materials')),
    sa.ForeignKeyConstraint(['organization_id', 'unit_code'], ['units_dictionary.organization_id', 'units_dictionary.code'], name=op.f('fk_receipt_items_organization_id_units_dictionary')),
    sa.ForeignKeyConstraint(['po_item_id'], ['po_items.id'], name=op.f('fk_receipt_items_po_item_id_po_items')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receipt_items'))
    )
    op.create_index('ix_receipt_items_org_po_item', 'receipt_items', ['organization_id', 'po_item_id'], unique=False)
    op.create_index('ix_receipt_items_org_receipt', 'receipt_items', ['organization_id', 'goods_receipt_id'], unique=False)
    op.create_index('uq_receipt_items_org_receipt_line', 'receipt_items', ['organization_id', 'goods_receipt_id', 'line_no'], unique=True)



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

    op.drop_index('uq_receipt_items_org_receipt_line', table_name='receipt_items')
    op.drop_index('ix_receipt_items_org_receipt', table_name='receipt_items')
    op.drop_index('ix_receipt_items_org_po_item', table_name='receipt_items')
    op.drop_table('receipt_items')
    op.drop_index('ix_receipt_checks_org_receipt', table_name='receipt_checks')
    op.drop_table('receipt_checks')
    op.drop_index('uq_material_reconciliations_org_step', table_name='material_reconciliations')
    op.drop_index('ix_material_reconciliations_org_late', table_name='material_reconciliations')
    op.drop_table('material_reconciliations')
    op.drop_index('uq_po_items_org_po_line', table_name='po_items')
    op.drop_index('ix_po_items_org_po', table_name='po_items')
    op.drop_table('po_items')
    op.drop_index('uq_goods_receipts_org_code', table_name='goods_receipts')
    op.drop_index('ix_goods_receipts_org_received', table_name='goods_receipts')
    op.drop_index('ix_goods_receipts_org_po', table_name='goods_receipts')
    op.drop_table('goods_receipts')
    op.drop_index('uq_quotation_items_org_quotation_line', table_name='quotation_items')
    op.drop_index('ix_quotation_items_org_rfq_item', table_name='quotation_items')
    op.drop_index('ix_quotation_items_org_quotation', table_name='quotation_items')
    op.drop_table('quotation_items')
    op.drop_index('uq_purchase_orders_org_code', table_name='purchase_orders')
    op.drop_index('ix_purchase_orders_org_supplier', table_name='purchase_orders')
    op.drop_index('ix_purchase_orders_org_quotation', table_name='purchase_orders')
    op.drop_index('ix_purchase_orders_org_project', table_name='purchase_orders')
    op.drop_table('purchase_orders')
    op.drop_index('uq_rfq_items_org_rfq_line', table_name='rfq_items')
    op.drop_index('ix_rfq_items_org_rfq', table_name='rfq_items')
    op.drop_table('rfq_items')
    op.drop_index('uq_quotations_org_code', table_name='quotations')
    op.drop_index('ix_quotations_org_supplier', table_name='quotations')
    op.drop_index('ix_quotations_org_rfq', table_name='quotations')
    op.drop_table('quotations')
    op.drop_index('uq_rfqs_org_code', table_name='rfqs')
    op.drop_index('ix_rfqs_org_project', table_name='rfqs')
    op.drop_index('ix_rfqs_org_long_lead', table_name='rfqs')
    op.drop_table('rfqs')

