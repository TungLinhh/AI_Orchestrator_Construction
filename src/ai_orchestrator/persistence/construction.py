"""Construction domain: projects, commercial, delivery, assurance, process.

Split from `models.py` rather than appended to it. That module is the agent
substrate and its own docstring says so; the construction domain is a different
body of knowledge about a different thing, and merging them produced a file
where finding a table meant reading past forty tables of someone else's
concern. Both declare on the same `Base`, so `Base.metadata` is still the whole
schema and Alembic still autogenerates against one target.

`models.py` remains the platform spine: tenancy, agents, tasks, workflows,
approvals, audit. This module is the business. They meet at exactly three
places, and the boundaries matter more than the tables.

Provenance is the first of them
    `O-Nexus` is built on one rule: **an AI service never writes a business row.**
    It emits a proposal, which passes quarantine, a falsifier and a hash-bound
    approval before anything is committed. So every business row carries
    `source`, and a check constraint makes the claim unforgeable in both
    directions: a row cannot say it came from an agent without naming the
    proposal, and cannot name a proposal while claiming to be human-entered.

    The alternative — trusting the calling code to set `source` correctly — makes
    the entire audit story a convention. This makes it a constraint. When
    someone asks six months from now "did a model write this number", the answer
    is a query, not an archaeology project.

    The `ai_orchestrator` proposals machinery is reused as-is for this. That is
    deliberate: the loop already proven to catch a defective procedure is the
    same loop that has to catch a defective extraction rule, and a second
    mechanism would be a second thing to get wrong.

Tenancy and money follow the spine's rules unchanged
    `organization_id NOT NULL` leading every index, `NUMERIC` for money,
    `timestamptz` for time. Those are not repeated here because they are already
    true of all 45 existing tables; a second dialect would be a second thing to
    forget.

Quantities and units are separate columns
    The single most common estimate error in construction is a quantity whose
    unit does not match the rate. So `quantity` and `unit_code` are distinct,
    `unit_code` references `units_dictionary`, and there is no column that holds
    both. "12.5m" cannot be stored because "12.5m" is not a number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import MONEY, Base, created_at_col, updated_at_col

ID = String(40)
SHORT = String(128)
NAME = String(255)
LONG = Text

#: A measured quantity: 4 decimal places, which is finer than a cubic metre or a
#: running metre ever needs, and coarse enough that a BOQ does not accumulate
#: meaningless precision across ten thousand lines. `RATE` keeps the money
#: precision because a unit rate is money per unit. Neither is ever a float:
#: `0.1 + 0.2` has to equal `0.3` in a quantity take-off.
QUANTITY = Numeric(18, 4)
RATE = Numeric(18, 6)
#: A measured area or volume on a zone.
AREA = Numeric(18, 2)

#: `source` values. A closed set, because the whole point is that it is
#: checkable.
SOURCE_HUMAN = "human"
SOURCE_AGENT = "agent_proposal"
SOURCE_IMPORT = "import"
SOURCE_SYSTEM = "system"
SOURCES = (SOURCE_HUMAN, SOURCE_AGENT, SOURCE_IMPORT, SOURCE_SYSTEM)


#: A `CheckConstraint` belongs to exactly one `Table`, so a module-level tuple
#: of them reused across ten tables would either raise or — worse — silently
#: attach to one and vanish from the rest. Each call mints fresh instances.
def provenance_checks() -> tuple[CheckConstraint, CheckConstraint]:
    """The two constraints that make the provenance claim checkable.

    Written as a factory rather than a constant because of the one-owner rule
    above, and as a factory rather than a mixin `__table_args__` because of a
    measured failure: a mixin's `__table_args__` is *replaced*, not merged, by a
    subclass that declares its own. Every table here declares its own indexes,
    so a mixin-declared constraint reached zero of the ten tables while every
    test still passed. `tests/unit/test_construction_schema.py` now asserts both
    constraints are present on every construction table, and the live-database
    test asserts the database refuses the bad rows.
    """
    return (
        CheckConstraint(f"source IN ({','.join(repr(s) for s in SOURCES)})", name="source_known"),
        # The provenance claim and the evidence for it cannot disagree. A row
        # that says `agent_proposal` with no proposal is a row nobody can audit;
        # a row with a proposal that says `human` is a row laundering a model
        # write past the approval engine. Both are refused by the database
        # rather than by a code review that has to remember.
        #
        # The name is `agent_source_needs_proposal` and not the longer
        # `proposal_required_for_agent_source` because of a measured limit. The
        # naming convention prefixes `ck_<table_name>_`, and the longest table in
        # the schema is `gate_criterion_evaluations` at 26 characters. Postgres
        # truncates identifiers over 63 *bytes* silently, keeping a prefix and
        # appending `_` plus four hex digits — so the 64-byte version reached the
        # database as `ck_gate_criterion_evaluations_proposal_required_for_age_dd1c`
        # and the rule could no longer be found by grepping for its own name. The
        # 28-character suffix puts the longest case at 58 bytes.
        # `tests/unit/test_construction_schema.py::TestIdentifiersFitPostgres` is
        # what stops the next table from reintroducing it.
        CheckConstraint(
            f"(source = '{SOURCE_AGENT}') = (proposal_id IS NOT NULL)",
            name="agent_source_needs_proposal",
        ),
    )


def domain_args(*extra: Any) -> tuple[Any, ...]:
    """`__table_args__` for a construction table: the provenance rules, then yours.

    Every table spells it as `__table_args__ = domain_args(...)` so the rules are
    impossible to leave off by accident without the word `domain_args` being
    visibly absent, which is reviewable in a diff.
    """
    return (*provenance_checks(), *extra)


class ConstructionMixin:
    """Columns every business table carries.

    A mixin rather than a base class so these tables stay ordinary `Base`
    subclasses — one declarative registry, one metadata, no second mapper to
    keep configured.

    Columns only. Constraints live in `domain_args` for the reason documented
    there.

    `proposal_id` points at the `ai_orchestrator` proposal that authorised the
    write. It is deliberately not a foreign key: proposals are substrate rows
    with their own retention story, and a construction table that cannot be
    inserted because a proposal was reaped is a construction table that lies
    about what happened.
    """

    organization_id: Mapped[str] = mapped_column(ID, nullable=False)
    source: Mapped[str] = mapped_column(
        SHORT, nullable=False, default=SOURCE_HUMAN, server_default=SOURCE_HUMAN
    )
    #: Who or what acted. A person name, a service name, or `migration:0007`.
    #: Free text, not an FK, because it has to survive the departure of the actor.
    source_actor: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    proposal_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


# ======================================================== clients & projects ==
class Client(ConstructionMixin, Base):
    """The party a contract is signed with. Also the owner/client in a tender."""

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    short_name: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: Vietnamese tax code, 10 or 13 digits. Text, not a number, because leading
    #: zeros are significant and an integer column would eat them.
    tax_code: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    address: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    city: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    country: Mapped[str] = mapped_column(SHORT, nullable=False, default="VN", server_default="VN")
    email: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    phone: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="active", server_default="active"
    )

    __table_args__ = domain_args(
        Index(
            "uq_clients_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_clients_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        Index(
            "ix_clients_org_name",
            "organization_id",
            "name",
        ),
    )


class ClientContact(ConstructionMixin, Base):
    """A named person at a client. Needed because approvals are addressed to
    people, and "the client" is not a thing that can sign anything."""

    __tablename__ = "client_contacts"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    client_id: Mapped[str] = mapped_column(ID, nullable=False)
    full_name: Mapped[str] = mapped_column(NAME, nullable=False)
    title: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    email: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    phone: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_client_contacts_client_id_clients",
        ),
        Index(
            "uq_client_contacts_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_client_contacts_org_client",
            "organization_id",
            "client_id",
        ),
    )


class Project(ConstructionMixin, Base):
    """A delivery engagement.

    The `contract_value` here is the *signed* value and is deliberately not
    derived from the contract table: a project predates its contract (site
    mobilisation work happens first) and a contract is a document with a
    lifecycle of its own. One source of truth for money is `contracts`; this
    column is the commercial envelope agreed at award, and a test asserts the
    two agree once a contract is signed.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    client_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    project_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="construction", server_default="construction"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="active", server_default="active"
    )
    contract_value: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    address: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Stored UTC, rendered in this zone. Construction reports are read in local
    #: time and the difference is a real class of bug at 02:00.
    timezone: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="Asia/Ho_Chi_Minh", server_default="Asia/Ho_Chi_Minh"
    )
    planned_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    planned_completion: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "client_id"],
            ["clients.organization_id", "clients.id"],
            name="fk_projects_client_id_clients",
        ),
        Index(
            "uq_projects_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_projects_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        Index(
            "ix_projects_org_status",
            "organization_id",
            "status",
        ),
    )


