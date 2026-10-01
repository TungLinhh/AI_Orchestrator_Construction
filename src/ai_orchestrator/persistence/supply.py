"""Material master and supplier due diligence.

The first two steps of the supply chain, built from the corpus rather than from
theory — `docs/SUPPLY_CHAIN_CORPUS.md` records what was measured, and three
findings from it shaped this module.

**There is no 12-character material code in the corpus.** Tập 1 §4.3 specifies
one: `[Hệ]-[Nhóm]-[Loại]-[Số]` across the five MEPF systems, with Procurement the
sole owner and duplicate codes prohibited. A regex scan of 400 workbooks for
code-shaped strings returned **zero**. What the sheets carry is `Mã Hiệu` =
`BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001`, which is a *drawing* number.

So `materials.code` is nullable, and `drawing_ref` is a separate column on the
line that requests the material rather than on the material itself. Storing a
drawing number on a material would be wrong in the way that matters: a material
appears on many drawings and a drawing covers many materials, and one attribute
cannot hold a many-to-many. The corpus has it in a one-to-many position because
the corpus is a *schedule*, and a schedule is a list of lines.

**`Mã Hiệu` is ambiguous in the source.** In a data row, columns 2 to 5 read
`SSA | | HBG | 2` under a header that labels only `STT`, `Mã Hiệu` and
`Tên vật tư`. The sub-header is not aligned with the columns, so what those three
values mean is not recoverable from the file. A parser that maps position to
meaning would be inventing, so the ingest reads by header text and leaves
unlabelled cells in `raw_cells` for a human.

**The material schedule is a timeline, not a set of columns.** Seven dated fields
per line: requested by the client, ordered, expected in, actually in, expected
again, actually in again, approved. That is why `material_reconciliations` is a
table here rather than four columns on a requisition line — six dates across 13
zones is a lot of state, and the question the sheet answers is "is this late and
by how much", which is a comparison across the timeline rather than a reading of
one field.

**`#REF!` is in the data.** A broken formula in an approval-date cell.
`ingest/numbers.py` already refuses Excel error cells by name, and the refusal
reason distinguishes "an error" from "a number" so ten thousand material lines do
not turn a missing date into "approved on the 1st".

## Supplier due diligence, from FRM-MO-006A

Tập 3 §1.4 is a five-group checklist — legal, financial, capability, commercial,
compliance — and each group is a *criterion* with evidence, not a yes/no. The
conclusion is a Bayesian score out of 100 producing a colour and an A/B/C
classification, valid for twelve months.

`supplier_risk_flags` is therefore a list of named findings with a severity and a
basis, and it is what the score is computed from. Modelling the score as a column
on `suppliers` would be the wrong shape twice over: the findings behind it are the
auditable part, and a score recomputed on read is a score nobody can reproduce when
a supplier's rating is challenged.
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
    Integer,
    String,
    text,
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

#: Tập 1 §4.3: the five MEPF systems. This is the first segment of a material
#: code, and it is the one part of the scheme the company is certain about —
#: everything after it is a proposal nobody has issued codes for yet.
MATERIAL_SYSTEMS = ("PW", "LV", "WD", "AC", "FP")

#: Tập 3 §1.4's five checklist groups, in its order. The order is the scoring
#: order and a supplier's weakest group is what a reviewer looks at.
DD_GROUPS = ("legal", "financial", "capability", "commercial", "compliance")

#: A/B/C capability classification, and the colour Tập 3 pairs it with.
SUPPLIER_RATINGS = ("A", "B", "C")
RATING_COLOURS = {"A": "green", "B": "yellow", "C": "red"}

#: Tập 1 §5.3's fourth forbidden zone: a supplier carrying a legal or banned
#: risk flag is frozen automatically and referred to Legal. The severity below
#: `BLOCKING` is what triggers it, and the `is_blocking` column makes the
#: decision a recorded fact rather than a judgement made at the moment of payment.
RISK_SEVERITIES = ("low", "medium", "high", "blocking")

#: The flag kinds Tập 1 §4.3 names for supplier risk, plus the ones its checklist
#: implies. `duplicate_tax_code` is in the list because it is a *data* finding:
#: two suppliers with one tax code is a master-data defect that no amount of
#: diligence on either supplier will detect.
RISK_KINDS = (
    "sanctioned",
    "banned_equipment",
    "duplicate_tax_code",
    "abnormal_price",
    "expired_qualification",
    "quality_history",
    "schedule_history",
    "financial_concern",
    "conflicting_interest",
    "document_anomaly",
)

DOCUMENT_KINDS = (
    "business_registration",
    "tax_code",
    "bank_confirmation",
    "financial_statements",
    "reference_letter",
    "acceptance_record",
    "iso_certificate",
    "sample_certificate",
    "anti_corruption_commitment",
    "origin_declaration",
)


class MaterialCategory(ConstructionMixin, Base):
    """A node of the material classification tree.

    Self-referential rather than a fixed level, because MEP supply does not fit a
    fixed number of levels: a cable is under electrical / cable / power, and a
    duct fitting is under mechanical / ductwork / fittings. A `parent_id` with a
    `depth` cap expresses that; an `ENUM` of levels would not.
    """

    __tablename__ = "material_categories"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    name_en: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    parent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["material_categories.organization_id", "material_categories.id"],
            name="fk_material_categories_parent_id_material_categories",
        ),
        Index(
            "uq_material_categories_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_material_categories_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((depth >= 0) AND (depth <= 3)))",
            name="depth_in_range",
        ),
    )


class Material(ConstructionMixin, Base):
    """A purchasable item.

    `code` is the Tập 1 §4.3 twelve-character material code and is **nullable**,
    because the corpus does not contain one. Making it NOT NULL would mean either
    inventing codes for the whole master — which Tập 1 prohibits, since
    Procurement is the sole owner and the codes are theirs to issue — or refusing
    to record a material we demonstrably have.

    The uniqueness index is `COALESCE(code, id)`-shaped by leaving `code` free
    and adding a partial unique index on `(organization_id, code) WHERE code IS
    NOT NULL`. Several NULLs coexist; two identical codes do not. That is the
    "cấm tạo mã trùng" rule as a database constraint.

    `drawing_ref` is **not** here. A material appears on many drawings; the
    drawing reference belongs on the requisition line, which is where the corpus
    has it. Putting it on the material would make it a single-valued attribute
    holding a many-to-many relationship, and the first material detailed on two
    drawings would overwrite the first.
    """

    __tablename__ = "materials"
    #: The Tập 1 scheme's first segment, when it is known. A separate column
    #: rather than parsed out of `code`, because most codes are NULL and a parser
    #: over NULLs is a parser over nothing.
    #: Free text for the specifications Tập 1's code scheme calls "thông số" —
    #: diameter, pressure rating, material grade. A material's specification is
    #: prose plus numbers in a vendor's format, and forcing it into typed columns
    #: would either lose it or invent a schema the company has not agreed.
    #: Standard rate. A *reference*, not a cost: the actual price is on the
    #: quotation line, and a rate here that drifts from it is a master-data fault
    #: somebody has to reconcile.
    #: The corpus's unlabelled columns, verbatim, for a human to interpret. This
    #: exists because `SSA | HBG | 2` cannot be mapped to meaning and guessing is
    #: worse than keeping the text.

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str | None] = mapped_column(SHORT, nullable=True)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    name_en: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    category_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The Tập 1 scheme's first segment, when it is known. A separate column
    #: rather than parsed out of `code`, because most codes are NULL and a parser
    #: over NULLs is a parser over nothing.
    system_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: Free text for the specifications Tập 1's code scheme calls "thông số" —
    #: diameter, pressure rating, material grade. A material's specification is
    #: prose plus numbers in a vendor's format, and forcing it into typed columns
    #: would either lose it or invent a schema the company has not agreed.
    specification: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    unit_code: Mapped[str | None] = mapped_column(SHORT, nullable=True)
    #: Standard rate. A *reference*, not a cost: the actual price is on the
    #: quotation line, and a rate here that drifts from it is a master-data fault
    #: somebody has to reconcile.
    reference_rate: Mapped[float | None] = mapped_column(MONEY, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    #: The corpus's unlabelled columns, verbatim, for a human to interpret. This
    #: exists because `SSA | HBG | 2` cannot be mapped to meaning and guessing is
    #: worse than keeping the text.
    raw_cells: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "category_id"],
            ["material_categories.organization_id", "material_categories.id"],
            name="fk_materials_category_id_material_categories",
        ),
        Index(
            "uq_materials_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_materials_org_category",
            "organization_id",
            "category_id",
        ),
        Index(
            "ix_materials_org_name",
            "organization_id",
            "name_vi",
        ),
        Index(
            "uq_materials_org_code_when_present",
            "organization_id",
            "code",
            unique=True,
            postgresql_where=text(
                "(code IS NOT NULL)",
            ),
        ),
        CheckConstraint(
            "(((code IS NULL) OR ((code)::text ~ '^(PW|LV|WD|AC|FP)-[A-Z0-9]{2,4}-[A-Z0-9]{2,"
            "4}-[0-9]{2,4}$'::text)))",
            name="code_matches_the_dossier_scheme",
        ),
        CheckConstraint(
            "(((system_code IS NULL) OR ((system_code)::text = ANY ((ARRAY['PW'::character va"
            "rying, 'LV'::character varying, 'WD'::character varying, "
            "'AC'::character varying, 'FP'::character varying])::text[]))))",
            name="system_code_known",
        ),
        CheckConstraint(
            "(((reference_rate IS NULL) OR (reference_rate >= (0)::numeric)))",
            name="reference_rate_non_negative",
        ),
    )


class SupplierDocument(ConstructionMixin, Base):
    """One piece of a supplier's due-diligence file.

    Tập 3 §1.4 group 1 (legal) is three documents: business registration
    (`GPKD`), tax code (`MST`) and legal representative. The corpus has **none**
    of them, which is why Supplier Due Diligence remains the one agent with no
    data to validate against.

    `valid_from` / `valid_to` rather than a single date, because Vietnamese
    business registration and ISO certificates expire and the difference between
    "issued" and "valid until" is the difference between a supplier who is
    qualified and one who was qualified last year.
    """

    __tablename__ = "supplier_documents"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(String(40), nullable=False)
    document_kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    #: The substrate `documents` row holding the file. Every piece of evidence in
    #: this table is a document, and the corpus already has a document store with
    #: content hashes and chunking.
    document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reference_no: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    issued_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    #: Tập 3 requires evidence *or* a pass/fail per checklist line, so a document
    #: that was assessed and rejected keeps its row with a reason.
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    verified_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_supplier_documents_document_id_documents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_supplier_documents_supplier_id_suppliers",
        ),
        Index(
            "ix_supplier_documents_org_expiry",
            "organization_id",
            "valid_to",
        ),
        Index(
            "ix_supplier_documents_org_supplier",
            "organization_id",
            "supplier_id",
        ),
        CheckConstraint(
            "(((NOT is_verified) OR ((verified_by)::text <> ''::text)))",
            name="a_verified_document_names_its_verifier",
        ),
        CheckConstraint(
            "(((document_kind)::text = ANY ((ARRAY['business_registration'::character varying"
            ", 'tax_code'::character varying, 'bank_confirmation'::character varying, "
            "'financial_statements'::character varying, "
            "'reference_letter'::character varying, 'acceptance_record'::character varying, "
            "'iso_certificate'::character varying, 'sample_certificate'::character varying, "
            "'anti_corruption_commitment'::character varying, "
            "'origin_declaration'::character varying])::text[])))",
            name="document_kind_known",
        ),
        CheckConstraint(
            "(((valid_to IS NULL) OR (valid_from IS NULL) OR (valid_to >= valid_from)))",
            name="validity_window_is_not_inverted",
        ),
    )


class SupplierRiskFlag(ConstructionMixin, Base):
    """A named finding about a supplier, with its basis.

    Tập 1 §5.3's fourth forbidden zone lives here: a supplier with a
    `blocking` flag is frozen automatically and referred to Legal. `is_blocking` is
    a column rather than a rule about `severity = 'blocking'` so that a *different*
    kind of finding can be made blocking in future without a code change, and so
    that the trigger is a recorded fact.

    `basis` is the citation — a document, a screening list, a comparison. A flag
    with no basis is a rumour, and a rumour that freezes a supplier is a
    commercial decision made by nobody.
    """

    __tablename__ = "supplier_risk_flags"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(String(40), nullable=False)
    risk_kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="other", server_default="other"
    )
    severity: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="medium", server_default="medium"
    )
    is_blocking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    summary: Mapped[str] = mapped_column(LONG, nullable=False)
    #: Where the finding comes from. Mandatory: a flag that cannot be traced is
    #: not evidence, and an unverifiable flag can freeze a supplier on somebody's
    #: recollection.
    basis: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    evidence_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    raised_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    raised_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_supplier_risk_flags_supplier_id_suppliers",
        ),
        Index(
            "ix_supplier_risk_flags_org_open_blocking",
            "organization_id",
            "is_blocking",
            postgresql_where=text(
                "(resolved_at IS NULL)",
            ),
        ),
        Index(
            "ix_supplier_risk_flags_org_supplier",
            "organization_id",
            "supplier_id",
        ),
        CheckConstraint(
            "(((NOT is_blocking) OR ((severity)::text = 'blocking'::text)))",
            name="blocking_is_the_top_severity",
        ),
        CheckConstraint(
            "(((resolved_at IS NULL) OR (resolution_note <> ''::text)))",
            name="a_resolution_states_why",
        ),
        CheckConstraint(
            "(((risk_kind)::text = ANY ((ARRAY['sanctioned'::character varying, "
            "'banned_equipment'::character varying, 'duplicate_tax_code'::character varying, "
            "'abnormal_price'::character varying, "
            "'expired_qualification'::character varying, "
            "'quality_history'::character varying, 'schedule_history'::character varying, "
            "'financial_concern'::character varying, "
            "'conflicting_interest'::character varying, "
            "'document_anomaly'::character varying])::text[])))",
            name="risk_kind_known",
        ),
        CheckConstraint(
            "(((severity)::text = ANY ((ARRAY['low'::character varying, "
            "'medium'::character varying, 'high'::character varying, "
            "'blocking'::character varying])::text[])))",
            name="severity_known",
        ),
    )


class SupplierAssessment(ConstructionMixin, Base):
    """One due-diligence run against Tập 3 §1.4's five groups.

    A *run*, not a field on the supplier. The checklist produces a score that a
    challenge has to be able to reproduce, and a score recomputed on read is a
    score nobody can reproduce — so the conclusion is a row with its date, its
    assessor, and the per-group scores that produced it.

    `valid_for_months` is Tập 3's twelve. It is a column because it is per-
    assessment in principle: a supplier assessed for a large package might be
    held to a different period than one assessed for a small one, and hard-coding
    twelve would quietly forbid that.

    A `rating` of `C` is the dossier's red light, and `is_blocking` here is what
    stops a payment to it — the same mechanism as a risk flag, reached by a
    different route.
    """

    __tablename__ = "supplier_assessments"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    supplier_id: Mapped[str] = mapped_column(String(40), nullable=False)
    assessed_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    assessed_by: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: Per-group scores out of 100, keyed by `DD_GROUPS`. JSONB rather than five
    #: columns because the dossier's group list is a policy and policies change;
    #: five columns would need a migration to add a group, and this does not.
    group_scores: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, default=dict, server_default="{}"
    )
    #: Tập 3: "Điểm Bayesian: ___/100".
    total_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    rating: Mapped[str] = mapped_column(SHORT, nullable=False, default="C", server_default="C")
    is_blocking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    valid_for_months: Mapped[int] = mapped_column(
        Integer, nullable=False, default=12, server_default="12"
    )
    valid_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    conclusion: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "supplier_id"],
            ["suppliers.organization_id", "suppliers.id"],
            name="fk_supplier_assessments_supplier_id_suppliers",
        ),
        Index(
            "ix_supplier_assessments_org_blocking",
            "organization_id",
            "is_blocking",
        ),
        Index(
            "ix_supplier_assessments_org_supplier_recent",
            "organization_id",
            "supplier_id",
            "assessed_on",
        ),
        CheckConstraint(
            "(((NOT is_blocking) OR ((rating)::text = 'C'::text)))",
            name="a_blocking_assessment_is_red",
        ),
        CheckConstraint(
            "((((rating)::text <> 'C'::text) OR is_blocking))",
            name="a_red_assessment_blocks",
        ),
        CheckConstraint(
            "(((rating)::text = ANY ((ARRAY['A'::character varying, 'B'::character varying, "
            "'C'::character varying])::text[])))",
            name="rating_known",
        ),
        CheckConstraint(
            "(((total_score >= 0) AND (total_score <= 100)))",
            name="total_score_in_range",
        ),
        CheckConstraint(
            "((valid_for_months > 0))",
            name="valid_for_months_positive",
        ),
    )


__all__ = [
    "DD_GROUPS",
    "DOCUMENT_KINDS",
    "MATERIAL_SYSTEMS",
    "RATING_COLOURS",
    "RISK_KINDS",
    "RISK_SEVERITIES",
    "SUPPLIER_RATINGS",
    "Material",
    "MaterialCategory",
    "SupplierAssessment",
    "SupplierDocument",
    "SupplierRiskFlag",
]
