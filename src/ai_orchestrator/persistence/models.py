"""Canonical data model.

This module is the schema. Everything else reads it; nothing else defines
tables. The rules that hold across all of it:

Tenant scope
    Every org-scoped table carries `organization_id NOT NULL`. Row-level
    security on top of that is defence in depth, not the only defence: the
    application also scopes queries, because a policy that only exists in the
    database is one `SET row_security = off` away from being absent.

No soft deletes on operational state
    Agents are `retired`, not deleted. Tasks keep their row so the audit chain
    survives. `deleted_at` appears only on user-facing documents where a purge
    right has to be honoured.

Money and time
    `NUMERIC` for money, `timestamptz` for time. Never float, never naive.

Versioning instead of mutation
    Skills, tools, policies, agent definitions and model profiles are
    versioned with an append-only version row. The current pointer is a partial
    unique index, so "two current versions" is impossible at the database level
    rather than by convention.

Auditability
    Audit rows and outbox rows are append-only. Corrections are new rows.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import MONEY, Base, created_at_col, updated_at_col

# Reused column types, named so the DDL is greppable.
ID = String(40)


def make_audit_id() -> str:
    """Primary key for an audit row.

    A dedicated factory so the audit table is self-consistent: every other table
    reuses the column type `ID` for a caller-supplied prefixed ULID, and the
    audit table supplies its own because nothing outside this module constructs
    one.
    """
    from ai_orchestrator.domain.ids import make_id

    return make_id("aud")


SHORT = String(128)
NAME = String(255)
LONG = Text
JSON = JSONB
TS = DateTime(timezone=True)


# ============================================================ identity & org ==
class Organization(Base):
    """Tenant root. The unit of isolation for everything else."""

    __tablename__ = "organizations"

    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        CheckConstraint(
            "(((spend_cap_usd IS NULL) OR (spend_cap_usd >= (0)::numeric)))",
            name="spend_cap_non_negative",
        ),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    slug: Mapped[str] = mapped_column(SHORT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    # Every optional-text column carries an explicit server default as well as a
    # Python default. A Python default only applies to the ORM; a raw INSERT —
    # which is how the API, the seed script and every other process writes — would
    # hit a NOT NULL violation instead of an empty value. Migrations are the only
    # place that creates schema, so the default has to be declared here.
    description: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="active", server_default="active"
    )
    # Per-org overrides of the platform defaults. NULL means "inherit".
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    # Defence in depth against runaway spend, independent of any single budget.
    spend_cap_usd: Mapped[float | None] = mapped_column(MONEY)
    data_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=365, server_default="365"
    )
    default_model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    default_timezone: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="UTC", server_default="UTC"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


class User(Base):
    """A human principal. Agents are *not* users: they authenticate as agents."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(SHORT, nullable=False)
    display_name: Mapped[str] = mapped_column(NAME, nullable=False)
    # Argon2id. Never a fast hash, and never recoverable.
    password_hash: Mapped[str | None] = mapped_column(SHORT)
    role: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="member", server_default="member"
    )
    is_org_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_privileged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Bumped on password change / forced logout; a token whose version is stale
    # is rejected without needing a per-token denylist.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    disabled_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "uq_users_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_users_org_role",
            "organization_id",
            "role",
        ),
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
    )


class OrgUnit(Base):
    """A node in the organization tree. Not necessarily an agent.

    Sales Department is an org unit; Sales Director Agent is an agent that
    leads it. Conflating the two is what produces an "agent" that is really a
    mailbox with a prompt attached.
    """

    __tablename__ = "organizational_units"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(ID)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    slug: Mapped[str] = mapped_column(SHORT, nullable=False)
    unit_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="department", server_default="department"
    )
    purpose: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # Populated when a unit has a leading agent. Not a foreign key: the head can
    # be reassigned or retired while the unit persists, and we want the history.
    head_agent_id: Mapped[str | None] = mapped_column(ID)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Materialised path, e.g. "/root/front-office/sales". Makes a subtree query
    # one LIKE instead of a recursive CTE, at the cost of an application-level
    # invariant that move() must maintain.
    path: Mapped[str] = mapped_column(SHORT, nullable=False, default="/", server_default="/")
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "parent_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_organizational_units_parent_id_organizational_units",
        ),
        Index(
            "uq_organizational_units_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_org_units_org_parent",
            "organization_id",
            "parent_id",
        ),
        UniqueConstraint("organization_id", "slug", name="uq_org_units_org_slug"),
        CheckConstraint(
            "(((id)::text <> (parent_id)::text))",
            name="not_own_parent",
        ),
    )


class Role(Base):
    """An organizational function. Not an agent instance.

    Holds the authority profile, which is what actually grants capability.
    """

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    parent_role_id: Mapped[str | None] = mapped_column(ID)
    # The authority profile, serialised from domain.authority.AuthorityProfile.
    authority_profile: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    allowed_capabilities: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    max_autonomy: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="l2_parent_review", server_default="l2_parent_review"
    )
    may_delegate_to_peers: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    may_spawn_subagents: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    max_delegation_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    is_system_role: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "parent_role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_roles_parent_role_id_roles",
        ),
        Index(
            "uq_roles_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("organization_id", "name", name="uq_roles_org_name"),
    )


# =================================================== agents & capabilities ==
class AgentDefinition(Base):
    """The blueprint. Versioned; agents point at a specific version."""

    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    role_id: Mapped[str] = mapped_column(ID, nullable=False)
    system_instructions: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default="''"
    )
    # Behaviour knobs the runtime understands. Kept as data so changing
    # behaviour does not require a code deploy.
    behavior_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    # Allowed child roles. Empty means "no delegation target" under default-deny.
    allowed_child_roles: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    allowed_task_types: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    memory_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The exact prompt revision that produced a run, for reproducibility.
    prompt_version: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="1", server_default="1"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_agent_definitions_role_id_roles",
        ),
        Index(
            "uq_agent_definitions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_agent_defs_org_role",
            "organization_id",
            "role_id",
        ),
        UniqueConstraint("organization_id", "name", "version", name="uq_agent_defs_name_version"),
    )


