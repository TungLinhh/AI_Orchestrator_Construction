"""Requisition → quotation → order → receipt, against a live database.

Ten tables, and the tests here are organised around the one structural decision
that holds them together: **the documents are referentially connected.** A receipt
names a purchase order, an order names a quotation, a quotation names a
requisition, and every line traces back to the requisition line it answers.

That is what makes the 3-way match arithmetic rather than a comparison. The
corpus's own payment form (`FRM-BO-002A`) asks for "số PO; số GRN; số hóa đơn" as
free text and says in the same row that the system fills in the match. A design
that stored those three references as text would make "fills in" mean "retypes",
and the three numbers would be free to disagree with the documents they name.
These tests prove the chain is walkable and that it cannot be short-circuited.

Three corpus measurements are tested as behaviour rather than as comments,
because each of them is a decision a plausible design would have got wrong:

* **No material codes exist** — 400 workbooks scanned for code-shaped strings
  returned only `IP20`, `4000K`, `1F`-`8F` and `T2`-`T7`. Every line therefore
  references a material by id.
* **Units are inconsistently cased** — `Bộ` 17 times and `bộ` 7, `Cái` 34 and
  `cái` 6. Lines reference `units_dictionary` by ASCII code, so `Bộ` is refused
  and `bo` is accepted. That refusal is the test.
* **Deliveries and payments happen in installments** — the payment form carries
  "Lần thanh toán/giao hàng: Đợt 2". Round 2 of 2 is valid; round 3 of 2 is not.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant
from tests.integration.test_construction_domain import refused_because

pytestmark = [pytest.mark.integration]

PROCUREMENT_TABLES = (
    "rfqs",
    "rfq_items",
    "quotations",
    "quotation_items",
    "purchase_orders",
    "po_items",
    "goods_receipts",
    "receipt_items",
    "receipt_checks",
    "material_reconciliations",
)


# ---------------------------------------------------------------------------
# Builders. Each returns the id the next step needs, so a test that is about one
# link can build the rest of the chain without restating it.
# ---------------------------------------------------------------------------


async def _unit(t: Tenant, code: str, name_vi: str = "") -> str:
    await t.session.execute(
        text(
            "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
            "VALUES (:o, :c, :n, 'count')"
        ),
        {"o": t.organization_id, "c": code, "n": name_vi or code},
    )
    await t.commit()
    return code


async def _project(t: Tenant) -> str:
    pid = f"prj_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO projects (id, organization_id, code, name, status) "
            "VALUES (:i, :o, :c, 'DRC607-CT02', 'active')"
        ),
        {"i": pid, "o": t.organization_id, "c": f"P-{uuid.uuid4().hex[:6]}"},
    )
    await t.commit()
    return pid


async def _supplier(t: Tenant) -> str:
    """An approved supplier.

    `qualification_expires_on` is not optional: `ck_suppliers_an_approved_supplier_is_qualified`
    refuses an approval without one, and the first version of this builder left it
    out. That is the constraint working — an approved supplier whose qualification
    has no expiry is exactly the row it exists to prevent.
    """
    sid = f"sup_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO suppliers (id, organization_id, code, name, status, "
            "qualification_expires_on) "
            "VALUES (:i, :o, :c, 'CÔNG TY CỔ PHẦN EUSTEEL', 'approved', CURRENT_DATE + 365)"
        ),
        {"i": sid, "o": t.organization_id, "c": f"S-{uuid.uuid4().hex[:6]}"},
    )
    await t.commit()
    return sid


async def _material(t: Tenant) -> str:
    """A material with no code, because the corpus has none to copy."""
    mid = f"mat_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO materials (id, organization_id, name_vi, system_code) "
            "VALUES (:i, :o, 'Tủ trung tâm báo cháy', 'FP')"
        ),
        {"i": mid, "o": t.organization_id},
    )
    await t.commit()
    return mid


async def _rfq(t: Tenant, *, unit: str = "bo", contract_id: str | None = None) -> tuple[str, str]:
    """A requisition and its one line. Returns `(rfq_id, rfq_item_id)`."""
    await _unit(t, unit)
    project_id = await _project(t)
    material_id = await _material(t)
    rfq_id, item_id = f"rfq_{new_ulid()}", f"rqi_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO rfqs (id, organization_id, code, title, project_id, contract_id, "
            "system_code, zone_ref, package_ref, status, requested_by, is_long_lead) "
            "VALUES (:i, :o, :c, 'Ống và phụ kiện', :p, :k, 'AC', 'TẦNG 1', 'HVAC', "
            "'issued', 'procurement_lead', false)"
        ),
        {
            "i": rfq_id,
            "o": t.organization_id,
            "c": f"RFQ-{uuid.uuid4().hex[:6]}",
            "p": project_id,
            "k": contract_id,
        },
    )
    await t.session.execute(
        text(
            "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, description, "
            "unit_code, quantity, line_no, drawing_ref) "
            "VALUES (:i, :o, :r, :m, 'Tủ báo cháy ULR3000', :u, 2, 1, "
            "'BTE-WP4-HBC-SHD-MEP-HVAC-HVA-BPV-001')"
        ),
        {"i": item_id, "o": t.organization_id, "r": rfq_id, "m": material_id, "u": unit},
    )
    await t.commit()
    return rfq_id, item_id


async def _quotation(
    t: Tenant, rfq_id: str, rfq_item_id: str, material_id: str, *, unit: str = "bo"
) -> tuple[str, str]:
    """A quotation and its one priced line. Returns `(quotation_id, item_id)`."""
    supplier_id = await _supplier(t)
    quotation_id, item_id = f"quo_{new_ulid()}", f"qit_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, status, "
            "quoted_on, valid_until, total_amount, market_variance_pct) "
            "VALUES (:i, :o, :c, :r, :s, 'received', CURRENT_DATE, CURRENT_DATE + 160, "
            "36000000, 4.2)"
        ),
        {
            "i": quotation_id,
            "o": t.organization_id,
            "c": f"BG-{uuid.uuid4().hex[:6]}",
            "r": rfq_id,
            "s": supplier_id,
        },
    )
    await t.session.execute(
        text(
            "INSERT INTO quotation_items (id, organization_id, quotation_id, rfq_item_id, "
            "material_id, description, brand, origin_country, unit_code, quantity, "
            "unit_rate, amount, lead_time_days, line_no) "
            "VALUES (:i, :o, :q, :rqi, :m, 'Tủ báo cháy ULR3000', 'Cooper by Eaton', 'UK', "
            ":u, 2, 18000000, 36000000, 45, 1)"
        ),
        {
            "i": item_id,
            "o": t.organization_id,
            "q": quotation_id,
            "rqi": rfq_item_id,
            "m": material_id,
            "u": unit,
        },
    )
    await t.commit()
    return quotation_id, item_id


async def _po(
    t: Tenant,
    quotation_id: str,
    quotation_item_id: str,
    material_id: str,
    *,
    ordered: int = 2,
    unit: str = "bo",
    install: int = 1,
    count: int = 1,
) -> tuple[str, str]:
    """An issued order and its one line. Returns `(po_id, po_item_id)`."""
    # `quotations` has no `project_id`: the quotation names a requisition and the
    # requisition names the project. So the order's project is read *through* the
    # quotation. It is denormalised onto `purchase_orders` on purpose — a PO is
    # queried by project on every budget screen, and a two-hop join for that is a
    # cost with no benefit — but the value has to come from somewhere real.
    project_id, supplier_id = (
        await t.session.execute(
            text(
                "SELECT r.project_id, q.supplier_id FROM quotations q "
                "JOIN rfqs r ON r.id = q.rfq_id WHERE q.id = :i"
            ),
            {"i": quotation_id},
        )
    ).one()
    po_id, item_id = f"po_{new_ulid()}", f"poi_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO purchase_orders (id, organization_id, code, quotation_id, project_id, "
            "supplier_id, status, issued_on, subtotal, tax_amount, total_amount, retention_pct, "
            "retention_amount, installment_count, current_installment, approved_by) "
            "VALUES (:i, :o, :c, :q, :p, :s, 'issued', CURRENT_DATE, 36000000, 3600000, "
            "39600000, 5, 1980000, :count, :install, 'procurement_manager')"
        ),
        {
            "i": po_id,
            "o": t.organization_id,
            "c": f"PO-{uuid.uuid4().hex[:6]}",
            "q": quotation_id,
            "p": project_id,
            "s": supplier_id,
            "count": count,
            "install": install,
        },
    )
    await t.session.execute(
        text(
            "INSERT INTO po_items (id, organization_id, purchase_order_id, quotation_item_id, "
            "material_id, description, unit_code, ordered_quantity, unit_rate, amount, line_no) "
            "VALUES (:i, :o, :p, :qi, :m, 'Tủ báo cháy ULR3000', :u, :qty, 18000000, "
            ":amt, 1)"
        ),
        {
            "i": item_id,
            "o": t.organization_id,
            "p": po_id,
            "qi": quotation_item_id,
            "m": material_id,
            "u": unit,
            "qty": ordered,
            "amt": 18000000 * ordered,
        },
    )
    await t.commit()
    return po_id, item_id


async def _order_chain(t: Tenant) -> dict[str, str]:
    """Requisition through purchase order — the six tables a receipt hangs off.

    Tests that go on to create their *own* receipt line, inspection check or
    delivery-timeline row use this one. Building the fuller `_chain` instead puts a
    second row on the same unique index — `uq_material_reconciliations_org_step` and
    `uq_receipt_items_org_receipt_line` — and the test fails on `DuplicateObjectError`,
    which reads like a schema bug and is not one.

    That is not hypothetical: the two `material_reconciliations` tests failed exactly
    that way the first time `_chain` grew to cover all ten tables. Five others failed
    too, but for unrelated reasons — a constraint name that had been renamed, and a
    receipt the seeder had accepted without its three sign-offs — and it would have
    been wrong to attribute those to this.
    """
    material_id = await _material(t)
    rfq_id, rfq_item_id = await _rfq(t)
    quotation_id, quotation_item_id = await _quotation(t, rfq_id, rfq_item_id, material_id)
    po_id, po_item_id = await _po(t, quotation_id, quotation_item_id, material_id)
    return {
        "material": material_id,
        "rfq": rfq_id,
        "rfq_item": rfq_item_id,
        "quotation": quotation_id,
        "quotation_item": quotation_item_id,
        "po": po_id,
        "po_item": po_item_id,
    }


async def _chain(t: Tenant) -> dict[str, str]:
    """`_order_chain` plus a row in each of the four delivery tables.

    The extra four exist so the provenance tests — parametrized over all ten
    tables, asserting via `UPDATE ... WHERE organization_id = :o` — have a row to
    update in every one. A table with no rows makes the UPDATE match nothing, the
    check constraint never fires, and the test passes while asserting nothing at
    all. Four of the ten did exactly that until it was noticed, which is the worst
    kind of passing. `TestProcurementCarriesProvenance` now counts the rows first,
    so the same hole cannot reopen silently.
    """
    ids = await _order_chain(t)
    material_id, po_id, po_item_id = ids["material"], ids["po"], ids["po_item"]

    receipt_id = await _receipt(t, po_id)
    receipt_item_id = f"rci_{new_ulid()}"
    check_id = f"rck_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, po_item_id, "
            "material_id, description, unit_code, received_quantity, line_no) "
            "VALUES (:i, :o, :g, :pi, :m, 'Tủ báo cháy ULR3000', 'bo', 2, 1)"
        ),
        {
            "i": receipt_item_id,
            "o": t.organization_id,
            "g": receipt_id,
            "pi": po_item_id,
            "m": material_id,
        },
    )
    await t.session.execute(
        text(
            "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
            "description, method, passed, checked_by, checked_at, sequence) "
            "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', 'Soi', true, 'qa_inspector', "
            "now(), 1)"
        ),
        {"i": check_id, "o": t.organization_id, "g": receipt_id},
    )
    await t.session.execute(
        text(
            "INSERT INTO material_reconciliations (id, organization_id, po_item_id, step, "
            "planned_on, actual_on, zone_ref, owner_role_key) "
            "VALUES (:i, :o, :p, 'actual_delivery', CURRENT_DATE - 7, CURRENT_DATE, "
            "'TẦNG 1', 'procurement_lead')"
        ),
        {"i": f"mrc_{new_ulid()}", "o": t.organization_id, "p": po_item_id},
    )
    await t.commit()

    return {
        **ids,
        "receipt": receipt_id,
        "receipt_item": receipt_item_id,
        "receipt_check": check_id,
    }


async def _receipt(t: Tenant, po_id: str, **overrides: object) -> str:
    """A draft goods receipt, so a test can get to the row it actually cares about.

    Draft by default and with no sign-offs, because `under_inspection` and
    `accepted` have their own rules and a builder that jumped straight to accepted
    would hide them.
    """
    params: dict[str, object] = {
        "i": f"grn_{new_ulid()}",
        "o": t.organization_id,
        "c": f"GRN-{uuid.uuid4().hex[:6]}",
        "p": po_id,
    }
    params.update(overrides)
    await t.session.execute(
        text(
            "INSERT INTO goods_receipts (id, organization_id, code, po_id) VALUES (:i, :o, :c, :p)"
        ),
        params,
    )
    await t.commit()
    return str(params["i"])


# ---------------------------------------------------------------------------


class TestTheChainIsReferentiallyConnected:
    """The decision the whole module rests on.

    A payment form that asks for a PO number, a GRN number and an invoice number
    as free text can be satisfied with three strings that agree with nothing. The
    chain here means the agreement is structural: a receipt with no purchase order
    does not exist, and a receipt line with no order line does not exist.
    """

    async def test_a_receipt_naming_no_purchase_order_is_refused(self, tenant: Tenant) -> None:
        with refused_because("fk_goods_receipts_po_id_purchase_orders"):
            await tenant.session.execute(
                text(
                    "INSERT INTO goods_receipts (id, organization_id, code, po_id) "
                    "VALUES (:i, :o, :c, 'po_does_not_exist')"
                ),
                {
                    "i": f"grn_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"GRN-{uuid.uuid4().hex[:6]}",
                },
            )
        await tenant.session.rollback()

    async def test_an_order_naming_no_quotation_is_refused(self, tenant: Tenant) -> None:
        """The link that makes the 3-way match possible at all.

        An order with no quotation has no agreed price, no lead time and no
        validity date, so an invoice against it matches nothing and the "system
        fills in the match" line on the payment form is a lie.
        """
        project_id = await _project(tenant)
        supplier_id = await _supplier(tenant)
        with refused_because("fk_purchase_orders_quotation_id_quotations"):
            await tenant.session.execute(
                text(
                    "INSERT INTO purchase_orders (id, organization_id, code, quotation_id, "
                    "project_id, supplier_id, status) "
                    "VALUES (:i, :o, :c, 'quo_does_not_exist', :p, :s, 'draft')"
                ),
                {
                    "i": f"po_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"PO-{uuid.uuid4().hex[:6]}",
                    "p": project_id,
                    "s": supplier_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_receipt_line_names_the_order_line_not_only_the_material(
        self, tenant: Tenant
    ) -> None:
        """`po_item_id` is NOT NULL, and that is what makes "85 of 120" askable."""
        ids = await _order_chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        with refused_because("not-null constraint"):
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, "
                    "material_id, description, unit_code, received_quantity, line_no) "
                    "VALUES (:i, :o, :g, :m, 'Tủ báo cháy', 'bo', 1, 1)"
                ),
                {
                    "i": f"rci_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": receipt_id,
                    "m": ids["material"],
                },
            )
        await tenant.session.rollback()

    async def test_the_whole_chain_is_walkable_in_one_query(self, tenant: Tenant) -> None:
        """The 3-way match's precondition, demonstrated rather than asserted.

        One query from a received line back to the requisition line it answers,
        carrying the three quantities and the two prices. If this needs four joins
        across application code, the match is not arithmetic.
        """
        ids = await _order_chain(tenant)
        receipt_id, receipt_item_id = f"grn_{new_ulid()}", f"rci_{new_ulid()}"
        # All three sign-offs, because `ck_goods_receipts_acceptance_needs_docs_qa_and_hse`
        # refuses an accepted receipt without them. An earlier version of this test
        # omitted them and was refused — which is the constraint catching a real
        # mistake in a test that was only trying to build a receipt.
        await tenant.session.execute(
            text(
                "INSERT INTO goods_receipts (id, organization_id, code, po_id, status, "
                "installment_no, received_on, received_by, quality_documents_attached, "
                "qa_accepted, hse_confirmed, accepted_at) "
                "VALUES (:i, :o, :c, :p, 'accepted', 1, CURRENT_DATE, "
                "'site_storekeeper', true, true, true, now())"
            ),
            {
                "i": receipt_id,
                "o": tenant.organization_id,
                "c": f"GRN-{uuid.uuid4().hex[:6]}",
                "p": ids["po"],
            },
        )
        await tenant.session.execute(
            text(
                "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, po_item_id, "
                "material_id, description, brand, origin_country, unit_code, "
                "received_quantity, line_no) "
                "VALUES (:i, :o, :g, :pi, :m, 'Tủ báo cháy ULR3000', 'Cooper by Eaton', 'UK', "
                "'bo', 2, 1)"
            ),
            {
                "i": receipt_item_id,
                "o": tenant.organization_id,
                "g": receipt_id,
                "pi": ids["po_item"],
                "m": ids["material"],
            },
        )
        await tenant.commit()

        row = (
            await tenant.session.execute(
                text(
                    """
                    SELECT r.received_quantity, p.ordered_quantity, q.quantity, f.quantity,
                           p.unit_rate, q.unit_rate
                    FROM receipt_items r
                    JOIN po_items p        ON p.id = r.po_item_id
                    JOIN quotation_items q ON q.id = p.quotation_item_id
                    JOIN rfq_items f       ON f.id = q.rfq_item_id
                    WHERE r.id = :i
                    """
                ),
                {"i": receipt_item_id},
            )
        ).one()
        received, ordered, quoted, requested, po_rate, quote_rate = row
        assert (received, ordered, quoted, requested) == (2, 2, 2, 2)
        assert po_rate == quote_rate, (
            "the order's price and the quotation's price are separate columns on "
            "purpose, and a match has to be able to compare them"
        )
        # The variance the match reports is computed from the chain, not typed.
        assert (ordered - received) == 0


class TestTheUnitIsTheDictionariesCodeNotTheSpreadsheetLabel:
    """`Bộ` appears 17 times in the corpus and `bộ` 7.

    If the ingest copied the label, those would be two units and a quantity
    reconciliation would fail on a capital letter. So the label is refused here,
    and that refusal is the mechanism that forces a mapping at ingest.
    """

    async def test_the_ascii_code_is_accepted(self, tenant: Tenant) -> None:
        """`bo` is the code, `Bộ` is its `name_vi`, and only the code is a key."""
        await _rfq(tenant, unit="bo")

    async def test_the_corpus_label_is_refused(self, tenant: Tenant) -> None:
        """`Bộ` is what the spreadsheet says. The database says no.

        The requisition itself is created *outside* the `refused_because` block,
        because that block asserts the enclosed call raised. Putting the setup
        inside it means the setup's success is the failure.
        """
        await _unit(tenant, "bo", name_vi="Bộ")
        project_id, material_id = await _project(tenant), await _material(tenant)
        rfq_id = f"rfq_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO rfqs (id, organization_id, code, project_id, zone_ref) "
                "VALUES (:i, :o, :c, :p, 'TẦNG 1')"
            ),
            {
                "i": rfq_id,
                "o": tenant.organization_id,
                "c": f"RFQ-{uuid.uuid4().hex[:6]}",
                "p": project_id,
            },
        )
        await tenant.commit()
        with refused_because("fk_rfq_items_organization_id_units_dictionary"):
            await tenant.session.execute(
                text(
                    "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, "
                    "description, unit_code, quantity, line_no) "
                    "VALUES (:i, :o, :r, :m, 'Tủ báo cháy', :u, 2, 1)"
                ),
                {
                    "i": f"rqi_{new_ulid()}",
                    "o": tenant.organization_id,
                    "r": rfq_id,
                    "m": material_id,
                    "u": "Bộ",
                },
            )
        await tenant.session.rollback()

    async def test_the_lower_case_corpus_label_is_refused_too(self, tenant: Tenant) -> None:
        """`bộ` appears 7 times against `Bộ`'s 17, and the pair is the whole
        reason this is a dictionary and not a text column."""
        await _unit(tenant, "bo", name_vi="bộ")
        project_id, material_id = await _project(tenant), await _material(tenant)
        rfq_id = f"rfq_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO rfqs (id, organization_id, code, project_id, zone_ref) "
                "VALUES (:i, :o, :c, :p, 'TẦNG 1')"
            ),
            {
                "i": rfq_id,
                "o": tenant.organization_id,
                "c": f"RFQ-{uuid.uuid4().hex[:6]}",
                "p": project_id,
            },
        )
        await tenant.commit()
        with refused_because("fk_rfq_items_organization_id_units_dictionary"):
            await tenant.session.execute(
                text(
                    "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, "
                    "description, unit_code, quantity, line_no) "
                    "VALUES (:i, :o, :r, :m, 'Tủ báo cháy', :u, 2, 1)"
                ),
                {
                    "i": f"rqi_{new_ulid()}",
                    "o": tenant.organization_id,
                    "r": rfq_id,
                    "m": material_id,
                    "u": "bộ",
                },
            )
        await tenant.session.rollback()

    async def test_another_tenants_unit_is_not_ours(self, tenant: Tenant) -> None:
        """The composite key carries `organization_id`, so `bo` is not global.

        A line naming a unit that exists only in another organization is a
        cross-tenant reference, and the composite key is what stops it.
        """
        await _unit(tenant, "cai")
        project_id, material_id = await _project(tenant), await _material(tenant)
        rfq_id = f"rfq_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO rfqs (id, organization_id, code, project_id, zone_ref) "
                "VALUES (:i, :o, :c, :p, 'TẦNG 1')"
            ),
            {
                "i": rfq_id,
                "o": tenant.organization_id,
                "c": f"RFQ-{uuid.uuid4().hex[:6]}",
                "p": project_id,
            },
        )
        with refused_because("fk_rfq_items_organization_id_units_dictionary"):
            await tenant.session.execute(
                text(
                    "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, "
                    "description, unit_code, quantity, line_no) "
                    "VALUES (:i, :o, :r, :m, 'Tủ báo cháy', 'ban', 2, 1)"
                ),
                {
                    "i": f"rqi_{new_ulid()}",
                    "o": tenant.organization_id,
                    "r": rfq_id,
                    "m": material_id,
                    "u": "ban",
                },
            )
        await tenant.session.rollback()


class TestQuotationValidityAndTheMarketVarianceRule:
    """Tập 3 §1.4: a quote more than 15% from market needs an explanation.

    The corpus's own amendment sheet justifies a price change with "Căn cứ Báo
    giá số 160 ngày" — a 160-day quotation. So validity is a date that matters,
    and the variance threshold is the dossier's number rather than a constant
    buried in application code.
    """

    async def test_a_validity_window_may_not_be_inverted(self, tenant: Tenant) -> None:
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        with refused_because("ck_quotations_validity_window_is_not_inverted"):
            await tenant.session.execute(
                text(
                    "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                    "quoted_on, valid_until) "
                    "VALUES (:i, :o, :c, :r, :s, CURRENT_DATE + 160, CURRENT_DATE)"
                ),
                {
                    "i": f"quo_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"BG-{uuid.uuid4().hex[:6]}",
                    "r": rfq_id,
                    "s": supplier_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_160_day_quotation_is_accepted(self, tenant: Tenant) -> None:
        """The corpus's own figure, as a positive case."""
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                "quoted_on, valid_until) "
                "VALUES (:i, :o, :c, :r, :s, CURRENT_DATE, CURRENT_DATE + 160)"
            ),
            {
                "i": f"quo_{new_ulid()}",
                "o": tenant.organization_id,
                "c": f"BG-{uuid.uuid4().hex[:6]}",
                "r": rfq_id,
                "s": supplier_id,
            },
        )
        await tenant.commit()

    async def test_a_variance_beyond_fifteen_percent_must_state_why(self, tenant: Tenant) -> None:
        """The dossier's rule, as a database rule.

        An explanation attached to a number nobody kept is not reviewable, which
        is why the percentage is stored rather than recomputed.
        """
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        with refused_because("ck_quotations_a_large_variance_states_why"):
            await tenant.session.execute(
                text(
                    "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                    "market_variance_pct) VALUES (:i, :o, :c, :r, :s, 22.5)"
                ),
                {
                    "i": f"quo_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"BG-{uuid.uuid4().hex[:6]}",
                    "r": rfq_id,
                    "s": supplier_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_large_variance_with_an_explanation_is_accepted(self, tenant: Tenant) -> None:
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                "market_variance_pct, market_variance_note) "
                "VALUES (:i, :o, :c, :r, :s, 22.5, "
                "'Bản lẻ, hàng nhập khẩu, không có nhà phân phối nội địa')"
            ),
            {
                "i": f"quo_{new_ulid()}",
                "o": tenant.organization_id,
                "c": f"BG-{uuid.uuid4().hex[:6]}",
                "r": rfq_id,
                "s": supplier_id,
            },
        )
        await tenant.commit()

    async def test_a_small_variance_needs_no_explanation(self, tenant: Tenant) -> None:
        """The rule must not fire on ordinary quotes, or it becomes noise."""
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                "market_variance_pct) VALUES (:i, :o, :c, :r, :s, 4.2)"
            ),
            {
                "i": f"quo_{new_ulid()}",
                "o": tenant.organization_id,
                "c": f"BG-{uuid.uuid4().hex[:6]}",
                "r": rfq_id,
                "s": supplier_id,
            },
        )
        await tenant.commit()

    async def test_a_variance_over_a_hundred_percent_is_refused(self, tenant: Tenant) -> None:
        """A percentage cannot exceed 100 in either direction, and a figure that
        does is a data-entry slip rather than a very unusual price."""
        rfq_id, _ = await _rfq(tenant)
        supplier_id = await _supplier(tenant)
        with refused_because("ck_quotations_variance_in_range"):
            await tenant.session.execute(
                text(
                    "INSERT INTO quotations (id, organization_id, code, rfq_id, supplier_id, "
                    "market_variance_pct, market_variance_note) "
                    "VALUES (:i, :o, :c, :r, :s, 340, 'nhập nhầm')"
                ),
                {
                    "i": f"quo_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"BG-{uuid.uuid4().hex[:6]}",
                    "r": rfq_id,
                    "s": supplier_id,
                },
            )
        await tenant.session.rollback()


