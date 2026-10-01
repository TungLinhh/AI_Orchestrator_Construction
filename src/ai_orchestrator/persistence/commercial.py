"""Commercial front end: opportunity, tender, bid.

Everything upstream of a signed contract. The previous tranche starts at
`projects`, which assumes a contract exists; this tranche is the part that
decides whether one will.

Three tables here are the platform's first real contact with an AI service, and
one of them is why:

`opportunities` and `bids` are ordinary business records that a person maintains.
`tender_requirements` is not. It is a model's reading of a tender package: the
mandatory clauses, the scope, the commercial terms, each one attributed to a
document, a page, a span of text, and a confidence. The Tender & BOQ agent writes
it as a proposal and a human confirms or rejects each row.

That makes this the first place where **"how wrong is this, and who says so"**
matters more than "is this right". A tender has a hundred requirements, a human
can check forty of them in an afternoon, and the ones nobody checks are the ones
that disqualify the bid. So:

* every extracted value keeps `source_page` and `source_span`, so a reviewer is
  comparing the model's answer against the sentence it came from rather than
  against their memory of the document;
* every extracted value keeps `extraction_confidence`, and a check constraint
  makes it **mandatory** for a row the model wrote — a model-extracted
  requirement with no stated confidence cannot be triaged, because the whole
  review queue is ordered by it;
* a row a human rejected stays, with `status='rejected'`. Deleted extractions
  are the training signal gone, and re-running the agent would produce the same
  wrong answer with no memory of the correction.

The unit of work here is the `bid_items` → `wbs_items` conversion on award. They
are deliberately separate tables rather than one with a nullable `bid_id`: a bid
is a position taken before any WBS exists, and forcing one table to hold both
means every query carries a `WHERE bid_id IS NOT NULL` that somebody will forget.

Money is `NUMERIC`. Quantities are `NUMERIC` with the unit in a column that
references `units_dictionary` through a composite foreign key including
`organization_id` — the same rule as `wbs_items`, and for the same reason: a
rate applied to the wrong unit is a wrong number that nothing downstream can
catch.
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

#: A probability or a margin, in percent with two decimals. Not a float: a
#: probability is compared for equality in the bid/no-bid report and 33.33 must
#: not become 33.33000000000001.
PERCENT = Numeric(6, 3)

# ---------------------------------------------------------------------------
# Closed vocabularies. Strings rather than Postgres enums, for the same reason
# the rest of the schema uses strings: adding a stage must not require a type
# migration, and a check constraint is enough to keep the set honest. Each is a
# module constant so a workflow and a query cannot disagree about the spelling.
# ---------------------------------------------------------------------------

#: Where an opportunity sits in the pipeline. `won` and `lost` are terminal and
#: carry the decision, not the stage, so the stage stays meaningful afterwards.
OPPORTUNITY_STAGES = (
    "lead",
    "qualified",
    "tender_received",
    "bidding",
    "submitted",
    "won",
    "lost",
    "abandoned",
)

#: The incoming package. Separate from `TENDER_DOCUMENT_ROLES`: a tender is
#: received, then read, then priced, and a bid is a different object entirely.
TENDER_STATUSES = (
    "received",
    "under_review",
    "extracted",
    "priced",
    "submitted",
    "withdrawn",
)

#: What a document *is* inside a package. This is the label the classifier
#: assigns, and it is the only thing a human sees when triaging a package of
#: forty files, so it is deliberately small and unambiguous.
TENDER_DOCUMENT_ROLES = (
    "invitation",
    "scope_of_work",
    "bill_of_quantities",
    "drawings",
    "evaluation_criteria",
    "contract_draft",
    "qualification",
    "submission_form",
    "other",
)

#: How far a document has been through reading. `needs_review` is the state the
#: operator UI sorts by, and it means a field came back below its confidence
#: threshold — not that the read failed.
EXTRACTION_STATUSES = ("pending", "classified", "extracted", "needs_review", "failed")

#: What kind of requirement a row is. Drives which reviewer sees it: a
#: qualification requirement goes to Procurement, a commercial one to Finance.
REQUIREMENT_KINDS = (
    "scope",
    "qualification",
    "commercial",
    "technical",
    "schedule",
    "warranty",
    "penalty",
    "other",
)

#: How much missing this one would cost. `critical` is what makes a bid
#: non-viable and is the only severity the bid/no-bid gate treats as a block.
REQUIREMENT_SEVERITIES = ("informational", "low", "medium", "high", "critical")

#: What a human decided about an extracted requirement. Rejected rows are kept
#: on purpose; see the module docstring.
REQUIREMENT_STATUSES = ("extracted", "confirmed", "rejected")

BID_STATUSES = (
    "draft",
    "priced",
    "internal_approved",
    "submitted",
    "won",
    "lost",
    "withdrawn",
)


class Opportunity(ConstructionMixin, Base):
    """A prospect, before there is a tender or a bid.

    `expected_value` is an opinion and lives here; `bids.bid_value` is a
    position taken. Keeping them separate means a pipeline forecast never
    silently becomes a commitment, which is the failure mode of a CRM with one
    money column.
    """

    __tablename__ = "opportunities"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    client_id: Mapped[str] = mapped_column(String(40), nullable=False)
    project_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="construction", server_default="construction"
    )
    stage: Mapped[str] = mapped_column(SHORT, nullable=False, default="lead", server_default="lead")
    expected_value: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    probability_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    #: Who in the business development team owns it. Free text, matching
    #: `project_roles.person_ref`: there is no user directory to point at.
    owner_name: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: When a response is due. Drives the overdue pipeline report, so it is a
    #: date and not a note.
    bid_deadline: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: How the opportunity arrived: `referral`, `inbound`, `tender_board`,
    #: `repeat_client`. A closed set for reporting win rate by source.
    lead_source: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_opportunities_client_id_clients",
        ),
        Index(
            "uq_opportunities_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_opportunities_org_client",
            "organization_id",
            "client_id",
        ),
        Index(
            "ix_opportunities_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        Index(
            "ix_opportunities_org_deadline",
            "organization_id",
            "bid_deadline",
        ),
        Index(
            "ix_opportunities_org_stage",
            "organization_id",
            "stage",
        ),
        CheckConstraint(
            "(((stage)::text = ANY ((ARRAY['lead'::character varying, "
            "'qualified'::character varying, 'tender_received'::character varying, "
            "'bidding'::character varying, 'submitted'::character varying, "
            "'won'::character varying, 'lost'::character varying, "
            "'abandoned'::character varying])::text[])))",
            name="stage_known",
        ),
        CheckConstraint(
            "(((probability_pct IS NULL) OR ((probability_pct >= (0)::numeric) AND (probabili"
            "ty_pct <= (100)::numeric))))",
            name="probability_in_range",
        ),
    )


class Tender(ConstructionMixin, Base):
    """An invitation to bid: the package a client sent.

    Not the same thing as an `opportunity` — public tenders arrive with no prior
    relationship, so `opportunity_id` is nullable — and emphatically not the
    same thing as a `bid`, which is our answer to this.
    """

    __tablename__ = "tenders"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: The client's own reference, which is what an email thread will mention and
    #: what the contract will cite. Not unique globally — two clients can number
    #: their tenders the same way — so the unique index carries the tenant.
    reference_no: Mapped[str] = mapped_column(SHORT, nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="received", server_default="received"
    )
    received_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    closing_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: `vi`, `en`, or `both`. Drives which extraction model profile runs and
    #: which language the output is written in; a bilingual package is the
    #: normal case here, not an edge case.
    language: Mapped[str] = mapped_column(SHORT, nullable=False, default="vi", server_default="vi")
    contract_value: Mapped[float | None] = mapped_column(MONEY, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_tenders_client_id_clients",
        ),
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["opportunities.organization_id", "opportunities.id"],
            name="fk_tenders_opportunity_id_opportunities",
        ),
        Index(
            "uq_tenders_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_tenders_org_closing",
            "organization_id",
            "closing_at",
        ),
        Index(
            "ix_tenders_org_opportunity",
            "organization_id",
            "opportunity_id",
        ),
        Index(
            "ix_tenders_org_reference",
            "organization_id",
            "reference_no",
            unique=True,
        ),
        Index(
            "ix_tenders_org_status",
            "organization_id",
            "status",
        ),
        CheckConstraint(
            "(((language)::text = ANY ((ARRAY['vi'::character varying, "
            "'en'::character varying, 'both'::character varying])::text[])))",
            name="language_known",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['received'::character varying, "
            "'under_review'::character varying, 'extracted'::character varying, "
            "'priced'::character varying, 'submitted'::character varying, "
            "'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
    )


class TenderDocument(ConstructionMixin, Base):
    """One file inside a tender package.

    A join to the substrate `documents` table, which holds the storage URI and
    the content hash. This table holds what the document *is for this tender*,
    which is the only thing the substrate has no opinion about.

    `role` is the classifier's label and `extraction_status` is the operator's
    work queue. A document that came back `needs_review` is one where a field
    landed below its confidence threshold, and it is the state the review screen
    sorts by.
    """

    __tablename__ = "tender_documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tender_id: Mapped[str] = mapped_column(String(40), nullable=False)
    document_id: Mapped[str] = mapped_column(String(40), nullable=False)
    role: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    extraction_status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pending", server_default="pending"
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: True when the package is images rather than text. A scanned tender is
    #: read by a different pipeline at a different cost, and pretending the two
    #: are the same is how a package of 300 scanned pages is submitted at the
    #: confidence a text PDF would get.
    is_scanned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    extracted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_tender_documents_document_id_documents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "tender_id"],
            ["tenders.organization_id", "tenders.id"],
            name="fk_tender_documents_tender_id_tenders",
        ),
        Index(
            "uq_tender_documents_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_tender_documents_org_status",
            "organization_id",
            "extraction_status",
        ),
        Index(
            "ix_tender_documents_org_tender",
            "organization_id",
            "tender_id",
        ),
        Index(
            "uq_tender_documents_org_tender_doc",
            "organization_id",
            "tender_id",
            "document_id",
            unique=True,
        ),
        CheckConstraint(
            "(((extraction_status)::text = ANY ((ARRAY['pending'::character varying, "
            "'classified'::character varying, 'extracted'::character varying, "
            "'needs_review'::character varying, 'failed'::character varying])::text[])))",
            name="extraction_status_known",
        ),
        CheckConstraint(
            "(((role)::text = ANY ((ARRAY['invitation'::character varying, "
            "'scope_of_work'::character varying, 'bill_of_quantities'::character varying, "
            "'drawings'::character varying, 'evaluation_criteria'::character varying, "
            "'contract_draft'::character varying, 'qualification'::character varying, "
            "'submission_form'::character varying, 'other'::character varying])::text[])))",
            name="role_known",
        ),
    )


class TenderRequirement(ConstructionMixin, Base):
    """One requirement read out of a tender package by the Tender & BOQ agent.

    The most consequential AI-authored table in the platform, and the only one
    where the *provenance* of a value is more important than the value: a human
    checking forty of a hundred requirements will check the ones that look
    strange, and the ones that will disqualify the bid are the ones that did not
    look strange.

    Which is why three columns are not optional:

    `source_page` and `source_span`
        Where the answer came from, so a reviewer compares it against the
        sentence rather than their memory. `source_span` holds the text the
        model actually read, clipped — enough to judge, not enough to
        reconstruct the document.

    `extraction_confidence`
        Mandatory for a model-written row, by check constraint. The review
        queue is ordered by it; a model row without one cannot be placed in that
        queue, which is the same argument as `proposal_id` being mandatory for an
        agent-sourced row: a value nobody can triage is a value nobody reviews.
    """

    __tablename__ = "tender_requirements"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tender_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Which document in the package this came from. Nullable, because a
    #: requirement can span two documents and a model that could not attribute
    #: the value is more useful saying so than guessing.
    tender_document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    detail: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: A requirement you do not meet is a non-responsive bid. These are the rows
    #: the bid/no-bid gate reads, and the only ones it treats as blocking.
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    severity: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="medium", server_default="medium"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="extracted", server_default="extracted"
    )
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The clipped text the model read. Not the whole document, and not a hash:
    #: a hash would prevent a reviewer from seeing what it saw.
    source_span: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    #: What a human said when they disagreed. Without it the correction loop
    #: cannot tell a wrong reading from a wrong judgement, and a rejected row
    #: with no reason teaches the next run nothing.
    reviewed_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "tender_document_id"],
            ["tender_documents.organization_id", "tender_documents.id"],
            name="fk_tender_requirements_tender_document_id_tender_documents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "tender_id"],
            ["tenders.organization_id", "tenders.id"],
            name="fk_tender_requirements_tender_id_tenders",
        ),
        Index(
            "uq_tender_requirements_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_tender_requirements_org_mandatory",
            "organization_id",
            "tender_id",
            "is_mandatory",
        ),
        Index(
            "ix_tender_requirements_org_review_queue",
            "organization_id",
            "status",
            "extraction_confidence",
        ),
        Index(
            "ix_tender_requirements_org_tender",
            "organization_id",
            "tender_id",
        ),
        CheckConstraint(
            "(((severity)::text = ANY ((ARRAY['informational'::character varying, "
            "'low'::character varying, 'medium'::character varying, "
            "'high'::character varying, 'critical'::character varying])::text[])))",
            name="severity_known",
        ),
        CheckConstraint(
            "((((source)::text <> 'agent_proposal'::text) OR (extraction_confidence IS NOT NULL)))",
            name="agent_row_needs_a_confidence",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['extracted'::character varying, "
            "'confirmed'::character varying, 'rejected'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((extraction_confidence IS NULL) OR ((extraction_confidence >= (0)::numeric) AN"
            "D (extraction_confidence <= (1)::numeric))))",
            name="confidence_in_range",
        ),
        CheckConstraint(
            "(((kind)::text = ANY ((ARRAY['scope'::character varying, "
            "'qualification'::character varying, 'commercial'::character varying, "
            "'technical'::character varying, 'schedule'::character varying, "
            "'warranty'::character varying, 'penalty'::character varying, "
            "'other'::character varying])::text[])))",
            name="kind_known",
        ),
    )


class Bid(ConstructionMixin, Base):
    """Our answer to a tender.

    Carries the margin explicitly rather than deriving it, because margin is a
    *position taken at a point in time* and a bid that is re-priced three times
    has three margins, only the last of which is in the forecast. Deriving it
    from `bid_items` would silently rewrite history on every edit.
    """

    __tablename__ = "bids"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    opportunity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tender_id: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    bid_value: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    margin_amount: Mapped[float | None] = mapped_column(MONEY, nullable=True)
    #: Signed, and allowed well past 100: a bid priced at a 140% negative margin
    #: is a number a gate must be able to store in order to refuse it. Clamping
    #: the column would hide exactly the bids that should never be submitted.
    margin_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Why we won or lost, in the client's terms rather than ours. The only
    #: route by which a lost bid improves the next one, and a free-text column
    #: is the honest shape for it.
    decision_reason: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["opportunities.organization_id", "opportunities.id"],
            name="fk_bids_opportunity_id_opportunities",
        ),
        ForeignKeyConstraint(
            ["organization_id", "tender_id"],
            ["tenders.organization_id", "tenders.id"],
            name="fk_bids_tender_id_tenders",
        ),
        Index(
            "uq_bids_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_bids_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        Index(
            "ix_bids_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_bids_org_tender",
            "organization_id",
            "tender_id",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'priced'::character varying, 'internal_approved'::character varying, "
            "'submitted'::character varying, 'won'::character varying, "
            "'lost'::character varying, 'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
    )


class BidItem(ConstructionMixin, Base):
    """A priced line in a bid, before there is a WBS to hang it on.

    On award these become `wbs_items`. Kept as a separate table rather than one
    table with a nullable `bid_id` because a bid and an awarded BOQ are
    different things with different owners and different lifecycles, and a
    shared table means every query carries a null check that somebody forgets.

    `is_optional` is for provisional sums and daywork, which are quoted but not
    committed. They roll up into the bid total and must not roll into the
    contract value, so the distinction has to be a column and not a naming
    convention.
    """

    __tablename__ = "bid_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    bid_id: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False)
    #: Composite with `organization_id` from the mixin, so a rate cannot be
    #: quoted against a unit belonging to another tenant.
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    quantity: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0, server_default="0")
    unit_rate: Mapped[float] = mapped_column(RATE, nullable=False, default=0, server_default="0")
    amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    is_optional: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: How confident an automated extraction was in the quantity and the rate.
    #: Same reasoning as `tender_requirements`: a number nobody can trace is a
    #: number nobody checks.
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "bid_id"],
            ["bids.organization_id", "bids.id"],
            name="fk_bid_items_bid_id_bids",
        ),
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_bid_items_organization_id_units_dictionary",
        ),
        Index(
            "uq_bid_items_org_bid_code",
            "organization_id",
            "bid_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "((((source)::text <> 'agent_proposal'::text) OR (extraction_confidence IS NOT NULL)))",
            name="agent_row_needs_a_confidence",
        ),
        CheckConstraint(
            "(((extraction_confidence IS NULL) OR ((extraction_confidence >= (0)::numeric) AN"
            "D (extraction_confidence <= (1)::numeric))))",
            name="confidence_in_range",
        ),
        CheckConstraint(
            "((quantity >= (0)::numeric))",
            name="quantity_non_negative",
        ),
    )


__all__ = [
    "BID_STATUSES",
    "EXTRACTION_STATUSES",
    "OPPORTUNITY_STAGES",
    "REQUIREMENT_KINDS",
    "REQUIREMENT_SEVERITIES",
    "REQUIREMENT_STATUSES",
    "TENDER_DOCUMENT_ROLES",
    "TENDER_STATUSES",
    "Bid",
    "BidItem",
    "Opportunity",
    "Tender",
    "TenderDocument",
    "TenderRequirement",
]