class Agent(Base):
    """An agent instance inside an organization. The canonical registry row."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    org_unit_id: Mapped[str | None] = mapped_column(ID)
    definition_id: Mapped[str] = mapped_column(ID, nullable=False)
    role_id: Mapped[str] = mapped_column(ID, nullable=False)
    parent_agent_id: Mapped[str | None] = mapped_column(ID)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # Business lifecycle. Never 'deleted'.
    lifecycle_status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="draft", server_default="draft"
    )
    # Operational status, deliberately separate from lifecycle.
    runtime_status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="idle", server_default="idle"
    )
    health: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="unknown", server_default="unknown"
    )
    autonomy_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="l2_parent_review", server_default="l2_parent_review"
    )
    # --- added by migration 0016 --------------------------------------------
    # The most autonomy the dossier permits this agent to act at, and what it has
    # actually been granted. A *pair*, and the pair is the point: a single column
    # cannot record "permitted L3, currently L1", which is the state every agent is
    # in and the state that makes an audit answerable.
    #
    # Both default to `L1`, so the 9 seeded agents land on the most cautious value.
    # A migration that quietly raised them would be the most consequential thing it
    # could do, and nothing in a `DEFAULT` clause looks consequential.
    #
    # `autonomy_level` above is left exactly as it is: it holds prose
    # (`l1_low_risk_autonomous`, `l2_parent_review`) on a *different* scale from the
    # dossier's L1-L4, is unconstrained, and has nine live rows. Mapping one scale
    # onto the other is a judgement about what those strings were meant to mean, not
    # a schema change, so it is separate work rather than a side effect of adding a
    # ceiling beside it.
    autonomy_ceiling: Mapped[str] = mapped_column(
        String(2), nullable=False, default="L1", server_default="L1"
    )
    granted_level: Mapped[str] = mapped_column(
        String(2), nullable=False, default="L1", server_default="L1"
    )
    # A kill switch is a *recorded state*, not the absence of a running agent. A
    # stopped agent and a disabled agent are different facts and only one of them is
    # reversible, so the switch carries a reason, a moment and a hand.
    kill_switch: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    kill_reason: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    killed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    killed_by: Mapped[str | None] = mapped_column(ID)
    runtime_adapter: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pydantic_ai", server_default="pydantic_ai"
    )
    model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    # Per-agent spend/budget envelope, in addition to the org ceiling.
    budget_limit_usd: Mapped[float | None] = mapped_column(MONEY)
    budget_limit_tokens: Mapped[int | None] = mapped_column(Integer)
    # Load and liveness, for discovery-based selection.
    active_tasks: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    queue_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    estimated_latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # A real `text[]`, not jsonb. A list of strings is a list of strings: jsonb has
    # no containment operator for arrays (`&&` and `?|` do not exist for them), so
    # capability search would have to load every row and filter in Python.
    # `text[]` gets `@>`, `&&` and a GIN index, which is what makes agent
    # discovery a single indexed query.
    capabilities: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    definition_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()
    retired_at: Mapped[dt.datetime | None] = mapped_column(TS)

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "definition_id"],
            ["agent_definitions.organization_id", "agent_definitions.id"],
            name="fk_agents_definition_id_agent_definitions",
        ),
        ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_agents_org_unit_id_organizational_units",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agents_parent_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "role_id"],
            ["roles.organization_id", "roles.id"],
            name="fk_agents_role_id_roles",
        ),
        Index(
            "uq_agents_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_agents_capabilities_gin",
            "capabilities",
            postgresql_using="gin",
        ),
        Index(
            "ix_agents_discovery",
            "organization_id",
            "lifecycle_status",
            "runtime_status",
        ),
        Index(
            "ix_agents_org_parent",
            "organization_id",
            "parent_agent_id",
        ),
        Index(
            "ix_agents_org_status",
            "organization_id",
            "lifecycle_status",
        ),
        Index(
            "ix_agents_org_unit",
            "organization_id",
            "org_unit_id",
        ),
        UniqueConstraint("organization_id", "name", name="uq_agents_org_name"),
        CheckConstraint(
            "(((autonomy_ceiling)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="ceiling_known",
        ),
        CheckConstraint(
            "(((granted_level)::text = ANY ((ARRAY['L1'::character varying, "
            "'L2'::character varying, 'L3'::character varying, "
            "'L4'::character varying])::text[])))",
            name="granted_known",
        ),
        CheckConstraint(
            "(((granted_level)::text <= (autonomy_ceiling)::text))",
            name="granted_within_ceiling",
        ),
        CheckConstraint(
            "(((NOT kill_switch) OR ((kill_reason <> ''::text) AND (killed_at IS NOT NULL))))",
            name="kill_is_recorded",
        ),
        CheckConstraint(
            "((kill_switch OR (killed_at IS NULL)))",
            name="killed_at_needs_kill",
        ),
        CheckConstraint(
            "(((budget_limit_usd IS NULL) OR (budget_limit_usd >= (0)::numeric)))",
            name="budget_non_negative",
        ),
    )


class AgentRelationship(Base):
    """A non-hierarchical edge between agents.

    The org tree is a tree; real organizations also need peer routes ("ask the
    Risk agent") that are not parent/child. Modelling those as a separate edge
    keeps the hierarchy simple and makes the edge auditable on its own.
    """

    __tablename__ = "agent_relationships"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    target_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    relationship_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="peer", server_default="peer"
    )
    # Whether delegation is permitted along this edge, and under what risk.
    can_delegate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    max_risk: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="low", server_default="low"
    )
    notes: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "source_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_relationships_source_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "target_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_relationships_target_agent_id_agents",
        ),
        Index(
            "uq_agent_relationships_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint(
            "organization_id",
            "source_agent_id",
            "target_agent_id",
            "relationship_type",
            name="uq_agent_rel_edge",
        ),
        CheckConstraint(
            "(((source_agent_id)::text <> (target_agent_id)::text))",
            name="no_self_edge",
        ),
    )


# ========================================================== skills & tools ==
SKILL_DERIVED_FROM_COMMENT = (
    "The run log a learned skill was composed from: task title, tool sequence, platform "
    "refusals, attempt count. Null for a skill a person wrote, which is most of them."
)


class Skill(Base):
    """A reusable capability layer. Versioned; never a tool, never an agent."""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # trusted | reviewed | experimental | deprecated | blocked
    governance_state: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="experimental", server_default="experimental"
    )
    # A skill with elevated capability cannot be published without approval.
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_global: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    current_version_id: Mapped[str | None] = mapped_column(ID)
    tags: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    license_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "uq_skills_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_skills_governance",
            "governance_state",
        ),
        Index(
            "ix_skills_org_name",
            "organization_id",
            "name",
        ),
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    skill_id: Mapped[str] = mapped_column(ID, nullable=False)
    version: Mapped[str] = mapped_column(SHORT, nullable=False)
    instructions: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="''")
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    required_tool_ids: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required_permissions: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    required_data_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    model_requirements: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    budget_constraints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    risk_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="low", server_default="low"
    )
    # The skill's own test evidence. A skill with no test result is experimental.
    test_results: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The run a *learned* skill was composed from: task title, tool sequence, the
    # platform's refusals, how many attempts it took. Null for a skill a person
    # wrote, which is most of them.
    #
    # Separate from `test_results` on purpose. That column is what the publication
    # gate checks, and the first version of the learning loop stored evidence
    # there -- which meant a learned skill could never be published without
    # someone fabricating `passed: true`. Asking for the artefact a reviewer
    # trusts most, and rewarding its invention, is the wrong way round.
    derived_from: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment=SKILL_DERIVED_FROM_COMMENT
    )
    security_scan: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "skill_id"],
            ["skills.organization_id", "skills.id"],
            name="fk_skill_versions_skill_id_skills",
        ),
        Index(
            "uq_skill_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint(
            "organization_id",
            "skill_id",
            "version",
            name="uq_skill_versions_skill_version",
        ),
    )


class Tool(Base):
    """An executable capability with a side effect or an external interaction."""

    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # read_only | low_risk_write | external_side_effect | privileged | destructive
    risk_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="low_risk_write", server_default="low_risk_write"
    )
    effect_class: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="mutate_internal", server_default="mutate_internal"
    )
    required_scopes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    data_classification: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="internal", server_default="internal"
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A tool with no rate limit is an outage waiting for a bad agent.
    rate_limit_per_minute: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    retry_policy: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Served over MCP rather than in-process.
    is_mcp: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    mcp_server_id: Mapped[str | None] = mapped_column(ID)
    current_version_id: Mapped[str | None] = mapped_column(ID)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "mcp_server_id"],
            ["mcp_servers.organization_id", "mcp_servers.id"],
            name="fk_tools_mcp_server_id_mcp_servers",
        ),
        Index(
            "uq_tools_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_tools_org_name",
            "organization_id",
            "name",
        ),
    )


class ToolVersion(Base):
    __tablename__ = "tool_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(ID, nullable=False)
    version: Mapped[str] = mapped_column(SHORT, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    output_schema: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    implementation_ref: Mapped[str] = mapped_column(SHORT, default="", server_default="''")
    # Whether a retry of this tool is safe. False for anything that moves money,
    # sends mail, or deletes.
    is_idempotent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Log the payload or only its shape. Off by default: raw tool payloads are
    # where prompt-injected user data ends up.
    log_payload: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tools.organization_id", "tools.id"],
            name="fk_tool_versions_tool_id_tools",
        ),
        Index(
            "uq_tool_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint(
            "organization_id",
            "tool_id",
            "version",
            name="uq_tool_versions_tool_version",
        ),
    )


class McpServer(Base):
    """A registered MCP server. External servers are untrusted by default."""

    __table_args__ = (
        Index(
            "uq_mcp_servers_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
    )

    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    # stdio | http | sse
    transport: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="stdio", server_default="stdio"
    )
    endpoint: Mapped[str] = mapped_column(SHORT, default="", server_default="''")
    command: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # NAME of the secret env var, never the value. A database must not hold keys.
    auth_secret_env: Mapped[str | None] = mapped_column(SHORT)
    auth_scheme: Mapped[str] = mapped_column(SHORT, default="none", server_default="none")
    # Untrusted-by-default posture: an allowlist, never an auto-trust.
    is_allowlisted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    tool_allowlist: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    require_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    max_payload_bytes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1_048_576, server_default="1048576"
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_health_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


class ToolPolicy(Base):
    """Per-tool runtime policy: rate, budget, approval, audit requirements."""

    __tablename__ = "tool_policies"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(ID, nullable=False)
    max_calls_per_task: Mapped[int] = mapped_column(
        Integer, nullable=False, default=50, server_default="50"
    )
    max_cost_usd: Mapped[float | None] = mapped_column(MONEY)
    require_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    require_audit: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    allowed_agents: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    denied_agents: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tools.organization_id", "tools.id"],
            name="fk_tool_policies_tool_id_tools",
        ),
        Index(
            "uq_tool_policies_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("organization_id", "tool_id", name="uq_tool_policies_org_tool"),
    )


class AgentSkillBinding(Base):
    __tablename__ = "agent_skill_bindings"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    skill_id: Mapped[str] = mapped_column(ID, nullable=False)
    # Pinned so a run records the exact skill that produced it.
    skill_version_id: Mapped[str | None] = mapped_column(ID)
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    granted_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_skill_bindings_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "skill_id"],
            ["skills.organization_id", "skills.id"],
            name="fk_agent_skill_bindings_skill_id_skills",
        ),
        ForeignKeyConstraint(
            ["organization_id", "skill_version_id"],
            ["skill_versions.organization_id", "skill_versions.id"],
            name="fk_agent_skill_bindings_skill_version_id_skill_versions",
        ),
        Index(
            "uq_agent_skill_bindings_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("agent_id", "skill_id", name="uq_agent_skill_bindings"),
    )


class AgentToolBinding(Base):
    __tablename__ = "agent_tool_bindings"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    tool_id: Mapped[str] = mapped_column(ID, nullable=False)
    # Risk ceiling for this specific grant. Narrower than the tool's own risk
    # when the operator has restricted it for this agent.
    max_risk: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="medium", server_default="medium"
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    rate_limit_override: Mapped[int | None] = mapped_column(Integer)
    granted_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_tool_bindings_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "tool_id"],
            ["tools.organization_id", "tools.id"],
            name="fk_agent_tool_bindings_tool_id_tools",
        ),
        Index(
            "uq_agent_tool_bindings_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("agent_id", "tool_id", name="uq_agent_tool_bindings"),
    )


# ====================================================== tasks & delegation
#
#: Shared by migration 0032 and this model, for the reason `DEL_INTENT_COMMENT`
#: exists: a `modify_comment` drift is not something a `__table_args__` rebuild
#: can repair.
DEL_SCOPE_COMMENT = (
    "The request this task belongs to: parent_task_id, or '' for a root. Stored rather "
    "than computed at index time, because an expression index cannot be declared on the "
    "ORM and a rule the drift test cannot see is a rule nobody verifies."
)

#: The comment on `tasks.intent_fingerprint`, shared by the migration and the model. A
#: `modify_comment` drift is what a rebuild-based sync cannot repair on its own, and the check
#: that caught it -- `test_there_is_no_drift` -- is the reason it is a constant and not prose
#: twice.
DEL_INTENT_COMMENT = (
    "Hash of the whole normalised goal, in order, plus the owner agent. "
    "What the delegating executor writes; what the partial unique index keys on. "
    "NULL means the row was not created by a delegation. It was the first 16 tokens until "
    "F272: that blocked a second run of the same tender from starting, because the run "
    "marker the platform appends sits past the window."
)


#: The comment on `tasks.requester_agent_id`, shared by migration 0027 and this model.
#: `test_there_is_no_drift` compares the two, and a comment written twice is a comment that
#: drifts.
REQ_AGENT_COMMENT = (
    "The agent that asked for this task, when an agent did. Separate from requester_id, "
    "which is a foreign key to users and can only hold a person. A human requester goes in "
    "requester_id; an agent requester goes here."
)


class Task(Base):
    """Canonical unit of work. The state here is the system's truth."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    org_unit_id: Mapped[str | None] = mapped_column(ID)
    parent_task_id: Mapped[str | None] = mapped_column(ID)
    root_task_id: Mapped[str | None] = mapped_column(ID)
    requester_id: Mapped[str | None] = mapped_column(ID)
    requester_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="human", server_default="human"
    )
    #: Which **agent** asked for this task. Separate from `requester_id`, which is a foreign key
    #: to `users` and can therefore only ever hold a person -- so before this column the answer
    #: to "who asked" was "an agent, unidentified". Migration 0027. `None` means the requester was
    #: a person, **or** an agent predating the column: the constraint forbids a non-agent
    #: requester from naming an agent, but deliberately does not require this column on rows
    #: that predate it, because filling it in would mean guessing.
    requester_agent_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=REQ_AGENT_COMMENT
    )
    owner_agent_id: Mapped[str | None] = mapped_column(ID)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    goal: Mapped[str] = mapped_column(LONG, nullable=False)
    task_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="execution", server_default="execution"
    )
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="created", server_default="created"
    )
    priority: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="normal", server_default="normal"
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    expected_output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Detects two agents independently requesting the same work. This is the
    # *intent* hash and never changes: two tasks can legitimately share it.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    #: A key for "this parent already gave this owner this instruction", from the first 12
    #: normalised tokens of the goal. Distinct from `fingerprint`, which hashes the **whole**
    #: goal: a restatement that appends a clause moves the whole-goal hash and not this one.
    #: Migration 0026. `None` means the row was not created by a delegation, and the partial
    #: unique index excludes NULL -- so the column says what it does not know instead of
    #: guessing.
    intent_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment=DEL_INTENT_COMMENT
    )
    #: Migration 0032. **The request this task belongs to, stored rather than computed.**
    #:
    #: The duplicate index has to say "has *this request* already asked this agent for this
    #: work?", and `parent_task_id` is nullable -- a NULL never compares equal, so roots
    #: would be exempt. The obvious fix, `COALESCE(parent_task_id, '')` inside the index,
    #: cannot be declared on the ORM: `make model-sync` splits the expression on its commas
    #: and the model stops importing with `ConstraintColumnNotFoundError`.
    #:
    #: So the value is data. `''` means a root, and every root shares one bucket -- so
    #: pressing Run twice with the same goal is still refused, which is the case the index
    #: was built for and the one a `COALESCE(parent_task_id, id)` key silently broke.
    intent_scope: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default="", default="", comment=DEL_SCOPE_COMMENT
    )
    # The shape of the work, distinct from the wording of the request. `fingerprint`
    # answers "have I been asked this before?" and is applied to a unique index, so
    # two differently-worded requests are two tasks. A procedure answers "is this
    # the same shape of work?", which is the question repetition detection needs and
    # the task hash cannot answer. Nullable: null means "not observed", which is
    # what a task whose trace was never written actually is.
    #
    # The comment below is the same text migration 0004 attached to the column.
    # It was written in the migration and never added to the model, so
    # autogenerate proposed to delete it — the only copy of the distinction
    # would have been removed by a migration whose stated purpose was to add
    # construction tables. Carrying it on the model is what stops that.
    procedure_fingerprint: Mapped[str | None] = mapped_column(
        String(64),
        comment=(
            "Hash of the tool sequence and argument *shape*. Distinct from "
            "`fingerprint`, which hashes the goal's wording: two tasks can be "
            "different requests and the same procedure."
        ),
    )
    # What the deduplication unique index is applied to. Equal to `fingerprint`
    # normally; salted when the caller explicitly asked for parallel work, so
    # the database can still tell the two rows apart. Keeping the intent hash
    # separate means "did these two agents ask for the same thing?" stays
    # answerable by querying `fingerprint` alone.
    dedup_key: Mapped[str] = mapped_column(String(96), nullable=False)
    budget_limit_usd: Mapped[float | None] = mapped_column(MONEY)
    budget_limit_tokens: Mapped[int | None] = mapped_column(Integer)
    spent_usd: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    spent_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    deadline_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # A task nobody is advancing is a deadlock; this is the lease that says so.
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(LONG)
    failure_category: Mapped[str | None] = mapped_column(SHORT)
    workflow_id: Mapped[str | None] = mapped_column(SHORT)
    workflow_run_id: Mapped[str | None] = mapped_column(SHORT)
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_tasks_org_unit_id_organizational_units",
        ),
        ForeignKeyConstraint(
            ["organization_id", "owner_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_tasks_owner_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_tasks_parent_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "requester_id"],
            ["users.organization_id", "users.id"],
            name="fk_tasks_requester_id_users",
        ),
        ForeignKeyConstraint(
            ["organization_id", "root_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_tasks_root_task_id_tasks",
        ),
        Index(
            "uq_tasks_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_tasks_requester_agent",
            "organization_id",
            "requester_agent_id",
            postgresql_where=text(
                "(requester_agent_id IS NOT NULL)",
            ),
        ),
        Index(
            "uq_tasks_live_intent",
            "organization_id",
            "owner_agent_id",
            "intent_scope",
            "intent_fingerprint",
            unique=True,
            postgresql_where=text(
                "((status)::text = ANY ((ARRAY['created'::character varying, "
                "'assigned'::character varying, 'running'::character varying])::text[]))",
            ),
        ),
        Index(
            "ix_tasks_fingerprint",
            "organization_id",
            "fingerprint",
            "status",
        ),
        Index(
            "ix_tasks_org_owner",
            "organization_id",
            "owner_agent_id",
            "status",
        ),
        Index(
            "ix_tasks_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_tasks_parent",
            "parent_task_id",
        ),
        Index(
            "uq_tasks_active_dedup_key",
            "organization_id",
            "dedup_key",
            unique=True,
            postgresql_where=text(
                "((status)::text <> ALL ((ARRAY['completed'::character varying, "
                "'failed'::character varying, 'canceled'::character varying, "
                "'expired'::character varying])::text[]))",
            ),
        ),
        Index(
            "ix_tasks_org_owner_procedure",
            "organization_id",
            "owner_agent_id",
            "procedure_fingerprint",
        ),
        CheckConstraint(
            "((((requester_type)::text = 'agent'::text) OR (requester_agent_id IS NULL)))",
            name="requester_kind_matches",
        ),
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(ID, nullable=False)
    depends_on_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    dependency_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="finish_to_start", server_default="finish_to_start"
    )
    is_satisfied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "depends_on_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_dependencies_depends_on_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_dependencies_task_id_tasks",
        ),
        Index(
            "uq_task_dependencies_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_deps_edge"),
        CheckConstraint(
            "(((task_id)::text <> (depends_on_task_id)::text))",
            name="no_self_dependency",
        ),
    )