class ProjectRole(ConstructionMixin, Base):
    """Who holds a role on a project.

    This is the table DOA routing resolves against, and the reason the platform
    can route an approval to a person rather than to a department. A department
    is a queue; a project role is a name with a fallback chain.

    `person_ref` is opaque — a user id, an email, a name in a spreadsheet. It is
    not an FK to `users` because the platform deliberately has no authentication
    and therefore no authoritative user table, and inventing one here would put
    a broken referential promise in the approval path.
    """

    __tablename__ = "project_roles"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    project_id: Mapped[str] = mapped_column(ID, nullable=False)
    #: Matches `doa_matrix.role_key`, e.g. `pm`, `hse_manager`, `finance_director`.
    role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    person_name: Mapped[str] = mapped_column(NAME, nullable=False)
    person_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: Ordered fallback. `is_primary` is the head of the chain, not a separate
    #: concept, so a vacancy cannot leave an approver with no successor.
    fallback_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_project_roles_project_id_projects",
        ),
        Index(
            "ix_project_roles_org_project",
            "organization_id",
            "project_id",
        ),
        Index(
            "ix_project_roles_org_role_key",
            "organization_id",
            "role_key",
            "project_id",
        ),
    )


class ProjectPhase(ConstructionMixin, Base):
    """Mobilisation, construction, testing, handover. A coarse calendar that
    projects do not always agree with the contractual milestones."""

    __tablename__ = "project_phases"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    project_id: Mapped[str] = mapped_column(ID, nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    planned_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    planned_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="planned", server_default="planned"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_project_phases_project_id_projects",
        ),
        Index(
            "ix_project_phases_org_project",
            "organization_id",
            "project_id",
            "sequence",
        ),
    )


