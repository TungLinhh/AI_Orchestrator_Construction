"""Browser regression for the procurement intake form (no model, no writes).

This drives the *served* console with ordinary user interactions -- fill, select,
click, blur -- and asserts that quotation values stay bound to the right
supplier/material pair while a person works. It never submits the form, so it
touches no database row and spends no model budget.

It exists because the console used to re-render the whole quotation panel on any
`change` event from a material or supplier row. The last field a person edits
holds focus; moving to the first quote field blurs it, fires `change`, and the
panel was rebuilt from scratch -- destroying the field the person was about to
type into. Normal entry lost every quotation value. A substring test of the
source could not see that; only a browser driving the real page can.

Usage:
    python scripts/procurement_intake_form_regression.py \
        --org <org-id> [--base http://127.0.0.1:8099] [--out <dir>]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

MATERIALS = [
    ("PIPE-01", "Ống cấp nước", "100", "m", "Ống PPR PN20 (fixture)", False),
    ("CABLE-01", "Cáp điện", "200", "m", "Cáp đồng 4x16mm2 (fixture)", False),
    ("PUMP-01", "Bơm nước", "2", "bộ", "Bơm 10m3/h, 30m cột áp (fixture)", True),
]
SUPPLIERS = ["NCC-A", "NCC-B", "NCC-C"]
PRICES = [
    [60000, 180000, 18000000],
    [40000, 140000, 14000000],
    [65000, 175000, 18500000],
]


async def _fill_row(page, row, values):
    for name, value in values:
        loc = row.locator(f'[name="{name}"]')
        if name in ("long_lead", "legal_valid", "spec_compliant"):
            await loc.select_option(
                "true" if value is True else "false" if value is False else str(value)
            )
        else:
            await loc.fill(str(value))


async def _dump_quotes(page):
    # Key by the labels a person sees, so the check is independent of how the page
    # keys its internal state, and of whether the panel groups lines per supplier
    # (the fixed markup) or places them flat under the panel (the old markup).
    return await page.evaluate(
        "() => Array.from(document.querySelectorAll("
        "'#intake-quotes [data-intake-quote]')).map(n => {"
        "  const group = n.closest('[data-quote-group]');"
        "  let h3 = group ? group.querySelector('h3') : null;"
        "  if (!h3) { let s = n.previousElementSibling;"
        "    while (s && s.tagName !== 'H3') s = s.previousElementSibling; h3 = s; }"
        "  const h4 = (n.querySelector('h4') || {}).textContent || '';"
        "  return {key: ((h3 && h3.textContent) || '').trim() + ':' + h4.split('·')[0].trim(),"
        "    vals: Object.fromEntries(Array.from(n.querySelectorAll('input,select'))"
        "      .map(i => [i.name, i.value]))};"
        "})"
    )


def _quote_node(page, supplier_index, material_index):
    # Locate by document order across the whole panel rather than by the group
    # wrapper. The fixed markup and the old flat markup both list lines supplier
    # by supplier, material by material, so a value that the old code discarded in
    # a panel rebuild fails the price assertions below instead of timing out on a
    # selector that only the new markup carries.
    return page.locator("#intake-quotes [data-intake-quote]").nth(
        supplier_index * len(MATERIALS) + material_index
    )


async def run(args) -> dict:
    receipt: dict = {
        "kind": "procurement_intake_form_regression",
        "base": args.base,
        "org": args.org,
        "page_errors": [],
    }
    async with async_playwright() as pw:
        launch = {"args": ["--no-sandbox"]}
        if args.chromium:
            launch["executable_path"] = args.chromium
        browser = await pw.chromium.launch(**launch)
        page = await browser.new_page(viewport={"width": 1440, "height": 1400})
        page.on("pageerror", lambda error: receipt["page_errors"].append(str(error)))
        try:
            await page.goto(
                f"{args.base}/api/v1/ui?org={args.org}#/processes/workflows",
                wait_until="domcontentloaded",
            )
            await page.wait_for_selector("#procurement-intake", state="attached", timeout=30000)
            await page.evaluate(
                "document.querySelector('#procurement-intake').closest('details').open = true"
            )
            await page.wait_for_selector("#procurement-intake", state="visible", timeout=30000)
            form = page.locator("#procurement-intake")
            for name, value in [
                ("boss_brief", "SYNTHETIC browser regression of the intake form; never submitted."),
                ("boq_source", "synthetic:regression"),
                ("material_review_owner", "Synthetic reviewer"),
                ("need_date", "2026-11-20"),
                ("g3_date", "2026-12-01"),
            ]:
                await form.locator(f'[name="{name}"]').fill(value)
            await form.locator('[name="synthetic"]').select_option("true")

            for _ in range(len(MATERIALS) - 1):
                await page.click("#intake-add-material")
            mrows = page.locator("#intake-materials [data-intake-material]")
            for i, (mid, name, qty, unit, spec, long_lead) in enumerate(MATERIALS):
                await _fill_row(
                    page,
                    mrows.nth(i),
                    [
                        ("material_id", mid),
                        ("name", name),
                        ("quantity", qty),
                        ("unit", unit),
                        ("technical_spec", spec),
                        ("long_lead", long_lead),
                    ],
                )
            srows = page.locator("#intake-suppliers [data-intake-supplier]")
            for i, sid in enumerate(SUPPLIERS):
                await _fill_row(
                    page,
                    srows.nth(i),
                    [
                        ("supplier_id", sid),
                        ("legal_valid", True),
                        ("legal_ref", f"synthetic:legal:{sid}"),
                        ("financial_ref", f"synthetic:finance:{sid}"),
                        ("hse_ref", f"synthetic:hse:{sid}"),
                        ("quote_ref", f"synthetic:quotation:{sid}"),
                        ("financial_health", "Fixture"),
                        ("hse", "Fixture"),
                    ],
                )

            # Count direct child replacements of the quotation panel while quotes
            # are typed. A field-edit label refresh only mutates descendants, so
            # it is not counted; a full panel rebuild replaces direct children.
            await page.evaluate(
                "() => { window.__quoteMutations = 0;"
                "  new MutationObserver(m => { window.__quoteMutations += m.length; })"
                "    .observe(document.getElementById('intake-quotes'), {childList:true});"
                "}"
            )
            for si in range(len(SUPPLIERS)):
                for mi in range(len(MATERIALS)):
                    node = _quote_node(page, si, mi)
                    await node.locator('[name="unit_price"]').fill(str(PRICES[si][mi]))
                    await node.locator('[name="delivery_days"]').fill(str(10 + si))
                    await node.locator('[name="warranty_months"]').fill("24")
                    await node.locator('[name="certificate_ref"]').fill(f"synthetic:cert:{si}:{mi}")
                    await node.locator('[name="spec_compliant"]').select_option("true")
            receipt["quote_mutations_during_entry"] = await page.evaluate(
                "() => window.__quoteMutations"
            )
            after_fill = await _dump_quotes(page)

            expected = {
                f"{SUPPLIERS[si]}:{MATERIALS[mi][0]}": str(PRICES[si][mi])
                for si in range(len(SUPPLIERS))
                for mi in range(len(MATERIALS))
            }

            # Editing a non-identity field must not disturb unrelated quotations.
            await mrows.nth(0).locator('[name="quantity"]').fill("150")
            await mrows.nth(0).locator('[name="name"]').fill("Ống cấp nước (sửa)")
            await page.locator('[name="material_review_owner"]').click()
            after_edit = await _dump_quotes(page)

            # Changing a business ID keeps that row's quotation (row identity is stable).
            await mrows.nth(2).locator('[name="material_id"]').fill("PUMP-02")
            await page.locator('[name="material_review_owner"]').click()
            after_id = await _dump_quotes(page)

            # Removing a row drops its quotation; re-adding the same ID must not inherit it.
            await mrows.nth(1).locator("[data-remove]").click()
            after_remove = await _dump_quotes(page)
            await page.click("#intake-add-material")
            await (
                page.locator("#intake-materials [data-intake-material]")
                .last.locator('[name="material_id"]')
                .fill("CABLE-01")
            )
            await page.locator('[name="material_review_owner"]').click()
            after_readd = await _dump_quotes(page)

            # Existing validation must still hold: 0 price and fractional days are invalid.
            first = page.locator("#intake-quotes [data-intake-quote]").first
            await first.locator('[name="unit_price"]').fill("0")
            await page.locator('[name="material_review_owner"]').click()
            zero = await first.locator('[name="unit_price"]').evaluate(
                "el => ({valid: el.checkValidity(), value: el.value})"
            )
            await first.locator('[name="delivery_days"]').fill("7.5")
            await page.locator('[name="material_review_owner"]').click()
            frac = await first.locator('[name="delivery_days"]').evaluate(
                "el => ({valid: el.checkValidity(), value: el.value})"
            )

            def prices(rows):
                return {row["key"]: row["vals"].get("unit_price") for row in rows}

            fill_map, edit_map = prices(after_fill), prices(after_edit)
            id_map, remove_map, readd_map = (
                prices(after_id),
                prices(after_remove),
                prices(after_readd),
            )
            receipt["checks"] = {
                "quotes_are_not_rebuilt_during_entry": receipt["quote_mutations_during_entry"] == 0,
                "all_prices_bound_after_fill": fill_map == expected,
                "non_identity_edit_preserves_prices": edit_map == expected,
                "identity_change_preserves_prices": all(
                    id_map.get(f"{SUPPLIERS[si]}:{MATERIALS[mi][0]}") == str(PRICES[si][mi])
                    for si in range(len(SUPPLIERS))
                    for mi in (0, 1)
                ),
                "removed_row_quotes_dropped": not any(k.endswith(":CABLE-01") for k in remove_map),
                "unrelated_prices_survive_remove": remove_map.get("NCC-A:PIPE-01") == "60000",
                "readded_row_not_resurrected": readd_map.get("NCC-A:CABLE-01") == "",
                "zero_price_rejected": zero["valid"] is False,
                "fractional_days_rejected": frac["valid"] is False,
                "no_page_errors": not receipt["page_errors"],
            }
            receipt["passed"] = all(receipt["checks"].values())
        finally:
            await browser.close()
    return receipt


async def main(args) -> None:
    receipt = await run(args)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "receipt.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False))
    print(
        json.dumps(
            {k: v for k, v in receipt.items() if k in ("passed", "checks", "page_errors")},
            indent=2,
            ensure_ascii=False,
        )
    )
    if not receipt["passed"]:
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8099")
    parser.add_argument("--org", required=True)
    parser.add_argument(
        "--chromium", default=None, help="Chromium binary; omit to use Playwright's"
    )
    parser.add_argument("--out", type=Path, default=None)
    asyncio.run(main(parser.parse_args()))