class Delegation(Base):
    """A structured request from one agent to another. Never a chat message."""

    __tablename__ = "delegations"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    child_task_id: Mapped[str | None] = mapped_column(ID)
    source_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    target_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="proposed", server_default="proposed"
    )
    objective: Mapped[str] = mapped_column(LONG, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The full ancestor chain at the time of the request. This is the evidence
    # that a cycle check actually ran, and what to show an auditor.
    delegation_path: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    budget_limit_usd: Mapped[float | None] = mapped_column(MONEY)
    budget_limit_tokens: Mapped[int | None] = mapped_column(Integer)
    required_output_schema: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Set when policy required a human to agree to the delegation itself.
    # Plain column, not a foreign key: `approvals -> executions -> delegations ->
    # approvals` is a real reference cycle, and `approvals.approval_id` being
    # the deciding column is enough to join on. A self-referential FK chain
    # would force either a deferred-constraint dance or a cycle in the DDL.
    approval_id: Mapped[str | None] = mapped_column(ID)
    denial_reason: Mapped[str | None] = mapped_column(LONG)
    deadline_at: Mapped[dt.datetime | None] = mapped_column(TS)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(TS)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "child_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_delegations_child_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_delegations_parent_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "source_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_delegations_source_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "target_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_delegations_target_agent_id_agents",
        ),
        Index(
            "uq_delegations_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_delegations_org_status",
            "organization_id",
            "status",
        ),
        Index(
            "ix_delegations_parent_task",
            "parent_task_id",
        ),
        Index(
            "ix_delegations_source",
            "organization_id",
            "source_agent_id",
            "status",
        ),
        Index(
            "ix_delegations_target",
            "organization_id",
            "target_agent_id",
            "status",
        ),
    )


