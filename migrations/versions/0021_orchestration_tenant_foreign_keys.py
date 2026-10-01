"""A row in the orchestration tranche may not name a parent in another tenant.

Revision ID: 0021
Revises: 0020

## What this is

Migration `0020` did this for the supply chain. The remaining
61 single-column foreign keys get the same treatment: `(organization_id, <column>)`
referencing `(organization_id, id)`.

RLS already prevents a cross-tenant **read** -- it is `FORCE`d on every tenant-scoped
table and the application role is not `BYPASSRLS`. What it does not do is check a foreign
key, so a write could name a parent in another tenant and succeed. The write is the only
place the hole showed, which is why every read-path test passed before this existed.

## The parent index comes first, and that is not a detail

A composite foreign key needs the referenced **pair** to be unique. `agents.id`
being unique on its own is not that statement, so `UNIQUE (organization_id, id)` has to
exist on all 29 parents before the first key can be added. The order is
therefore forced: indexes, then keys, and `downgrade` is the exact reverse.

## The constraint names do not change

A composite key keeps its bare name and gains a leading `organization_id` column. The
convention `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` reads
`column_0_name`, which on a composite key is `organization_id` for all 61 of them,
so following it would collapse every name to `fk_<table>_organization_id_<parent>` and
lose which relationship each one is. Each is therefore created with an explicit name, the
way `0016` and `0020` bypassed the convention for the same reason.

## The 61 keys

| child | column | parent |
|---|---|---|
| `a2a_agents` | `local_agent_id` | `agents` |
| `a2a_endpoints` | `a2a_agent_id` | `a2a_agents` |
| `agent_definitions` | `role_id` | `roles` |
| `agent_relationships` | `source_agent_id` | `agents` |
| `agent_relationships` | `target_agent_id` | `agents` |
| `agent_skill_bindings` | `agent_id` | `agents` |
| `agent_skill_bindings` | `skill_id` | `skills` |
| `agent_skill_bindings` | `skill_version_id` | `skill_versions` |
| `agent_tool_bindings` | `agent_id` | `agents` |
| `agent_tool_bindings` | `tool_id` | `tools` |
| `agents` | `definition_id` | `agent_definitions` |
| `agents` | `org_unit_id` | `organizational_units` |
| `agents` | `parent_agent_id` | `agents` |
| `agents` | `role_id` | `roles` |
| `approvals` | `assigned_approver_id` | `users` |
| `approvals` | `decided_by` | `users` |
| `approvals` | `execution_id` | `executions` |
| `approvals` | `task_id` | `tasks` |
| `audit_logs` | `execution_id` | `executions` |
| `audit_logs` | `task_id` | `tasks` |
| `blackboard_entries` | `task_id` | `tasks` |
| `blackboard_entries` | `written_by_agent_id` | `agents` |
| `blackboard_entries` | `written_by_execution_id` | `executions` |
| `budget_ledger` | `execution_id` | `executions` |
| `budget_ledger` | `task_id` | `tasks` |
| `delegations` | `child_task_id` | `tasks` |
| `delegations` | `parent_task_id` | `tasks` |
| `delegations` | `source_agent_id` | `agents` |
| `delegations` | `target_agent_id` | `agents` |
| `documents` | `org_unit_id` | `organizational_units` |
| `evaluation_runs` | `execution_id` | `executions` |
| `executions` | `agent_id` | `agents` |
| `executions` | `delegation_id` | `delegations` |
| `executions` | `task_id` | `tasks` |
| `memory_chunks` | `memory_item_id` | `memory_items` |
| `memory_items` | `agent_id` | `agents` |
| `memory_items` | `org_unit_id` | `organizational_units` |
| `memory_items` | `task_id` | `tasks` |
| `messages` | `task_id` | `tasks` |
| `model_profiles` | `fallback_profile_id` | `model_profiles` |
| `model_usage` | `agent_id` | `agents` |
| `model_usage` | `execution_id` | `executions` |
| `model_usage` | `task_id` | `tasks` |
| `organizational_units` | `parent_id` | `organizational_units` |
| `policy_versions` | `policy_id` | `policies` |
| `roles` | `parent_role_id` | `roles` |
| `skill_versions` | `skill_id` | `skills` |
| `subagent_runs` | `child_task_id` | `tasks` |
| `subagent_runs` | `parent_agent_id` | `agents` |
| `subagent_runs` | `parent_task_id` | `tasks` |
| `task_dependencies` | `depends_on_task_id` | `tasks` |
| `task_dependencies` | `task_id` | `tasks` |
| `tasks` | `org_unit_id` | `organizational_units` |
| `tasks` | `owner_agent_id` | `agents` |
| `tasks` | `parent_task_id` | `tasks` |
| `tasks` | `requester_id` | `users` |
| `tasks` | `root_task_id` | `tasks` |
| `tool_policies` | `tool_id` | `tools` |
| `tool_versions` | `tool_id` | `tools` |
| `tools` | `mcp_server_id` | `mcp_servers` |
| `workflow_runs` | `task_id` | `tasks` |

## Generated, not typed

`scripts/gen_tenant_fk_migrations.py` reads these from `pg_constraint` and prints the
count, because a hand-typed list of 61 names has 61 chances to transpose
a column -- and a transposed constraint does not fail loudly, it enforces the wrong thing
(F133).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The parents that need `UNIQUE (organization_id, id)` before any composite key can
#: reference them.
PARENTS: tuple[str, ...] = (
    "a2a_agents",
    "a2a_endpoints",
    "agent_definitions",
    "agent_relationships",
    "agent_skill_bindings",
    "agent_tool_bindings",
    "delegations",
    "events",
    "executions",
    "idempotency_records",
    "mcp_servers",
    "memory_chunks",
    "memory_items",
    "messages",
    "model_profiles",
    "organizational_units",
    "outbox_events",
    "policies",
    "policy_versions",
    "roles",
    "skill_versions",
    "skills",
    "subagent_runs",
    "task_dependencies",
    "tool_policies",
    "tool_versions",
    "tools",
    "users",
    "workflow_runs",
)

#: `(child table, constraint name, foreign column, parent table)`. The name is reused
#: verbatim so nothing that quotes it breaks.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("a2a_agents", "fk_a2a_agents_local_agent_id_agents", "local_agent_id", "agents"),
    ("a2a_endpoints", "fk_a2a_endpoints_a2a_agent_id_a2a_agents", "a2a_agent_id", "a2a_agents"),
    ("agent_definitions", "fk_agent_definitions_role_id_roles", "role_id", "roles"),
    ("agent_relationships", "fk_agent_relationships_source_agent_id_agents", "source_agent_id", "agents"),
    ("agent_relationships", "fk_agent_relationships_target_agent_id_agents", "target_agent_id", "agents"),
    ("agent_skill_bindings", "fk_agent_skill_bindings_agent_id_agents", "agent_id", "agents"),
    ("agent_skill_bindings", "fk_agent_skill_bindings_skill_id_skills", "skill_id", "skills"),
    ("agent_skill_bindings", "fk_agent_skill_bindings_skill_version_id_skill_versions", "skill_version_id", "skill_versions"),
    ("agent_tool_bindings", "fk_agent_tool_bindings_agent_id_agents", "agent_id", "agents"),
    ("agent_tool_bindings", "fk_agent_tool_bindings_tool_id_tools", "tool_id", "tools"),
    ("agents", "fk_agents_definition_id_agent_definitions", "definition_id", "agent_definitions"),
    ("agents", "fk_agents_org_unit_id_organizational_units", "org_unit_id", "organizational_units"),
    ("agents", "fk_agents_parent_agent_id_agents", "parent_agent_id", "agents"),
    ("agents", "fk_agents_role_id_roles", "role_id", "roles"),
    ("approvals", "fk_approvals_assigned_approver_id_users", "assigned_approver_id", "users"),
    ("approvals", "fk_approvals_decided_by_users", "decided_by", "users"),
    ("approvals", "fk_approvals_execution_id_executions", "execution_id", "executions"),
    ("approvals", "fk_approvals_task_id_tasks", "task_id", "tasks"),
    ("audit_logs", "fk_audit_logs_execution_id_executions", "execution_id", "executions"),
    ("audit_logs", "fk_audit_logs_task_id_tasks", "task_id", "tasks"),
    ("blackboard_entries", "fk_blackboard_entries_task_id_tasks", "task_id", "tasks"),
    ("blackboard_entries", "fk_blackboard_entries_written_by_agent_id_agents", "written_by_agent_id", "agents"),
    ("blackboard_entries", "fk_blackboard_entries_written_by_execution_id_executions", "written_by_execution_id", "executions"),
    ("budget_ledger", "fk_budget_ledger_execution_id_executions", "execution_id", "executions"),
    ("budget_ledger", "fk_budget_ledger_task_id_tasks", "task_id", "tasks"),
    ("delegations", "fk_delegations_child_task_id_tasks", "child_task_id", "tasks"),
    ("delegations", "fk_delegations_parent_task_id_tasks", "parent_task_id", "tasks"),
    ("delegations", "fk_delegations_source_agent_id_agents", "source_agent_id", "agents"),
    ("delegations", "fk_delegations_target_agent_id_agents", "target_agent_id", "agents"),
    ("documents", "fk_documents_org_unit_id_organizational_units", "org_unit_id", "organizational_units"),
    ("evaluation_runs", "fk_evaluation_runs_execution_id_executions", "execution_id", "executions"),
    ("executions", "fk_executions_agent_id_agents", "agent_id", "agents"),
    ("executions", "fk_executions_delegation_id_delegations", "delegation_id", "delegations"),
    ("executions", "fk_executions_task_id_tasks", "task_id", "tasks"),
    ("memory_chunks", "fk_memory_chunks_memory_item_id_memory_items", "memory_item_id", "memory_items"),
    ("memory_items", "fk_memory_items_agent_id_agents", "agent_id", "agents"),
    ("memory_items", "fk_memory_items_org_unit_id_organizational_units", "org_unit_id", "organizational_units"),
    ("memory_items", "fk_memory_items_task_id_tasks", "task_id", "tasks"),
    ("messages", "fk_messages_task_id_tasks", "task_id", "tasks"),
    ("model_profiles", "fk_model_profiles_fallback_profile_id_model_profiles", "fallback_profile_id", "model_profiles"),
    ("model_usage", "fk_model_usage_agent_id_agents", "agent_id", "agents"),
    ("model_usage", "fk_model_usage_execution_id_executions", "execution_id", "executions"),
    ("model_usage", "fk_model_usage_task_id_tasks", "task_id", "tasks"),
    ("organizational_units", "fk_organizational_units_parent_id_organizational_units", "parent_id", "organizational_units"),
    ("policy_versions", "fk_policy_versions_policy_id_policies", "policy_id", "policies"),
    ("roles", "fk_roles_parent_role_id_roles", "parent_role_id", "roles"),
    ("skill_versions", "fk_skill_versions_skill_id_skills", "skill_id", "skills"),
    ("subagent_runs", "fk_subagent_runs_child_task_id_tasks", "child_task_id", "tasks"),
    ("subagent_runs", "fk_subagent_runs_parent_agent_id_agents", "parent_agent_id", "agents"),
    ("subagent_runs", "fk_subagent_runs_parent_task_id_tasks", "parent_task_id", "tasks"),
    ("task_dependencies", "fk_task_dependencies_depends_on_task_id_tasks", "depends_on_task_id", "tasks"),
    ("task_dependencies", "fk_task_dependencies_task_id_tasks", "task_id", "tasks"),
    ("tasks", "fk_tasks_org_unit_id_organizational_units", "org_unit_id", "organizational_units"),
    ("tasks", "fk_tasks_owner_agent_id_agents", "owner_agent_id", "agents"),
    ("tasks", "fk_tasks_parent_task_id_tasks", "parent_task_id", "tasks"),
    ("tasks", "fk_tasks_requester_id_users", "requester_id", "users"),
    ("tasks", "fk_tasks_root_task_id_tasks", "root_task_id", "tasks"),
    ("tool_policies", "fk_tool_policies_tool_id_tools", "tool_id", "tools"),
    ("tool_versions", "fk_tool_versions_tool_id_tools", "tool_id", "tools"),
    ("tools", "fk_tools_mcp_server_id_mcp_servers", "mcp_server_id", "mcp_servers"),
    ("workflow_runs", "fk_workflow_runs_task_id_tasks", "task_id", "tasks"),
)


def upgrade() -> None:
    for parent in PARENTS:
        op.create_index(f"uq_{parent}_org_id", parent, ["organization_id", "id"], unique=True)
    for child, name, column, parent in FOREIGN_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(
            name, child, parent, ["organization_id", column], ["organization_id", "id"]
        )


def downgrade() -> None:
    """The exact reverse, and the order is forced: keys off before the indexes they need,
    because dropping an index a live constraint depends on is refused by Postgres with a
    message that does not name the constraint."""
    for child, name, column, parent in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(name, child, parent, [column], ["id"])
    for parent in reversed(PARENTS):
        op.drop_index(f"uq_{parent}_org_id", table_name=parent)
