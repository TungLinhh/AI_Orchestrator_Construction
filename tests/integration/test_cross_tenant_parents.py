"""A row may not name a parent in another tenant, proven against a live database.

Migration `0020` made eighteen supply-chain foreign keys composite:
`(organization_id, purchase_order_id)` referencing `(organization_id, id)`. This file is
the proof that the change did anything.

## Why this needed its own file

Tenant isolation was already tested, thoroughly, and all of it passed **before** `0020`
existed. That is the point worth stating plainly:

* `organizations` has no RLS; it is the tenant root.
* Every other tenant-scoped table has RLS `FORCE`d.

So a cross-tenant **read** returns nothing, and every isolation test proved it. What RLS
does not do is check a *foreign key*. `po_items.purchase_order_id` pointed at
`purchase_orders.id`, and `id` is unique on its own, so a row belonging to tenant A could
name a parent belonging to tenant B and the insert **succeeded**. The write was the only
place the hole showed, which is why no read-path test ever found it.

## Why the second tenant is a real session and not an admin insert

The first draft created the other organization's rows through the owner role. That is
wrong twice over. It would not test the thing — an owner insert bypasses the very
mechanism under test — and it could not even run: with `0020` in place, building *our*
order against *their* project is itself refused, so the fixture would have failed in setup
rather than at the assertion, naming the wrong constraint.

So `other_tenant` builds a second organization and a second session bound to it, exactly
the way the `tenant` fixture does. Both sides of every test are then written by a session
inside its own tenant, and the only thing that crosses is the id in the foreign key —
which is the thing under test.

## What each test refuses, and why that specific relationship

The eighteen are not interchangeable, so neither are the tests. Each one is a *negative*
assertion — a test that says what must **not** happen, which is worth more than one that
says what must, because a positive test passes just as happily when the constraint is
absent:

* `po_items` → `purchase_orders` — a line priced against someone else's order.
* `rfqs` → `contracts` — a requisition under a contract that is not ours.
* `quotations` → `suppliers` — a quote from a supplier that never quoted.
* `receipt_items` → `goods_receipts` — goods against a delivery note that is not ours.

`TestTheSameShapeIsStillAllowedWithinOneTenant` is the other half. A constraint that
refuses everything is not isolation, it is a broken schema, and only a positive test tells
the two apart.

## How a refusal is recognised

On the **constraint name**, not on the wording of the message. Asserting on message text
would fail the next time Postgres rewords it, and pass if the wording changed while the
behaviour did not. The constraint name is the one stable thing here: it is what the
migration declared, and a refusal by some *other* constraint on the same table is a green
test of nothing.

## The spine these helpers have to build

`rfq → rfq_items → quotations → quotation_items → purchase_orders → po_items`, and every
link is `NOT NULL`. An order cannot be created without the two documents behind it, and a
`po_items` row cannot name a quotation line that does not exist. That is the design this
repository chose — "the documents are referentially connected" — and it is why there are
this many helpers and not one.

Worth recording because it cost three rounds of a test failing in setup rather than at its
assertion:

* an order without a quotation was refused by a **not-null** check;
* a requisition line for `cay` was refused by the **composite** key to
  `units_dictionary (organization_id, code)` — a table with no `id` at all, so its units
  are per tenant;
* a contract with no counterparty was refused by `ck_contracts_exactly_one_counterparty`.

Every one of those refusals is correct, and not one of them is about tenancy. A test that
fails in its fixture has not started.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_orchestrator.domain.ids import new_ulid
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

UNIT = "cay"


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


# --- the spine -------------------------------------------------------------------
#
# One function per link, each taking the tenant and returning the id it created, so a
# test that only needs a purchase order reads as one line and a test that needs to be
# specific about *which* parent crosses can build only the part it cares about.


async def _unit(t: Tenant) -> None:
    await t.session.execute(
        text(
            "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
            "VALUES (:o, :c, 'Cái', 'count') ON CONFLICT DO NOTHING"
        ),
        {"o": t.organization_id, "c": UNIT},
    )
    await t.commit()


async def _project(t: Tenant) -> str:
    pid = f"prj_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO projects (id, organization_id, code, name, status) "
            "VALUES (:i, :o, :c, 'Dự án', 'active')"
        ),
        {"i": pid, "o": t.organization_id, "c": _code("P")},
    )
    await t.commit()
    return pid


async def _supplier(t: Tenant) -> str:
    """An approved supplier.

    `qualification_expires_on` is not optional: `ck_suppliers_an_approved_supplier_is_qualified`
    refuses an `approved` supplier without a live qualification, and that check fires
    before any foreign key does — so a missing date here makes every test in this file
    pass for the wrong reason.
    """
    sid = f"sup_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, status, "
            "qualification_expires_on) "
            "VALUES (:i, :o, :c, 'CÔNG TY CỔ PHẦN EUSTEEL', 'approved', CURRENT_DATE + 365)"
        ),
        {"i": sid, "o": t.organization_id, "c": _code("S")},
    )
    await t.commit()
    return sid


async def _material(t: Tenant) -> str:
    mid = f"mat_{new_ulid()}"
    await t.session.execute(
        text("INSERT INTO materials (id, organization_id, name_vi) VALUES (:i, :o, 'Ống')"),
        {"i": mid, "o": t.organization_id},
    )
    await t.commit()
    return mid


async def _contract(t: Tenant, project_id: str) -> str:
    """A contract.

    `ck_contracts_exactly_one_counterparty` requires precisely one of `supplier_id` or
    `client_id`. A good check — a contract naming nobody is not a contract — and also the
    kind of thing that turns a test about foreign keys into a test about check constraints
    if the fixture is wrong.
    """
    cid = f"ctr_{new_ulid()}"
    supplier = await _supplier(t)
    await t.session.execute(
        text(
            "INSERT INTO contracts (id, organization_id, code, project_id, supplier_id) "
            "VALUES (:i, :o, :c, :p, :s)"
        ),
        {"i": cid, "o": t.organization_id, "c": _code("HD"), "p": project_id, "s": supplier},
    )
    await t.commit()
    return cid


async def _rfq(t: Tenant, project_id: str, material_id: str) -> tuple[str, str]:
    """A requisition and its line. Returns `(rfq id, rfq_item id)`."""
    rfq_id = f"rfq_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, code, project_id, system_code, "
            "zone_ref, package_ref, status, requested_by, is_long_lead) "
            "VALUES (:i, :o, :c, :p, 'AC', 'TẦNG 1', 'HVAC', 'issued', "
            "'procurement_lead', false)"
        ),
        {"i": rfq_id, "o": t.organization_id, "c": _code("RFQ"), "p": project_id},
    )
    line_id = f"rfi_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, unit_code, "
            "quantity) VALUES (:i, :o, :r, :m, :u, 10)"
        ),
        {"i": line_id, "o": t.organization_id, "r": rfq_id, "m": material_id, "u": UNIT},
    )
    await t.commit()
    return rfq_id, line_id


async def _quotation(
    t: Tenant, rfq_id: str, rfq_item_id: str, material_id: str, supplier_id: str
) -> tuple[str, str]:
    """A quotation and its line. Returns `(quotation id, quotation_item id)`."""
    quo_id = f"quo_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO quotations (id, organization_id, rfq_id, supplier_id, code, "
            "status) VALUES (:i, :o, :r, :s, :c, 'received')"
        ),
        {"i": quo_id, "o": t.organization_id, "r": rfq_id, "s": supplier_id, "c": _code("Q")},
    )
    item_id = f"qoi_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO quotation_items (id, organization_id, quotation_id, rfq_item_id, "
            "material_id, unit_code, quantity) VALUES (:i, :o, :q, :r, :m, :u, 10)"
        ),
        {
            "i": item_id,
            "o": t.organization_id,
            "q": quo_id,
            "r": rfq_item_id,
            "m": material_id,
            "u": UNIT,
        },
    )
    await t.commit()
    return quo_id, item_id


async def _order(t: Tenant) -> tuple[str, str]:
    """A complete order. Returns `(purchase_order id, quotation_item id)`."""
    await _unit(t)
    project = await _project(t)
    supplier = await _supplier(t)
    material = await _material(t)
    rfq_id, rfq_item_id = await _rfq(t, project, material)
    quotation_id, quotation_item_id = await _quotation(t, rfq_id, rfq_item_id, material, supplier)
    po_id = f"po_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO purchase_orders (id, organization_id, code, project_id, "
            "supplier_id, quotation_id, status, approved_by) "
            "VALUES (:i, :o, :c, :p, :s, :q, 'issued', 'procurement_manager')"
        ),
        {
            "i": po_id,
            "o": t.organization_id,
            "c": _code("PO"),
            "p": project,
            "s": supplier,
            "q": quotation_id,
        },
    )
    await t.commit()
    return po_id, quotation_item_id


class TestCrossTenantParentsAreRefused:
    async def test_po_item_cannot_name_another_tenants_order(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """A receipt line priced against someone else's order is the sharpest case.

        `po_items` carries the unit price. Naming another tenant's order attaches *their*
        price to *our* material, and the three-way match then agrees with itself — which is
        why this is the one worth asserting the constraint name on.
        """
        foreign_order, _ = await _order(other_tenant)
        _ours, quotation_item = await _order(tenant)
        material = await _material(tenant)

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO po_items (id, organization_id, purchase_order_id, "
                    "quotation_item_id, material_id, unit_code, ordered_quantity, "
                    "unit_rate, amount) "
                    "VALUES (:i, :o, :po, :q, :m, :u, 10, 999999, 9999990)"
                ),
                {
                    "i": f"poi_{new_ulid()}",
                    "o": tenant.organization_id,
                    "po": foreign_order,
                    "q": quotation_item,
                    "m": material,
                    "u": UNIT,
                },
            )
        await tenant.session.rollback()
        assert "fk_po_items_purchase_order_id_purchase_orders" in str(refusal.value), (
            "The refusal named a different constraint, so this test is no longer testing "
            "the one it claims to test."
        )

    async def test_rfq_cannot_name_another_tenants_contract(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        foreign_contract = await _contract(other_tenant, await _project(other_tenant))
        our_project = await _project(tenant)

        # `contract_id` is nullable, so a row with *no* contract is not a violation. The
        # only thing that can refuse this statement is the composite key.
        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO rfqs (id, organization_id, code, title, project_id, "
                    "contract_id, system_code, zone_ref, package_ref, status, "
                    "requested_by, is_long_lead) "
                    "VALUES (:i, :o, :c, 'Yêu cầu báo giá', :p, :k, 'AC', 'TẦNG 1', "
                    "'HVAC', 'issued', 'procurement_lead', false)"
                ),
                {
                    "i": f"rfq_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": _code("RFQ"),
                    "p": our_project,
                    "k": foreign_contract,
                },
            )
        await tenant.session.rollback()
        assert "fk_rfqs_contract_id_contracts" in str(refusal.value)

    async def test_quotation_cannot_name_another_tenants_supplier(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        await _unit(tenant)
        foreign_supplier = await _supplier(other_tenant)
        our_rfq, _line = await _rfq(tenant, await _project(tenant), await _material(tenant))

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO quotations (id, organization_id, rfq_id, supplier_id, "
                    "code, status) VALUES (:i, :o, :r, :s, :c, 'received')"
                ),
                {
                    "i": f"quo_{new_ulid()}",
                    "o": tenant.organization_id,
                    "r": our_rfq,
                    "s": foreign_supplier,
                    "c": _code("Q"),
                },
            )
        await tenant.session.rollback()
        assert "fk_quotations_supplier_id_suppliers" in str(refusal.value)

    async def test_receipt_item_cannot_name_another_tenants_receipt(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        # The material, the order and the order line are all ours. Only the delivery note
        # is not, so the refusal can be about the one key and nothing else.
        order, quotation_item = await _order(tenant)
        material = await _material(tenant)
        line_id = f"poi_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO po_items (id, organization_id, purchase_order_id, "
                "quotation_item_id, material_id, unit_code, ordered_quantity, "
                "unit_rate, amount) VALUES (:i, :o, :po, :q, :m, :u, 10, 100, 1000)"
            ),
            {
                "i": line_id,
                "o": tenant.organization_id,
                "po": order,
                "q": quotation_item,
                "m": material,
                "u": UNIT,
            },
        )
        await tenant.commit()

        # Prove the two tenants are genuinely different rows, so a later reader does not
        # have to take it on trust: a fixture that silently returned the *same* id twice
        # would make every test below pass for the wrong reason.
        foreign = f"grn_{new_ulid()}"
        await other_tenant.session.execute(
            text(
                "INSERT INTO goods_receipts (id, organization_id, code, po_id) "
                "VALUES (:i, :o, :c, :p)"
            ),
            {
                "i": foreign,
                "o": other_tenant.organization_id,
                "c": _code("GRN"),
                "p": (await _order(other_tenant))[0],
            },
        )
        await other_tenant.commit()
        assert other_tenant.organization_id != tenant.organization_id

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, "
                    "po_item_id, material_id, unit_code, received_quantity) "
                    "VALUES (:i, :o, :g, :p, :m, :u, 10)"
                ),
                {
                    "i": f"rci_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": foreign,
                    "p": line_id,
                    "m": material,
                    "u": UNIT,
                },
            )
        await tenant.session.rollback()
        assert "fk_receipt_items_goods_receipt_id_goods_receipts" in str(refusal.value)


class TestTheSameShapeIsStillAllowedWithinOneTenant:
    async def test_a_po_item_may_name_our_own_order(self, tenant: Tenant) -> None:
        """A constraint that refuses everything is not isolation, it is a broken schema.

        Every test above is negative. Without this one, a migration that dropped the
        foreign key outright — or pointed it at a column that never matches — would pass
        all four.
        """
        order, quotation_item = await _order(tenant)
        material = await _material(tenant)
        line_id = f"poi_{new_ulid()}"

        await tenant.session.execute(
            text(
                "INSERT INTO po_items (id, organization_id, purchase_order_id, "
                "quotation_item_id, material_id, unit_code, ordered_quantity, "
                "unit_rate, amount) VALUES (:i, :o, :po, :q, :m, :u, 10, 100, 1000)"
            ),
            {
                "i": line_id,
                "o": tenant.organization_id,
                "po": order,
                "q": quotation_item,
                "m": material,
                "u": UNIT,
            },
        )
        await tenant.commit()

        row = (
            await tenant.session.execute(
                text("SELECT purchase_order_id FROM po_items WHERE id = :i"), {"i": line_id}
            )
        ).first()
        assert row is not None, "The insert reported success and the row is not there."
        assert str(row[0]) == order
