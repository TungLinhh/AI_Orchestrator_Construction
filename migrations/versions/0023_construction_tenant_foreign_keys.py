"""A row in the construction tranche may not name a parent in another tenant.

Revision ID: 0023
Revises: 0022

## What this is

Migration `0020` did this for the supply chain. The remaining
42 single-column foreign keys get the same treatment: `(organization_id, <column>)`
referencing `(organization_id, id)`.

RLS already prevents a cross-tenant **read** -- it is `FORCE`d on every tenant-scoped
table and the application role is not `BYPASSRLS`. What it does not do is check a foreign
key, so a write could name a parent in another tenant and succeed. The write is the only
place the hole showed, which is why every read-path test passed before this existed.

## The parent index comes first, and that is not a detail

A composite foreign key needs the referenced **pair** to be unique. `bids.id`
being unique on its own is not that statement, so `UNIQUE (organization_id, id)` has to
exist on all 20 parents before the first key can be added. The order is
therefore forced: indexes, then keys, and `downgrade` is the exact reverse.

## The constraint names do not change

A composite key keeps its bare name and gains a leading `organization_id` column. The
convention `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` reads
`column_0_name`, which on a composite key is `organization_id` for all 42 of them,
so following it would collapse every name to `fk_<table>_organization_id_<parent>` and
lose which relationship each one is. Each is therefore created with an explicit name, the
way `0016` and `0020` bypassed the convention for the same reason.

## The 42 keys

| child | column | parent |
|---|---|---|
| `bid_items` | `bid_id` | `bids` |
| `bids` | `opportunity_id` | `opportunities` |
| `bids` | `tender_id` | `tenders` |
| `claim_events` | `claim_id` | `contract_claims` |
| `contract_claims` | `contract_id` | `contracts` |
| `contract_claims` | `variation_id` | `contract_variations` |
| `contract_clauses` | `contract_id` | `contracts` |
| `contract_milestones` | `contract_id` | `contracts` |
| `contract_variations` | `contract_id` | `contracts` |
| `contracts` | `project_id` | `projects` |
| `contracts` | `supplier_id` | `suppliers` |
| `gate_conditions` | `gate_decision_id` | `gate_decisions` |
| `gate_criteria` | `gate_definition_id` | `gate_definitions` |
| `gate_criterion_evaluations` | `gate_criterion_id` | `gate_criteria` |
| `gate_criterion_evaluations` | `gate_instance_id` | `gate_instances` |
| `gate_decisions` | `gate_instance_id` | `gate_instances` |
| `gate_instances` | `gate_definition_id` | `gate_definitions` |
| `material_categories` | `parent_id` | `material_categories` |
| `material_reconciliations` | `po_item_id` | `po_items` |
| `materials` | `category_id` | `material_categories` |
| `milestones` | `project_id` | `projects` |
| `milestones` | `wbs_id` | `wbs` |
| `project_phases` | `project_id` | `projects` |
| `project_roles` | `project_id` | `projects` |
| `purchase_orders` | `project_id` | `projects` |
| `rfqs` | `project_id` | `projects` |
| `sop_forms` | `sop_step_id` | `sop_steps` |
| `sop_raci` | `sop_step_id` | `sop_steps` |
| `sop_steps` | `sop_version_id` | `sop_versions` |
| `sop_versions` | `sop_definition_id` | `sop_definitions` |
| `supplier_assessments` | `supplier_id` | `suppliers` |
| `supplier_documents` | `supplier_id` | `suppliers` |
| `supplier_risk_flags` | `supplier_id` | `suppliers` |
| `tender_documents` | `tender_id` | `tenders` |
| `tender_requirements` | `tender_document_id` | `tender_documents` |
| `tender_requirements` | `tender_id` | `tenders` |
| `tenders` | `opportunity_id` | `opportunities` |
| `wbs` | `parent_id` | `wbs` |
| `wbs` | `project_id` | `projects` |
| `wbs_items` | `wbs_id` | `wbs` |
| `zones` | `parent_zone_id` | `zones` |
| `zones` | `project_id` | `projects` |

## Generated, not typed

`scripts/gen_tenant_fk_migrations.py` reads these from `pg_constraint` and prints the
count, because a hand-typed list of 42 names has 42 chances to transpose
a column -- and a transposed constraint does not fail loudly, it enforces the wrong thing
(F133).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The parents that need `UNIQUE (organization_id, id)` before any composite key can
#: reference them.
PARENTS: tuple[str, ...] = (
    "bids",
    "contract_claims",
    "contract_variations",
    "gate_conditions",
    "gate_criteria",
    "gate_criterion_evaluations",
    "gate_decisions",
    "gate_definitions",
    "gate_instances",
    "material_categories",
    "opportunities",
    "sop_definitions",
    "sop_forms",
    "sop_raci",
    "sop_steps",
    "sop_versions",
    "tender_documents",
    "tender_requirements",
    "tenders",
    "zones",
)

#: `(child table, constraint name, foreign column, parent table)`. The name is reused
#: verbatim so nothing that quotes it breaks.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("bid_items", "fk_bid_items_bid_id_bids", "bid_id", "bids"),
    ("bids", "fk_bids_opportunity_id_opportunities", "opportunity_id", "opportunities"),
    ("bids", "fk_bids_tender_id_tenders", "tender_id", "tenders"),
    ("claim_events", "fk_claim_events_claim_id_contract_claims", "claim_id", "contract_claims"),
    ("contract_claims", "fk_contract_claims_contract_id_contracts", "contract_id", "contracts"),
    ("contract_claims", "fk_contract_claims_variation_id_contract_variations", "variation_id", "contract_variations"),
    ("contract_clauses", "fk_contract_clauses_contract_id_contracts", "contract_id", "contracts"),
    ("contract_milestones", "fk_contract_milestones_contract_id_contracts", "contract_id", "contracts"),
    ("contract_variations", "fk_contract_variations_contract_id_contracts", "contract_id", "contracts"),
    ("contracts", "fk_contracts_project_id_projects", "project_id", "projects"),
    ("contracts", "fk_contracts_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("gate_conditions", "fk_gate_conditions_gate_decision_id_gate_decisions", "gate_decision_id", "gate_decisions"),
    ("gate_criteria", "fk_gate_criteria_gate_definition_id_gate_definitions", "gate_definition_id", "gate_definitions"),
    ("gate_criterion_evaluations", "fk_gate_criterion_evaluations_gate_criterion_id_gate_criteria", "gate_criterion_id", "gate_criteria"),
    ("gate_criterion_evaluations", "fk_gate_criterion_evaluations_gate_instance_id_gate_instances", "gate_instance_id", "gate_instances"),
    ("gate_decisions", "fk_gate_decisions_gate_instance_id_gate_instances", "gate_instance_id", "gate_instances"),
    ("gate_instances", "fk_gate_instances_gate_definition_id_gate_definitions", "gate_definition_id", "gate_definitions"),
    ("material_categories", "fk_material_categories_parent_id_material_categories", "parent_id", "material_categories"),
    ("material_reconciliations", "fk_material_reconciliations_po_item_id_po_items", "po_item_id", "po_items"),
    ("materials", "fk_materials_category_id_material_categories", "category_id", "material_categories"),
    ("milestones", "fk_milestones_project_id_projects", "project_id", "projects"),
    ("milestones", "fk_milestones_wbs_id_wbs", "wbs_id", "wbs"),
    ("project_phases", "fk_project_phases_project_id_projects", "project_id", "projects"),
    ("project_roles", "fk_project_roles_project_id_projects", "project_id", "projects"),
    ("purchase_orders", "fk_purchase_orders_project_id_projects", "project_id", "projects"),
    ("rfqs", "fk_rfqs_project_id_projects", "project_id", "projects"),
    ("sop_forms", "fk_sop_forms_sop_step_id_sop_steps", "sop_step_id", "sop_steps"),
    ("sop_raci", "fk_sop_raci_sop_step_id_sop_steps", "sop_step_id", "sop_steps"),
    ("sop_steps", "fk_sop_steps_sop_version_id_sop_versions", "sop_version_id", "sop_versions"),
    ("sop_versions", "fk_sop_versions_sop_definition_id_sop_definitions", "sop_definition_id", "sop_definitions"),
    ("supplier_assessments", "fk_supplier_assessments_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("supplier_documents", "fk_supplier_documents_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("supplier_risk_flags", "fk_supplier_risk_flags_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("tender_documents", "fk_tender_documents_tender_id_tenders", "tender_id", "tenders"),
    ("tender_requirements", "fk_tender_requirements_tender_document_id_tender_documents", "tender_document_id", "tender_documents"),
    ("tender_requirements", "fk_tender_requirements_tender_id_tenders", "tender_id", "tenders"),
    ("tenders", "fk_tenders_opportunity_id_opportunities", "opportunity_id", "opportunities"),
    ("wbs", "fk_wbs_parent_id_wbs", "parent_id", "wbs"),
    ("wbs", "fk_wbs_project_id_projects", "project_id", "projects"),
    ("wbs_items", "fk_wbs_items_wbs_id_wbs", "wbs_id", "wbs"),
    ("zones", "fk_zones_parent_zone_id_zones", "parent_zone_id", "zones"),
    ("zones", "fk_zones_project_id_projects", "project_id", "projects"),
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