class SubagentRun(Base):
    """An ephemeral, task-bound agent. Bounded on every axis by construction."""

    __tablename__ = "subagent_runs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    parent_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    parent_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    child_task_id: Mapped[str | None] = mapped_column(ID)
    # A subagent has no agent row: it is deliberately not a permanent identity.
    virtual_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="spawning", server_default="spawning"
    )
    objective: Mapped[str] = mapped_column(LONG, nullable=False)
    context_scope: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    tool_scope: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    skill_scope: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    spawn_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    max_fanout: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    budget_limit_usd: Mapped[float | None] = mapped_column(MONEY)
    budget_limit_tokens: Mapped[int | None] = mapped_column(Integer)
    ttl_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default="600"
    )
    ttl_expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    failure_reason: Mapped[str | None] = mapped_column(LONG)
    started_at: Mapped[dt.datetime | None] = mapped_column(TS)
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "child_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_subagent_runs_child_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_subagent_runs_parent_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "parent_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_subagent_runs_parent_task_id_tasks",
        ),
        Index(
            "uq_subagent_runs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_subagent_runs_parent",
            "organization_id",
            "parent_agent_id",
            "status",
        ),
        Index(
            "ix_subagent_runs_task",
            "parent_task_id",
        ),
        CheckConstraint(
            "((max_fanout >= 0))",
            name="fanout_non_negative",
        ),
    )


TOOL_CALLS_COMMENT = (
    "Tool calls this execution made. Written once when the execution is finished, "
    "not incremented per call: the number cannot change its meaning until the run "
    "ends, and a write per call would sit on the hot path for nothing."
)
MODEL_CALLS_COMMENT = (
    "Model calls this execution made. The pair (model_calls, tool_calls) is what "
    "shows a model that lost the thread: many model calls, few useful tool calls."
)