# =================================================================== zones ==
class UnitDictionary(ConstructionMixin, Base):
    """Controlled list of units of measure.

    Natural key, and a composite one: the primary key is
    `(organization_id, code)`, not a surrogate `id`.

    That is forced, not preferred. Every table in this schema is tenant-scoped,
    and a vocabulary is tenant-scoped too — but "m2" means the same thing to
    both tenants, so there is one row per tenant per unit, and no single-column
    key can be both unique and tenant-correct. An earlier draft of this file
    gave the table a ULID and had `wbs_items` reference `code`; the database
    refused it, because the only unique index on `code` was
    `(organization_id, code)` and a foreign key needs a single-column target.

    Making the key composite rather than widening it to a surrogate fixes the
    error and closes a hole at the same time. `wbs_items` then carries a
    composite foreign key over `(organization_id, unit_code)`, so a quantity
    cannot reference a unit belonging to another tenant — not because a policy
    forbids it, but because the row could not be written. RLS already prevented
    reading another tenant's vocabulary; this prevents *pointing at* it.

    Codes are ASCII because they arrive in spreadsheet headers. Names are
    bilingual because the documents are.
    """

    __tablename__ = "units_dictionary"

    # Not `primary_key=True`: the key is the composite declared in
    # `__table_args__`, and marking `code` as a primary key on its own makes
    # SQLAlchemy warn that the two disagree and silently pick one.
    code: Mapped[str] = mapped_column(SHORT)
    name_vi: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    name_en: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: length | area | volume | count | time | mass | lump. Lets the cost
    #: analyst refuse to multiply a rate in VND by a quantity in days.
    dimension: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="count", server_default="count"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        PrimaryKeyConstraint("organization_id", "code"),
        Index(
            "ix_units_dictionary_org_code",
            "organization_id",
            "code",
        ),
        CheckConstraint(
            "(((dimension)::text = ANY ((ARRAY['length'::character varying, "
            "'area'::character varying, 'volume'::character varying, "
            "'count'::character varying, 'time'::character varying, "
            "'mass'::character varying, 'lump'::character varying])::text[])))",
            name="dimension_known",
        ),
    )


class Zone(ConstructionMixin, Base):
    """A physical area of a site: a building, a block, a zone code.

    The corpus is organised this way — Bãi Tràm is tracked by zone across
    BOH, BPV, BSN, BUT, CLU, GEN, HPV, INF, KID and the rest — so zone is a
    first-class dimension of the domain rather than a property of a work item.
    """

    __tablename__ = "zones"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    project_id: Mapped[str] = mapped_column(ID, nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    parent_zone_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    area_m2: Mapped[float] = mapped_column(AREA, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "parent_zone_id"],
            ["zones.organization_id", "zones.id"],
            name="fk_zones_parent_zone_id_zones",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_zones_project_id_projects",
        ),
        Index(
            "uq_zones_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_zones_org_project",
            "organization_id",
            "project_id",
        ),
        Index(
            "ix_zones_org_project_code",
            "organization_id",
            "project_id",
            "code",
            unique=True,
        ),
    )


