"""The supply chain cannot reference a row in another tenant.

Migration `0020`. The first tranche of the 135 single-column foreign keys measured in
`PRODUCT_GAP.md` §8a.

## What is actually wrong, and what is not

**Not a data leak.** RLS is `FORCE`d on all 99 tenant-scoped tables and the application
role is not `BYPASSRLS`, so a cross-tenant *read* returns nothing. Every tenant-isolation
test proves it and they all pass.

**A referential-integrity hole on write.** `po_items.purchase_order_id` points at
`purchase_orders.id`, and `id` is unique on its own — so a row belonging to tenant A can
name a parent belonging to tenant B, and the insert **succeeds**. The write is the only
place it shows, which is why no read-path test ever found it.

The fix is a composite foreign key: `(organization_id, purchase_order_id)` referencing
`(organization_id, id)`. Postgres then has to check both columns together, and a parent
from another tenant cannot match.

## The parent index comes first, and that is not a detail

A composite foreign key needs the referenced **pair** to be unique. `purchase_orders.id`
being unique on its own is not the same statement, so `UNIQUE (organization_id, id)` has
to exist on all ten parents before the first key can be added. Ordering is therefore
forced: indexes, then keys, and `downgrade` is the exact reverse.

## The constraint names do not change

A composite key keeps its bare name -- `fk_po_items_purchase_order_id_purchase_orders` --
and gains a leading `organization_id` column. That is deliberate:

* the convention `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` is applied
  to `column_0_name`, which on a composite key is `organization_id` for all twenty of
  them, so following the convention would collapse every name to
  `fk_po_items_organization_id_purchase_orders` and lose which relationship it is;
* renaming eighteen constraints churns eighteen references, and a name change is a
  *silent* break for anything that quotes the name in an error message or a test.

So each one is created with an explicit name and the naming convention is bypassed the way
`0016` bypassed it for the same reason (F109).

## The eighteen, by table

| table | foreign key | parent |
|---|---|---|
| `rfqs` | `contract_id` | `contracts` |
| `rfq_items` | `material_id` | `materials` |
| `rfq_items` | `rfq_id` | `rfqs` |
| `quotations` | `supplier_id` | `suppliers` |
| `quotations` | `rfq_id` | `rfqs` |
| `quotation_items` | `material_id` | `materials` |
| `quotation_items` | `rfq_item_id` | `rfq_items` |
| `quotation_items` | `quotation_id` | `quotations` |
| `purchase_orders` | `quotation_id` | `quotations` |
| `purchase_orders` | `supplier_id` | `suppliers` |
| `po_items` | `material_id` | `materials` |
| `po_items` | `quotation_item_id` | `quotation_items` |
| `po_items` | `purchase_order_id` | `purchase_orders` |
| `goods_receipts` | `po_id` | `purchase_orders` |
| `receipt_items` | `material_id` | `materials` |
| `receipt_items` | `po_item_id` | `po_items` |
| `receipt_items` | `goods_receipt_id` | `goods_receipts` |
| `receipt_checks` | `goods_receipt_id` | `goods_receipts` |

`rfqs.project_id` and `purchase_orders.project_id` are **not** in the list: `projects`
already carries `UNIQUE (organization_id, id)` from `0017`, so those two are already
composite-safe. Measured, not assumed -- `scripts/gen_composite_fks.py` skips a parent
that already has the pair and says so.

## This is a tranche, not the whole thing

Eighteen of a hundred and thirty-five. The rest is the substrate -- tasks, agents,
approvals, audit, events -- and it goes in its own migrations for the same reason this
one is nine tables and not a hundred: a migration nobody can read in one sitting is a
migration nobody can review.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-01 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The ten parents that need `UNIQUE (organization_id, id)` before any composite key
#: can reference them. A composite foreign key needs the referenced *pair* to be unique,
#: and `id` being unique on its own is not that statement.
PARENTS: tuple[str, ...] = (
    "contracts",
    "goods_receipts",
    "materials",
    "po_items",
    "purchase_orders",
    "quotation_items",
    "quotations",
    "rfq_items",
    "rfqs",
    "suppliers",
)

#: `(child table, constraint name, foreign column, parent table)`. The name is reused
#: verbatim so nothing that quotes it breaks -- see the module docstring.
FOREIGN_KEYS: tuple[tuple[str, str, str, str], ...] = (
    ("rfqs", "fk_rfqs_contract_id_contracts", "contract_id", "contracts"),
    ("rfq_items", "fk_rfq_items_material_id_materials", "material_id", "materials"),
    ("rfq_items", "fk_rfq_items_rfq_id_rfqs", "rfq_id", "rfqs"),
    ("quotations", "fk_quotations_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("quotations", "fk_quotations_rfq_id_rfqs", "rfq_id", "rfqs"),
    ("quotation_items", "fk_quotation_items_material_id_materials", "material_id", "materials"),
    ("quotation_items", "fk_quotation_items_rfq_item_id_rfq_items", "rfq_item_id", "rfq_items"),
    ("quotation_items", "fk_quotation_items_quotation_id_quotations", "quotation_id", "quotations"),
    ("purchase_orders", "fk_purchase_orders_quotation_id_quotations", "quotation_id", "quotations"),
    ("purchase_orders", "fk_purchase_orders_supplier_id_suppliers", "supplier_id", "suppliers"),
    ("po_items", "fk_po_items_material_id_materials", "material_id", "materials"),
    ("po_items", "fk_po_items_quotation_item_id_quotation_items", "quotation_item_id", "quotation_items"),
    ("po_items", "fk_po_items_purchase_order_id_purchase_orders", "purchase_order_id", "purchase_orders"),
    ("goods_receipts", "fk_goods_receipts_po_id_purchase_orders", "po_id", "purchase_orders"),
    ("receipt_items", "fk_receipt_items_material_id_materials", "material_id", "materials"),
    ("receipt_items", "fk_receipt_items_po_item_id_po_items", "po_item_id", "po_items"),
    ("receipt_items", "fk_receipt_items_goods_receipt_id_goods_receipts", "goods_receipt_id", "goods_receipts"),
    ("receipt_checks", "fk_receipt_checks_goods_receipt_id_goods_receipts", "goods_receipt_id", "goods_receipts"),
)


def upgrade() -> None:
    for parent in PARENTS:
        op.create_index(f"uq_{parent}_org_id", parent, ["organization_id", "id"], unique=True)
    for child, name, column, parent in FOREIGN_KEYS:
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(
            name,
            child,
            parent,
            ["organization_id", column],
            ["organization_id", "id"],
        )


def downgrade() -> None:
    """The exact reverse, and the order is forced: keys off before the indexes they
    need, because dropping an index a live constraint depends on is refused by Postgres
    and the message does not say which constraint."""
    for child, name, column, _parent in reversed(FOREIGN_KEYS):
        op.drop_constraint(name, child, type_="foreignkey")
        op.create_foreign_key(name, child, _parent, [column], ["id"])
    for parent in reversed(PARENTS):
        op.drop_index(f"uq_{parent}_org_id", table_name=parent)
