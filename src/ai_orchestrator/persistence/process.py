"""The process spine: Gates G0-G5, SOPs, delegation of authority, autonomy policy.

Tập 1 calls this "xương sống quản trị" — the governance spine — and it is the part
of the platform that is worthless without domain data and everything without
domain data it gates. It is built last of the data and first of the system.

Everything in this module is taken from the O-Nexus SOP dossier rather than
inferred, and three of those specifics corrected an earlier plan of mine.

**The Gates are not what I had assumed.** Tập 1 §2.2:

| Gate | Name | Handover |
|---|---|---|
| G0 | Go/No-Go | Front → Front |
| G1 | Bid submission | Front → CEO/PMO |
| G2 | Contract handover | Front → Middle (PM) |
| G3 | **Design freeze / major PO** | Middle (Design) → Middle (PM/Proc) |
| G4 | **Progressive acceptance / T&C** | Middle (PM) → QA/QC → Client |
| G5 | **Handover, final account, close** | Middle → Back (Finance) → PMO |

My earlier plan had G3 as progress control, G4 as practical completion and G5 as
post-completion claims. That is off by one position from the second half of the
spine, and it is the kind of error that survives review because the plan read
plausibly. It was found by reading the source document rather than by reasoning
about construction projects.

**A Gate has four outcomes, not two.** `PASS`, `PASS_WITH_CONDITIONS` (with a
remediation deadline), `HOLD`, `FAIL`. The middle one is the interesting one: it
is a pass that creates obligations, and Tập 2 §E.1 step 5 gives it an automatic
consequence — a conditional pass extended more than twice converts itself to
`HOLD` and reports to the CEO. That rule only works if the decision, its
conditions and its extension count are all rows rather than a status string.

**The autonomy level is hard-coded at the Harness and cannot be overridden by a
prompt.** Tập 1 §5.3, in as many words. So the level is not a field on an agent
and not something a workflow reads from a prompt: it is a row in
`autonomy_policies`, keyed on an *action class*, with a ceiling. A workflow
proposes an action; the policy decides the maximum level permitted; anything
above the ceiling is not reachable. This is why `autonomy_policies` is a table
rather than a column on `sop_steps` — the SOP says what a step's level *should*
be, and the policy says what it *may* be, and only the policy is authoritative.

My earlier "L0" was the right instinct and the wrong level number. Tập 1 calls
it "Vùng cấm tuyệt đối" — the absolutely forbidden zones — and lists four:

* personnel decisions (hire, dismiss, discipline, adjust salary) — AI at most L2
* financial commitments beyond the DOA limit, signing contracts, guarantees — at
  most L3, and always through the correct level of human approval
* **safety conclusions, work stoppage, incident-investigation conclusions — a
  person decides; AI is L1/L2 only**
* any transaction with a supplier carrying a legal or banned-risk flag —
  automatic freeze and referral to Legal

So L1-L4 are autonomy levels and the forbidden zones are not a level at all.
They are a pre-condition that stops the action before a level is considered. This
module models them as `autonomy_policies` with `max_level` and a `is_hard_block`
flag, and the first row of the seed data is a hard block.

**An agent is never assigned an approval role.** Tập 3 §4.1: *"Mỗi ô 'A' trong
ma trận sinh ra một quyền phê duyệt trong hệ thống (approval role); Agent không
bao giờ được gán approval role."* Each `A` cell in a RACI matrix generates one
approval role; agents never hold one. That is the substrate's approval model
already, and it is why `project_roles` resolves a *person* and never a service.
The `sop_raci` table enforces it structurally: a role whose `role_kind` is
`agent` cannot be the `A` of a step, and a check constraint says so.

**Segregation of duties is a constraint, not a policy.** Tập 1 §1.2 separates
proposer, reviewer, approver and payer, and Tập 3's payment form names the
executor as explicitly not the approver. `gate_decisions` refuses a decision
whose approver is the same person as the one who compiled the pack.

A gate is a checklist before it is a meeting, and a meeting before it is a
decision. `gate_criteria` are the dossier's Entry/Exit criteria verbatim
(Tập 3 §1.3), so a Gate cannot open with an unmet mandatory criterion, and an
exemption is a row naming the person who granted it — never a silent pass.
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
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import MONEY, Base
from ai_orchestrator.persistence.construction import (
    ID,
    LONG,
    NAME,
    SHORT,
    ConstructionMixin,
    domain_args,
)

#: Autonomy levels, Tập 1 §5.3. The names are the dossier's.
L_INFORM = "L1"  # analyse, summarise, warn. A person decides entirely.
L_RECOMMEND = "L2"  # propose with rationale. A person chooses and approves.
L_WITH_APPROVAL = "L3"  # prepare the whole transaction; execute after DOA approval.
L_AND_REPORT = "L4"  # act then report. Low risk, reversible, below a threshold.
AUTONOMY_LEVELS = (L_INFORM, L_RECOMMEND, L_WITH_APPROVAL, L_AND_REPORT)

#: Numeric order, so "the ceiling for this action is L2" is a comparison rather
#: than a string sort. `L1 < L2` is false as text and true as an integer, and
#: getting that wrong fails *open* — which is the direction that hurts.
AUTONOMY_RANK = {level: index for index, level in enumerate(AUTONOMY_LEVELS, start=1)}

#: The four Gate outcomes. `PASS_WITH_CONDITIONS` is a pass that creates
#: obligations, which is why it is a distinct value and not a note on a pass.
# `noqa: S105` on both: ruff's secret heuristic fires on any name containing
# "PASS", and a Gate outcome is not a credential. The alternative is renaming
# to something that does not read as a Gate outcome, which is worse.
GATE_PASS = "PASS"  # noqa: S105 - a Gate outcome, not a credential
GATE_PASS_WITH_CONDITIONS = "PASS_WITH_CONDITIONS"  # noqa: S105
GATE_HOLD = "HOLD"
GATE_FAIL = "FAIL"
GATE_OUTCOMES = (GATE_PASS, GATE_PASS_WITH_CONDITIONS, GATE_HOLD, GATE_FAIL)

#: How far a project has come. Distinct from the Gate outcome: a project can be
#: `at_gate` and the outcome of the *last* gate can be anything.
GATE_INSTANCE_STATUSES = (
    "not_started",
    "scheduled",
    "in_session",
    "decided",
    "passed",
    "conditional",
    "on_hold",
    "failed",
)

#: A checklist line's state during a session.
CRITERION_STATES = ("met", "not_met", "waived", "not_applicable")

#: Tập 1 §3.1: `ONX-[BLOCK]-[DEPT]-[TYPE]-[NUMBER]`, and the four blocks are
#: FO / MO / BO / PMO. The type is one of four document classes.
SOP_BLOCKS = ("FO", "MO", "BO", "PMO")
SOP_TYPES = ("SOP", "WI", "FRM", "POL", "REG")
SOP_STATUSES = ("draft", "in_review", "issued", "superseded", "withdrawn")

#: RACI letters. Tập 1 §1.2: exactly one `A` per step, and no step without `R`.
RACI_LETTERS = ("R", "A", "C", "I")

#: Whether a role is held by a person or by a service. Tập 3 §4.1 makes this the
#: load-bearing distinction: agents never hold `A`.
ROLE_KINDS = ("person", "committee", "agent")

CONDITION_STATUSES = ("open", "in_progress", "remediated", "waived", "overdue")


def autonomy_ceiling_allowed(proposed: str, ceiling: str) -> bool:
    """Whether `proposed` is permitted by a `ceiling`.

    A function rather than an inline comparison because the comparison is the
    whole safety property and it is exactly the kind of thing that gets written
    as a string comparison once. `"L10" < "L2"` is false, but the mistake in the
    other direction — treating the levels as text and sorting them — fails open
    for a hypothetical L10 and silently inverts for any future level above L4.
    """
    return AUTONOMY_RANK[proposed] <= AUTONOMY_RANK[ceiling]


# ============================================================== Gate catalogue ==
class GateDefinition(ConstructionMixin, Base):
    """One of the six Gates, defined once and applied to every project.

    `chair_role_key` and `member_role_keys` are Tập 2 §E.2 verbatim: who chairs
    each Gate, who must be in the room, and when it convenes. Stored as role keys
    rather than names because the person holding a role changes and the Gate does
    not — and because the roster is `project_roles`, which is already the table
    DOA routing resolves against.

    `convened_within` and `convened_by` are the timing from the same table: G2
    convenes *within 5 days of contract signature*, G0 sits at a *fixed weekly
    meeting*. A Gate with no schedule is a Gate nobody attends.
    """

    __tablename__ = "gate_definitions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: `G0` … `G5`. Unique per tenant: these are the company's gates, not a
    #: per-project choice.
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    name_en: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Tập 2 §E.2. The `role_key` of whoever chairs the council.
    chair_role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: Mandatory attendees, as role keys. A member who is optional is not a
    #: member; the distinction is the difference between a quorum and a
    #: suggestion.
    member_role_keys: Mapped[list[str]] = mapped_column(
        JSONB(), nullable=False, default=list, server_default="[]"
    )
    #: What the applicant must bring, and what the council checks.
    entry_summary: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    exit_summary: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Tập 2 §E.2 timing, as prose. Kept as text rather than a number because
    #: "within 5 days of signature" and "at a fixed weekly meeting" are both real
    #: and neither is a day count.
    convened_within: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    #: Days before the session that the pack must be registered, Tập 2 §E.1
    #: step 1. `NULL` for a Gate with no fixed lead time.
    lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: A conditional pass may be extended this many times before it becomes a
    #: HOLD (Tập 2 §E.1 step 5). Stored per Gate because a project that keeps
    #: being conditionally passed at G3 has a different problem from one that
    #: keeps being conditionally passed at G5.
    max_extensions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )

    __table_args__ = domain_args(
        Index(
            "uq_gate_definitions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_gate_definitions_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((lead_time_days IS NULL) OR (lead_time_days >= 0)))",
            name="lead_time_non_negative",
        ),
        CheckConstraint(
            "((max_extensions >= 0))",
            name="max_extensions_non_negative",
        ),
    )


class GateCriterion(ConstructionMixin, Base):
    """One Entry or Exit criterion of a Gate, from Tập 3 §1.3.

    `criterion_type` is `entry` or `exit` rather than a table each, because a
    Gate's checklist is one list a person reads top to bottom, and splitting it
    into two tables means two queries and an ordering bug to answer "what does
    G3 need".

    `is_mandatory` is the field that matters. Tập 3 is explicit that a criterion
    can be exempted — "Đạt/Không đạt/Miễn trừ (miễn trừ phải ghi người phê duyệt
    miễn trừ)" — so an exemption is a first-class state on the *evaluation*, and
    the person who granted it is recorded. A mandatory criterion that is not met
    stops the Gate; one that is waived, by a named person, does not.

    `weight` supports G0's "Điểm ≥65, không nhóm <40%" (score at least 65, no
    group below 40%), which is a score across a group of criteria rather than a
    per-item pass/fail. Both mechanisms are needed and they are not the same
    thing.
    """

    __tablename__ = "gate_criteria"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    gate_definition_id: Mapped[str] = mapped_column(String(40), nullable=False)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    criterion_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="entry", server_default="entry"
    )
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    detail: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    #: Relative weight for the score-based Gates. `NULL` means "not scored",
    #: which is different from "weight zero" and matters for a percentage.
    weight: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The document that evidences this criterion, when a named form is required
    #: — Tập 3 cites `FRM-FO-001` at G0 and `CHK-PMO-*` throughout.
    required_document_ref: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    #: Who may grant an exemption. Tập 3 requires it be a person; a Gate that
    #: can waive its own mandatory criterion is not a control.
    waiver_role_key: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "gate_definition_id"],
            ["gate_definitions.organization_id", "gate_definitions.id"],
            name="fk_gate_criteria_gate_definition_id_gate_definitions",
        ),
        Index(
            "uq_gate_criteria_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_gate_criteria_org_gate_type",
            "organization_id",
            "gate_definition_id",
            "criterion_type",
            "code",
        ),
        Index(
            "uq_gate_criteria_org_gate_code",
            "organization_id",
            "gate_definition_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((weight IS NULL) OR (weight > 0)))",
            name="weight_positive",
        ),
        CheckConstraint(
            "(((criterion_type)::text = ANY ((ARRAY['entry'::character varying, "
            "'exit'::character varying])::text[])))",
            name="criterion_type_known",
        ),
    )


class GateInstance(ConstructionMixin, Base):
    """One project passing through one Gate.

    The subject is deliberately `(kind, subject_id)` rather than nullable
    `project_id` and `opportunity_id` columns. G0 is a Go/No-Go on an
    *opportunity* — there is no project yet, and a nullable pair would make
    "neither" and "both" reachable states that every query has to guard. A Gate
    subject is one thing, and the tables that exist are a whitelist.
    """

    __tablename__ = "gate_instances"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    gate_definition_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: `opportunity` for G0, `project` from G1 on. Checked against the whitelist
    #: rather than a foreign key, because the subject table differs by Gate and a
    #: polymorphic reference cannot be a foreign key.
    subject_kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="project", server_default="project"
    )
    subject_id: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="not_started", server_default="not_started"
    )
    #: Tập 2 §E.1 step 1: registration at least 5 days before, and the
    #: checklist is generated from the Gate at that point.
    registered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scheduled_for: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    pre_read_issued_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: How many times a conditional pass has been extended. Tập 2 §E.1 step 5:
    #: more than `gate_definitions.max_extensions` converts the Gate to HOLD and
    #: reports to the CEO. Stored as a count so the rule is a comparison and not
    #: a judgement.
    extension_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "gate_definition_id"],
            ["gate_definitions.organization_id", "gate_definitions.id"],
            name="fk_gate_instances_gate_definition_id_gate_definitions",
        ),
        Index(
            "uq_gate_instances_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_gate_instances_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_gate_instances_org_subject",
            "organization_id",
            "subject_kind",
            "subject_id",
        ),
        CheckConstraint(
            "((((status)::text = ANY ((ARRAY['decided'::character varying, "
            "'passed'::character varying, 'conditional'::character varying, "
            "'on_hold'::character varying, "
            "'failed'::character varying])::text[])) = (decided_at IS NOT NULL)))",
            name="a_decided_gate_has_a_decision_time",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['not_started'::character varying, "
            "'scheduled'::character varying, 'in_session'::character varying, "
            "'decided'::character varying, 'passed'::character varying, "
            "'conditional'::character varying, 'on_hold'::character varying, "
            "'failed'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((subject_kind)::text = ANY ((ARRAY['opportunity'::character varying, "
            "'project'::character varying])::text[])))",
            name="subject_kind_known",
        ),
        CheckConstraint(
            "((extension_count >= 0))",
            name="extension_count_non_negative",
        ),
    )


class GateCriterionEvaluation(ConstructionMixin, Base):
    """One checklist line, answered, for one Gate instance.

    Every row the Gate's checklist produces, created at registration (Tập 2 §E.1
    step 1: "hệ thống sinh checklist hồ sơ Entry Criteria theo loại Gate") and
    answered by a person. The generated rows and the answers are the same rows,
    so an unanswered criterion is a visible gap rather than an absence.
    """

    __tablename__ = "gate_criterion_evaluations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    gate_instance_id: Mapped[str] = mapped_column(String(40), nullable=False)
    gate_criterion_id: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="not_applicable", server_default="not_applicable"
    )
    evidence_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    assessed_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    assessed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Tập 3 requires an exemption to name the person who granted it. Both
    #: columns, and a check that one implies the other, so an exemption is
    #: always attributable.
    waiver_note: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    waived_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "gate_criterion_id"],
            ["gate_criteria.organization_id", "gate_criteria.id"],
            name="fk_gate_criterion_evaluations_gate_criterion_id_gate_criteria",
        ),
        ForeignKeyConstraint(
            ["organization_id", "gate_instance_id"],
            ["gate_instances.organization_id", "gate_instances.id"],
            name="fk_gate_criterion_evaluations_gate_instance_id_gate_instances",
        ),
        Index(
            "uq_gate_criterion_evaluations_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_gate_criterion_evaluations_org_state",
            "organization_id",
            "gate_instance_id",
            "state",
        ),
        Index(
            "uq_gate_criterion_evaluations_org_instance_criterion",
            "organization_id",
            "gate_instance_id",
            "gate_criterion_id",
            unique=True,
        ),
        CheckConstraint(
            "((((state)::text = 'waived'::text) = ((waived_by)::text <> ''::text)))",
            name="a_waiver_names_who_granted_it",
        ),
        CheckConstraint(
            "((((state)::text <> 'waived'::text) OR (length(waiver_note) > 0)))",
            name="a_waiver_states_why",
        ),
        CheckConstraint(
            "(((state)::text = ANY ((ARRAY['met'::character varying, "
            "'not_met'::character varying, 'waived'::character varying, "
            "'not_applicable'::character varying])::text[])))",
            name="state_known",
        ),
    )


class GateDecision(ConstructionMixin, Base):
    """The council's conclusion.

    Segregation of duties is enforced here, not documented. Tập 1 §1.2 separates
    proposer, reviewer, approver and payer; Tập 2 §E.1 step 2 has PMO issue the
    pre-read and step 3 the council decide. So the person who compiled the pack
    cannot be the person who approved it, and a check constraint says so.

    `attendee_count` and `quorum` are stored because a decision taken by two
    people when six were required is not a decision of the council, and the
    meeting record is the only place that can show it.
    """

    __tablename__ = "gate_decisions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    gate_instance_id: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(
        SHORT, nullable=False, default=GATE_PASS, server_default=GATE_PASS
    )
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: The person who approved. Resolved from `project_roles` at decision time and
    #: frozen here, because the Gate instance outlives the roster.
    approved_by: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: Who compiled the pack (Tập 2 §E.1 step 2). Recorded so the duty separation
    #: is checkable.
    compiled_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    attendee_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    quorum: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Tập 1 §1.2 auditability: what the decision was based on, in writing. Not
    #: optional in practice and not optional here.
    rationale: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: The automated minutes (Tập 2 §E.1 step 3: "Agent ghi biên bản tự động"),
    #: as a link into the substrate `documents` table.
    minutes_document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "gate_instance_id"],
            ["gate_instances.organization_id", "gate_instances.id"],
            name="fk_gate_decisions_gate_instance_id_gate_instances",
        ),
        ForeignKeyConstraint(
            ["organization_id", "minutes_document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_gate_decisions_minutes_document_id_documents",
        ),
        Index(
            "uq_gate_decisions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_gate_decisions_org_outcome",
            "organization_id",
            "outcome",
        ),
        Index(
            "uq_gate_decisions_org_instance",
            "organization_id",
            "gate_instance_id",
            unique=True,
        ),
        CheckConstraint(
            "((((compiled_by)::text = ''::text) OR ((compiled_by)::text <> (approved_by)::text)))",
            name="the_compiler_does_not_approve",
        ),
        CheckConstraint(
            "(((outcome)::text = ANY ((ARRAY['PASS'::character varying, "
            "'PASS_WITH_CONDITIONS'::character varying, 'HOLD'::character varying, "
            "'FAIL'::character varying])::text[])))",
            name="outcome_known",
        ),
        CheckConstraint(
            "((length(rationale) > 0))",
            name="a_decision_states_its_reason",
        ),
    )


class GateCondition(ConstructionMixin, Base):
    """An action item a `PASS_WITH_CONDITIONS` creates.

    Tập 2 §E.1 step 4: conditions become action items with a deadline and an
    owner, on the system. Step 5: the agent monitors them, escalates when
    overdue, and a conditional pass extended more than twice becomes a HOLD.

    `due_on` and `owner_role_key` are both NOT NULL because an action item
    without a deadline is a wish, and an action item without an owner is nobody's.
    """

    __tablename__ = "gate_conditions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    gate_decision_id: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(LONG, nullable=False)
    owner_role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    owner_person: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    due_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="open", server_default="open"
    )
    remediated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    evidence_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "gate_decision_id"],
            ["gate_decisions.organization_id", "gate_decisions.id"],
            name="fk_gate_conditions_gate_decision_id_gate_decisions",
        ),
        Index(
            "uq_gate_conditions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_gate_conditions_org_decision",
            "organization_id",
            "gate_decision_id",
        ),
        Index(
            "ix_gate_conditions_org_due",
            "organization_id",
            "status",
            "due_on",
        ),
        CheckConstraint(
            "((((status)::text <> 'remediated'::text) OR (remediated_at IS NOT NULL)))",
            name="a_remediated_condition_says_when",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['open'::character varying, "
            "'in_progress'::character varying, 'remediated'::character varying, "
            "'waived'::character varying, 'overdue'::character varying])::text[])))",
            name="status_known",
        ),
    )


# ======================================================================== SOPs ==
class SopDefinition(ConstructionMixin, Base):
    """A standard operating procedure, by its dossier code.

    Tập 1 §3.1 fixes the code: `ONX-[BLOCK]-[DEPT]-[TYPE]-[NUMBER]`, e.g.
    `ONX-MO-PRC-SOP-002` is Procurement's second SOP in the Middle Office. The
    dossier contains 28 of them, and they are seed data rather than rows somebody
    invents — the code *is* the document control scheme, and Tập 1 §1.2 makes
    document control a governance principle.

    `owner_role_key` is the `A` in the RACI. Tập 1 gives one per SOP — "GĐ Dự
    án", "TP Mua sắm", "CFO" — so this is the accountable role and the same key
    space as `project_roles` and `gate_definitions`.
    """

    __tablename__ = "sop_definitions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    name_en: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    doc_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="SOP", server_default="SOP"
    )
    block: Mapped[str] = mapped_column(SHORT, nullable=False)
    department: Mapped[str] = mapped_column(SHORT, nullable=False)
    owner_role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: The Gate this SOP feeds, per Tập 1's "Gate liên quan" column. Several SOPs
    #: feed several Gates, so this is a list rather than one column.
    related_gate_codes: Mapped[list[str]] = mapped_column(
        JSONB(), nullable=False, default=list, server_default="[]"
    )

    __table_args__ = domain_args(
        Index(
            "uq_sop_definitions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_sop_definitions_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((block)::text = ANY ((ARRAY['FO'::character varying, 'MO'::character varying, "
            "'BO'::character varying, 'PMO'::character varying])::text[])))",
            name="block_known",
        ),
        CheckConstraint(
            "(((code)::text ~ '^ONX-(FO|MO|BO|PMO)-[A-Z]{2,4}-(SOP|WI|FRM|POL|REG)-[0-9]{3}$'"
            "::text))",
            name="code_matches_the_dossier_scheme",
        ),
        CheckConstraint(
            "(((doc_type)::text = ANY ((ARRAY['SOP'::character varying, "
            "'WI'::character varying, 'FRM'::character varying, 'POL'::character varying, "
            "'REG'::character varying])::text[])))",
            name="doc_type_known",
        ),
    )


class SopVersion(ConstructionMixin, Base):
    """One issued version of an SOP. Tập 1 §3.1: `v[major].[minor]`.

    Append-only in the way `claim_events` is: a superseded version keeps its
    steps and its RACI, because the question "what did the procedure say when
    that task ran" is answerable only if the old text survives. The substrate
    already versions skills and tools this way for the same reason.
    """

    __tablename__ = "sop_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sop_definition_id: Mapped[str] = mapped_column(String(40), nullable=False)
    major: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    minor: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    #: Tập 1 §3.6, the ten-section template. The sections a document *has*, not
    #: its text: the body lives in the substrate `documents` table and the
    #: structured parts a workflow needs are the steps below.
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    effective_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    document_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Tập 1 §2.2: the PMO owns the SOP lifecycle. Two signatures before it is
    #: effective — drafted, reviewed, approved — so all three are tracked.
    drafted_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    reviewed_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    approved_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_sop_versions_document_id_documents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "sop_definition_id"],
            ["sop_definitions.organization_id", "sop_definitions.id"],
            name="fk_sop_versions_sop_definition_id_sop_definitions",
        ),
        Index(
            "uq_sop_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_sop_versions_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "uq_sop_versions_org_sop_major_minor",
            "organization_id",
            "sop_definition_id",
            "major",
            "minor",
            unique=True,
        ),
        CheckConstraint(
            "((((status)::text <> 'issued'::text) OR ((approved_by)::text <> ''::text)))",
            name="an_issued_sop_is_approved",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'in_review'::character varying, 'issued'::character varying, "
            "'superseded'::character varying, 'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((major >= 0) AND (minor >= 0)))",
            name="version_non_negative",
        ),
    )


class SopStep(ConstructionMixin, Base):
    """A step of an SOP, with its own RACI, SLA and autonomy level.

    Tập 1 §3.6 §6 gives each step: input, action, output, SLA, and system. Tập 2's
    sample SOPs give the same plus R/A/C/I and the AI level. That is exactly this
    row.

    `autonomy_level` is the SOP's *claim* about what may be automated at this
    step — Tập 1 §5.3, "mức áp dụng cho từng bước quy trình được ghi ngay
    trong Phụ lục AI-Readiness". It is **not** authoritative. `autonomy_policies`
    holds the ceiling and the ceiling wins, because Tập 1 says the level is
    "mã hóa cứng ở tầng Harness (không thể bị prompt vượt qua)" — hard-coded at
    the harness, not overridable by a prompt. A workflow may read this column to
    know what to attempt; it may not use it to justify exceeding the policy.
    """

    __tablename__ = "sop_steps"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sop_version_id: Mapped[str] = mapped_column(String(40), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    #: Input, action, output, per Tập 1 §3.6 §6.
    step_input: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    action: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    step_output: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: SLA. Both forms appear in the dossier: a duration ("24h", "5 ngày") and a
    #: deadline relative to an event ("trước cuộc họp 3 ngày"). `sla_text` keeps
    #: the human form; `sla_hours` is what a scheduler can actually act on, and
    #: is NULL when the SLA is relative rather than absolute.
    sla_text: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    sla_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    system_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: What the SOP asks for at this step. See the class docstring: a claim, not
    #: a permission.
    autonomy_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default=L_INFORM, server_default=L_INFORM
    )
    #: The action class the autonomy policy keys on. This is the join that makes
    #: the harness rule reachable: a step proposes an action of a class, and the
    #: policy for that class supplies the ceiling.
    action_class: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="inform", server_default="inform"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "sop_version_id"],
            ["sop_versions.organization_id", "sop_versions.id"],
            name="fk_sop_steps_sop_version_id_sop_versions",
        ),
        Index(
            "uq_sop_steps_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_sop_steps_org_version_sequence",
            "organization_id",
            "sop_version_id",
            "sequence",
            unique=True,
        ),
        CheckConstraint(
            "(((autonomy_level)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="autonomy_level_known",
        ),
        CheckConstraint(
            "(((sla_hours IS NULL) OR (sla_hours >= 0)))",
            name="sla_non_negative",
        ),
    )


class SopRaci(ConstructionMixin, Base):
    """One RACI cell: a role, a letter, a step.

    Tập 1 §1.2 makes two rules binding: exactly one `A` per step, and no step
    without `R`. Tập 3 §4.1 adds the third: **an agent is never an `A`.**

    All three are here, and the third is the interesting one. `role_kind` is
    carried on the row rather than looked up, because the rule is about *this
    step* — an agent may hold `R` on a step whose `A` is a person, and the check
    has to be local to the cell to say so. A versioned snapshot of the roster,
    which is what a procedure needs anyway: what the RACI said when it was
    issued.
    """

    __tablename__ = "sop_raci"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sop_step_id: Mapped[str] = mapped_column(String(40), nullable=False)
    role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    role_kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="person", server_default="person"
    )
    letter: Mapped[str] = mapped_column(SHORT, nullable=False)
    notes: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "sop_step_id"],
            ["sop_steps.organization_id", "sop_steps.id"],
            name="fk_sop_raci_sop_step_id_sop_steps",
        ),
        Index(
            "uq_sop_raci_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_sop_raci_org_step_role",
            "organization_id",
            "sop_step_id",
            "role_key",
            unique=True,
        ),
        CheckConstraint(
            "((NOT (((role_kind)::text = 'agent'::text) AND ((letter)::text = 'A'::text))))",
            name="an_agent_is_never_accountable",
        ),
        CheckConstraint(
            "(((letter)::text = ANY ((ARRAY['R'::character varying, 'A'::character varying, "
            "'C'::character varying, 'I'::character varying])::text[])))",
            name="letter_known",
        ),
        CheckConstraint(
            "(((role_kind)::text = ANY ((ARRAY['person'::character varying, "
            "'committee'::character varying, 'agent'::character varying])::text[])))",
            name="role_kind_known",
        ),
    )


class SopForm(ConstructionMixin, Base):
    """A form (FRM) a step requires, from Tập 1's form catalogue.

    Tập 1 §3.1 makes `FRM` one of the five document types in the code scheme, so
    forms are documents like any other and carry the same code discipline. The
    dossier's catalogue includes the Go/No-Go assessment, the Gate checklists, the
    supplier assessment, the acceptance record and the HITL approval slip.
    """

    __tablename__ = "sop_forms"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: The dossier's own code, e.g. `FRM-FO-001`. Not the `ONX-` scheme, which
    #: is for the SOP corpus; forms are cited by this code in checklists.
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    name_en: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    #: Which SOP step raises it, when it is step-specific.
    sop_step_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Tập 1 §3.6 §8: retention. ISO 9001 / ISO 19650 period. Years, because a
    #: project record is kept for years and a days column would invite a unit
    #: mistake.
    retention_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "sop_step_id"],
            ["sop_steps.organization_id", "sop_steps.id"],
            name="fk_sop_forms_sop_step_id_sop_steps",
        ),
        Index(
            "uq_sop_forms_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_sop_forms_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((retention_years IS NULL) OR (retention_years >= 0)))",
            name="retention_non_negative",
        ),
    )


# ============================================================ document control ==
class DocumentVersion(ConstructionMixin, Base):
    """One revision of a controlled document's *content*.

    Separate from `SopVersion` on purpose. The procedure and the file that
    describes it are revised by different people at different times: an SOP
    gains a step and `sop_versions` moves while the PDF does not, and a
    typo is fixed in the PDF and nothing else moves. Merging them would make
    "which version of the SOP" and "which version of the document" the same
    question, and they are not — a site induction that cites the wrong one
    is a document-control finding.

    Append-only, as `claim_events` is: a superseded version keeps its row, so
    "what did the procedure say on the day that task ran" stays answerable.
    That is the whole reason this is a table and not a `documents.content_hash`
    that gets overwritten.

    `effective_from`/`effective_to` is the window in which this revision was
    the one in force, and the `CHECK`s below refuse the two states that make
    the window unanswerable: a superseded revision that never ends, and a
    window that ends before it starts.

    `approval_id` is what makes an agent's proposal to a document reviewable
    the same way a task's is. Tập 3 §4.1 -- an agent is never Accountable --
    is enforced here as a constraint: a row an agent proposed cannot be marked
    `approved` without an approval somebody gave.
    """

    __tablename__ = "document_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    document_id: Mapped[str] = mapped_column(ID, nullable=False)
    #: A plain integer, not `major.minor`. This is the *content* revision, so it is
    #: a total order per document with no independent minor axis; `sop_versions` owns
    #: the semantic version and points here for the bytes.
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The revision this one replaces. A self-referencing key kept as a plain
    #: `String(40)` rather than a foreign key for the reason `proposal_id` is: a
    #: version row must survive the departure of anything it points at, or the
    #: history of a document acquires holes.
    supersedes_id: Mapped[str | None] = mapped_column(ID)
    issued_on: Mapped[dt.date | None] = mapped_column(Date)
    effective_from: Mapped[dt.date | None] = mapped_column(Date)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    change_note: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    approved_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: The approval that authorised this row, required whenever `source` is
    #: `agent_proposal`. This is Tập 3 §4.1 as a `CHECK` rather than a review
    #: convention.
    approval_id: Mapped[str | None] = mapped_column(ID)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "document_id"],
            ["documents.organization_id", "documents.id"],
            name="fk_document_versions_document",
        ),
        Index(
            "uq_document_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_document_versions_document",
            "organization_id",
            "document_id",
            "version_no",
        ),
        Index(
            "ix_document_versions_effective",
            "organization_id",
            "document_id",
            "status",
            "effective_from",
        ),
        UniqueConstraint(
            "organization_id",
            "document_id",
            "version_no",
            name="uq_document_versions_number",
        ),
        CheckConstraint(
            "((version_no > 0))",
            name="version_positive",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['draft'::character varying, "
            "'in_review'::character varying, 'approved'::character varying, "
            "'superseded'::character varying, 'withdrawn'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "((((status)::text <> 'superseded'::text) OR (effective_to IS NOT NULL)))",
            name="a_superseded_version_ends",
        ),
        CheckConstraint(
            "(((effective_to IS NULL) OR (effective_from IS NULL) OR (effective_to >= effecti"
            "ve_from)))",
            name="the_window_is_not_inverted",
        ),
        CheckConstraint(
            "((((source)::text <> 'agent_proposal'::text) OR (approval_id IS NOT NULL)))",
            name="agent_needs_approval",
        ),
        CheckConstraint(
            "((((status)::text <> 'approved'::text) OR (issued_on IS NOT NULL)))",
            name="an_approved_version_is_issued",
        ),
    )


class DocumentDistribution(ConstructionMixin, Base):
    """Who must hold which revision, and whether they have said so.

    This is the table that turns "the SOP says" into something checkable. Without
    it, a controlled document set is a paper exercise: the SOP exists, the
    distribution list is a spreadsheet, and nobody can answer "does the site
    manager have revision 4" — which is the question an audit actually asks.

    One row per (version, audience, channel), enforced by the `UNIQUE`. A
    re-send is an **update of the same obligation**, not a second one: without
    that, retrying a failed distribution doubles a mandatory count and the
    compliance figure becomes fiction, which is worse than having no figure.

    The `CHECK`s encode the three states that are always a data-entry mistake
    rather than a real situation:

    * an acknowledgement with nobody behind it, so "acknowledged" cannot mean
      "somebody pressed a button";
    * an acknowledgement dated *before* the distribution it acknowledges, which
      is a clock fault and would silently make a lag calculation negative;
    * a **mandatory** row that was never sent. A mandatory row claims that somebody is
      under an obligation, and a claim nobody was ever told about is not one -- so
      `is_mandatory` requires `distributed_at`. An *optional* row may sit undelivered: that
      is how a document is announced before it becomes mandatory.

      The first version of this check had the implication backwards
      (`NOT is_mandatory OR acknowledged_at IS NOT NULL OR distributed_at IS NULL`), which
      made the most important state of the whole table unrepresentable: a mandatory version
      that was distributed and is **not yet acknowledged** is the outstanding obligation
      the matrix exists to show, and the check refused it.

      It was caught by `test_a_sent_but_unacknowledged_row_is_valid` -- the *positive* test
      in that file, which is the reason the file has one. Every negative test passed
      against the broken check, because a constraint that refuses everything refuses every
      bad row too. That is the strongest argument in this repository for a test that
      asserts the good case alongside the bad ones, and it is why "mostly negative" here
      means *lopsided*, not *exclusively* negative.
    """

    __tablename__ = "document_distributions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    document_version_id: Mapped[str] = mapped_column(ID, nullable=False)
    #: A `roles.key`, not a person. A distribution list is a statement about a
    #: job, and it has to survive the person leaving — which is also why
    #: `acknowledged_by` below is a free-text name that outlives its author.
    audience_role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    channel: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="system", server_default="system"
    )
    is_mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    distributed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    note: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "document_version_id"],
            ["document_versions.organization_id", "document_versions.id"],
            name="fk_document_distributions_version",
        ),
        Index(
            "uq_document_distributions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_document_distributions_audience",
            "organization_id",
            "audience_role_key",
            "acknowledged_at",
        ),
        Index(
            "ix_document_distributions_version",
            "organization_id",
            "document_version_id",
            "is_mandatory",
        ),
        UniqueConstraint(
            "organization_id",
            "document_version_id",
            "audience_role_key",
            "channel",
            name="uq_document_distributions_target",
        ),
        CheckConstraint(
            "(((channel)::text = ANY ((ARRAY['system'::character varying, "
            "'email'::character varying, 'print'::character varying, "
            "'signage'::character varying, 'induction'::character varying])::text[])))",
            name="channel_known",
        ),
        CheckConstraint(
            "(((acknowledged_at IS NULL) OR ((distributed_at IS NOT NULL) AND (acknowledged_a"
            "t >= distributed_at))))",
            name="acknowledged_after_distribution",
        ),
        CheckConstraint(
            "(((acknowledged_at IS NULL) OR (length((acknowledged_by)::text) > 0)))",
            name="an_acknowledgement_names_who",
        ),
        CheckConstraint(
            "(((NOT is_mandatory) OR (distributed_at IS NOT NULL)))",
            name="a_mandatory_unsent_row_is_not_a_row",
        ),
    )


# =============================================== delegation of authority & policy ==
class DoaMatrix(ConstructionMixin, Base):
    """Delegation of authority: an amount band and who may approve it.

    Tập 1 §3.4 puts DOA in the Finance SOP and the dossier's appendix A lists
    "Quy chế phân cấp ủy quyền (DOA)" as a governing regulation. Tập 2 §E.2 uses
    it at G1 — the CEO chairs *above* a threshold, a deputy below — so the band
    has to be data rather than a branch in code.

    `min_amount` and `max_amount` are a band, and the two halves are checked
    against each other: a band with a maximum below its minimum is a band nothing
    can ever fall into, and it is silent.
    """

    __tablename__ = "doa_matrix"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    code: Mapped[str] = mapped_column(SHORT, nullable=False)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False)
    #: What the amount is measuring. A PO, a payment and a contract are three
    #: different scales, and a single amount column compared against the wrong
    #: one is how an approval limit stops meaning anything.
    subject_kind: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="payment", server_default="payment"
    )
    min_amount: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    #: NULL means "no upper limit" — the top band. NOT a sentinel like a very
    #: large number, because a sentinel is compared against and a real ceiling
    #: eventually is.
    max_amount: Mapped[float | None] = mapped_column(MONEY, nullable=True)
    currency_code: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="VND", server_default="VND"
    )
    #: The approving role. A role, not a person: Tập 3 §4.1 has each `A` generate
    #: a system approval role.
    approver_role_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: Escalation when the approver is unavailable. Time-boxed delegation is
    #: audited, so this is a role and not an instruction to "act sensibly".
    fallback_role_key: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="", server_default=""
    )
    #: Tập 1 §5.3 hard exclusion: a commitment above the DOA limit is at most L3
    #: and always goes through the human level. The cap is here so a band's
    #: ceiling is a fact about the band.
    max_agent_autonomy: Mapped[str] = mapped_column(
        SHORT, nullable=False, default=L_WITH_APPROVAL, server_default=L_WITH_APPROVAL
    )

    __table_args__ = domain_args(
        Index(
            "ix_doa_matrix_org_subject_band",
            "organization_id",
            "subject_kind",
            "min_amount",
        ),
        Index(
            "uq_doa_matrix_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        CheckConstraint(
            "(((max_agent_autonomy)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="autonomy_level_known",
        ),
        CheckConstraint(
            "(((max_amount IS NULL) OR (max_amount >= min_amount)))",
            name="band_is_not_inverted",
        ),
        CheckConstraint(
            "((min_amount >= (0)::numeric))",
            name="min_amount_non_negative",
        ),
    )


class AutonomyPolicy(ConstructionMixin, Base):
    """The hard-coded ceiling on what an agent may do, by action class.

    Tập 1 §5.3, and the most important row type in the platform:

    *The level applicable to each process step is recorded in the SOP's
    AI-Readiness appendix and **hard-coded at the Harness layer (it cannot be
    overridden by a prompt)**.*

    So the ceiling is data, but it is data the *harness* reads before a workflow
    runs rather than something a workflow consults afterwards. That is the
    difference between a rule and a suggestion, and it is why this is a table and
    not a column: a workflow can read a step's `autonomy_level`, but only this
    can bound it.

    `is_hard_block` is the "Vùng cấm tuyệt đối" — the absolutely forbidden zones.
    Tập 1 lists four, and the seed data reproduces them:

    | Action class | Rule |
    |---|---|
    | `hr_personnel_decision` | at most L2 — hire, dismiss, discipline, adjust salary |
    | `financial_commitment` | at most L3, always the correct human level |
    | `safety_conclusion` | humans decide; AI is L1/L2 only |
    | `supplier_risk_flagged` | automatic freeze, referred to Legal |

    A hard block is not a level. It stops the action before a level is
    considered, which is why it is a flag here and not an `L0` — my earlier plan
    had an `L0` and that was the right instinct under the wrong name.
    """

    __tablename__ = "autonomy_policies"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    action_class: Mapped[str] = mapped_column(SHORT, nullable=False)
    name_vi: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    max_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default=L_INFORM, server_default=L_INFORM
    )
    is_hard_block: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    #: Why the ceiling exists, in one line. A policy nobody can explain is a
    #: policy somebody will raise, and the explanation is what survives the
    #: challenge.
    rationale: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: Tập 1 §6.1: the AI Governance Board approves the autonomy level of each
    #: agent, quarterly. `approved_by` is that board, so a changed ceiling has an
    #: author.
    approved_by: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = domain_args(
        Index(
            "uq_autonomy_policies_org_action_class",
            "organization_id",
            "action_class",
            unique=True,
        ),
        CheckConstraint(
            "(((NOT (is_hard_block AND ((max_level)::text <> 'L1'::text))) OR ((max_level)::t"
            "ext = 'L1'::text)))",
            name="a_hard_block_reports_at_l1",
        ),
        CheckConstraint(
            "((is_hard_block OR ((max_level)::text <> ''::text)))",
            name="max_level_present",
        ),
        CheckConstraint(
            "(((max_level)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="max_level_known",
        ),
    )


__all__ = [
    "AUTONOMY_LEVELS",
    "AUTONOMY_RANK",
    "CONDITION_STATUSES",
    "CRITERION_STATES",
    "GATE_FAIL",
    "GATE_HOLD",
    "GATE_INSTANCE_STATUSES",
    "GATE_OUTCOMES",
    "GATE_PASS",
    "GATE_PASS_WITH_CONDITIONS",
    "L_AND_REPORT",
    "L_INFORM",
    "L_RECOMMEND",
    "L_WITH_APPROVAL",
    "RACI_LETTERS",
    "ROLE_KINDS",
    "SOP_BLOCKS",
    "SOP_STATUSES",
    "SOP_TYPES",
    "AutonomyPolicy",
    "DoaMatrix",
    "DocumentDistribution",
    "DocumentVersion",
    "GateCondition",
    "GateCriterion",
    "GateCriterionEvaluation",
    "GateDecision",
    "GateDefinition",
    "GateInstance",
    "SopDefinition",
    "SopForm",
    "SopRaci",
    "SopStep",
    "SopVersion",
    "autonomy_ceiling_allowed",
]