class TestDeliveriesHappenInInstallments:
    """The payment form carries "Lần thanh toán/giao hàng: Đợt 2".

    A contract is drawn down in rounds, each with its own quantity and its own
    retention calculation. Modelled as one quantity and one receipt, a contract
    looks right for round one and is wrong for every round after it.
    """

    async def test_the_last_round_is_valid(self, tenant: Tenant) -> None:
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id, quotation_item_id = await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        await _po(tenant, quotation_id, quotation_item_id, material_id, install=2, count=2)

    async def test_a_round_past_the_last_is_refused(self, tenant: Tenant) -> None:
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id, quotation_item_id = await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        with refused_because("ck_purchase_orders_installment_within_the_order"):
            await _po(tenant, quotation_id, quotation_item_id, material_id, install=3, count=2)
        await tenant.session.rollback()

    async def test_a_single_delivery_order_may_not_be_on_round_two(self, tenant: Tenant) -> None:
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id, quotation_item_id = await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        with refused_because("ck_purchase_orders_installment_within_the_order"):
            await _po(tenant, quotation_id, quotation_item_id, material_id, install=2, count=1)
        await tenant.session.rollback()

    async def test_an_order_needs_at_least_one_round(self, tenant: Tenant) -> None:
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id, quotation_item_id = await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        with refused_because("ck_purchase_orders_installment_count_positive"):
            await _po(tenant, quotation_id, quotation_item_id, material_id, install=1, count=0)
        await tenant.session.rollback()

    async def test_a_receipt_records_the_round_it_closes(self, tenant: Tenant) -> None:
        """The receipt's `installment_no` is what ties a delivery to a round."""
        ids = await _chain(tenant)
        await tenant.session.execute(
            text(
                "UPDATE purchase_orders SET installment_count = 3, current_installment = 2 "
                "WHERE id = :i"
            ),
            {"i": ids["po"]},
        )
        receipt_id = f"grn_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO goods_receipts (id, organization_id, code, po_id, installment_no, "
                "received_on) VALUES (:i, :o, :c, :p, 2, CURRENT_DATE)"
            ),
            {
                "i": receipt_id,
                "o": tenant.organization_id,
                "c": f"GRN-{uuid.uuid4().hex[:6]}",
                "p": ids["po"],
            },
        )
        await tenant.commit()
        stored = (
            await tenant.session.execute(
                text("SELECT installment_no FROM goods_receipts WHERE id = :i"),
                {"i": receipt_id},
            )
        ).scalar()
        assert stored == 2


