"""Contracts, variations, claims, and the supplier master.

The signed agreement and everything that happens to it after signature. This
tranche closes the chain the previous one left open: a `bid` now has something to
become, and `projects.contract_value` — documented as agreeing with the signed
contract — finally has a contract to agree with.

Four decisions in here are the reason the module exists rather than six more
tables.

One `contracts` table, with a party direction
A contract with a client and a contract with a supplier are the same kind of
document with the same clauses, the same milestone structure and the same
variation machinery. Splitting them into `client_contracts` and
`supplier_contracts` would duplicate every column and make every report a union.
`client_id` and `supplier_id` are both nullable and a check constraint requires
**exactly one**, so "a contract with neither" and "a contract with both" are
states the database refuses rather than states a query has to filter out.

No `change_orders` table
A variation and a change order are the same object at different stages: an
instruction that has not been priced yet, and the priced thing that came out of
it. `contract_variations` carries both, distinguished by `status`, and the
distinction that actually matters is recorded in the money:

    value_claimed  vs  value_approved
    days_claimed   vs  days_approved

**The gap between those pairs is the money.** A variation that was instructed in
writing and never valued is a receivable that does not appear in any report
unless somebody is tracking it, and it is the single most common way a
construction company loses margin. `variation_type='time'` with
`days_claimed=60, days_approved=0` is a claim waiting to happen, and this schema
makes that a query rather than an archaeology project.

`claims_events` exists because a claim is not a row, it is a chronology
Contracts lose disputes on procedure, not on merit: notice was late, the
assessment was never issued, the correspondence was never filed. A `claims` row
with four date columns cannot represent "the letter went out on the 4th, chased
on the 19th, assessed on the 2nd of next month". The chronology is append-only —
events are added, never edited — because a chronology that can be rewritten
proves nothing.

`contract_clauses` is the agent's output, and `contracts` is what was agreed
The Contract Intelligence service reads a contract and produces *clauses* with
a source span and a confidence, in `contract_clauses`. It does not write
`contracts.retention_pct`. That is not a style preference, it is the platform's
one rule: an agent emits a proposal, a human confirms, and a human's
confirmation is what lands on the contract record. The handful of commercial
terms that reports and gates compute on therefore live as columns on `contracts`
— retention, liquidated damages, advance, payment terms, warranty — with no
extraction-confidence column beside each one, because a value on a signed
contract is not a model's opinion about a value. The provenance of *how it was
read* lives in the clause table, next to the text it was read from.

The supplier bank account is a reference, not an account number
`approved_bank_account_ref` plus `last4`, never a full account. A platform that
holds full bank details has a breach surface that grows with every supplier, and
the only capability that actually needs it — refusing to pay a payee that is not
the approved one — is satisfied by a reference and four digits.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import MONEY, Base
from ai_orchestrator.persistence.construction import (
    LONG,
    NAME,
    SHORT,
    ConstructionMixin,
    domain_args,
)

#: A percentage of contract value. 5 digits, 3 decimals: retention is 5%, LD
#: caps are 5-10%, and a bid margin can be -140.5%, but none of them need more
#: precision and all of them must survive a round trip without turning into a
#: float.
PERCENT = Numeric(6, 3)
#: Days. Small on purpose — a 30-day payment term does not need BIGINT, and a
#: narrow column is a narrower thing to get wrong.
DAYS = Numeric(8, 0)

SUPPLIER_CATEGORIES = (
    "materials",
    "equipment",
    "subcontractor",
    "services",
    "labour",
    "transport",
    "other",
)

#: `blacklisted` is distinct from `rejected` and both are distinct from
#: `suspended`, because they carry different consequences and a supplier that has
#: been suspended for a documentation lapse must not look the same as one barred
#: for fraud. Only `blacklisted` and `rejected` are permanent.
SUPPLIER_STATUSES = (
    "prospect",
    "under_review",
    "approved",
    "conditional",
    "suspended",
    "rejected",
    "blacklisted",
)

CONTRACT_TYPES = ("works", "supply", "services", "framework", "consultancy")

CONTRACT_STATUSES = (
    "draft",
    "for_review",
    "approved",
    "signed",
    "active",
    "suspended",
    "terminated",
    "completed",
    "closed",
)

#: Which side of the contract we are on. Derived from which of `client_id` and
#: `supplier_id` is set, and stored so a report does not have to re-derive it —
#: but the check constraint on those two columns is what actually guarantees it,
#: and this column is a convenience. If the two ever disagree, the constraint
#: fires first.
CONTRACT_PARTY_ROLES = ("employer", "supplier", "subcontractor", "consultant")

MILESTONE_KINDS = (
    "payment",
    "delivery",
    "progress",
    "acceptance",
    "warranty",
    "mobilisation",
    "demobilisation",
    "other",
)

MILESTONE_STATUSES = ("pending", "due", "achieved", "overdue", "waived")

#: `instructed` is the state that matters: a written instruction that has not been
#: priced. It is deliberately distinct from `proposed` (nobody has instructed
#: anything yet) and from `approved`.
VARIATION_STATUSES = (
    "proposed",
    "instructed",
    "assessed",
    "approved",
    "rejected",
    "executed",
    "valued",
    "settled",
)

#: Who originated it. A variation the employer instructs and the contractor
#: claims against is the ordinary case; one we instruct is a scope change we are
#: absorbing, and the two have different approval routes.
VARIATION_ORIGINATORS = ("employer", "contractor")

CLAIM_TYPES = ("time", "money", "both", "defect", "delay", "other")

CLAIM_STATUSES = (
    "draft",
    "notified",
    "assessed",
    "submitted",
    "accepted",
    "rejected",
    "settled",
    "withdrawn",
)

#: The chronology. Append-only: an event is added, never edited, because a
#: rewritable chronology is not evidence of anything.
CLAIM_EVENT_TYPES = (
    "notice",
    "correspondence",
    "meeting",
    "assessment",
    "submission",
    "decision",
    "payment",
    "note",
)

#: A contract clause, as read by an agent.
CLAUSE_CATEGORIES = (
    "payment",
    "retention",
    "penalty",
    "warranty",
    "liability",
    "scope",
    "termination",
    "force_majeure",
    "dispute",
    "other",
)

CLAUSE_STATUSES = ("extracted", "confirmed", "rejected")


class Supplier(ConstructionMixin, Base):
    """A company we buy from, or subcontract to.

    `category` rather than a boolean, because a subcontractor needs a different
    approval route, a different set of qualification documents and a different
    retention arrangement from a materials supplier, and a boolean would make all
    three of those `category = 'x' and not is_subcontractor`.

    `qualification_expires_on` is a date rather than a note because most
    supplier-approval rules are "current as at today", and a note is not
    queryable.
    """

    __tablename__ = "suppliers"
    #: Vietnamese tax code. Text, not a number: leading zeros are significant.
    #: A reference to the approved bank record held elsewhere, and the last four
    #: digits for display and for matching a payee. **Not** a full account
    #: number: the only capability that needs one is refusing to pay an
    #: unapproved payee, and a reference plus four digits does that without the
    #: platform becoming a store of every supplier's bank details.

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    short_name: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: Vietnamese tax code. Text, not a number: leading zeros are significant.
    tax_code: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    category: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="materials", server_default="materials"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="prospect", server_default="prospect"
    )
    address: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    country: Mapped[str] = mapped_column(SHORT, nullable=False, default="VN", server_default="VN")
    email: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    phone: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    contact_name: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    payment_terms_days: Mapped[int] = mapped_column(
        DAYS, nullable=False, default=30, server_default="30"
    )
    #: A reference to the approved bank record held elsewhere, and the last four
    #: digits for display and for matching a payee. **Not** a full account
    #: number: the only capability that needs one is refusing to pay an
    #: unapproved payee, and a reference plus four digits does that without the
    #: platform becoming a store of every supplier's bank details.
    approved_bank_account_ref: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    approved_bank_account_last4: Mapped[str] = mapped_column(
        String(4), nullable=False, default="", server_default=""
    )
    qualification_expires_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        Index(
            "uq_suppliers_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_suppliers_org_category",
            "organization_id",
            "category",
        ),
        Index(
            "ix_suppliers_org_expiry",
            "organization_id",
            "qualification_expires_on",
        ),
        Index(
            "ix_suppliers_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "uq_suppliers_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((category)::text = ANY ((ARRAY['materials'::character varying, "
            "'equipment'::character varying, 'subcontractor'::character varying, "
            "'services'::character varying, 'labour'::character varying, "
            "'transport'::character varying, 'other'::character varying])::text[])))",
            name="category_known",
        ),
        CheckConstraint(
            "((((status)::text <> 'approved'::text) OR ((qualification_expires_on IS NOT NULL"
            ") AND (qualification_expires_on >= CURRENT_DATE))))",
            name="an_approved_supplier_is_qualified",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['prospect'::character varying, "
            "'under_review'::character varying, 'approved'::character varying, "
            "'conditional'::character varying, 'suspended'::character varying, "
            "'rejected'::character varying, 'blacklisted'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((payment_terms_days >= (0)::numeric))",
            name="payment_terms_non_negative",
        ),
    )


class Contract(ConstructionMixin, Base):
    """A signed agreement, with a client or with a supplier.

    `retention_pct`, `liquidated_damages_pct` and `advance_payment_pct` are the
    commercial terms reports and gates compute on, which is why they are columns
    and not rows in `contract_clauses`. The clause table is where an agent's
    *reading* lives, with the sentence it read; this is where what was **agreed**
    lives, and a value on a signed contract is not a model's opinion about a
    value. See the module docstring.

    `liquidated_damages_pct` is the cap, not the daily rate. A contract states
    both and they are different numbers, and only the cap is what decides whether
    a delay penalty is catastrophic.
    """

    __tablename__ = "contracts"
    #: 10% is the common Vietnamese construction figure and the number most often
    #: misread off a contract.
    #: The cap on delay penalties, not the rate.
    #: The executed document. Links to the substrate `documents` table, which
    #: holds the storage URI and content hash.

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: Unique on (org, code) rather than globally: two clients will both number
    #: their contracts "HĐ-01/2026", and the contract is cited by number in
    #: correspondence, so the number is the client's, not ours.
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: A framework or call-off contract may not belong to a project yet.
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    client_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    supplier_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    party_role: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="employer", server_default="employer"
    )
    contract_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="works", server_default="works"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    title: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    signed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    effective_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    contract_value: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    #: Money withheld until completion, as a percentage of the contract value.
    #: 10% is the common Vietnamese construction figure and the number most often
    #: misread off a contract.
    retention_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    #: The cap on delay penalties, not the rate.
    liquidated_damages_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    advance_payment_pct: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    payment_terms_days: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    warranty_months: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    #: The executed document. Links to the substrate `documents` table, which
    #: holds the storage URI and content hash.
    document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_contracts_client_id_clients",
        ),
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_contracts_document_id_documents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_contracts_project_id_projects",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_contracts_supplier_id_suppliers",
        ),
        Index(
            "uq_contracts_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_contracts_org_client",
            "organization_id",
            "client_id",
        ),
        Index(
            "ix_contracts_org_project",
            "organization_id",
            "project_id",
        ),
        Index(
            "ix_contracts_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_contracts_org_supplier",
            "organization_id",
            "supplier_id",
        ),
        Index(
            "uq_contracts_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((contract_type)::text = ANY ((ARRAY['works'::character varying, "
            "'supply'::character varying, 'services'::character varying, "
            "'framework'::character varying, 'consultancy'::character varying])::text[])))",
            name="contract_type_known",
        ),
        CheckConstraint(
            "(((party_role)::text = ANY ((ARRAY['employer'::character varying, "
            "'supplier'::character varying, 'subcontractor'::character varying, "
            "'consultant'::character varying])::text[])))",
            name="party_role_known",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'for_review'::character varying, 'approved'::character varying, "
            "'signed'::character varying, 'active'::character varying, "
            "'suspended'::character varying, 'terminated'::character varying, "
            "'completed'::character varying, 'closed'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((((status)::text <> ALL ((ARRAY['signed'::character varying, "
            "'active'::character varying, 'completed'::character varying, "
            "'closed'::character varying])::text[])) OR (signed_at IS NOT NULL)))",
            name="signed_requires_a_date",
        ),
        CheckConstraint(
            "(((client_id IS NULL) <> (supplier_id IS NULL)))",
            name="exactly_one_counterparty",
        ),
        CheckConstraint(
            "(((COALESCE(retention_pct, (0)::numeric) + COALESCE(advance_payment_pct, "
            "(0)::numeric)) <= (100)::numeric))",
            name="retention_plus_advance_within_contract_value",
        ),
        CheckConstraint(
            "(((advance_payment_pct IS NULL) OR ((advance_payment_pct >= (0)::numeric) AND (a"
            "dvance_payment_pct <= (100)::numeric))))",
            name="advance_payment_in_range",
        ),
        CheckConstraint(
            "(((liquidated_damages_pct IS NULL) OR ((liquidated_damages_pct >= (0)::numeric) "
            "AND (liquidated_damages_pct <= (100)::numeric))))",
            name="liquidated_damages_in_range",
        ),
        CheckConstraint(
            "(((payment_terms_days IS NULL) OR (payment_terms_days >= (0)::numeric)))",
            name="payment_terms_non_negative",
        ),
        CheckConstraint(
            "(((retention_pct IS NULL) OR ((retention_pct >= (0)::numeric) AND (retention_pct"
            " <= (100)::numeric))))",
            name="retention_in_range",
        ),
        CheckConstraint(
            "(((warranty_months IS NULL) OR (warranty_months >= (0)::numeric)))",
            name="warranty_non_negative",
        ),
    )


class ContractMilestone(ConstructionMixin, Base):
    """A dated obligation under a contract: a payment, a delivery, an acceptance.

    Separate from `milestones` in the project module, which is the project's own
    view of dated events. This one is the *contractual* schedule: what the
    contract says happens, with what happens if it does not. The two are related
    but they are updated by different people and they disagree — a contract
    milestone that has slipped and a project milestone that has not is precisely
    the signal a claim is built on.

    `amount` is set only for `kind = 'payment'`, and the check enforces that, so
    a delivery milestone cannot carry a payment amount and quietly appear in a
    cash schedule.
    """

    __tablename__ = "contract_milestones"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pending", server_default="pending"
    )
    sequence: Mapped[int] = mapped_column(
        Numeric(6, 0), nullable=False, default=0, server_default="0"
    )
    due_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    achieved_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float | None] = mapped_column(MONEY, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "contract_id"],
            ["contracts.organization_id", "contracts.id"],
            name="fk_contract_milestones_contract_id_contracts",
        ),
        Index(
            "ix_contract_milestones_org_due",
            "organization_id",
            "contract_id",
            "due_date",
        ),
        Index(
            "ix_contract_milestones_org_kind",
            "organization_id",
            "kind",
            "status",
        ),
        Index(
            "uq_contract_milestones_org_code",
            "organization_id",
            "contract_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((kind)::text = ANY ((ARRAY['payment'::character varying, "
            "'delivery'::character varying, 'progress'::character varying, "
            "'acceptance'::character varying, 'warranty'::character varying, "
            "'mobilisation'::character varying, 'demobilisation'::character varying, "
            "'other'::character varying])::text[])))",
            name="kind_known",
        ),
        CheckConstraint(
            "((((kind)::text = 'payment'::text) = (amount IS NOT NULL)))",
            name="amount_only_on_payment_milestones",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['pending'::character varying, "
            "'due'::character varying, 'achieved'::character varying, "
            "'overdue'::character varying, 'waived'::character varying])::text[])))",
            name="status_known",
        ),
    )


class ContractVariation(ConstructionMixin, Base):
    """A change to the contracted scope, price or time.

    Carries both a **claimed** and an **approved** figure for value and for time.
    The difference between them is the whole point of the table: an instructed
    variation that has been valued at nothing is a receivable that appears in no
    report, and it is the most common way margin disappears on a project. The
    approved figure is set when the valuation is agreed, and a report of
    "instructed but unvalued" is a single indexed query.

    There is deliberately no `change_orders` table. A variation and a change
    order are the same object before and after pricing, and modelling them as two
    tables guarantees a period in which an instruction exists in one and its
    priced form in the other, with nothing joining them.
    """

    __tablename__ = "contract_variations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    variation_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="scope", server_default="scope"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="proposed", server_default="proposed"
    )
    originator: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="employer", server_default="employer"
    )
    #: The date the instruction was issued in writing. The distinction from
    #: `raised_at` is the distinction between a claim and an assertion, and many
    #: contracts time-bar variations that were never instructed.
    instructed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    raised_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    settled_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    value_claimed: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    value_approved: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    days_claimed: Mapped[int] = mapped_column(DAYS, nullable=False, default=0, server_default="0")
    days_approved: Mapped[int] = mapped_column(DAYS, nullable=False, default=0, server_default="0")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "contract_id"],
            ["contracts.organization_id", "contracts.id"],
            name="fk_contract_variations_contract_id_contracts",
        ),
        Index(
            "uq_contract_variations_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_contract_variations_org_status",
            "organization_id",
            "contract_id",
            "status",
        ),
        Index(
            "uq_contract_variations_org_code",
            "organization_id",
            "contract_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['proposed'::character varying, "
            "'instructed'::character varying, 'assessed'::character varying, "
            "'approved'::character varying, 'rejected'::character varying, "
            "'executed'::character varying, 'valued'::character varying, "
            "'settled'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((((status)::text <> ALL ((ARRAY['instructed'::character varying, "
            "'assessed'::character varying, 'approved'::character varying, "
            "'executed'::character varying, 'valued'::character varying, "
            "'settled'::character varying])::text[])) OR (instructed_at IS NOT NULL)))",
            name="a_privileged_variation_was_instructed",
        ),
        CheckConstraint(
            "(((originator)::text = ANY ((ARRAY['employer'::character varying, "
            "'contractor'::character varying])::text[])))",
            name="originator_known",
        ),
        CheckConstraint(
            "(((variation_type)::text = ANY ((ARRAY['quantity'::character varying, "
            "'scope'::character varying, 'design'::character varying, "
            "'price'::character varying, 'time'::character varying, "
            "'omission'::character varying])::text[])))",
            name="variation_type_known",
        ),
        CheckConstraint(
            "(((days_claimed >= (0)::numeric) AND (days_approved >= (0)::numeric)))",
            name="days_non_negative",
        ),
    )


class ContractClaim(ConstructionMixin, Base):
    """A claim for time or money against a contract.

    `notified_at` is the field that wins or loses the claim. Most contracts time-
    bar a claim that was not notified within a stated period of the event giving
    rise to it, and the deadline is invisible in a schema that has only
    `claimed_at`. Kept separate from `raised_at` — when we decided to claim — for
    the same reason: the difference is the defence.

    Claimed and awarded, again, both. A claim submitted at 100 and awarded at 60
    is a normal outcome, and modelling only the awarded figure loses the
    information about how much was contested.
    """

    __tablename__ = "contract_claims"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(40), nullable=False)
    variation_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    claim_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="both", server_default="both"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    #: When the event happened. The clock for the notification deadline runs from
    #: here, not from when we noticed it.
    event_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    raised_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notified_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    assessed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    settled_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    amount_claimed: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    amount_awarded: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    days_claimed: Mapped[int] = mapped_column(DAYS, nullable=False, default=0, server_default="0")
    days_awarded: Mapped[int] = mapped_column(DAYS, nullable=False, default=0, server_default="0")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "contract_id"],
            ["contracts.organization_id", "contracts.id"],
            name="fk_contract_claims_contract_id_contracts",
        ),
        ForeignKeyConstraint(
            ["organization_id", "variation_id"],
            ["contract_variations.organization_id", "contract_variations.id"],
            name="fk_contract_claims_variation_id_contract_variations",
        ),
        Index(
            "uq_contract_claims_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_contract_claims_org_status",
            "organization_id",
            "contract_id",
            "status",
        ),
        Index(
            "ix_contract_claims_org_unnotified",
            "organization_id",
            "notified_at",
            "event_date",
        ),
        Index(
            "uq_contract_claims_org_code",
            "organization_id",
            "contract_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((claim_type)::text = ANY ((ARRAY['time'::character varying, "
            "'money'::character varying, 'both'::character varying, "
            "'defect'::character varying, 'delay'::character varying, "
            "'other'::character varying])::text[])))",
            name="claim_type_known",
        ),
        CheckConstraint(
            "((((status)::text = 'draft'::text) OR (notified_at IS NOT NULL)))",
            name="a_submitted_claim_was_notified",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'notified'::character varying, 'assessed'::character varying, "
            "'submitted'::character varying, 'accepted'::character varying, "
            "'rejected'::character varying, 'settled'::character varying, "
            "'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((days_claimed >= (0)::numeric) AND (days_awarded >= (0)::numeric)))",
            name="days_non_negative",
        ),
        CheckConstraint(
            "(((notified_at IS NULL) OR (event_date IS NULL) OR (notified_at >= event_date)))",
            name="notice_is_not_before_the_event",
        ),
    )


class ClaimEvent(ConstructionMixin, Base):
    """One entry in a claim's chronology. **Append-only.**

    Never updated, never deleted. A chronology that can be rewritten is not
    evidence, and disputes are usually lost on exactly this point: "when did the
    employer receive the notice" is answered by this table and by nothing else.

    `occurred_at` is a full timestamp and not a date, because several events in
    one day are normal and their order is the point.
    """

    __tablename__ = "claim_events"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    claim_id: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="note", server_default="note"
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    detail: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Where the evidence is: a `documents` row, a file reference, an email
    #: thread id. Free text, because the evidence for "we met on the 14th" is
    #: sometimes a scan and sometimes a calendar invite.
    evidence_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    recorded_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["contract_claims.organization_id", "contract_claims.id"],
            name="fk_claim_events_claim_id_contract_claims",
        ),
        Index(
            "ix_claim_events_org_claim_occurred",
            "organization_id",
            "claim_id",
            "occurred_at",
        ),
        Index(
            "ix_claim_events_org_type",
            "organization_id",
            "event_type",
        ),
        CheckConstraint(
            "(((event_type)::text = ANY ((ARRAY['notice'::character varying, "
            "'correspondence'::character varying, 'meeting'::character varying, "
            "'assessment'::character varying, 'submission'::character varying, "
            "'decision'::character varying, 'payment'::character varying, "
            "'note'::character varying])::text[])))",
            name="event_type_known",
        ),
    )


class ContractClause(ConstructionMixin, Base):
    """A clause read out of a contract by the Contract Intelligence service.

    Where `contracts` holds what was **agreed**, this holds what was **read**, and
    how sure the reader was. The distinction is the platform's one rule: an agent
    emits a proposal, a human confirms, and the human's confirmation is what lands
    on the contract record. Nothing here is a contract term until a person says so
    — `status='confirmed'` is that moment, and `confirmed_clause_id` is how the
    confirmed value is traced back to the sentence it came from.

    `extraction_confidence` is mandatory for an agent-written row, by the same
    argument as `tender_requirements`: the review queue is ordered by it, and a
    row without one cannot be placed in the queue, so it is a clause that looks
    recorded and will never be read.
    """

    __tablename__ = "contract_clauses"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    contract_id: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    #: The text the agent read, clipped. Not a hash and not the whole document:
    #: a hash makes comparison impossible and the whole document is not evidence
    #: of anything a reviewer needs.
    text_excerpt: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: The value that was read out, where the clause carries one. A JSON column
    #: because a retention clause yields `{"pct": 10}` and a penalty clause yields
    #: `{"pct": 5, "basis": "daily_delay"}` and forcing them into typed columns
    #: would mean a union type in the database.
    extracted_value: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default="{}"
    )
    is_favourable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: How far this departs from the company's standard position, from the agent's
    #: reading of it. Drives "review the four contracts that deviate most".
    deviation_score: Mapped[float | None] = mapped_column(PERCENT, nullable=True)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="extracted", server_default="extracted"
    )
    source_page: Mapped[int | None] = mapped_column(Numeric(6, 0), nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    reviewed_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "contract_id"],
            ["contracts.organization_id", "contracts.id"],
            name="fk_contract_clauses_contract_id_contracts",
        ),
        Index(
            "ix_contract_clauses_org_category",
            "organization_id",
            "category",
        ),
        Index(
            "ix_contract_clauses_org_contract",
            "organization_id",
            "contract_id",
        ),
        Index(
            "ix_contract_clauses_org_review_queue",
            "organization_id",
            "status",
            "extraction_confidence",
        ),
        CheckConstraint(
            "(((category)::text = ANY ((ARRAY['payment'::character varying, "
            "'retention'::character varying, 'penalty'::character varying, "
            "'warranty'::character varying, 'liability'::character varying, "
            "'scope'::character varying, 'termination'::character varying, "
            "'force_majeure'::character varying, 'dispute'::character varying, "
            "'other'::character varying])::text[])))",
            name="category_known",
        ),
        CheckConstraint(
            "((((source)::text <> 'agent_proposal'::text) OR (extraction_confidence IS NOT NULL)))",
            name="agent_row_needs_a_confidence",
        ),
        CheckConstraint(
            "((((status)::text <> 'confirmed'::text) OR (((reviewed_by)::text <> ''::text) AN"
            "D (reviewed_at IS NOT NULL))))",
            name="a_confirmed_clause_names_its_reviewer",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['extracted'::character varying, "
            "'confirmed'::character varying, 'rejected'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((deviation_score IS NULL) OR ((deviation_score >= (0)::numeric) AND (deviation"
            "_score <= (100)::numeric))))",
            name="deviation_score_in_range",
        ),
        CheckConstraint(
            "(((extraction_confidence IS NULL) OR ((extraction_confidence >= (0)::numeric) AN"
            "D (extraction_confidence <= (1)::numeric))))",
            name="confidence_in_range",
        ),
    )


__all__ = [
    "CLAIM_EVENT_TYPES",
    "CLAIM_STATUSES",
    "CLAIM_TYPES",
    "CLAUSE_CATEGORIES",
    "CLAUSE_STATUSES",
    "CONTRACT_PARTY_ROLES",
    "CONTRACT_STATUSES",
    "CONTRACT_TYPES",
    "MILESTONE_KINDS",
    "MILESTONE_STATUSES",
    "SUPPLIER_CATEGORIES",
    "SUPPLIER_STATUSES",
    "VARIATION_ORIGINATORS",
    "VARIATION_STATUSES",
    "ClaimEvent",
    "Contract",
    "ContractClaim",
    "ContractClause",
    "ContractMilestone",
    "ContractVariation",
    "Supplier",
]
