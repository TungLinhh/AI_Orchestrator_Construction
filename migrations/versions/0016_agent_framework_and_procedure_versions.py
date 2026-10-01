"""The agent framework's missing parts, and the missing end of the learning loop.

Phase 4. Not a new agent framework — the substrate already has `agent_definitions`,
`agents`, `agent_relationships` and `agent_skill_bindings`, and a version of this
migration tried to create a fifth one. See `docs/FAILED_APPROACHES.md` F108.

What was actually missing, measured against the live schema:

| Need | State before this migration |
|---|---|
| autonomy **ceiling**, distinct from the level granted | `agents.autonomy_level` is an unconstrained `varchar` holding `l1_low_risk_autonomous` and `l2_parent_review` — a different scale, in prose, with no `CHECK` |
| kill switch as a recorded state | nothing; `lifecycle_status` exists and all 9 seeded rows read `active` |
| shadow runs | no table |
| decision log, refusals included | no table |
| anything to apply an approved change to | **no `procedures` table exists at all** |

So this migration adds the ceiling and the granted level as a *checked pair* on
`agents`, a kill switch that cannot be thrown without a reason and a timestamp,
`agent_shadow_runs`, `ai_decision_log`, and `procedures` + `procedure_versions`.

## The learning loop was open at both ends

Measured before writing any of this:

    ProposalGenerator.generate()   -> an ApprovalPacket, or a reason there is none
    submit_for_approval()          -> an `approvals` row
    ApprovalService.decide()       -> a human approves or rejects
    load_approved()                -> **zero callers outside its own module**

and no `procedures` table to apply an approved change to, and no writer for one. The
platform could notice a repeated failure, propose a change to a procedure, record a
human's approval of that change, and then do nothing with it. The next run does the
same thing again. That is a suggestion box with an audit trail, not adaptation.

`procedure_versions` is the table that was missing, built so the loop cannot close the
wrong way round:

* A version is written **`shadow`**. Getting it there records the approval that
  authorised it; it does not change behaviour.
* At most one version per procedure can be `active`, enforced by a **partial unique
  index** rather than by application code — two agents running two versions of one SOP
  must be impossible, not unlikely.
* Promotion is a separate act with per-run evidence behind it, because a count can
  tell you a promotion happened and cannot tell you whether it was right.

## Two autonomy columns, and why the old one is left alone

`autonomy_level` holds `l1_low_risk_autonomous` / `l2_parent_review` — prose, a
different scale from the dossier's L1–L4, and unconstrained. It is **not** rewritten
here. Nine seeded rows depend on it, and the scale mapping is a judgement about what
those strings were ever meant to mean, not a schema change.

Instead this migration adds the pair that *can* be enforced:

* `autonomy_ceiling` — the most the dossier permits. A decision somebody records.
* `granted_level` — what has actually been granted, `L1`–`L4`, defaulted to `L1`.

with `CHECK (granted_level <= autonomy_ceiling)`. The codes are single digits in
order, so a string comparison is the comparison. **Every agent lands on L1**, which is
the point: nothing raises a ceiling here, and nothing should until there is a measured
shadow agreement rate to raise it with.

`autonomy_ceiling` is a column rather than a branch in the executor because raising an
agent's autonomy should be a decision with a record, not a deploy.

## The decision log's most common row is a refusal

`ai_decision_log` includes `refused` and `escalated`, and a `CHECK` refuses a refusal
with an empty rationale. A log of what agents *did* teaches you what the system can do;
a log of what it *declined to do and why* is the only thing that lets anybody audit
whether the autonomy levels are set correctly. The dossier's segregation-of-duties
principle is a statement about refusals, so refusals are the rows worth keeping.

## New tables take composite foreign keys

`procedure_versions.procedure_id`, `agent_shadow_runs.agent_id` and
`ai_decision_log.agent_id` are composite `(organization_id, id)` foreign keys. A bare
FK lets a row in tenant A point at a row in tenant B — the open gap recorded in
`PRODUCT_GAP.md` on about a dozen older tables. Those get fixed on their own schedule;
a **new** table has no excuse, and `uq_*_org_id` on each parent exists solely to make
it expressible.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-27 15:10:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "ao_app"


def _protect(table: str) -> None:
    """Enable and force RLS, install the isolation policy, grant DML.

    Identical to what 0002 did for the original 44 tables, 0007 for the first
    construction tranche and 0014 for `progress_snapshots`, including the `FORCE`.
    Without `FORCE` the owner bypasses the policy, and the owner is the role that
    ran this migration.
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
    # ---------------------------------------------------------------- agents --
    # Three columns on the table that already exists, rather than a fifth agent
    # table beside the four the substrate has.
    #
    # `autonomy_ceiling` is the most the dossier permits; `granted_level` is what has
    # been granted; the CHECK is the whole point of having both. Defaults are `L1` on
    # purpose: every one of the 9 seeded agents lands on the most cautious value, and
    # a migration that quietly raised them to L3 would be the most consequential
    # thing in this file.
    op.add_column(
        "agents",
        sa.Column("autonomy_ceiling", sa.String(length=2), server_default="L1", nullable=False),
    )
    op.add_column(
        "agents",
        sa.Column("granted_level", sa.String(length=2), server_default="L1", nullable=False),
    )
    # A kill switch is a decision, and a decision without a reason or a moment is not
    # one. Both directions are checked: a thrown switch needs a reason and a
    # timestamp, and a `killed_at` with the switch off is a kill nobody performed.
    # Without the second check, `kill_switch = false` with a stale `killed_at` reads
    # as "was killed, has since been re-enabled" and cannot be told from "never
    # killed", which are different states with different reasons to exist.
    op.add_column(
        "agents",
        sa.Column("kill_switch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "agents", sa.Column("kill_reason", sa.Text(), server_default="", nullable=False)
    )
    op.add_column("agents", sa.Column("killed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agents", sa.Column("killed_by", sa.String(length=40), nullable=True))
    # Names are given WITHOUT the `ck_agents_` prefix on purpose. This is the
    # `ALTER TABLE` path rather than `create_table`, so SQLAlchemy's
    # `ck_%(table_name)s_%(constraint_name)s` convention still applies to it and
    # supplies the prefix itself. The `create_table` constraints above do NOT do
    # this -- they are wrapped in `op.f()` precisely to say "already named" -- so the
    # two styles differ and the doubled name
    # `ck_agents_ck_agents_ceiling_known` is what happens if you forget which is
    # which.
    op.create_check_constraint(
        "ceiling_known", "agents", "autonomy_ceiling IN ('L1','L2','L3','L4')"
    )
    op.create_check_constraint(
        "granted_known", "agents", "granted_level IN ('L1','L2','L3','L4')"
    )
    # Single-digit codes in order, so a string comparison is the comparison. An agent
    # granted more than its ceiling is the failure this exists to make impossible.
    op.create_check_constraint(
        "granted_within_ceiling", "agents", "granted_level <= autonomy_ceiling"
    )
    op.create_check_constraint(
        "kill_is_recorded",
        "agents",
        "NOT kill_switch OR (kill_reason <> '' AND killed_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "killed_at_needs_kill", "agents", "kill_switch OR killed_at IS NULL"
    )
    # Needed by the composite foreign keys below. A composite FK requires the
    # referenced *pair* to be unique, and `agents.id` being unique on its own is not
    # the same statement -- Postgres needs `UNIQUE (organization_id, id)` present.
    op.create_index("uq_agents_org_id", "agents", ["organization_id", "id"], unique=True)
    # The same for the two parents the new tables point at. Created here rather than
    # in a migration of their own because they are only load-bearing for the tables
    # below, and because adding a unique index to a table with existing rows can fail
    # -- both are empty of rows in the dev and test schemas at this point, which is
    # what makes doing it here safe. `tasks` is also one of the tables carrying the
    # open bare-FK gap; that is fixed on its own schedule, and retrofitting composite
    # keys onto shipped tables is not a side effect of adding four new ones.
    op.create_index("uq_approvals_org_id", "approvals", ["organization_id", "id"], unique=True)
    op.create_index("uq_tasks_org_id", "tasks", ["organization_id", "id"], unique=True)

    # ------------------------------------------------------------- procedures --
    op.create_table(
        "procedures",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("department", sa.String(length=128), server_default="", nullable=False),
        # Which version is live. Deliberately *not* a foreign key to
        # `procedure_versions`: the two reference each other, and expressing that
        # properly needs the version row to exist first. Both are written in one
        # transaction and a test proves they agree, which is checked rather than
        # assumed.
        sa.Column("current_version_id", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE",
            name=op.f("fk_procedures_organization_id_organizations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedures")),
        sa.UniqueConstraint("organization_id", "code", name=op.f("uq_procedures_org_code")),
    )
    op.create_index("uq_procedures_org_id", "procedures", ["organization_id", "id"], unique=True)
    _protect("procedures")

    op.create_table(
        "procedure_versions",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        sa.Column("procedure_id", sa.String(length=40), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="shadow", nullable=False),
        # The procedure text. Immutable once written: a version editable after its
        # approval was granted is a version whose approval covered different words.
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("body_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("autonomy_ceiling", sa.String(length=2), server_default="L1", nullable=False),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column("source", sa.String(length=128), server_default="human", nullable=False),
        sa.Column("source_actor", sa.String(length=128), server_default="", nullable=False),
        sa.Column("proposal_id", sa.String(length=40), nullable=True),
        # The link that closes the loop. An agent-proposed version that cannot name
        # the approval that authorised it is a change nobody agreed to.
        sa.Column("approval_id", sa.String(length=40), nullable=True),
        sa.Column("shadow_runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("shadow_agreements", sa.Integer(), server_default="0", nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "procedure_id"],
            ["procedures.organization_id", "procedures.id"],
            name=op.f("fk_procedure_versions_procedure"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "approval_id"],
            ["approvals.organization_id", "approvals.id"],
            name=op.f("fk_procedure_versions_approval"),
        ),
        sa.CheckConstraint(
            "(source = 'agent_proposal') = (proposal_id IS NOT NULL)",
            name=op.f("ck_procedure_versions_agent_needs_proposal"),
        ),
        sa.CheckConstraint(
            "source <> 'agent_proposal' OR approval_id IS NOT NULL",
            name=op.f("ck_procedure_versions_agent_needs_approval"),
        ),
        sa.CheckConstraint(
            "status IN ('shadow','active','superseded','rejected')",
            name=op.f("ck_procedure_versions_status_known"),
        ),
        sa.CheckConstraint(
            "autonomy_ceiling IN ('L1','L2','L3','L4')",
            name=op.f("ck_procedure_versions_ceiling_known"),
        ),
        sa.CheckConstraint(
            "version_no > 0", name=op.f("ck_procedure_versions_version_positive")
        ),
        # An agreement rate above 100% would make every promotion threshold in the
        # application meaningless, and the number would still look plausible.
        sa.CheckConstraint(
            "shadow_agreements <= shadow_runs",
            name=op.f("ck_procedure_versions_agreements_within_runs"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_procedure_versions")),
        sa.UniqueConstraint(
            "organization_id", "procedure_id", "version_no",
            name=op.f("uq_procedure_versions_org_proc_no"),
        ),
    )
    op.create_index("uq_procedure_versions_org_id", "procedure_versions", ["organization_id", "id"], unique=True)
    # The invariant the application cannot be trusted with: at most one live version
    # per procedure. Partial, so `shadow` and `rejected` versions are unconstrained
    # -- there is meant to be a queue of them.
    op.create_index(
        "uq_procedure_versions_one_active",
        "procedure_versions",
        ["organization_id", "procedure_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    _protect("procedure_versions")

    # ------------------------------------------------------------- shadow runs --
    op.create_table(
        "agent_shadow_runs",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        # Nullable: a shadow run for a *procedure version* has no agent yet, and a
        # shadow run for an *agent* need not be testing a procedure. Two nullable
        # alternatives beat a table that pretends the two are the same thing.
        sa.Column("agent_id", sa.String(length=40), nullable=True),
        sa.Column("procedure_version_id", sa.String(length=40), nullable=True),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        # What the new version would have done, and what actually happened. Two
        # columns rather than one, because the comparison is the entire value of a
        # shadow run and a single `result` column would be asserting the answer.
        sa.Column("would_have_decided", sa.Text(), server_default="", nullable=False),
        sa.Column("actually_decided", sa.Text(), server_default="", nullable=False),
        sa.Column("agreed", sa.Boolean(), nullable=False),
        sa.Column("divergence", sa.Text(), server_default="", nullable=False),
        sa.Column("observed_on", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name=op.f("fk_agent_shadow_runs_agent"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "procedure_version_id"],
            ["procedure_versions.organization_id", "procedure_versions.id"],
            name=op.f("fk_agent_shadow_runs_version"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name=op.f("fk_agent_shadow_runs_task"),
        ),
        # A disagreement with no explanation cannot be acted on: promotion thresholds
        # are about rates, and the rate is useless without the reasons.
        sa.CheckConstraint(
            "agreed OR divergence <> ''",
            name=op.f("ck_shadow_runs_disagreement_is_explained"),
        ),
        # A run attached to nothing is not evidence of anything.
        sa.CheckConstraint(
            "agent_id IS NOT NULL OR procedure_version_id IS NOT NULL",
            name=op.f("ck_shadow_runs_attached_to_something"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_shadow_runs")),
    )
    op.create_index("uq_agent_shadow_runs_org_id", "agent_shadow_runs", ["organization_id", "id"], unique=True)
    op.create_index(
        "ix_shadow_runs_version", "agent_shadow_runs", ["organization_id", "procedure_version_id"]
    )
    _protect("agent_shadow_runs")

    # --------------------------------------------------------- decision log ----
    op.create_table(
        "ai_decision_log",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("organization_id", sa.String(length=40), nullable=False),
        # Nullable, and this is not an oversight. A promotion is a *system* decision
        # about an agent, not a decision the agent took, and a human-written procedure
        # version has no proposing agent to point at. `actor_type` carries the "who
        # acted" that `agent_id` cannot: the same shape `audit_logs` already uses with
        # a nullable `actor_id` and a required `actor_type`, for the same reason.
        sa.Column("agent_id", sa.String(length=40), nullable=True),
        sa.Column("actor_type", sa.String(length=128), server_default="agent", nullable=False),
        sa.Column("task_id", sa.String(length=40), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        # The level it was *allowed* to act at, recorded per decision rather than read
        # from the agent later. A ceiling raised afterwards would otherwise rewrite
        # the meaning of every earlier row.
        sa.Column("autonomy_level", sa.String(length=2), nullable=False),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column("policy_rule", sa.String(length=128), server_default="", nullable=False),
        sa.Column("inputs_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("outcome", sa.Text(), server_default="", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name=op.f("fk_ai_decision_log_agent"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name=op.f("fk_ai_decision_log_task"),
        ),
        sa.CheckConstraint(
            "decision IN ('acted','proposed','refused','escalated','shadowed')",
            name=op.f("ck_ai_decision_log_decision_known"),
        ),
        sa.CheckConstraint(
            "autonomy_level IN ('L1','L2','L3','L4')",
            name=op.f("ck_ai_decision_log_level_known"),
        ),
        # A refusal with no reason is indistinguishable from a failure to decide,
        # and those two need different investigations. This is the row the dossier's
        # segregation-of-duties principle is actually about.
        sa.CheckConstraint(
            "decision <> 'refused' OR rationale <> ''",
            name=op.f("ck_ai_decision_log_refusal_is_explained"),
        ),
        # A row with neither an agent nor an actor type cannot be attributed to
        # anybody, and an unattributable decision is the one row kind a decision log
        # must not contain.
        sa.CheckConstraint(
            "agent_id IS NOT NULL OR actor_type <> ''",
            name=op.f("ck_ai_decision_log_is_attributable"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_decision_log")),
    )
    op.create_index("uq_ai_decision_log_org_id", "ai_decision_log", ["organization_id", "id"], unique=True)
    # A log nobody can query by time is a log nobody reads.
    op.create_index(
        "ix_ai_decision_log_occurred", "ai_decision_log", ["organization_id", "occurred_at"]
    )
    op.create_index(
        "ix_ai_decision_log_agent", "ai_decision_log", ["organization_id", "agent_id"]
    )
    _protect("ai_decision_log")


def downgrade() -> None:
    # Reverse creation order so the partial unique index and the FKs drop cleanly.
    for table in ("ai_decision_log", "agent_shadow_runs", "procedure_versions", "procedures"):
        op.execute(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"')
        op.drop_table(table)
    op.drop_index("uq_agents_org_id", table_name="agents")
    # Short names here too, for the same reason as in `upgrade`:
    # `op.drop_constraint` re-applies `ck_%(table_name)s_%(constraint_name)s`, so
    # passing the final name makes it look for `ck_agents_ck_agents_...` and fail
    # with `UndefinedObjectError` naming a constraint that does not exist. Symmetry
    # with `create_check_constraint` is the rule; neither takes the name that is
    # actually in `pg_constraint`.
    for constraint in (
        "killed_at_needs_kill",
        "kill_is_recorded",
        "granted_within_ceiling",
        "granted_known",
        "ceiling_known",
    ):
        op.drop_constraint(constraint, "agents", type_="check")
    for column in ("killed_by", "killed_at", "kill_reason", "kill_switch", "granted_level", "autonomy_ceiling"):
        op.drop_column("agents", column)
    # The two supporting indexes on existing tables. Dropping them is not optional:
    # left in place, a re-upgrade of this migration dies on
    # `DuplicateTableError: relation "uq_approvals_org_id" already exists`, which is
    # a downgrade that does not round-trip -- the state where `alembic current` says
    # 0015 and the database says 0016, and which only a round-trip test finds.
    op.drop_index("uq_approvals_org_id", table_name="approvals")
    op.drop_index("uq_tasks_org_id", table_name="tasks")
