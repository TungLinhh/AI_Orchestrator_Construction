"""Requisition, quotation, purchase order, goods receipt.

The transaction flow around the material master, built from the corpus. Five
measurements from `docs/SUPPLY_CHAIN_CORPUS.md` shaped it, and three of them
would have been got wrong by a design that had not looked at the files.

**The three documents are referentially connected, and that is the whole point.**
A GRN names a PO; a PO names a quotation; a quotation names an RFQ. The 3-way
match is therefore a *join* of three rows that cannot be wrong independently,
rather than three numbers somebody typed into a form. The corpus's own payment
form (`FRM-BO-002A`) asks for "số PO; số GRN; số hóa đơn" as free text — and Tập
3 §1.5 says the system fills in the match. Making the chain referential is what
turns the match from a comparison into arithmetic, and it is why a payment cannot
be approved against a PO that was never received from.

**Deliveries and payments happen in installments.** The payment form carries
"Lần thanh toán/giao hàng: Đợt 2" — round 2. A contract is not delivered once
and paid once; it is drawn down in rounds, each with its own quantity, its own
acceptance and its own retention calculation. A single `po_items.quantity` with a
single receipt would model one round and silently lose the rest, and the
retention arithmetic would be wrong for every round after the first.

**Origin and brand are separate values.** The GRN sheet has `Nhãn hiệu/Brandname`
and `Xuất xứ/C/O` as two columns; the price appendix collapses them into
`Nhà sản xuất / Xuất xứ`. The sheet is right and the appendix is lossy, so this
module keeps them apart. A receipt that says "Cooper / UK" as one string cannot
answer "is this locally manufactured", which is a compliance question and
occasionally a tariff one.

**There are no material codes in the corpus.** A scan of 400 workbooks for
code-shaped strings found only `IP20`, `4000K`, `1F`-`8F` and `T2`-`T7` — an
ingress rating, a colour temperature, floor numbers and type marks. Nothing that
is a material master. Hence `materials.code` is nullable with a partial unique
index, and every line here references a material by id rather than by a code
typed in a spreadsheet.

**Units are inconsistently cased.** `Bộ` 17 times, `bộ` 7; `Cái` 34, `cái` 6. So
every unit on a line references `units_dictionary` by its ASCII code and the
corpus's Vietnamese label is mapped at ingest rather than copied. A copy would
make `Bộ` and `bộ` two units, and a quantity reconciliation would fail on a
capital letter.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import MONEY, Base
from ai_orchestrator.persistence.construction import (
    LONG,
    NAME,
    QUANTITY,
    RATE,
    SHORT,
    ConstructionMixin,
    domain_args,
)

#: Days. Payment terms, quotation validity, credit — all small numbers where a
#: days column invites a unit mistake.
DAYS = Numeric(8, 0)
#: A percentage, for tolerance and retention. 6,3 matches the money convention.
PERCENT = Numeric(6, 3)

RFQ_STATUSES = (
    "draft",
    "issued",
    "partially_quoted",
    "quoted",
    "closed",
    "cancelled",
)

#: Tập 3 §1.4 group 4: a quote more than 15% from the market needs an
#: explanation. That is a *rule about a comparison*, so the comparison is stored
#: rather than recomputed from prices nobody kept.
MARKET_VARIANCE_THRESHOLD_PCT = 15.0

QUOTATION_STATUSES = ("received", "under_review", "accepted", "rejected", "withdrawn")

#: A purchase order's lifecycle. `closed` is reached from `partially_received`,
#: not from `issued`: an order is only closed once its receipts account for it or
#: a decision cancelled the balance.
PO_STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "issued",
    "partially_received",
    "received",
    "cancelled",
    "closed",
)

#: Tập 3 §1.5's reason codes. A variance outside tolerance is one of these, and
#: the code determines whether the invoice is paid, queried or rejected — so it
#: is not a free-text note.
VARIANCE_REASON_CODES = ("price", "quantity", "quality", "document")

#: The seven dated steps a material line moves through, measured from the
#: `VẬT TƯ <zone>` sheets. This is a timeline rather than a set of columns
#: because the question the sheet answers is "is this late and by how much",
#: which is a comparison across the whole sequence.
RECONCILIATION_STEPS = (
    "requested_by_client",
    "ordered_by_procurement",
    "expected_delivery",
    "actual_delivery",
    "expected_delivery_2nd",
    "actual_delivery_2nd",
    "approved",
)

RECEIPT_STATUSES = ("draft", "under_inspection", "accepted", "rejected", "cancelled")


class Rfq(ConstructionMixin, Base):
    """A request for quotation — the requisition that starts the chain.

    `package_ref` and `zone_ref` are the two things a requisition in this corpus
    is always scoped to: a work package and a physical zone. The material sheets
    are organised by zone and the drawings are organised by package, and a
    requisition that does not carry both cannot be checked against either.

    Free text rather than a foreign key, because a requisition legitimately
    arrives naming a drawing that has not been uploaded yet. A foreign key would
    refuse the document that starts the chain.
    """

    __tablename__ = "rfqs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    project_id: Mapped[str] = mapped_column(String(40), nullable=False)
    contract_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Which block of scope this buys. Tập 1 §4.3's first code segment; also the
    #: second segment of `materials.system_code`, so a requisition and its lines
    #: are checked against the same vocabulary.
    system_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    zone_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    package_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    requested_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    requested_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    required_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: `Tập 1 §3.5` G3's exit criterion is "PO chính trong ngân sách" and
    #: "không NCC ngoài danh mục phê duyệt", so a requisition is explicitly
    #: long-lead-aware. A long-lead item is one the programme cannot absorb a slip
    #: in, and it is the reason this table exists separately from a PO.
    is_long_lead: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "contract_id"],
            ["contracts.organization_id", "contracts.id"],
            name="fk_rfqs_contract_id_contracts",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_rfqs_project_id_projects",
        ),
        Index(
            "uq_rfqs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_rfqs_org_long_lead",
            "organization_id",
            "is_long_lead",
        ),
        Index(
            "ix_rfqs_org_project",
            "organization_id",
            "project_id",
            "status",
        ),
        Index(
            "uq_rfqs_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'issued'::character varying, 'partially_quoted'::character varying, "
            "'quoted'::character varying, 'closed'::character varying, "
            "'cancelled'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((system_code IS NULL) OR ((system_code)::text = ANY ((ARRAY['PW'::character va"
            "rying, 'LV'::character varying, 'WD'::character varying, "
            "'AC'::character varying, 'FP'::character varying])::text[]))))",
            name="system_code_known",
        ),
    )


class RfqItem(ConstructionMixin, Base):
    """One line of a requisition: a material, a quantity, a unit.

    No price. A requisition asks *what*; `quotation_items` is where the price
    arrives, and a requisition that carried one would let a price exist before a
    supplier quoted it — which is the 3-way match's first line of defence.
    """

    __tablename__ = "rfq_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    rfq_id: Mapped[str] = mapped_column(String(40), nullable=False)
    material_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: ASCII code from `units_dictionary`, never the spreadsheet's `Bộ`/`bộ`.
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    quantity: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0, server_default="0")
    #: The drawing this line is detailed on. The corpus has `Mã Hiệu` =
    #: `BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001` in exactly this position, and it
    #: belongs on the line rather than on the material because a material is
    #: detailed on many drawings.
    drawing_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "material_id"],
            ["materials.organization_id", "materials.id"],
            name="fk_rfq_items_material_id_materials",
        ),
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            name="fk_rfq_items_rfq_id_rfqs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_rfq_items_organization_id_units_dictionary",
        ),
        Index(
            "uq_rfq_items_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_rfq_items_org_rfq",
            "organization_id",
            "rfq_id",
        ),
        Index(
            "uq_rfq_items_org_rfq_line",
            "organization_id",
            "rfq_id",
            "line_no",
            unique=True,
        ),
        CheckConstraint(
            "((quantity > (0)::numeric))",
            name="quantity_positive",
        ),
    )

    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class Quotation(ConstructionMixin, Base):
    """A supplier's response to an RFQ.

    `valid_until` is not a formality. The corpus's own contract-amendment sheet
    justifies a price change with "Căn cứ Báo giá số 160 ngày" — a 160-day
    quotation. A purchase order raised against an expired quotation is being
    priced on terms the supplier no longer offers, and the check that prevents it
    has to be a date on the quotation.

    `market_variance_pct` is stored rather than derived because Tập 3 §1.4
    requires an explanation for anything more than 15% out, and an explanation
    attached to a number nobody kept is not reviewable.
    """

    __tablename__ = "quotations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    rfq_id: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="received", server_default="received"
    )
    quoted_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    total_amount: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    #: The whole quotation, or the lines. A supplier quotes a total and we
    #: allocate it, and recording which we did is the difference between a
    #: reconcilable document and an unexplained total.
    includes_tax: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    tax_rate_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    #: Tập 3 §1.4: over 15% from the market requires an explanation.
    market_variance_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    market_variance_note: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "rfq_id"],
            ["rfqs.organization_id", "rfqs.id"],
            name="fk_quotations_rfq_id_rfqs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_quotations_supplier_id_suppliers",
        ),
        Index(
            "uq_quotations_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_quotations_org_rfq",
            "organization_id",
            "rfq_id",
        ),
        Index(
            "ix_quotations_org_supplier",
            "organization_id",
            "supplier_id",
        ),
        Index(
            "uq_quotations_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((market_variance_pct IS NULL) OR (abs(market_variance_pct) <= 15.0) OR (market"
            "_variance_note <> ''::text)))",
            name="a_large_variance_states_why",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['received'::character varying, "
            "'under_review'::character varying, 'accepted'::character varying, "
            "'rejected'::character varying, 'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((market_variance_pct IS NULL) OR (abs(market_variance_pct) <= (100)::numeric)))",
            name="variance_in_range",
        ),
        CheckConstraint(
            "(((valid_until IS NULL) OR (quoted_on IS NULL) OR (valid_until >= quoted_on)))",
            name="validity_window_is_not_inverted",
        ),
    )


class QuotationItem(ConstructionMixin, Base):
    """One priced line of a quotation.

    `amount` is stored rather than derived from `quantity * unit_rate`, because
    Tập 1's own variation rules acknowledge that they do not always agree — a
    daywork line or a provisional sum genuinely does not multiply out. Storing
    both lets a line be *checked* rather than assumed, and `wbs_items` carries
    the same pair for the same reason.
    """

    __tablename__ = "quotation_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    quotation_id: Mapped[str] = mapped_column(String(40), nullable=False)
    rfq_item_id: Mapped[str] = mapped_column(String(40), nullable=False)
    material_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: The GRN sheet keeps `Nhãn hiệu` and `Xuất xứ` apart; the price appendix
    #: collapses them. Separate columns, because "is this locally made" is a
    #: compliance question the collapsed string cannot answer.
    brand: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    origin_country: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    manufacturer: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    quantity: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0, server_default="0")
    unit_rate: Mapped[float] = mapped_column(RATE, nullable=False, default=0, server_default="0")
    amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    #: Delivery lead time in days. A quotation without one cannot be scheduled,
    #: and the material sheets track expected against actual delivery precisely
    #: because somebody committed to a date.
    lead_time_days: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Declared with the other columns rather than after `__table_args__`. The
    #: index below names it as a *string*, so either order works; this one means a
    #: reader does not have to look past the constraints to find out what
    #: `line_no` is.
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "material_id"],
            ["materials.organization_id", "materials.id"],
            name="fk_quotation_items_material_id_materials",
        ),
        ForeignKeyConstraint(
            ["organization_id", "quotation_id"],
            ["quotations.organization_id", "quotations.id"],
            name="fk_quotation_items_quotation_id_quotations",
        ),
        ForeignKeyConstraint(
            ["organization_id", "rfq_item_id"],
            ["rfq_items.organization_id", "rfq_items.id"],
            name="fk_quotation_items_rfq_item_id_rfq_items",
        ),
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_quotation_items_organization_id_units_dictionary",
        ),
        Index(
            "uq_quotation_items_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_quotation_items_org_quotation",
            "organization_id",
            "quotation_id",
        ),
        Index(
            "ix_quotation_items_org_rfq_item",
            "organization_id",
            "rfq_item_id",
        ),
        Index(
            "uq_quotation_items_org_quotation_line",
            "organization_id",
            "quotation_id",
            "line_no",
            unique=True,
        ),
        CheckConstraint(
            "(((lead_time_days IS NULL) OR (lead_time_days >= (0)::numeric)))",
            name="lead_time_non_negative",
        ),
        CheckConstraint(
            "((quantity > (0)::numeric))",
            name="quantity_positive",
        ),
        CheckConstraint(
            "((unit_rate >= (0)::numeric))",
            name="unit_rate_non_negative",
        ),
    )


__all__ = [
    "DAYS",
    "MARKET_VARIANCE_THRESHOLD_PCT",
    "PERCENT",
    "PO_STATUSES",
    "QUOTATION_STATUSES",
    "RECEIPT_STATUSES",
    "RECONCILIATION_STEPS",
    "RFQ_STATUSES",
    "VARIANCE_REASON_CODES",
    "Quotation",
    "QuotationItem",
    "Rfq",
    "RfqItem",
]


# ============================================================================
# Step 3: purchase order, goods receipt, and the material timeline
# ============================================================================


class PurchaseOrder(ConstructionMixin, Base):
    """The order placed on a supplier.

    `quotation_id` is NOT NULL, and that is the decision the whole chain rests
    on. A purchase order without a quotation has no agreed price, no lead time
    and no validity date, so a payment against it cannot be matched to anything
    and the 3-way match becomes three numbers somebody typed. The corpus's own
    payment form asks for the PO and quotation references as free text and says
    the system fills in the match — this is what "fills in" means.

    `installment_count` and `current_installment` exist because the payment form
    carries "Lần thanh toán/giao hàng: Đợt 2". Deliveries and payments happen
    in rounds, each with its own quantity and its own retention calculation.
    Modelled as a single `quantity` with a single receipt, a contract would look
    right for round one and wrong for every round after it.

    `retention_pct` is a *copy* of the contract's rate rather than a reference to
    it. That is deliberate and it is the one place in this schema a number is
    knowingly duplicated: the retention actually withheld is a term of *this*
    order as issued, and a contract amendment after the order was placed must not
    retroactively change what was withheld. `contracts.retention_pct` remains the
    source of truth for a *new* order.
    """

    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(40), nullable=False)
    project_id: Mapped[str] = mapped_column(String(40), nullable=False)
    supplier_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    issued_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    required_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    subtotal: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    tax_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    total_amount: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    #: Deliberately copied from the contract rather than referenced. See the
    #: class docstring: an order's retention is the term it was issued with.
    retention_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    retention_amount: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    #: Total rounds this order is delivered in. `1` for a single-delivery order.
    installment_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    #: "Đợt 2" — which round this order is for. `1` for a single-delivery order.
    current_installment: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    approved_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_purchase_orders_project_id_projects",
        ),
        ForeignKeyConstraint(
            ["organization_id", "quotation_id"],
            ["quotations.organization_id", "quotations.id"],
            name="fk_purchase_orders_quotation_id_quotations",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_purchase_orders_supplier_id_suppliers",
        ),
        Index(
            "uq_purchase_orders_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_purchase_orders_org_project",
            "organization_id",
            "project_id",
            "status",
        ),
        Index(
            "ix_purchase_orders_org_quotation",
            "organization_id",
            "quotation_id",
        ),
        Index(
            "ix_purchase_orders_org_supplier",
            "organization_id",
            "supplier_id",
        ),
        Index(
            "uq_purchase_orders_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "((current_installment <= installment_count))",
            name="installment_within_the_order",
        ),
        CheckConstraint(
            "((current_installment >= 1))",
            name="installment_positive",
        ),
        CheckConstraint(
            "((installment_count >= 1))",
            name="installment_count_positive",
        ),
        CheckConstraint(
            "((retention_amount >= (0)::numeric))",
            name="retention_non_negative",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'pending_approval'::character varying, 'approved'::character varying, "
            "'issued'::character varying, 'partially_received'::character varying, "
            "'received'::character varying, 'cancelled'::character varying, "
            "'closed'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((((status)::text <> ALL ((ARRAY['issued'::character varying, "
            "'partially_received'::character varying, 'received'::character varying, "
            "'closed'::character varying])::text[])) OR ((approved_by)::text <> ''::text)))",
            name="an_issued_order_names_its_approver",
        ),
        CheckConstraint(
            "(((retention_pct IS NULL) OR ((retention_pct >= (0)::numeric) AND (retention_pct"
            " <= (100)::numeric))))",
            name="retention_in_range",
        ),
    )


class PoItem(ConstructionMixin, Base):
    """One ordered line.

    `ordered_quantity` against `received_quantity` is the variance the 3-way match
    tests, and both live on this row rather than being summed from receipts at
    query time — because "how much of this order have we actually received" is
    asked on every payment screen and a sum over receipt rows is a different
    number depending on which drafts are included.
    """

    __tablename__ = "po_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    purchase_order_id: Mapped[str] = mapped_column(String(40), nullable=False)
    quotation_item_id: Mapped[str] = mapped_column(String(40), nullable=False)
    material_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    ordered_quantity: Mapped[float] = mapped_column(
        QUANTITY, nullable=False, default=0, server_default="0"
    )
    received_quantity: Mapped[float] = mapped_column(
        QUANTITY, nullable=False, default=0, server_default="0"
    )
    unit_rate: Mapped[float] = mapped_column(RATE, nullable=False, default=0, server_default="0")
    amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "material_id"],
            ["materials.organization_id", "materials.id"],
            name="fk_po_items_material_id_materials",
        ),
        ForeignKeyConstraint(
            ["organization_id", "purchase_order_id"],
            ["purchase_orders.organization_id", "purchase_orders.id"],
            name="fk_po_items_purchase_order_id_purchase_orders",
        ),
        ForeignKeyConstraint(
            ["organization_id", "quotation_item_id"],
            ["quotation_items.organization_id", "quotation_items.id"],
            name="fk_po_items_quotation_item_id_quotation_items",
        ),
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_po_items_organization_id_units_dictionary",
        ),
        Index(
            "uq_po_items_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_po_items_org_po",
            "organization_id",
            "purchase_order_id",
        ),
        Index(
            "uq_po_items_org_po_line",
            "organization_id",
            "purchase_order_id",
            "line_no",
            unique=True,
        ),
        CheckConstraint(
            "((ordered_quantity > (0)::numeric))",
            name="ordered_quantity_positive",
        ),
        CheckConstraint(
            "(((received_quantity >= (0)::numeric) AND (received_quantity <= ordered_quantity)))",
            name="received_within_ordered",
        ),
    )


class GoodsReceipt(ConstructionMixin, Base):
    """A delivery, and its inspection.

    The corpus's GRN sheet has two blocks: an inspection checklist
    (`Nội dung kiểm tra` / `P/P kiểm tra` / `Kết quả`) and an item list. Both are
    modelled — the checklist as `receipt_checks` rows, not a boolean — because a
    receipt that passed "quality" is a claim, and the claim is only worth
    something if the checks that produced it are visible.

    `po_id` is NOT NULL. A receipt with no purchase order is stock arriving from
    nowhere, and it is the one thing the 3-way match cannot accommodate.
    """

    __tablename__ = "goods_receipts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    po_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    #: "Đợt 2" — the round this delivery belongs to.
    installment_no: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    received_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    received_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    supplier_delivery_note: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    #: Tập 3 §1.5 requires quality documents attached, and for subcontractors
    #: specifically "nghiệm thu QA/QC + xác nhận HSE". Three separate booleans
    #: rather than one `documents_attached`, because the third is two different
    #: sign-offs from two different departments and a single flag would let
    #: either substitute for the other.
    quality_documents_attached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    qa_accepted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    hse_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "po_id"],
            ["purchase_orders.organization_id", "purchase_orders.id"],
            name="fk_goods_receipts_po_id_purchase_orders",
        ),
        Index(
            "uq_goods_receipts_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_goods_receipts_org_po",
            "organization_id",
            "po_id",
        ),
        Index(
            "ix_goods_receipts_org_received",
            "organization_id",
            "received_on",
        ),
        Index(
            "uq_goods_receipts_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "((((status)::text <> 'accepted'::text) OR (accepted_at IS NOT NULL)))",
            name="an_accepted_receipt_says_when",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'under_inspection'::character varying, 'accepted'::character varying, "
            "'rejected'::character varying, 'cancelled'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((installment_no >= 1))",
            name="installment_positive",
        ),
        CheckConstraint(
            "((((status)::text <> 'accepted'::text) OR (quality_documents_attached AND qa_acc"
            "epted AND hse_confirmed)))",
            name="acceptance_needs_docs_qa_and_hse",
        ),
    )


class ReceiptItem(ConstructionMixin, Base):
    """One received line.

    `po_item_id` rather than a material id, so the receipt line is *the same
    line* of the order rather than a new claim about a material. That reference
    is what makes "received 85 of 120 ordered" answerable, and it is what the
    3-way match joins on.
    """

    __tablename__ = "receipt_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    goods_receipt_id: Mapped[str] = mapped_column(String(40), nullable=False)
    po_item_id: Mapped[str] = mapped_column(String(40), nullable=False)
    material_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    brand: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    origin_country: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    received_quantity: Mapped[float] = mapped_column(
        QUANTITY, nullable=False, default=0, server_default="0"
    )
    #: What the delivery was rejected for, when a line is partly rejected. Tập 3
    #: §1.5's reason codes: a quantity variance is `quantity`, not a free note.
    variance_reason: Mapped[str | None] = mapped_column(SHORT, nullable=True)
    variance_note: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "goods_receipt_id"],
            ["goods_receipts.organization_id", "goods_receipts.id"],
            name="fk_receipt_items_goods_receipt_id_goods_receipts",
        ),
        ForeignKeyConstraint(
            ["organization_id", "material_id"],
            ["materials.organization_id", "materials.id"],
            name="fk_receipt_items_material_id_materials",
        ),
        ForeignKeyConstraint(
            ["organization_id", "po_item_id"],
            ["po_items.organization_id", "po_items.id"],
            name="fk_receipt_items_po_item_id_po_items",
        ),
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_receipt_items_organization_id_units_dictionary",
        ),
        Index(
            "ix_receipt_items_org_po_item",
            "organization_id",
            "po_item_id",
        ),
        Index(
            "ix_receipt_items_org_receipt",
            "organization_id",
            "goods_receipt_id",
        ),
        Index(
            "uq_receipt_items_org_receipt_line",
            "organization_id",
            "goods_receipt_id",
            "line_no",
            unique=True,
        ),
        CheckConstraint(
            "(((variance_reason IS NULL) OR ((variance_reason)::text = ANY ((ARRAY['price'::c"
            "haracter varying, 'quantity'::character varying, 'quality'::character varying, "
            "'document'::character varying])::text[]))))",
            name="variance_reason_known",
        ),
        CheckConstraint(
            "((received_quantity > (0)::numeric))",
            name="received_quantity_positive",
        ),
    )


class ReceiptCheck(ConstructionMixin, Base):
    """One line of a receipt's inspection checklist.

    `Nội dung kiểm tra` / `P/P kiểm tra` / `Kết quả` / `Ghi chú`, verbatim from
    the GRN sheet. Rows rather than a boolean, because "quality passed" is a
    claim and a claim is only worth something when the checks behind it are
    visible to whoever disputes it — which for a subcontract delivery is QA and
    HSE, and for a supplier it is the person who signed the delivery.

    `passed` is nullable rather than false by default. "Not checked" and "checked
    and failed" are different facts, and a receipt cannot be accepted on a
    checklist that was never performed.
    """

    __tablename__ = "receipt_checks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    goods_receipt_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False)
    #: "P/P kiểm tra" — the method, verbatim from the sheet.
    method: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    result: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    checked_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remark: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "goods_receipt_id"],
            ["goods_receipts.organization_id", "goods_receipts.id"],
            name="fk_receipt_checks_goods_receipt_id_goods_receipts",
        ),
        Index(
            "ix_receipt_checks_org_receipt",
            "organization_id",
            "goods_receipt_id",
            "sequence",
        ),
        CheckConstraint(
            "(((passed IS NOT FALSE) OR (remark <> ''::text)))",
            name="a_failed_check_says_why",
        ),
        CheckConstraint(
            "(((passed IS NOT NULL) OR ((checked_by)::text = ''::text)))",
            name="an_unchecked_line_names_nobody",
        ),
        CheckConstraint(
            "(((passed IS NULL) OR (checked_at IS NOT NULL)))",
            name="a_performed_check_says_when",
        ),
    )


class MaterialReconciliation(ConstructionMixin, Base):
    """The seven-step delivery timeline for one ordered line.

    Measured from the `VẬT TƯ <zone>` sheets, which track seven dated fields
    per line: requested by the client, ordered, expected in, actually in, expected
    again, actually in again, and approved. That is a timeline rather than a set
    of columns, and the question the sheet answers is "is this late and by how
    much" — a comparison across the whole sequence, not a reading of one field.

    The steps are rows here, and `step` is a closed vocabulary. Storing seven
    nullable date columns on `po_items` would put the second and third expected
    dates somewhere sensible-looking and the *comparison* would be eleven
    different ad-hoc queries rather than one.
    """

    __tablename__ = "material_reconciliations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    po_item_id: Mapped[str] = mapped_column(String(40), nullable=False)
    step: Mapped[str] = mapped_column(SHORT, nullable=False)
    planned_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    zone_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: Tập 1's chain: Design → Middle → PM/Proc. A late step here is a Gate G3
    #: input — "PO chính trong ngân sách" — so who owns the slip is a fact worth
    #: keeping rather than inferring from the date.
    owner_role_key: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    note: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "po_item_id"],
            ["po_items.organization_id", "po_items.id"],
            name="fk_material_reconciliations_po_item_id_po_items",
        ),
        Index(
            "ix_material_reconciliations_org_late",
            "organization_id",
            "step",
            "planned_on",
        ),
        Index(
            "uq_material_reconciliations_org_step",
            "organization_id",
            "po_item_id",
            "step",
            unique=True,
        ),
        CheckConstraint(
            "((((step)::text <> 'actual_delivery_2nd'::text) OR (actual_on IS NULL) OR (plann"
            "ed_on IS NOT NULL)))",
            name="a_retry_presumes_an_original",
        ),
        CheckConstraint(
            "(((step)::text = ANY ((ARRAY['requested_by_client'::character varying, "
            "'ordered_by_procurement'::character varying, "
            "'expected_delivery'::character varying, 'actual_delivery'::character varying, "
            "'expected_delivery_2nd'::character varying, "
            "'actual_delivery_2nd'::character varying, "
            "'approved'::character varying])::text[])))",
            name="step_known",
        ),
    )