# ===================================================================== WBS ==
class Wbs(ConstructionMixin, Base):
    """Work breakdown structure node. A tree of scope, not a schedule.

    Separated from `milestones` because the two answer different questions and
    are updated by different people: the WBS is the estimating department's
    structure and is stable once priced, the milestone set carries dates and
    moves weekly.
    """

    __tablename__ = "wbs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    project_id: Mapped[str] = mapped_column(ID, nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_leaf: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["wbs.organization_id", "wbs.id"],
            name="fk_wbs_parent_id_wbs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_wbs_project_id_projects",
        ),
        Index(
            "uq_wbs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_wbs_org_project",
            "organization_id",
            "project_id",
            "sequence",
        ),
        Index(
            "ix_wbs_org_project_code",
            "organization_id",
            "project_id",
            "code",
            unique=True,
        ),
    )


class WbsItem(ConstructionMixin, Base):
    """A priced line against a WBS node.

    The four money columns are all here rather than three because
    `quantity * rate` is not always `amount`: provisional sums, daywork and
    measured work genuinely disagree, and forcing agreement here would force an
    estimator to invent precision. `amount` is what the project is committed to;
    a line where the arithmetic does not close is a signal, and the cost analyst
    exists to find it.
    """

    __tablename__ = "wbs_items"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    wbs_id: Mapped[str] = mapped_column(ID, nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False)
    #: Half of the composite foreign key into `units_dictionary`. The other half
    #: is `organization_id` from the mixin, and that is what makes the reference
    #: tenant-safe: a line cannot name a unit belonging to another tenant,
    #: because the pair `(organization_id, unit_code)` either exists in this
    #: tenant's vocabulary or the insert is refused. RLS stops the *read*; this
    #: stops the *pointer*, which is the direction that corrupts an estimate.
    unit_code: Mapped[str] = mapped_column(SHORT, nullable=False)
    quantity: Mapped[float] = mapped_column(QUANTITY, nullable=False, default=0, server_default="0")
    unit_rate: Mapped[float] = mapped_column(RATE, nullable=False, default=0, server_default="0")
    amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    #: How confident an automated extraction was in this quantity and unit, kept
    #: so a wrong extraction can be traced back to the field that carried it.
    extraction_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "unit_code"],
            ["units_dictionary.organization_id", "units_dictionary.code"],
            name="fk_wbs_items_organization_id_units_dictionary",
        ),
        ForeignKeyConstraint(
            ["organization_id", "wbs_id"],
            ["wbs.organization_id", "wbs.id"],
            name="fk_wbs_items_wbs_id_wbs",
        ),
        Index(
            "uq_wbs_items_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_wbs_items_org_wbs",
            "organization_id",
            "wbs_id",
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


class Milestone(ConstructionMixin, Base):
    """A dated event on a project.

    `kind` splits contractual from internal because the consequences differ
    sharply: a missed contractual milestone is a claim, a missed internal one is
    a conversation. `gate_code` links a milestone to the Gate spine without a
    foreign key to it, since gates arrive in a later tranche and a forward
    reference would make this migration non-reversible on its own.
    """

    __tablename__ = "milestones"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    project_id: Mapped[str] = mapped_column(ID, nullable=False)
    wbs_id: Mapped[str | None] = mapped_column(ID, nullable=True)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="internal", server_default="internal"
    )
    gate_code: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    baseline_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    forecast_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pending", server_default="pending"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_milestones_project_id_projects",
        ),
        ForeignKeyConstraint(
            ["organization_id", "wbs_id"],
            ["wbs.organization_id", "wbs.id"],
            name="fk_milestones_wbs_id_wbs",
        ),
        Index(
            "ix_milestones_org_gate",
            "organization_id",
            "gate_code",
        ),
        Index(
            "ix_milestones_org_project",
            "organization_id",
            "project_id",
        ),
        CheckConstraint(
            "(((kind)::text = ANY ((ARRAY['contractual'::character varying, "
            "'internal'::character varying, 'gate'::character varying])::text[])))",
            name="milestone_kind_known",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['pending'::character varying, "
            "'at_risk'::character varying, 'achieved'::character varying, "
            "'missed'::character varying])::text[])))",
            name="milestone_status_known",
        ),
    )


__all__ = [
    "AREA",
    "SOURCES",
    "SOURCE_AGENT",
    "SOURCE_HUMAN",
    "SOURCE_IMPORT",
    "SOURCE_SYSTEM",
    "Client",
    "ClientContact",
    "ConstructionMixin",
    "Milestone",
    "Project",
    "ProjectPhase",
    "ProjectRole",
    "UnitDictionary",
    "Wbs",
    "WbsItem",
    "Zone",
    "domain_args",
    "provenance_checks",
]