class Execution(Base):
    """One attempt at one task by one agent. The unit of observability."""

    __tablename__ = "executions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(ID, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(ID)
    delegation_id: Mapped[str | None] = mapped_column(ID)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="running", server_default="running"
    )
    runtime_adapter: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pydantic_ai", server_default="pydantic_ai"
    )
    model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    model_used: Mapped[str | None] = mapped_column(SHORT)
    # Everything needed to explain the run later. Deliberately no chain of
    # thought: the platform records decisions, inputs and outcomes.
    agent_definition_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    skill_versions: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    tool_versions: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    workflow_id: Mapped[str | None] = mapped_column(SHORT)
    workflow_run_id: Mapped[str | None] = mapped_column(SHORT)
    trace_id: Mapped[str | None] = mapped_column(SHORT)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    decision_record: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    artifacts: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    escalation: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_kind: Mapped[str | None] = mapped_column(SHORT)
    error_category: Mapped[str | None] = mapped_column(SHORT)
    error_message: Mapped[str | None] = mapped_column(LONG)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Written once when the execution finishes, not incremented per call: the number
    # cannot change its meaning until the run ends, and a write per call would sit
    # on the hot path of every tool call for the same answer. NULL on historical
    # rows -- those made an unknown number, and 0 would claim they made none.
    tool_call_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment=TOOL_CALLS_COMMENT
    )
    model_call_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment=MODEL_CALLS_COMMENT
    )
    started_at: Mapped[dt.datetime] = created_at_col()
    finished_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_executions_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "delegation_id"],
            ["delegations.organization_id", "delegations.id"],
            name="fk_executions_delegation_id_delegations",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_executions_task_id_tasks",
        ),
        Index(
            "uq_executions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_executions_agent",
            "organization_id",
            "agent_id",
            "created_at",
        ),
        Index(
            "ix_executions_org_task",
            "organization_id",
            "task_id",
        ),
        Index(
            "ix_executions_trace",
            "trace_id",
        ),
    )


# ==================================================== workflows & messages ==
class WorkflowRun(Base):
    """Temporal owns the history; this row is the platform's searchable view.

    Deliberately a projection, not the source of truth. Replaying history from
    the database instead of from Temporal would put two engines in charge of
    the same state machine.
    """

    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(ID)
    workflow_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    workflow_version: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="v1", server_default="v1"
    )
    temporal_workflow_id: Mapped[str] = mapped_column(SHORT, nullable=False)
    temporal_run_id: Mapped[str | None] = mapped_column(SHORT)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="running", server_default="running"
    )
    input: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    result: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    current_step: Mapped[str | None] = mapped_column(SHORT)
    started_at: Mapped[dt.datetime] = created_at_col()
    completed_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_workflow_runs_task_id_tasks",
        ),
        Index(
            "uq_workflow_runs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_workflow_runs_org_status",
            "organization_id",
            "status",
        ),
        UniqueConstraint("temporal_workflow_id", name="uq_workflow_runs_temporal"),
    )


class BlackboardEntry(Base):
    """Structured coordination state for multi-agent work.

    A blackboard is not a chat log. Each entry is a typed field a workflow
    defined, mutated deliberately by a named agent. Free-form conversation
    cannot be queried, cannot be diffed, and cannot be replayed.
    """

    __tablename__ = "blackboard_entries"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str] = mapped_column(ID, nullable=False)
    # e.g. 'findings', 'open_questions', 'draft'
    slot: Mapped[str] = mapped_column(SHORT, nullable=False)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Optimistic concurrency: a stale writer is rejected rather than silently
    # overwriting a peer agent's contribution.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    written_by_agent_id: Mapped[str | None] = mapped_column(ID)
    written_by_execution_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_blackboard_entries_task_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "written_by_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_blackboard_entries_written_by_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "written_by_execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_blackboard_entries_written_by_execution_id_executions",
        ),
        UniqueConstraint("task_id", "slot", name="uq_blackboard_task_slot"),
    )


class Message(Base):
    """Projection of structured events for the operator-facing conversation.

    This is a *view*, written by the projection consumer. Nothing in the
    platform derives business state from it, which is what keeps chat from
    becoming an accidental source of truth.
    """

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(ID)
    thread_id: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    # user | agent | system | tool | approval
    sender_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    sender_id: Mapped[str | None] = mapped_column(ID)
    sender_name: Mapped[str] = mapped_column(SHORT, default="", server_default="''")
    role: Mapped[str] = mapped_column(SHORT, nullable=False, default="note", server_default="note")
    content: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="''")
    # Structured references rendered alongside the text: task, delegation, tool
    # call, A2A call, approval, event, workflow.
    references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    event_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_messages_task_id_tasks",
        ),
        Index(
            "uq_messages_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_messages_org_task",
            "organization_id",
            "task_id",
            "created_at",
        ),
        Index(
            "ix_messages_thread",
            "organization_id",
            "thread_id",
            "created_at",
        ),
    )


# ======================================================== events & outbox ==
class Event(Base):
    """An immutable, already-published fact."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # CloudEvents-shaped envelope. `type` includes the version.
    type: Mapped[str] = mapped_column(SHORT, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    source: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="control-plane", server_default="control-plane"
    )
    subject: Mapped[str] = mapped_column(SHORT, nullable=False)
    # 1.0 compatibility
    spec_version: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="1.0", server_default="1.0"
    )
    trace_id: Mapped[str | None] = mapped_column(SHORT)
    actor_id: Mapped[str | None] = mapped_column(ID)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        Index(
            "uq_events_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_events_org_subject",
            "organization_id",
            "subject",
            "occurred_at",
        ),
        Index(
            "ix_events_org_type",
            "organization_id",
            "type",
            "occurred_at",
        ),
    )


class OutboxEvent(Base):
    """Written in the same transaction as the state change that caused it.

    The relay then publishes to NATS. Publishing inside the business
    transaction would either hold a lock across a network call or lose the
    event on rollback; neither is acceptable for a fact other components act on.
    """

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(ID, nullable=False)
    event_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    subject: Mapped[str] = mapped_column(SHORT, nullable=False)
    # The NATS subject to publish on.
    nats_subject: Mapped[str] = mapped_column(NAME, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    trace_id: Mapped[str | None] = mapped_column(SHORT)
    # NULL until published. The relay claims rows with FOR UPDATE SKIP LOCKED,
    # so two relay instances cannot publish the same row twice.
    published_at: Mapped[dt.datetime | None] = mapped_column(TS)
    publish_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(LONG)
    dead_lettered_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        Index(
            "uq_outbox_events_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_outbox_unpublished",
            "published_at",
            "created_at",
        ),
        UniqueConstraint("event_id", name="uq_outbox_events_event"),
    )


class ConsumerOffset(Base):
    """Durable-consumer bookkeeping, including the dedup key.

    Storing the processed event id set here is what makes "at-least-once
    delivery, exactly-once effect" achievable without a distributed transaction.
    """

    __tablename__ = "consumer_offsets"

    __table_args__ = (UniqueConstraint("consumer_name", name="uq_consumer_offsets_consumer"),)

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    consumer_name: Mapped[str] = mapped_column(SHORT, nullable=False)
    stream_name: Mapped[str] = mapped_column(SHORT, nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    last_event_id: Mapped[str | None] = mapped_column(ID)
    processed_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    duplicate_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    updated_at: Mapped[dt.datetime] = updated_at_col()


class IdempotencyRecord(Base):
    """A claim that an operation has already been performed.

    The unique constraint on the key is the actual mechanism; this table holds
    the response so a retry can be answered without redoing the work.
    """

    __tablename__ = "idempotency_records"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    idempotency_key: Mapped[str] = mapped_column(SHORT, nullable=False)
    operation: Mapped[str] = mapped_column(SHORT, nullable=False)
    # A hash of the request. A replay with a *different* body is a client bug
    # and must be rejected rather than answered with the first response.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    resource_id: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)

    __table_args__ = (
        Index(
            "uq_idempotency_records_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_idempotency_expiry",
            "expires_at",
        ),
        UniqueConstraint("idempotency_key", "operation", name="uq_idempotency_key_operation"),
    )


# ================================================ approvals & governance ==
class Approval(Base):
    """A pause point. The only thing that unblocks a human-gated action."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(ID)
    execution_id: Mapped[str | None] = mapped_column(ID)
    # The action being approved, as a typed description. Never a chat message.
    action_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    action_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    effect_class: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="mutate_internal", server_default="mutate_internal"
    )
    risk_level: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="medium", server_default="medium"
    )
    reason: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # A hash of exactly what is being approved, so an approval cannot be
    # stretched to cover a different payload than the one reviewed.
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[str] = mapped_column(ID, nullable=False)
    requested_by_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="agent", server_default="agent"
    )
    # `text[]` rather than jsonb so the inbox query can use array containment
    # instead of loading every pending approval and filtering in Python.
    required_approver_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list, server_default="{}"
    )
    assigned_approver_id: Mapped[str | None] = mapped_column(ID)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="pending", server_default="pending"
    )
    decision: Mapped[str | None] = mapped_column(SHORT)
    decision_note: Mapped[str | None] = mapped_column(LONG)
    decided_by: Mapped[str | None] = mapped_column(ID)
    decided_at: Mapped[dt.datetime | None] = mapped_column(TS)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    # The Temporal signal name, so the workflow resumes on the right one.
    signal_name: Mapped[str | None] = mapped_column(SHORT)
    workflow_id: Mapped[str | None] = mapped_column(SHORT)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "assigned_approver_id"],
            ["users.organization_id", "users.id"],
            name="fk_approvals_assigned_approver_id_users",
        ),
        ForeignKeyConstraint(
            ["organization_id", "decided_by"],
            ["users.organization_id", "users.id"],
            name="fk_approvals_decided_by_users",
        ),
        ForeignKeyConstraint(
            ["organization_id", "execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_approvals_execution_id_executions",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_approvals_task_id_tasks",
        ),
        Index(
            "uq_approvals_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_approvals_org_status",
            "organization_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_approvals_task",
            "task_id",
        ),
        CheckConstraint(
            "((((status)::text <> 'approved'::text) OR (decided_at IS NOT NULL)))",
            name="approved_requires_decision_time",
        ),
    )


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    scope: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="organization", server_default="organization"
    )
    current_version_id: Mapped[str | None] = mapped_column(ID)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "uq_policies_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("organization_id", "name", name="uq_policies_org_name"),
    )


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[str] = mapped_column(ID, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # The rule set from domain.policy.PolicySet.
    rules: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "policy_id"],
            ["policies.organization_id", "policies.id"],
            name="fk_policy_versions_policy_id_policies",
        ),
        Index(
            "uq_policy_versions_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_policy_versions_current",
            "organization_id",
            "policy_id",
            unique=True,
            postgresql_where=text(
                "is_current",
            ),
        ),
    )


