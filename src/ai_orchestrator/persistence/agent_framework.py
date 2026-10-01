"""The agent framework's missing parts, and the missing end of the learning loop.

Migration `0016`. Four tables, and the six columns on `agents` live in
`models.py` beside the model they extend.

## What this is not

Not a new agent framework. The substrate has had `agents`, `agent_definitions`,
`agent_relationships` and `agent_skill_bindings` since the initial schema, and a first
version of this module created a *fifth* one. See `docs/FAILED_APPROACHES.md` F108.
What was actually missing, measured against the live schema rather than assumed:

| Need | State before `0016` |
|---|---|
| autonomy **ceiling**, distinct from granted | unconstrained prose column |
| kill switch as a recorded state | nothing; all 9 rows read `active` |
| shadow runs | no table |
| decision log, refusals included | no table |
| anything to apply an approved change to | **no `procedures` table at all** |

## The learning loop was open at both ends

Measured, before writing any of this:

    ProposalGenerator.generate()   -> an ApprovalPacket, or a reason there is none
    submit_for_approval()          -> an `approvals` row
    ApprovalService.decide()       -> a human approves or rejects
    load_approved()                -> **zero callers outside its own module**

and no `procedures` table, and no writer for one. The platform could notice a repeated
failure, propose a change to a procedure, record a human's approval of that change,
and then do nothing with it. The next run does the same thing again. That is a
suggestion box with an audit trail, not adaptation.

`ProcedureVersion` is the table that was missing, and it is built so the loop cannot
close the wrong way round:

* A version is written **`shadow`**. Getting it there records the approval that
  authorised it; it does not change behaviour.
* At most one version per procedure can be `active`, enforced by a **partial unique
  index** rather than by application code. Two agents running two versions of one SOP
  has to be impossible, not unlikely.
* Promotion is a separate act with per-run evidence behind it, because a count can
  tell you a promotion happened and cannot tell you whether it was right.

## `current_version_id` is not a foreign key, deliberately

`Procedure.current_version_id` points at `ProcedureVersion`, and
`ProcedureVersion.procedure_id` points back at `Procedure`. Expressing that as real
foreign keys on both sides needs the version row to exist before the procedure can
name it, which is the wrong order. So one side is a plain column, both are written in
one transaction, and the agreement between them is a test rather than an assumption.

This is the only place in the schema where a pointer is not a foreign key, and it is
recorded here rather than left to be discovered.

## Composite foreign keys throughout

Every parent reference is `(organization_id, id)`, not a bare `id`. A bare FK lets a
row in tenant A point at a row in tenant B — the open gap `PRODUCT_GAP.md` records on
about a dozen older tables, which get fixed on their own schedule. A **new** table has
no excuse, so the `uq_*_org_id` unique indexes exist on `procedures`,
`procedure_versions`, `agent_shadow_runs` and `ai_decision_log` solely to make it
expressible.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import Base, created_at_col
from ai_orchestrator.persistence.construction import (
    LONG,
    NAME,
    SHORT,
    ConstructionMixin,
    domain_args,
)

#: An autonomy level. `String(2)` rather than a native enum, for the reason the rest
#: of this repository uses checked `varchar`: a native enum makes adding a fifth value
#: a `CREATE TYPE` that cannot be rolled back into a second migration.
LEVEL = String(2)


class Procedure(Base):
    """A named procedure — one SOP an agent runs on.

    Versioned rather than mutable, so "what did the HR agent do in March" has an
    answer that does not change when the SOP is revised in April.
    """

    __tablename__ = "procedures"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="")
    department: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: Which version is live. Not a foreign key — see the module docstring.
    current_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[dt.datetime] = created_at_col()

    # Plain `Base`, not `ConstructionMixin`: `0016` gives this table no
    # `source`/`proposal_id` pair, and a mixin that declares columns the migration
    # does not create is a drift failure waiting for the next `alembic check`.
    __table_args__ = (
        Index(
            "uq_procedures_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("organization_id", "code", name="uq_procedures_org_code"),
    )


class ProcedureVersion(ConstructionMixin, Base):
    """One version of a procedure's instructions, and its route to becoming live.

    `status` is the whole state machine and it has four values, not two:

    * `shadow` — written, approved, and **not** in effect. This is where every
      agent-proposed version lands, and it is the only state learning can reach
      without a person deciding to promote.
    * `active` — the one version agents actually run. At most one per procedure,
      by partial unique index.
    * `superseded` — was active, has been replaced. Kept rather than deleted
      because "what was in force" is a question with a date attached.
    * `rejected` — approved or not, and decided against. Also kept: a rejected
      change is the record of a thing somebody thought about and declined.
    """

    __tablename__ = "procedure_versions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    procedure_id: Mapped[str] = mapped_column(String(40), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="shadow", server_default="shadow"
    )
    #: The procedure text. Immutable once written: a version editable after its
    #: approval was granted is a version whose approval covered different words.
    body: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    body_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    autonomy_ceiling: Mapped[str] = mapped_column(
        LEVEL, nullable=False, default="L1", server_default="L1"
    )
    rationale: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: The approval that authorised this version. **Required** for an
    #: `agent_proposal`, and that requirement is the link which closes the loop: a
    #: learned change that cannot name the approval it was granted under is a change
    #: nobody agreed to.
    approval_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: Denormalised shadow totals. Kept on the version so a promotion query does not
    #: aggregate a run table on every row it reads — and constrained so the
    #: agreement count can never exceed the run count, which would make every
    #: promotion threshold in the application meaningless while still looking
    #: plausible.
    shadow_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    shadow_agreements: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    promoted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    promoted_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "approval_id"],
            ["approvals.organization_id", "approvals.id"],
            name="fk_procedure_versions_approval",
        ),
        ForeignKeyConstraint(
            ["organization_id", "procedure_id"],
            ["procedures.organization_id", "procedures.id"],
            name="fk_procedure_versions_procedure",
        ),
        Index(
            "uq_procedure_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_procedure_versions_one_active",
            "organization_id",
            "procedure_id",
            unique=True,
            postgresql_where=text(
                "((status)::text = 'active'::text)",
            ),
        ),
        UniqueConstraint(
            "organization_id",
            "procedure_id",
            "version_no",
            name="uq_procedure_versions_org_proc_no",
        ),
        CheckConstraint(
            "((((source)::text = 'agent_proposal'::text) = (proposal_id IS NOT NULL)))",
            name="agent_needs_proposal",
        ),
        CheckConstraint(
            "((((source)::text <> 'agent_proposal'::text) OR (approval_id IS NOT NULL)))",
            name="agent_needs_approval",
        ),
        CheckConstraint(
            "(((status)::text = ANY ((ARRAY['shadow'::character varying, "
            "'active'::character varying, 'superseded'::character varying, "
            "'rejected'::character varying])::text[])))",
            name="status_known",
        ),
        CheckConstraint(
            "(((autonomy_ceiling)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="ceiling_known",
        ),
        CheckConstraint(
            "((version_no > 0))",
            name="version_positive",
        ),
        CheckConstraint(
            "((shadow_agreements <= shadow_runs))",
            name="agreements_within_runs",
        ),
    )


class AgentShadowRun(Base):
    """One comparison between what a new version would have done and what happened.

    Two nullable parents, and the reason is worth stating: a shadow run for a
    *procedure version* has no agent yet, and a shadow run for an *agent* need not
    be testing a procedure. Two nullable alternatives beat a table that pretends
    those are the same thing.
    """

    __tablename__ = "agent_shadow_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    procedure_version_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: What the new version would have done, and what actually happened. Two
    #: columns rather than one, because the comparison *is* the value of a shadow
    #: run and a single `result` column would be asserting the answer.
    would_have_decided: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )
    actually_decided: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )
    agreed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: Required when `agreed` is false. A disagreement with no explanation cannot
    #: be acted on: promotion is about rates, and a rate is useless without reasons.
    divergence: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    observed_on: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_shadow_runs_agent",
        ),
        ForeignKeyConstraint(
            ["organization_id", "procedure_version_id"],
            ["procedure_versions.organization_id", "procedure_versions.id"],
            name="fk_agent_shadow_runs_version",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_agent_shadow_runs_task",
        ),
        Index(
            "uq_agent_shadow_runs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_shadow_runs_version",
            "organization_id",
            "procedure_version_id",
        ),
        CheckConstraint(
            "((agreed OR (divergence <> ''::text)))",
            name="ck_shadow_runs_disagreement_is_explained",
        ),
        CheckConstraint(
            "(((agent_id IS NOT NULL) OR (procedure_version_id IS NOT NULL)))",
            name="ck_shadow_runs_attached_to_something",
        ),
    )


class AIDecisionLog(Base):
    """What an agent decided, at what level it was allowed to, and why.

    The most common row here is expected to be a **refusal**, and the `CHECK`
    enforces that a refusal carries a rationale. A log of what agents *did* teaches
    you what the system can do; a log of what it *declined to do and why* is the only
    thing that lets anybody audit whether the autonomy levels are set correctly. The
    dossier's segregation-of-duties principle is a statement about refusals.

    `autonomy_level` is the level the decision was *allowed* at, recorded per row
    rather than read from the agent later. A ceiling raised afterwards would otherwise
    rewrite the meaning of every earlier row, which is the one thing an audit log
    must not allow.
    """

    __tablename__ = "ai_decision_log"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    organization_id: Mapped[str] = mapped_column(String(40), nullable=False)
    #: Nullable on purpose. A promotion is a *system* decision about an agent, and a
    #: human-written procedure version has no proposing agent at all. `actor_type`
    #: carries the "who acted" that `agent_id` cannot, which is the shape `audit_logs`
    #: already uses and the reason it already has a nullable `actor_id`.
    agent_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    actor_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="agent", server_default="agent"
    )
    task_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    autonomy_level: Mapped[str] = mapped_column(LEVEL, nullable=False)
    rationale: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    policy_rule: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    inputs_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    outcome: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_ai_decision_log_agent",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_ai_decision_log_task",
        ),
        Index(
            "uq_ai_decision_log_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_ai_decision_log_occurred",
            "organization_id",
            "occurred_at",
        ),
        Index(
            "ix_ai_decision_log_agent",
            "organization_id",
            "agent_id",
        ),
        CheckConstraint(
            "(((decision)::text = ANY ((ARRAY['acted'::character varying, "
            "'proposed'::character varying, 'refused'::character varying, "
            "'escalated'::character varying, 'shadowed'::character varying, "
            "'killed'::character varying, 'revived'::character varying])::text[])))",
            name="decision_known",
        ),
        CheckConstraint(
            "((((decision)::text <> ALL ((ARRAY['refused'::character varying, "
            "'killed'::character varying, "
            "'revived'::character varying])::text[])) OR (rationale <> ''::text)))",
            name="explained_is_explained",
        ),
        CheckConstraint(
            "(((autonomy_level)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="level_known",
        ),
        CheckConstraint(
            "(((agent_id IS NOT NULL) OR ((actor_type)::text <> ''::text)))",
            name="is_attributable",
        ),
    )


__all__ = [
    "AIDecisionLog",
    "AgentShadowRun",
    "Procedure",
    "ProcedureVersion",
]
