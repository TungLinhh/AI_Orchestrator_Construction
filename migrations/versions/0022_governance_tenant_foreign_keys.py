"""A row in the governance tranche may not name a parent in another tenant.

Revision ID: 0022
Revises: 0021

## What this is

Migration `0020` did this for the supply chain. The remaining
14 single-column foreign keys get the same treatment: `(organization_id, <column>)`
referencing `(organization_id, id)`.

RLS already prevents a cross-tenant **read** -- it is `FORCE`d on every tenant-scoped
table and the application role is not `BYPASSRLS`. What it does not do is check a foreign
key, so a write could name a parent in another tenant and succeed. The write is the only
place the hole showed, which is why every read-path test passed before this existed.

## The parent index comes first, and that is not a detail

A composite foreign key needs the referenced **pair** to be unique. `approvals.id`
being unique on its own is not that statement, so `UNIQUE (organization_id, id)` has to
exist on all 9 parents before the first key can be added. The order is
therefore forced: indexes, then keys, and `downgrade` is the exact reverse.

## The constraint names do not change

A composite key keeps its bare name and gains a leading `organization_id` column. The
convention `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` reads
`column_0_name`, which on a composite key is `organization_id` for all 14 of them,
so following it would collapse every name to `fk_<table>_organization_id_<parent>` and
lose which relationship each one is. Each is therefore created with an explicit name, the
way `0016` and `0020` bypassed the convention for the same reason.

## The 14 keys

| child | column | parent |
|---|---|---|
| `audit_logs` | `approval_id` | `approvals` |
| `budget_ledger` | `budget_id` | `budgets` |
| `budget_ledger` | `reverses_entry_id` | `budget_ledger` |
| `client_contacts` | `client_id` | `clients` |
| `contracts` | `client_id` | `clients` |
| `contracts` | `document_id` | `documents` |
| `evaluation_runs` | `case_id` | `evaluation_cases` |
| `gate_decisions` | `minutes_document_id` | `documents` |
| `opportunities` | `client_id` | `clients` |
| `projects` | `client_id` | `clients` |
| `sop_versions` | `document_id` | `documents` |
| `supplier_documents` | `document_id` | `documents` |
| `tender_documents` | `document_id` | `documents` |
| `tenders` | `client_id` | `clients` |

## Generated, not typed

`scripts/gen_tenant_fk_migrations.py` reads these from `pg_constraint` and prints the
count, because a hand-typed list of 14 names has 14 chances to transpose
a column -- and a transposed constraint does not fail loudly, it enforces the wrong thing
(F133).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The parents that need `UNIQUE (organization_id, id)` before any composite key can
#: reference them.
PARENTS: tuple[str, ...] = (
    "budget_ledger",
    "budgets",
    "client_contacts",
    "clients",
    "connectors",
    "credentials_metadata",
    "documents",
    "evaluation_cases",
    "evaluation_runs",
)

#: `(child table, constraint name, foreign column, parent table)`. The name is reused
#: verbatim so nothing that quotes it breaks.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("audit_logs", "fk_audit_logs_approval_id_approvals", "approval_id", "approvals"),
    ("budget_ledger", "fk_budget_ledger_budget_id_budgets", "budget_id", "budgets"),
    ("budget_ledger", "fk_budget_ledger_reverses_entry_id_budget_ledger", "reverses_entry_id", "budget_ledger"),
    ("client_contacts", "fk_client_contacts_client_id_clients", "client_id", "clients"),
    ("contracts", "fk_contracts_client_id_clients", "client_id", "clients"),
    ("contracts", "fk_contracts_document_id_documents", "document_id", "documents"),
    ("evaluation_runs", "fk_evaluation_runs_case_id_evaluation_cases", "case_id", "evaluation_cases"),
    ("gate_decisions", "fk_gate_decisions_minutes_document_id_documents", "minutes_document_id", "documents"),
    ("opportunities", "fk_opportunities_client_id_clients", "client_id", "clients"),
    ("projects", "fk_projects_client_id_clients", "client_id", "clients"),
    ("sop_versions", "fk_sop_versions_document_id_documents", "document_id", "documents"),
    ("supplier_documents", "fk_supplier_documents_document_id_documents", "document_id", "documents"),
    ("tender_documents", "fk_tender_documents_document_id_documents", "document_id", "documents"),
    ("tenders", "fk_tenders_client_id_clients", "client_id", "clients"),
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