class Budget(Base):
    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # organization | department | agent | task | subagent | model | workflow
    scope_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    scope_id: Mapped[str | None] = mapped_column(ID)
    period: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="monthly", server_default="monthly"
    )
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_cost_usd: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    spent_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    spent_cost_usd: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    reserved_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reserved_cost_usd: Mapped[float] = mapped_column(
        MONEY, nullable=False, default=0, server_default="0"
    )
    period_started_at: Mapped[dt.datetime] = created_at_col()
    period_ends_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "uq_budgets_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint(
            "organization_id",
            "scope_type",
            "scope_id",
            "period",
            name="uq_budgets_scope_period",
        ),
        CheckConstraint(
            "(((max_tokens >= 0) AND (max_cost_usd >= (0)::numeric)))",
            name="budget_non_negative",
        ),
    )


class BudgetLedgerEntry(Base):
    """Append-only. A correction is a reversing row, never an update."""

    __tablename__ = "budget_ledger"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    budget_id: Mapped[str] = mapped_column(ID, nullable=False)
    task_id: Mapped[str | None] = mapped_column(ID)
    execution_id: Mapped[str | None] = mapped_column(ID)
    # reserve | commit | release | reverse | reset
    entry_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    model_used: Mapped[str | None] = mapped_column(SHORT)
    reverses_entry_id: Mapped[str | None] = mapped_column(ID)
    note: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "budget_id"],
            ["budgets.organization_id", "budgets.id"],
            name="fk_budget_ledger_budget_id_budgets",
        ),
        ForeignKeyConstraint(
            ["organization_id", "execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_budget_ledger_execution_id_executions",
        ),
        ForeignKeyConstraint(
            ["organization_id", "reverses_entry_id"],
            ["budget_ledger.organization_id", "budget_ledger.id"],
            name="fk_budget_ledger_reverses_entry_id_budget_ledger",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_budget_ledger_task_id_tasks",
        ),
        Index(
            "uq_budget_ledger_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_budget_ledger_budget",
            "budget_id",
            "created_at",
        ),
    )


class ModelProfile(Base):
    """A named model capability requirement, not a concrete model.

    Agents reference `reasoning_high`, not `openai/gpt-...`. Swapping the
    provider behind a profile then requires no agent change at all.
    """

    __tablename__ = "model_profiles"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(SHORT, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # Ordered candidates. A provider that is not in this list is never called,
    # which is what makes a privacy restriction enforceable.
    providers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    # Provider names permitted for a given data classification.
    privacy_rules: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # The classification ceiling for this *profile*, distinct from the per-provider
    # ceilings in `privacy_rules`. It is a column rather than something derived,
    # because it is the value an operator sets per organisation and the value the
    # API has to be able to report; the API read `max_classification` before this
    # column existed and returned 500 on every request to that endpoint.
    max_classification: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="restricted", server_default="restricted"
    )
    fallback_profile_id: Mapped[str | None] = mapped_column(ID)
    max_latency_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60_000, server_default="60000"
    )
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    requires_tool_calling: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    requires_structured_output: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "fallback_profile_id"],
            ["model_profiles.organization_id", "model_profiles.id"],
            name="fk_model_profiles_fallback_profile_id_model_profiles",
        ),
        Index(
            "uq_model_profiles_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_model_profiles_org_name",
            "organization_id",
            "name",
        ),
    )


class ModelUsage(Base):
    """Per-call usage and cost. The basis for every cost dashboard."""

    __tablename__ = "model_usage"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    task_id: Mapped[str | None] = mapped_column(ID)
    execution_id: Mapped[str | None] = mapped_column(ID)
    agent_id: Mapped[str | None] = mapped_column(ID)
    model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    provider: Mapped[str] = mapped_column(SHORT, nullable=False)
    model_used: Mapped[str] = mapped_column(NAME, nullable=False)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reasoning_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # fallback | retry | primary: lets the evaluation harness tell a real pass
    # from a pass that only happened because a fallback engaged.
    routing_reason: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="primary", server_default="primary"
    )
    status: Mapped[str] = mapped_column(SHORT, nullable=False, default="ok", server_default="ok")
    error_kind: Mapped[str | None] = mapped_column(SHORT)
    trace_id: Mapped[str | None] = mapped_column(SHORT)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_model_usage_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_model_usage_execution_id_executions",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_model_usage_task_id_tasks",
        ),
        Index(
            "ix_model_usage_agent",
            "organization_id",
            "agent_id",
        ),
        Index(
            "ix_model_usage_org_time",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_model_usage_task",
            "task_id",
        ),
    )


# =================================================== memory & knowledge ==
class MemoryItem(Base):
    """One memory record. Tier determines who may read it and how long it lives."""

    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Scoped more narrowly than the org whenever the tier calls for it.
    org_unit_id: Mapped[str | None] = mapped_column(ID)
    agent_id: Mapped[str | None] = mapped_column(ID)
    task_id: Mapped[str | None] = mapped_column(ID)
    # run_context | working | task_episodic | agent | department | organization
    # | semantic | audit
    tier: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="working", server_default="working"
    )
    kind: Mapped[str] = mapped_column(SHORT, nullable=False, default="note", server_default="note")
    title: Mapped[str] = mapped_column(NAME, nullable=False, default="", server_default="''")
    content: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="''")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # public | internal | confidential | restricted | secret
    classification: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="internal", server_default="internal"
    )
    source_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="agent", server_default="agent"
    )
    source_ref: Mapped[str | None] = mapped_column(SHORT)
    # Human-supplied provenance. The platform cannot infer that a scraped page
    # is trustworthy, so an item without a source is not treated as evidence.
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    confidence: Mapped[float | None] = mapped_column()
    retention_until: Mapped[dt.datetime | None] = mapped_column(TS)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_by: Mapped[str | None] = mapped_column(ID)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_memory_items_agent_id_agents",
        ),
        ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_memory_items_org_unit_id_organizational_units",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_memory_items_task_id_tasks",
        ),
        Index(
            "uq_memory_items_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_memory_agent",
            "organization_id",
            "agent_id",
        ),
        Index(
            "ix_memory_expiry",
            "retention_until",
            postgresql_where=text(
                "(retention_until IS NOT NULL)",
            ),
        ),
        Index(
            "ix_memory_org_tier",
            "organization_id",
            "tier",
            "created_at",
        ),
        Index(
            "ix_memory_unit",
            "organization_id",
            "org_unit_id",
        ),
    )