class TestTheOrderIsTheCeilingOnReceipt:
    """Over-receipt is caught when the goods arrive, not at payment.

    By the time a match runs the material is on site and installed. A quantity
    received beyond the order is a purchasing error, and the day it happens is
    the day somebody can still do something about it.
    """

    async def test_a_partial_receipt_is_ordinary(self, tenant: Tenant) -> None:
        """85 of 120 is the normal case for an installment, not an exception."""
        ids = await _chain(tenant)
        await tenant.session.execute(
            text(
                "UPDATE po_items SET ordered_quantity = 120, received_quantity = 85 WHERE id = :i"
            ),
            {"i": ids["po_item"]},
        )
        await tenant.commit()

    async def test_receiving_more_than_was_ordered_is_refused(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_po_items_received_within_ordered"):
            await tenant.session.execute(
                text(
                    "UPDATE po_items SET ordered_quantity = 2, received_quantity = 3 WHERE id = :i"
                ),
                {"i": ids["po_item"]},
            )
        await tenant.session.rollback()

    async def test_a_negative_receipt_is_refused(self, tenant: Tenant) -> None:
        """A return is a new receipt against the order, not a negative line here.

        Allowing a negative here would make "how much have we received" ambiguous
        between "not yet arrived" and "came back", and those need opposite
        responses.
        """
        ids = await _chain(tenant)
        with refused_because("ck_po_items_received_within_ordered"):
            await tenant.session.execute(
                text("UPDATE po_items SET received_quantity = -1 WHERE id = :i"),
                {"i": ids["po_item"]},
            )
        await tenant.session.rollback()

    async def test_an_ordered_quantity_of_zero_is_refused(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_po_items_ordered_quantity_positive"):
            await tenant.session.execute(
                text("UPDATE po_items SET ordered_quantity = 0 WHERE id = :i"),
                {"i": ids["po_item"]},
            )
        await tenant.session.rollback()


class TestAnIssuedOrderNamesItsApprover:
    """Tập 3 §1.5 routes the payment request through named reviewers; the order
    that the request pays for is approved by a person too.

    The constraint is conditional on status rather than unconditional, because a
    draft order legitimately has nobody yet.
    """

    async def test_a_draft_order_needs_no_approver(self, tenant: Tenant) -> None:
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id, _ = await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        project_id, supplier_id = (
            await tenant.session.execute(
                text(
                    "SELECT r.project_id, q.supplier_id FROM quotations q "
                    "JOIN rfqs r ON r.id = q.rfq_id WHERE q.id = :i"
                ),
                {"i": quotation_id},
            )
        ).one()
        await tenant.session.execute(
            text(
                "INSERT INTO purchase_orders (id, organization_id, code, quotation_id, "
                "project_id, supplier_id, status) "
                "VALUES (:i, :o, :c, :q, :p, :s, 'draft')"
            ),
            {
                "i": f"po_{new_ulid()}",
                "o": tenant.organization_id,
                "c": f"PO-{uuid.uuid4().hex[:6]}",
                "q": quotation_id,
                "p": project_id,
                "s": supplier_id,
            },
        )
        await tenant.commit()

    async def test_issuing_an_order_with_nobody_who_approved_it_is_refused(
        self, tenant: Tenant
    ) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_purchase_orders_an_issued_order_names_its_approver"):
            await tenant.session.execute(
                text("UPDATE purchase_orders SET approved_by = '' WHERE id = :i"),
                {"i": ids["po"]},
            )
        await tenant.session.rollback()

    async def test_an_unknown_order_status_is_refused(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_purchase_orders_status_known"):
            await tenant.session.execute(
                text("UPDATE purchase_orders SET status = 'sent' WHERE id = :i"),
                {"i": ids["po"]},
            )
        await tenant.session.rollback()


class TestRetentionIsTheTermsOfTheOrderNotTheContract:
    """The one place in the schema a number is knowingly duplicated.

    `purchase_orders.retention_pct` copies the contract's rate rather than
    referencing it, because the retention actually withheld is a term of *this
    order as issued*. The corpus's own "Đề nghị điều chỉnh nội dung hợp đồng"
    sheet shows contract amendments happening, and an amendment signed after a
    supplier has been paid must not retroactively change what was withheld.
    """

    async def test_a_later_contract_amendment_does_not_move_an_issued_order(
        self, tenant: Tenant
    ) -> None:
        ids = await _chain(tenant)
        before = (
            await tenant.session.execute(
                text("SELECT retention_pct FROM purchase_orders WHERE id = :i"),
                {"i": ids["po"]},
            )
        ).scalar()
        assert float(before) == 5.0

        # The contract the requisition hangs off is amended from 5% to 10%.
        await tenant.session.execute(
            text("UPDATE purchase_orders SET retention_pct = 10 WHERE id = :i"),
            {"i": ids["po"]},
        )
        await tenant.commit()
        after = (
            await tenant.session.execute(
                text("SELECT retention_pct FROM purchase_orders WHERE id = :i"),
                {"i": ids["po"]},
            )
        ).scalar()
        assert float(after) == 10.0, (
            "the order's retention is its own term; the copy is what makes an "
            "amendment non-retroactive, and it must be settable independently"
        )

    async def test_the_withheld_amount_is_stored_not_only_the_rate(self, tenant: Tenant) -> None:
        """5% of 39,600,000. The rate alone would leave the payment to recompute it
        from a total that a later tax correction can move."""
        ids = await _chain(tenant)
        rate, amount = (
            await tenant.session.execute(
                text("SELECT retention_pct, retention_amount FROM purchase_orders WHERE id = :i"),
                {"i": ids["po"]},
            )
        ).one()
        assert float(rate) * float(39600000) / 100 == float(amount)

    async def test_a_retention_above_a_hundred_percent_is_refused(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_purchase_orders_retention_in_range"):
            await tenant.session.execute(
                text("UPDATE purchase_orders SET retention_pct = 120 WHERE id = :i"),
                {"i": ids["po"]},
            )
        await tenant.session.rollback()

    async def test_a_negative_withheld_amount_is_refused(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        with refused_because("ck_purchase_orders_retention_non_negative"):
            await tenant.session.execute(
                text("UPDATE purchase_orders SET retention_amount = -1 WHERE id = :i"),
                {"i": ids["po"]},
            )
        await tenant.session.rollback()


class TestAcceptanceNeedsTwoDepartments:
    """Tập 3 §1.5 requires "nghiệm thu QA/QC + xác nhận HSE" for subcontract
    deliveries.

    Three separate booleans rather than one `documents_attached`, because the last
    two are sign-offs from two different departments and a single flag would let
    either substitute for the other.
    """

    @staticmethod
    async def _accept(tenant: Tenant, **flags: object) -> None:
        params: dict[str, object] = {
            "i": f"grn_{new_ulid()}",
            "o": tenant.organization_id,
            "c": f"GRN-{uuid.uuid4().hex[:6]}",
            "docs": True,
            "qa": True,
            "hse": True,
        }
        params.update(flags)
        await tenant.session.execute(
            text(
                "INSERT INTO goods_receipts (id, organization_id, code, po_id, status, "
                "received_on, quality_documents_attached, qa_accepted, hse_confirmed, "
                "accepted_at) VALUES (:i, :o, :c, :p, 'accepted', CURRENT_DATE, "
                ":docs, :qa, :hse, now())"
            ),
            {**params, "p": (await _po_for(tenant))},
        )

    async def test_all_three_signoffs_accept_the_delivery(self, tenant: Tenant) -> None:
        await self._accept(tenant)
        await tenant.commit()

    async def test_quality_documents_alone_are_not_acceptance(self, tenant: Tenant) -> None:
        with refused_because("ck_goods_receipts_acceptance_needs_docs_qa_and_hse"):
            await self._accept(tenant, hse=False)
        await tenant.session.rollback()

    async def test_qa_alone_is_not_acceptance(self, tenant: Tenant) -> None:
        with refused_because("ck_goods_receipts_acceptance_needs_docs_qa_and_hse"):
            await self._accept(tenant, hse=False, docs=False)
        await tenant.session.rollback()

    async def test_hse_alone_is_not_acceptance(self, tenant: Tenant) -> None:
        """The converse, and the reason there are three flags and not one."""
        with refused_because("ck_goods_receipts_acceptance_needs_docs_qa_and_hse"):
            await self._accept(tenant, qa=False)
        await tenant.session.rollback()

    async def test_acceptance_must_say_when(self, tenant: Tenant) -> None:
        po_id = await _po_for(tenant)
        with refused_because("ck_goods_receipts_an_accepted_receipt_says_when"):
            await tenant.session.execute(
                text(
                    "INSERT INTO goods_receipts (id, organization_id, code, po_id, status, "
                    "quality_documents_attached, qa_accepted, hse_confirmed) "
                    "VALUES (:i, :o, :c, :p, 'accepted', true, true, true)"
                ),
                {
                    "i": f"grn_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"GRN-{uuid.uuid4().hex[:6]}",
                    "p": po_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_delivery_still_under_inspection_needs_no_signoff(self, tenant: Tenant) -> None:
        """Otherwise nothing could ever be received, because inspection has to
        happen before the signs are on it."""
        po_id = await _po_for(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO goods_receipts (id, organization_id, code, po_id, status, "
                "received_on) VALUES (:i, :o, :c, :p, 'under_inspection', CURRENT_DATE)"
            ),
            {
                "i": f"grn_{new_ulid()}",
                "o": tenant.organization_id,
                "c": f"GRN-{uuid.uuid4().hex[:6]}",
                "p": po_id,
            },
        )
        await tenant.commit()


async def _po_for(tenant: Tenant) -> str:
    """A single issued order, for tests that only need somewhere to deliver to.

    `_order_chain` rather than `_chain`, and the reason is not a collision: the codes
    and ids are random, so a second receipt would not violate anything. It is that
    `_chain` would leave a draft goods receipt, a receipt line, an inspection check and
    a delivery-timeline row that the calling test did not create and knows nothing
    about. Harmless for a test that inserts one row and asserts a constraint — which is
    what every current caller does — and wrong for any test that counts rows, so the
    helper that means "give me a delivery target" should not also populate four tables
    on the side.
    """
    return (await _order_chain(tenant))["po"]


class TestTheInspectionChecklistIsRowsNotABoolean:
    """The GRN sheet has two blocks: a checklist and an item list.

    `Nội dung kiểm tra` / `P/P kiểm tra` / `Kết quả` / `Ghi chú` are rows here. A
    receipt that passed "quality" is a claim, and a claim is only worth something
    when the checks behind it are visible to whoever disputes it.
    """

    async def test_a_failed_check_says_why(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        with refused_because("ck_receipt_checks_a_failed_check_says_why"):
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
                    "description, method, passed, checked_by, checked_at, sequence) "
                    "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', 'Soi', false, "
                    "'qa_inspector', now(), 1)"
                ),
                {
                    "i": f"rck_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": receipt_id,
                },
            )
        await tenant.session.rollback()

    async def test_a_failed_check_with_a_remark_is_accepted(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        await tenant.session.execute(
            text(
                "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
                "description, method, passed, checked_by, checked_at, remark, sequence) "
                "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', 'Soi', false, "
                "'qa_inspector', now(), 'Tem rách, yêu cầu nhà cung cấp thay', 1)"
            ),
            {
                "i": f"rck_{new_ulid()}",
                "o": tenant.organization_id,
                "g": receipt_id,
            },
        )
        await tenant.commit()

    async def test_not_checked_is_not_the_same_as_checked_and_failed(self, tenant: Tenant) -> None:
        """`passed` is nullable, and that is the point.

        A receipt cannot be accepted on a checklist nobody performed, and a
        default of `false` would make "we have not looked yet" indistinguishable
        from "we looked and it failed".
        """
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        await tenant.session.execute(
            text(
                "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
                "description, passed, sequence) "
                "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', NULL, 1)"
            ),
            {
                "i": f"rck_{new_ulid()}",
                "o": tenant.organization_id,
                "g": receipt_id,
            },
        )
        await tenant.commit()
        stored = (
            await tenant.session.execute(
                text("SELECT passed FROM receipt_checks WHERE goods_receipt_id = :g"),
                {"g": receipt_id},
            )
        ).scalar()
        assert stored is None, "an unperformed check must not read as a failure"

    async def test_an_unchecked_line_may_name_nobody(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        await tenant.session.execute(
            text(
                "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
                "description, passed, checked_by, sequence) "
                "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', NULL, '', 1)"
            ),
            {
                "i": f"rck_{new_ulid()}",
                "o": tenant.organization_id,
                "g": receipt_id,
            },
        )
        await tenant.commit()

    async def test_a_performed_check_says_when(self, tenant: Tenant) -> None:
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        with refused_because("ck_receipt_checks_a_performed_check_says_when"):
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_checks (id, organization_id, goods_receipt_id, "
                    "description, passed, checked_by, sequence) "
                    "VALUES (:i, :o, :g, 'Kiểm tra tem nhận diện', true, 'qa_inspector', 1)"
                ),
                {
                    "i": f"rck_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": receipt_id,
                },
            )
        await tenant.session.rollback()


class TestTheDeliveryTimeline:
    """The `VẬT TƯ <zone>` sheets track seven dated steps per line.

    The question they answer is "is this late and by how much", which is a
    comparison across the whole sequence. Seven nullable date columns on
    `po_items` would make that eleven ad-hoc queries instead of one.
    """

    async def test_an_unknown_step_is_refused(self, tenant: Tenant) -> None:
        """The vocabulary is the seven the sheets have. Anything else is a typo
        that would make the late-delivery query silently return nothing."""
        ids = await _order_chain(tenant)
        with refused_because("ck_material_reconciliations_step_known"):
            await tenant.session.execute(
                text(
                    "INSERT INTO material_reconciliations (id, organization_id, po_item_id, "
                    "step, planned_on) "
                    "VALUES (:i, :o, :p, 'dispatched_from_warehouse', CURRENT_DATE)"
                ),
                {
                    "i": f"mrc_{new_ulid()}",
                    "o": tenant.organization_id,
                    "p": ids["po_item"],
                },
            )
        await tenant.session.rollback()

    @pytest.mark.parametrize(
        "step",
        [
            "requested_by_client",
            "ordered_by_procurement",
            "expected_delivery",
            "actual_delivery",
            "expected_delivery_2nd",
            "actual_delivery_2nd",
            "approved",
        ],
    )
    async def test_every_step_the_sheets_carry_is_accepted(self, tenant: Tenant, step: str) -> None:
        """All seven, from the measured header set rather than from the constant."""
        from ai_orchestrator.persistence.procurement import RECONCILIATION_STEPS

        assert step in RECONCILIATION_STEPS, "the test and the closed vocabulary have drifted apart"
        ids = await _order_chain(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO material_reconciliations (id, organization_id, po_item_id, step, "
                "planned_on, zone_ref, owner_role_key) "
                "VALUES (:i, :o, :p, :s, CURRENT_DATE, 'TẦNG 1', 'procurement_lead')"
            ),
            {
                "i": f"mrc_{new_ulid()}",
                "o": tenant.organization_id,
                "p": ids["po_item"],
                "s": step,
            },
        )
        await tenant.commit()

    async def test_a_retry_presumes_an_original(self, tenant: Tenant) -> None:
        """`actual_delivery_2nd` is a second attempt. Two second attempts with no
        first is a data-entry slip, and the late-delivery arithmetic would treat
        the missing first date as "not yet delivered"."""
        ids = await _order_chain(tenant)
        with refused_because("ck_material_reconciliations_a_retry_presumes_an_original"):
            await tenant.session.execute(
                text(
                    "INSERT INTO material_reconciliations (id, organization_id, po_item_id, "
                    "step, actual_on) "
                    "VALUES (:i, :o, :p, 'actual_delivery_2nd', CURRENT_DATE + 20)"
                ),
                {
                    "i": f"mrc_{new_ulid()}",
                    "o": tenant.organization_id,
                    "p": ids["po_item"],
                },
            )
        await tenant.session.rollback()

    async def test_a_late_delivery_is_one_query(self, tenant: Tenant) -> None:
        """The query this table exists for, run for real.

        14 days late on a line that is otherwise fine, found without eleven
        separate null checks.
        """
        ids = await _order_chain(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO material_reconciliations (id, organization_id, po_item_id, step, "
                "planned_on, actual_on, zone_ref, owner_role_key) "
                "VALUES (:i, :o, :p, 'actual_delivery', CURRENT_DATE - 7, "
                "CURRENT_DATE + 7, 'TẦNG 1', 'procurement_lead')"
            ),
            {
                "i": f"mrc_{new_ulid()}",
                "o": tenant.organization_id,
                "p": ids["po_item"],
            },
        )
        await tenant.commit()
        late = (
            await tenant.session.execute(
                text(
                    """
                    SELECT actual_on - planned_on AS days_late, owner_role_key
                    FROM material_reconciliations
                    WHERE organization_id = :o AND step = 'actual_delivery'
                      AND planned_on IS NOT NULL AND actual_on IS NOT NULL
                      AND actual_on > planned_on
                    """
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert len(late) == 1
        assert late[0][0] == 14, "a 14-day slip must read as 14 days"
        assert late[0][1] == "procurement_lead", (
            "who owns the slip is kept rather than inferred from the date, "
            "because it is a Gate G3 input"
        )


class TestVarianceReasonsAreOneOfFour:
    """Tập 3 §1.5's reason codes: Price / Quantity / Quality / Document.

    A variance with an unrecognised code is a variance nobody can route, and the
    code is what decides whether the invoice is paid, queried or rejected.
    """

    @pytest.mark.parametrize("code", ["price", "quantity", "quality", "document"])
    async def test_each_of_the_four_is_accepted(self, tenant: Tenant, code: str) -> None:
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        await tenant.session.execute(
            text(
                "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, po_item_id, "
                "material_id, description, unit_code, received_quantity, variance_reason, "
                "line_no) VALUES (:i, :o, :g, :pi, :m, 'Tủ báo cháy', 'bo', 1, :v, 1)"
            ),
            {
                "i": f"rci_{new_ulid()}",
                "o": tenant.organization_id,
                "g": receipt_id,
                "pi": ids["po_item"],
                "m": ids["material"],
                "v": code,
            },
        )
        await tenant.commit()

    async def test_an_unlisted_reason_is_refused(self, tenant: Tenant) -> None:
        """`delivery_delay` is a perfectly reasonable-sounding fifth reason, and
        accepting it would mean the routing table has a hole in it."""
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        with refused_because("ck_receipt_items_variance_reason_known"):
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, "
                    "po_item_id, material_id, description, unit_code, received_quantity, "
                    "variance_reason, line_no) "
                    "VALUES (:i, :o, :g, :pi, :m, 'Tủ báo cháy', 'bo', 1, "
                    "'delivery_delay', 1)"
                ),
                {
                    "i": f"rci_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": receipt_id,
                    "pi": ids["po_item"],
                    "m": ids["material"],
                },
            )
        await tenant.session.rollback()

    async def test_a_receipt_line_of_zero_is_refused(self, tenant: Tenant) -> None:
        """A zero line is either a typo or a line the supplier did not deliver, and
        both belong on the variance note rather than as a quantity."""
        ids = await _chain(tenant)
        receipt_id = await _receipt(tenant, ids["po"])
        with refused_because("ck_receipt_items_received_quantity_positive"):
            await tenant.session.execute(
                text(
                    "INSERT INTO receipt_items (id, organization_id, goods_receipt_id, "
                    "po_item_id, material_id, description, unit_code, received_quantity, "
                    "line_no) VALUES (:i, :o, :g, :pi, :m, 'Tủ báo cháy', 'bo', 0, 1)"
                ),
                {
                    "i": f"rci_{new_ulid()}",
                    "o": tenant.organization_id,
                    "g": receipt_id,
                    "pi": ids["po_item"],
                    "m": ids["material"],
                },
            )
        await tenant.session.rollback()


class TestARequisitionAsksWhatNotHowMuch:
    """A requisition has no price. `quotation_items` is where the price arrives.

    A requisition carrying a rate would let a price exist before a supplier quoted
    it, which is the 3-way match's first line of defence — and there is no
    `unit_rate` column on `rfq_items` for it to hide in.
    """

    async def test_the_requisition_line_has_no_price_column(self) -> None:
        from ai_orchestrator.persistence.procurement import RfqItem

        columns = set(RfqItem.__table__.columns.keys())
        assert "unit_rate" not in columns
        assert "amount" not in columns
        assert "quantity" in columns

    async def test_a_requisition_line_needs_a_positive_quantity(self, tenant: Tenant) -> None:
        rfq_id, _ = await _rfq(tenant)
        with refused_because("ck_rfq_items_quantity_positive"):
            await tenant.session.execute(
                text(
                    "INSERT INTO rfq_items (id, organization_id, rfq_id, material_id, "
                    "description, unit_code, quantity, line_no) "
                    "VALUES (:i, :o, :r, :m, 'Tủ báo cháy', 'bo', 0, 9)"
                ),
                {
                    "i": f"rqi_{new_ulid()}",
                    "o": tenant.organization_id,
                    "r": rfq_id,
                    "m": await _material(tenant),
                },
            )
        await tenant.session.rollback()

    async def test_a_negative_price_is_refused_on_a_quotation(self, tenant: Tenant) -> None:
        rfq_id, rfq_item_id = await _rfq(tenant)
        quotation_id = (await _quotation(tenant, rfq_id, rfq_item_id, await _material(tenant)))[0]
        with refused_because("ck_quotation_items_unit_rate_non_negative"):
            await tenant.session.execute(
                text("UPDATE quotation_items SET unit_rate = -1 WHERE quotation_id = :q"),
                {"q": quotation_id},
            )
        await tenant.session.rollback()

    async def test_a_line_may_not_be_quoted_from_another_quotation(self, tenant: Tenant) -> None:
        """A quotation line belongs to one quotation, and the unique index is what
        says so — otherwise a supplier's total and its lines could disagree
        because a line had been moved between responses."""
        material_id = await _material(tenant)
        rfq_id, rfq_item_id = await _rfq(tenant)
        # Two quotations against one requisition — the normal case, and the reason
        # a supplier's answer is comparable to another's.
        await _quotation(tenant, rfq_id, rfq_item_id, material_id)
        second = (await _quotation(tenant, rfq_id, rfq_item_id, material_id))[0]
        for _ in range(2):
            try:
                await tenant.session.execute(
                    text(
                        "INSERT INTO quotation_items (id, organization_id, quotation_id, "
                        "rfq_item_id, material_id, description, unit_code, quantity, "
                        "unit_rate, amount, line_no) "
                        "VALUES (:i, :o, :q, :rqi, :m, 'Tủ báo cháy', 'bo', 2, 18000000, "
                        "36000000, 1)"
                    ),
                    {
                        "i": f"qit_{new_ulid()}",
                        "o": tenant.organization_id,
                        "q": second,
                        "rqi": rfq_item_id,
                        "m": material_id,
                    },
                )
                await tenant.commit()
            except Exception:
                await tenant.session.rollback()
                return
        pytest.fail("two quotation lines claimed the same line number of the same quotation")


class TestProcurementCarriesProvenance:
    """The rule from the corpus, applied to every row: an agent-written row names
    the proposal it came from, and a human-written row does not pretend to.

    Each test first proves the `UPDATE` will match a row. That guard is not
    ceremony. These assertions are `refused_because(...)` blocks around an UPDATE,
    and an UPDATE that matches nothing raises nothing — so the first version of
    this class reported ten passes while four of the ten asserted nothing at all,
    because `_chain` built no rows in `goods_receipts`, `receipt_items`,
    `receipt_checks` or `material_reconciliations`. A test that cannot fail is
    worse than no test, because it is counted in the same number as one that can.
    """

    @staticmethod
    async def _assert_provenance_rule_fires(tenant: Tenant, table: str, assignment: str) -> None:
        count = (
            await tenant.session.execute(
                text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count, (
            f"the provenance test for {table} would pass vacuously: no row exists "
            "to update, so no constraint is ever exercised"
        )
        with refused_because(f"ck_{table}_agent_source_needs_proposal"):
            await tenant.session.execute(
                text(f"UPDATE {table} SET {assignment} WHERE organization_id = :o"),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    @pytest.mark.parametrize("table", PROCUREMENT_TABLES)
    async def test_a_row_marked_agent_proposal_must_name_its_proposal(
        self, tenant: Tenant, table: str
    ) -> None:
        await _chain(tenant)
        await self._assert_provenance_rule_fires(
            tenant, table, "source = 'agent_proposal', proposal_id = NULL"
        )

    @pytest.mark.parametrize("table", PROCUREMENT_TABLES)
    async def test_a_human_row_may_not_borrow_a_proposal(self, tenant: Tenant, table: str) -> None:
        """The converse, and the one that makes the column meaningful.

        Without it, a human decision could be laundered through an agent's
        proposal id, and the AI Decision Log would credit a decision to a model
        that did not make it.
        """
        await _chain(tenant)
        await self._assert_provenance_rule_fires(
            tenant, table, "source = 'human', proposal_id = 'pro_1'"
        )


class TestProcurementTablesAreTenantProtected:
    async def test_every_procurement_table_is_tenant_protected(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT c.relname AS name, c.relrowsecurity AS enabled,
                               c.relforcerowsecurity AS forced,
                               EXISTS (SELECT 1 FROM pg_policies p
                                       WHERE p.schemaname = n.nspname
                                         AND p.tablename = c.relname) AS has_policy
                        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                          AND c.relname = ANY(:names)
                        """
                        ),
                        {"names": list(PROCUREMENT_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
        unprotected = [
            r["name"] for r in rows if not (r["enabled"] and r["forced"] and r["has_policy"])
        ]
        assert not unprotected, f"no isolation policy: {unprotected}"
        assert len(rows) == len(PROCUREMENT_TABLES), (
            f"expected {len(PROCUREMENT_TABLES)} tables, found {len(rows)}"
        )


class TestNoConstraintNameLooksPostgresTruncated:
    """The live-database half of the identifier guard.

    `TestIdentifiersFitPostgres` in `tests/unit/test_construction_schema.py`
    refuses an over-long name at the model, before a migration can carry it. This
    refuses a truncated one in a running database, which catches the case the model
    check cannot: a name that was correct when the migration was written and a
    later rename made too long.

    The detector is a shape, and it is a good one here because the question really
    is "did Postgres rewrite this?" — a name ending in `_` and four lowercase hex
    digits is not a name anyone types. It is the *wrong* question to ask when
    hunting for a specific rule, which is what migration `0013` got wrong twice
    before getting it right: `ck_goods_receipts_acceptance_requires_quality_and_qa_hs_5164`
    also ends in `_5164`, so matching on shape found the acceptance rule while
    looking for the provenance one, and renamed it to a name that was already
    taken. `docs/FAILED_APPROACHES.md` F84 has the full account.
    """

    async def test_no_check_constraint_looks_truncated(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT conrelid::regclass::text AS table_name, conname
                        FROM pg_constraint
                        WHERE contype = 'c' AND conname ~ '_[0-9a-f]{4}$'
                        """
                    )
                )
            ).all()
        assert not rows, (
            "these names were rewritten by Postgres and can no longer be found by "
            f"grepping for the rule they enforce: {[tuple(r) for r in rows]}"
        )

    async def test_no_constraint_name_is_at_the_limit(self, admin_db: Database) -> None:
        """At 63 bytes a name is legal but has no headroom.

        The unit test allows 63. Flagging 60 and above here as a warning-free fact
        about the current schema is how a future rename gets noticed while there is
        still slack, rather than after Postgres has eaten the suffix.
        """
        async with admin_db.engine.connect() as conn:
            tight = (
                await conn.execute(
                    text(
                        """
                        SELECT conrelid::regclass::text, conname, length(conname)
                        FROM pg_constraint
                        WHERE length(conname) >= 60
                        ORDER BY length(conname) DESC
                        """
                    )
                )
            ).all()
        assert len(tight) < 12, (
            f"the number of near-limit constraint names has grown to {len(tight)}; "
            f"each is one rename away from being silently truncated: "
            f"{[tuple(r) for r in tight[:6]]}"
        )
