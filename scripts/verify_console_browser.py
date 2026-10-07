"""Check the served console in Chromium, including every palette and small screens.

Run with ``uv run --with playwright python scripts/verify_console_browser.py
--base http://127.0.0.1:8100 --org <id> --chromium <existing executable>``.
The probe changes browser-local preferences only; it never approves or starts work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from playwright.sync_api import sync_playwright


def luminance(rgb: list[float]) -> float:
    channels = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * weight for v, weight in zip(channels, (0.2126, 0.7152, 0.0722), strict=True))


def contrast(first: str, second: str) -> float:
    def parse(value: str) -> list[float]:
        value = value.lstrip("#")
        return [int(value[i : i + 2], 16) / 255 for i in (0, 2, 4)]

    a, b = sorted([luminance(parse(first)), luminance(parse(second))])
    return (b + 0.05) / (a + 0.05)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium")
    parser.add_argument("--out", type=Path, default=Path(".devdata/ui/appearance"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {"palettes": [], "routes": [], "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=args.chromium)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.on("pageerror", lambda error: report["errors"].append(str(error)))
        url = args.base + "/api/v1/ui?org=" + args.org
        page.goto(url + "#/settings/appearance", wait_until="domcontentloaded")
        page.locator("#ui-preferences").wait_for()
        # A real workflow may have emitted a completion notification during boot.
        page.locator(".ui-toast").last.wait_for(state="hidden", timeout=15000)
        page.evaluate("toast('Browser-only notification probe', false)")
        page.locator(".ui-toast").last.wait_for(state="visible")
        page.locator(".ui-toast").last.wait_for(state="hidden", timeout=15000)
        report["toast_visibility"] = True
        for mode in ("light", "dark"):
            for palette in page.evaluate("UIPalettes.items.map(p => p.id)"):
                page.locator('[name="theme"]').select_option(mode)
                page.locator(f'[name="palette"][value="{palette}"]').check()
                page.locator('#ui-preferences button[type="submit"]').click()
                page.wait_for_function(
                    "() => { const s=document.querySelector('#ui-preferences .form-result'); "
                    "return s.textContent.includes('saved') || s.textContent.includes('Đã lưu'); }"
                )
                tokens = page.evaluate(
                    "UIPalettes.tokens(UIPreferences.get().palette,"
                    " document.documentElement.dataset.colorMode)"
                )
                ratios = {
                    "body": contrast(tokens["text"], tokens["surface"]),
                    "secondary": contrast(tokens["text-2"], tokens["surface-2"]),
                    "muted": contrast(tokens["text-3"], tokens["nav-bg"]),
                    "link": contrast(tokens["accent"], tokens["surface"]),
                    "selected": contrast(tokens["accent"], tokens["accent-soft"]),
                    "button": contrast(tokens["on-accent"], tokens["accent"]),
                    "button_hover": contrast(tokens["on-accent"], tokens["accent-2"]),
                    "success": contrast(tokens["ok"], tokens["ok-soft"]),
                    "warning": contrast(tokens["warn"], tokens["warn-soft"]),
                    "error": contrast(tokens["late"], tokens["late-soft"]),
                }
                assert min(ratios.values()) >= 4.5, (palette, mode, ratios)
                page.mouse.move(0, 0)
                actual = page.locator('#ui-preferences button[type="submit"]').evaluate(
                    "el => ({color: getComputedStyle(el).color,"
                    " background: getComputedStyle(el).backgroundColor})"
                )
                expected = page.evaluate(
                    "t => {const el=document.createElement('span'); document.body.append(el);"
                    " el.style.color=t['on-accent']; const c=getComputedStyle(el).color;"
                    " el.style.color=t.accent; const b=getComputedStyle(el).color;"
                    " el.remove(); return {color:c, background:b};}",
                    tokens,
                )
                assert actual == expected, (palette, mode, actual, expected)
                page.reload()
                page.locator("#ui-preferences").wait_for()
                assert page.locator(f'[name="palette"][value="{palette}"]').is_checked()
                assert page.locator('[name="theme"]').input_value() == mode
                report["palettes"].append(
                    {"palette": palette, "mode": mode, "contrast": ratios, "persisted": True}
                )
                page.screenshot(path=str(args.out / f"{palette}-{mode}.png"), full_page=True)
        page.evaluate('UIPreferences.save({theme:"auto",palette:"navy"})')
        page.emulate_media(color_scheme="dark")
        page.wait_for_function('document.documentElement.dataset.colorMode === "dark"')
        page.emulate_media(color_scheme="light")
        page.wait_for_function('document.documentElement.dataset.colorMode === "light"')
        report["system_theme_changes"] = True
        # Intercept writes at the network boundary: prove the real form payload
        # without creating fictional records in the operator's organization.
        captured = {}

        def capture_write(route):
            captured["body"] = route.request.post_data_json
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"detail": "Browser probe intercepted; no record created"}),
            )

        page.goto(url + "#/processes/workflows", wait_until="domcontentloaded")
        page.locator("#procurement-intake").wait_for(state="attached")
        page.locator("#procurement-intake").evaluate("el => el.parentElement.open = true")
        procurement = page.locator("#procurement-intake")
        for name, value in {
            "boss_brief": "Browser-only fictional procurement form verification",
            "boq_source": "browser-probe BOQ",
            "material_review_owner": "browser-probe reviewer",
            "need_date": "2026-11-01",
            "g3_date": "2026-11-15",
        }.items():
            procurement.locator(f'[name="{name}"]').fill(value)
        material = procurement.locator("[data-intake-material]")
        for name, value in {
            "name": "Browser-only valve",
            "quantity": "5.5",
            "unit": "piece",
            "technical_spec": "Browser-only specification PN16",
        }.items():
            material.locator(f'[name="{name}"]').fill(value)
        material.locator('[name="unit"]').blur()
        for supplier in procurement.locator("[data-intake-supplier]").all():
            for name in ("legal_ref", "financial_ref", "hse_ref", "quote_ref"):
                supplier.locator(f'[name="{name}"]').fill("browser-probe source")
            for name in ("financial_health", "hse"):
                supplier.locator(f'[name="{name}"]').fill("browser-probe unverified")
        procurement.locator("[data-intake-supplier]").last.locator('[name="hse"]').blur()
        for quote in procurement.locator("[data-intake-quote]").all():
            for name, value in {
                "unit_price": "125000",
                "delivery_days": "7",
                "warranty_months": "12",
            }.items():
                quote.locator(f'[name="{name}"]').fill(value)
        page.route("**/api/v1/workflows/procurement", capture_write)
        with page.expect_response("**/api/v1/workflows/procurement"):
            procurement.locator('button[type="submit"]').click()
        payload = captured["body"]
        assert len(payload["boq"]) == 1 and len(payload["suppliers"]) == 3
        assert payload["boq"][0]["quantity"] == 5.5
        assert all(
            len(s["quotes"]) == 1
            and s["quotes"][0]["quantity"] == 5.5
            and s["quotes"][0]["unit_price"] == 125000
            and s["quotes"][0]["spec_compliant"] is False
            for s in payload["suppliers"]
        )
        page.unroute("**/api/v1/workflows/procurement", capture_write)
        report["procurement_form_payload"] = {"materials": 1, "suppliers": 3, "written": False}
        page.goto(url + "#/processes/provision", wait_until="domcontentloaded")
        cycle = page.locator('#blueprint-draft [name="cycle"]')
        cycle.wait_for()
        assert cycle.locator("option").count() == 4
        page.locator('#blueprint-draft [name="department"]').select_option("procurement")
        assert cycle.is_disabled() and not cycle.is_visible()
        page.locator('#blueprint-draft [name="department"]').select_option("hr")
        assert cycle.is_enabled() and cycle.is_visible()
        report["hr_cycle_switch"] = True
        workflows = page.evaluate("apiGet('/workflows').then(r => r.items)")
        campaign = next((r["id"] for r in workflows if r["kind"] == "mep_hiring"), None)
        procurement = next((r["id"] for r in workflows if r["kind"] == "procurement"), None)
        routes = [
            "#/settings/appearance",
            "#/work",
            "#/departments",
            "#/processes/workflows",
            "#/processes/provision",
        ]
        if campaign:
            routes.append("#/processes/workflows/" + campaign)
        if procurement:
            routes.append("#/processes/workflows/" + procurement)
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 900})
            for index, route in enumerate(routes):
                page.goto(url + route, wait_until="domcontentloaded")
                page.wait_for_function(
                    "() => !document.querySelector('#pageError').hidden || "
                    "[...document.querySelectorAll('.view:not([hidden]) .empty')]"
                    ".every(el => !/Loading…|Đang tải…/.test(el.textContent))"
                )
                assert page.locator("#pageError").is_hidden(), page.locator(
                    "#pageError"
                ).inner_text()
                if route.endswith("appearance"):
                    page.locator("#ui-preferences").wait_for()
                if campaign and route.endswith(campaign):
                    page.locator(".campaign-workspace").wait_for()
                    assert page.locator(".campaign-stage").count() >= 20
                    page.get_by_text("Create a revision for review", exact=True).click()
                    page.locator('#workflow-revision [name="position"]').fill(
                        "Draft preserved while reading"
                    )
                    page.wait_for_timeout(11000)
                    assert (
                        page.locator('#workflow-revision [name="position"]').input_value()
                        == "Draft preserved while reading"
                    )
                    revision = page.locator("#workflow-revision")
                    revision.locator('[name="reason"]').fill("Browser-only draft verification")
                    source_hash = revision.locator('[name="expected_hash"]').input_value()
                    endpoint = "**/api/v1/workflows/" + campaign + "/revisions"
                    page.route(endpoint, capture_write)
                    with page.expect_response(endpoint):
                        revision.locator('button[type="submit"]').click()
                    assert captured["body"]["expected_hash"] == source_hash
                    assert len(source_hash) == 64
                    assert captured["body"]["brief"]["position"] == "Draft preserved while reading"
                    page.unroute(endpoint, capture_write)
                    report["revision_form_payload"] = {"hash_bound": True, "written": False}
                    page.reload()
                    page.locator(".campaign-workspace").wait_for()
                if procurement and route.endswith(procurement):
                    handover = page.locator('[data-campaign-anchor="delivery"]')
                    handover.wait_for()
                    assert "Onboarding" not in handover.inner_text()
                    before = page.url
                    handover.click()
                    page.wait_for_function(
                        "() => {const r=document.querySelector('#campaign-stage-delivery')"
                        ".getBoundingClientRect(); return r.top >= 0 && r.top < innerHeight;}"
                    )
                    assert page.url == before
                    report["procurement_exact_handover_link"] = True
                overflow = page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                assert not overflow, (width, route, "horizontal overflow")
                assert not page.locator("#managementBody > .error").count(), (width, route)
                report["routes"].append({"route": route, "width": width, "overflow": overflow})
                page.evaluate("scrollTo(0, 0)")
                page.screenshot(path=str(args.out / f"route-{index}-{width}.png"), full_page=True)
        if campaign:
            # Fixture GETs exercise human stopping points; no model or writes.
            probe = {
                "id": campaign,
                "title": "Browser-only conditional form fixture",
                "kind": "mep_hiring",
                "mode": "live",
                "status": "blocked",
                "completed": 10,
                "total": 20,
                "sources": [],
                "summary": {},
                "evidence": {
                    "real_model_calls": 0,
                    "fake_model_calls": 0,
                    "events": 0,
                    "audits": 0,
                },
                "control": {"state": "waiting", "paused": False, "feedback": {}},
                "stages": [
                    {
                        "id": "browser_interview_1",
                        "key": "interview_technical",
                        "title": "Interview fixture",
                        "status": "waiting_for_input",
                        "output": {},
                    }
                ],
            }
            endpoint = "**/api/v1/workflows/" + campaign

            def fixture_read(route):
                assert route.request.method == "GET"
                route.fulfill(status=200, content_type="application/json", body=json.dumps(probe))

            def refresh_form(form_id):
                old = page.locator("#" + form_id).element_handle()
                page.locator("#workflow-refresh").click()
                page.wait_for_function("el => !el.isConnected", arg=old)
                page.locator("#" + form_id).wait_for()

            page.route(endpoint, fixture_read)
            page.goto(url + "#/processes/workflows/" + campaign)
            page.reload()
            page.locator('#workflow-evidence [name="source"]').fill("browser-only draft source")
            refresh_form("workflow-evidence")
            assert (
                page.locator('#workflow-evidence [name="source"]').input_value()
                == "browser-only draft source"
            )
            probe["stages"][0].update(id="browser_interview_2", key="interview_hr")
            refresh_form("workflow-evidence")
            assert page.locator('#workflow-evidence [name="source"]').input_value() == ""
            probe["stages"] = [
                {
                    "id": "browser_offer",
                    "key": "offer",
                    "title": "Offer fixture",
                    "status": "completed",
                    "output": {},
                    "artifact_hash": "a" * 64,
                },
                {
                    "id": "browser_acceptance",
                    "key": "offer_acceptance",
                    "title": "Acceptance fixture",
                    "status": "waiting_for_input",
                    "output": {},
                },
            ]
            refresh_form("workflow-evidence")
            assert page.locator('#workflow-evidence [name="offer_hash"]').input_value() == "a" * 64
            page.locator('#workflow-evidence [name="source"]').fill(
                "browser-only acceptance source"
            )
            probe["stages"][0]["artifact_hash"] = "b" * 64
            refresh_form("workflow-evidence")
            assert page.locator('#workflow-evidence [name="offer_hash"]').input_value() == "b" * 64
            assert page.locator('#workflow-evidence [name="source"]').input_value() == ""
            probe["control"] = {
                "state": "paused",
                "paused": True,
                "feedback": {
                    "state": "awaiting_confirmation",
                    "revision": 1,
                    "message": "Browser fixture feedback",
                    "assessment": {
                        "understanding": "Browser fixture",
                        "questions": ["Which source?"],
                        "plan": [],
                        "skill_lesson": "Browser fixture",
                    },
                },
            }
            refresh_form("workflow-evidence")
            page.locator('#workflow-feedback-answer [name="answers"]').fill(
                "Browser-only draft answer"
            )
            refresh_form("workflow-feedback-answer")
            assert (
                page.locator('#workflow-feedback-answer [name="answers"]').input_value()
                == "Browser-only draft answer"
            )
            probe["control"]["feedback"]["revision"] = 2
            refresh_form("workflow-feedback-answer")
            assert page.locator('#workflow-feedback-answer [name="answers"]').input_value() == ""
            report["conditional_form_fixtures"] = {
                "draft_after_blur": True,
                "stage_change_clears": True,
                "offer_hash_change_clears": True,
                "feedback_revision_change_clears": True,
                "written": False,
            }
            page.unroute(endpoint, fixture_read)
        assert not report["errors"], report["errors"]
        browser.close()
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "palettes": len(report["palettes"]),
                "routes": len(report["routes"]),
                "errors": report["errors"],
                "report": str(args.out / "report.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