class MemoryChunk(Base):
    """A retrievable span of a memory item, with its embedding.

    `embedding_model` is part of the identity: mixing vectors from two models
    in one index returns nonsense with high confidence, and nothing about the
    query would reveal it.
    """

    __tablename__ = "memory_chunks"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    memory_item_id: Mapped[str] = mapped_column(ID, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    content: Mapped[str] = mapped_column(LONG, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    embedding: Mapped[Any | None] = mapped_column(Vector(512))
    embedding_model: Mapped[str | None] = mapped_column(SHORT)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "memory_item_id"],
            ["memory_items.organization_id", "memory_items.id"],
            name="fk_memory_chunks_memory_item_id_memory_items",
        ),
        Index(
            "uq_memory_chunks_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_memory_chunks_org_model",
            "organization_id",
            "embedding_model",
        ),
        Index(
            "ix_memory_chunks_vector",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops=["vector_cosine_ops"],
        ),
        UniqueConstraint("memory_item_id", "chunk_index", name="uq_memory_chunks_item_index"),
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    org_unit_id: Mapped[str | None] = mapped_column(ID)
    title: Mapped[str] = mapped_column(NAME, nullable=False)
    content_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="text/plain", server_default="text/plain"
    )
    # A URI or a reference, never the bytes. Content lives in the object store
    # or in memory_chunks.
    storage_uri: Mapped[str] = mapped_column(SHORT, default="", server_default="''")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    classification: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="internal", server_default="internal"
    )
    source_type: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="upload", server_default="upload"
    )
    source_ref: Mapped[str | None] = mapped_column(SHORT)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    retention_until: Mapped[dt.datetime | None] = mapped_column(TS)
    #: `ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]`, parsed by
    #: `ai_orchestrator.domain.document_code.DocumentCode` and never concatenated anywhere.
    #: Nullable on purpose: `documents` also holds an uploaded attachment with no dossier
    #: number, and inventing a code for it would put a lie in the identifier column. The
    #: `CHECK` below is the shape only -- whether the block and the kind are *real* is the
    #: parser's judgement, and only the parser's.
    code: Mapped[str | None] = mapped_column(String(32))
    #: Whether the document is still **valid**. Not the same question as
    #: `retention_until`, which is how long it must be **kept**. A construction permit has
    #: a three-year life and a seven-year retention obligation, so it expires long before
    #: it may be destroyed; a schema with one date has to record the wrong one. The second
    #: `CHECK` refuses an expiry that outlives the retention deadline, because that
    #: combination is never a decision anybody made on purpose.
    expires_on: Mapped[dt.date | None] = mapped_column(Date)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "org_unit_id"],
            ["organizational_units.organization_id", "organizational_units.id"],
            name="fk_documents_org_unit_id_organizational_units",
        ),
        Index(
            "uq_documents_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "uq_documents_org_code",
            "organization_id",
            "code",
            unique=True,
        ),
        UniqueConstraint("organization_id", "content_hash", name="uq_documents_org_hash"),
        CheckConstraint(
            "(((code IS NULL) OR ((code)::text ~ '^ONX-[A-Z]{2,3}-[A-Z]{2,3}-[A-Z]{2,4}-[0-9]"
            "{3}$'::text)))",
            name="code_is_well_formed",
        ),
        CheckConstraint(
            "(((expires_on IS NULL) OR (retention_until IS NULL) OR (expires_on <= ((retentio"
            "n_until AT TIME ZONE 'UTC'::text))::date)))",
            name="expiry_precedes_retention",
        ),
    )


# ==================================================== audit & observability ==
class QuarantinedProposal(Base):
    """A proposal the danger scan refused. **The payload is deliberately not here.**

    Hermes leaves a quarantined skill on disk and hides it from the index. This
    platform does not, because a quarantined row in the database is one query away
    from being used — and the operator dashboard that would eventually surface it is
    exactly the query that would do it.

    So the row records *what* was proposed, by whom, and what tripped the scan, and
    never *what it said*. The text is reconstructible from `evidence_task_ids` if a
    human genuinely needs it, which is a decision someone makes deliberately. A
    column that was never written cannot be selected, exported, or read by a
    mistake.

    The absence of a foreign key to `organizations` is intentional: the record must
    outlive the organisation, or an operator loses the only trace that anything was
    attempted.
    """

    __tablename__ = "quarantined_proposals"

    id: Mapped[str] = mapped_column(ID, primary_key=True, default=make_audit_id)
    # NOT NULL, matching migration 0005. It was nullable here for as long as the
    # table existed, and that is a real hole rather than a cosmetic one: the RLS
    # predicate is `organization_id = current_setting('app.current_tenant')`, and
    # a NULL never satisfies that comparison. A quarantine record with a NULL org
    # would therefore be invisible to the tenant it belongs to while still
    # occupying a row and still appearing in owner-role reports — the worst
    # combination, since it looks recorded and cannot be found. Found by
    # autogenerate; fixed in migration 0006 and here together, because neither
    # half alone is the fix.
    organization_id: Mapped[str] = mapped_column(ID, nullable=False)
    procedure_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_by: Mapped[str] = mapped_column(NAME, nullable=False)
    #: The findings that tripped the scan. Each names its field and its class, and
    #: clips the matched text — enough to identify the finding, not enough to
    #: reassemble a payload from.
    findings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    evidence_task_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        Index(
            "ix_quarantined_proposals_org_created",
            "organization_id",
            "created_at",
        ),
    )


class AuditLog(Base):
    """Append-only. The answer to "who did what, when, under which policy"."""

    __tablename__ = "audit_logs"

    # A prefixed ULID like every other table, so an id is meaningful in a log line
    # and can be referenced from an approval or an event. The `sequence` column
    # below carries ordering; `id` carries identity. Making `sequence` the
    # primary key instead would leave no stable id to reference.
    id: Mapped[str] = mapped_column(ID, primary_key=True, default=make_audit_id)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    # Monotonic, and independent of wall-clock resolution: two audit entries in
    # the same millisecond must still have a defined order.
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        unique=True,
        server_default=func.nextval("audit_log_seq"),
        index=True,
    )
    actor_id: Mapped[str | None] = mapped_column(ID)
    actor_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    action: Mapped[str] = mapped_column(SHORT, nullable=False)
    resource_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(ID)
    task_id: Mapped[str | None] = mapped_column(ID)
    execution_id: Mapped[str | None] = mapped_column(ID)
    outcome: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="success", server_default="success"
    )
    policy_decision: Mapped[str | None] = mapped_column(SHORT)
    policy_rule_id: Mapped[str | None] = mapped_column(SHORT)
    policy_reason: Mapped[str | None] = mapped_column(LONG)
    approval_id: Mapped[str | None] = mapped_column(ID)
    trace_id: Mapped[str | None] = mapped_column(SHORT)
    # Shaped context, not raw payloads. A payload that may contain a secret or
    # user data is redacted at the call site before it reaches here.
    context: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(SHORT)
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "approval_id"],
            ["approvals.organization_id", "approvals.id"],
            name="fk_audit_logs_approval_id_approvals",
        ),
        ForeignKeyConstraint(
            ["organization_id", "execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_audit_logs_execution_id_executions",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_audit_logs_task_id_tasks",
        ),
        Index(
            "ix_audit_actor",
            "organization_id",
            "actor_id",
        ),
        Index(
            "ix_audit_org_time",
            "organization_id",
            "created_at",
        ),
        Index(
            "ix_audit_resource",
            "resource_type",
            "resource_id",
        ),
        Index(
            "ix_audit_task",
            "task_id",
        ),
    )


class CredentialMetadata(Base):
    """Metadata about a credential. The secret itself is never stored here.

    Only the *name* of the secret and where it is injected from. This table
    exists so an operator can answer "which credential does this integration
    need, and is it configured" without any value ever reaching the database.
    """

    __tablename__ = "credentials_metadata"

    __table_args__ = (
        Index(
            "uq_credentials_metadata_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("name", name="uq_credentials_metadata_name"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(SHORT, nullable=False)
    provider: Mapped[str] = mapped_column(SHORT, nullable=False)
    # The env var / vault path that holds the value.
    secret_ref: Mapped[str] = mapped_column(SHORT, nullable=False)
    classification: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="secret", server_default="secret"
    )
    is_configured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    expires_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


class Connector(Base):
    """An external system integration."""

    __tablename__ = "connectors"

    __table_args__ = (
        Index(
            "uq_connectors_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("organization_id", "name", name="uq_connectors_org_name"),
    )

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    status: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="inactive", server_default="inactive"
    )
    # Config with references to secret names, never their values.
    config: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    # Keyed by name, value is always a secret NAME.
    secret_refs: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    requires_approval_to_activate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(TS)
    last_error: Mapped[str | None] = mapped_column(LONG)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


# ============================================================== A2A & eval ==
class A2AAgent(Base):
    """A remote agent known to the platform. Untrusted until validated."""

    __tablename__ = "a2a_agents"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # A local agent exposed remotely, or a genuinely remote one.
    local_agent_id: Mapped[str | None] = mapped_column(ID)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    capabilities: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    supported_tasks: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    protocol_version: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="0.3.0", server_default="0.3.0"
    )
    # The Agent Card, validated and normalised on registration.
    agent_card: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    card_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    card_verified_at: Mapped[dt.datetime | None] = mapped_column(TS)
    auth_scheme: Mapped[str] = mapped_column(SHORT, default="none", server_default="none")
    auth_secret_ref: Mapped[str | None] = mapped_column(SHORT)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 'unknown' is a valid, honest initial value. A remote agent that has never
    # been probed must not be reported healthy.
    health: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="unknown", server_default="unknown"
    )
    availability: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="unknown", server_default="unknown"
    )
    last_contact_at: Mapped[dt.datetime | None] = mapped_column(TS)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "local_agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_a2a_agents_local_agent_id_agents",
        ),
        Index(
            "uq_a2a_agents_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_a2a_agents_org",
            "organization_id",
            "is_active",
        ),
    )


class A2AEndpoint(Base):
    __tablename__ = "a2a_endpoints"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        ID, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    a2a_agent_id: Mapped[str] = mapped_column(ID, nullable=False)
    url: Mapped[str] = mapped_column(NAME, nullable=False)
    transport: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="jsonrpc", server_default="jsonrpc"
    )
    require_tls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    max_concurrent_calls: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4, server_default="4"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "a2a_agent_id"],
            ["a2a_agents.organization_id", "a2a_agents.id"],
            name="fk_a2a_endpoints_a2a_agent_id_a2a_agents",
        ),
        Index(
            "uq_a2a_endpoints_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        UniqueConstraint("a2a_agent_id", "url", name="uq_a2a_endpoints_agent_url"),
    )


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    suite_name: Mapped[str] = mapped_column(SHORT, nullable=False)
    name: Mapped[str] = mapped_column(NAME, nullable=False)
    description: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    # The fixed inputs a runtime adapter must handle identically.
    task_spec: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    agent_spec: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    expected_behavior: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    rubric: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        Index(
            "uq_evaluation_cases_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_eval_cases_suite",
            "suite_name",
            "is_active",
        ),
    )


class EvaluationRun(Base):
    """A benchmark result: one case, one runtime adapter, one score."""

    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str | None] = mapped_column(ID, ForeignKey("organizations.id"))
    case_id: Mapped[str] = mapped_column(ID, nullable=False)
    runtime_adapter: Mapped[str] = mapped_column(SHORT, nullable=False)
    model_profile: Mapped[str] = mapped_column(
        SHORT, nullable=False, default="default", server_default="default"
    )
    model_used: Mapped[str | None] = mapped_column(NAME)
    execution_id: Mapped[str | None] = mapped_column(ID)
    observed_behavior: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    success: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    score: Mapped[float | None] = mapped_column()
    failure_category: Mapped[str | None] = mapped_column(SHORT)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[float] = mapped_column(MONEY, nullable=False, default=0, server_default="0")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    notes: Mapped[str] = mapped_column(LONG, default="", server_default="''")
    created_at: Mapped[dt.datetime] = created_at_col()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "case_id"],
            ["evaluation_cases.organization_id", "evaluation_cases.id"],
            name="fk_evaluation_runs_case_id_evaluation_cases",
        ),
        ForeignKeyConstraint(
            ["organization_id", "execution_id"],
            ["executions.organization_id", "executions.id"],
            name="fk_evaluation_runs_execution_id_executions",
        ),
        Index(
            "uq_evaluation_runs_org_id",
            "organization_id",
            "id",
            unique=True,
        ),
        Index(
            "ix_eval_runs_adapter",
            "runtime_adapter",
            "success",
        ),
        Index(
            "ix_eval_runs_case",
            "case_id",
            "runtime_adapter",
        ),
    )


__all__ = [name for name in dir() if name[0].isupper()]


class WorkflowCommand(Base):
    """Coalesced command generation; the database owns the acknowledgement."""

    __tablename__ = "workflow_commands"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "root_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_workflow_commands_organization_id_tasks",
        ),
        Index(
            "ix_workflow_commands_organization_id_state",
            "organization_id",
            "state",
        ),
        UniqueConstraint("organization_id", "root_task_id", name="uq_workflow_commands_org_root"),
        CheckConstraint(
            "(((requested_seq >= settled_seq) AND (settled_seq >= 0)))",
            name="sequence_valid",
        ),
        CheckConstraint(
            "(((kind)::text = ANY ((ARRAY['business_workflow'::character varying, "
            "'agent_workflow'::character varying])::text[])))",
            name="kind_known",
        ),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, ForeignKey("organizations.id"), nullable=False)
    root_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    settled_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    state: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'pending'"))
    feedback: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()


class ConnectorAction(Base):
    """Prepared input and observed outcome of one bounded external write."""

    __tablename__ = "connector_actions"

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "root_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_connector_actions_organization_id_tasks",
        ),
        ForeignKeyConstraint(
            ["organization_id", "stage_task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_connector_actions_stage_task",
        ),
        UniqueConstraint("organization_id", "action_key", name="uq_connector_actions_org_key"),
        CheckConstraint(
            "(((state)::text = ANY ((ARRAY['prepared'::character varying, "
            "'sending'::character varying, 'confirmed'::character varying, "
            "'unknown'::character varying])::text[])))",
            name="state_known",
        ),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    organization_id: Mapped[str] = mapped_column(ID, ForeignKey("organizations.id"), nullable=False)
    root_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    stage_task_id: Mapped[str] = mapped_column(ID, nullable=False)
    action_key: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, server_default=text("'prepared'")
    )
    receipt: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = created_at_col()
    updated_at: Mapped[dt.datetime] = updated_at_col()
