"""Read-only Phase 2 component matrix, axe and keyboard/clipboard contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/02-components"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "final_ui_acceptance": False,
        "checks": [],
        "matrix": [],
        "blocked_requests": [],
        "errors": [],
        "console": [],
        "screenshots": [],
        "scope": "Phase 2 style guide; not full-app acceptance",
        "captured_at": datetime.now(UTC).isoformat(),
        "axe_version": "4.10.3",
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )

        def guard(route):
            r = route.request
            if urlparse(r.url).hostname not in {"localhost", "127.0.0.1"} or r.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                report["blocked_requests"].append({"url": r.url, "method": r.method})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        page.on(
            "console",
            lambda m: report["console"].append(m.text) if m.type in {"error", "warning"} else None,
        )
        page.goto(
            args.base + "/api/v1/ui?org=" + args.org + "#/_styleguide",
            wait_until="domcontentloaded",
        )
        page.wait_for_selector("#uiStyleguide")
        page.evaluate("async()=>{await document.fonts.ready}")
        check(
            "local-route-hidden-from-nav", page.locator('nav [href="#/_styleguide"]').count() == 0
        )
        saved = page.evaluate("localStorage.getItem('onx-ui-preferences-v1')")
        axe = Path("artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js").read_text()
        page.add_script_tag(content=axe)
        for palette in page.evaluate("UIPalettes.items.map(p=>p.id)"):
            for mode in ["light", "dark"]:
                for density in ["comfortable", "compact"]:
                    page.locator('[name="guide-palette"]').select_option(palette)
                    page.locator('[name="guide-mode"]').select_option(mode)
                    page.locator('[name="guide-density"]').select_option(density)
                    page.wait_for_function("!document.documentElement.dataset.uiSwitching")
                    page.evaluate("UI.hydrate()")
                    audit = page.evaluate("""async()=>{
                      const r=await axe.run(document.getElementById('uiStyleguide'));
                      return r.violations.map(v=>({id:v.id,impact:v.impact,
                        nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}));
                    }""")
                    name = f"{palette}-{mode}-{density}"
                    check(name + "-axe", not audit, audit)
                    button_height = page.locator(".ui-btn-primary.ui-btn-md").first.evaluate(
                        "e=>e.getBoundingClientRect().height"
                    )
                    check(
                        name + "-button-density",
                        abs(button_height - (32 if density == "compact" else 40)) < 1,
                        button_height,
                    )
                    check(
                        name + "-overflow",
                        page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                    )
                    check(
                        name + "-vietnamese-content",
                        page.evaluate("""()=>
                      [...document.querySelectorAll('#uiStyleguide input,#uiStyleguide textarea')]
                      .filter(el=>el.value.includes('ệ')).every(el=>el.scrollHeight<=el.clientHeight+2)
                    """),
                    )
                    path = name + ".png"
                    page.screenshot(path=str(args.out / path), full_page=True)
                    report["screenshots"].append(path)
                    if (palette, mode, density) in [
                        ("navy", "light", "comfortable"),
                        ("graphite", "dark", "compact"),
                    ]:
                        top = name + "-top.png"
                        page.screenshot(path=str(args.out / top))
                        report["screenshots"].append(top)
                    report["matrix"].append(
                        {"palette": palette, "mode": mode, "density": density, "axe": audit}
                    )
        # Tabs, disabled items and indicator work using actual keyboard events.
        tabs = page.locator('[role="tablist"] [role="tab"]')
        tabs.nth(0).focus()
        page.keyboard.press("ArrowRight")
        check(
            "tab-arrow-selection",
            tabs.nth(1).get_attribute("aria-selected") == "true"
            and tabs.nth(1).evaluate("e=>e===document.activeElement"),
        )
        page.keyboard.press("End")
        check("tab-skips-disabled", tabs.nth(1).evaluate("e=>e===document.activeElement"))
        page.keyboard.press("Home")
        check("tab-home", tabs.nth(0).get_attribute("aria-selected") == "true")
        check(
            "tab-indicator",
            page.locator('[role="tablist"] .ui-tab-indicator').evaluate(
                "e=>e.getBoundingClientRect().width"
            )
            > 0,
        )
        trigger = page.locator("[data-ui-menu-trigger]")
        trigger.focus()
        page.keyboard.press("ArrowDown")
        check(
            "menu-opens-first",
            page.locator('[role="menu"] button').nth(0).evaluate("e=>e===document.activeElement"),
        )
        page.keyboard.press("ArrowDown")
        check(
            "menu-skips-disabled",
            page.locator('[role="menu"] button').nth(2).evaluate("e=>e===document.activeElement"),
        )
        page.keyboard.press("Escape")
        check(
            "menu-escape-returns-focus",
            trigger.evaluate("e=>e===document.activeElement")
            and trigger.get_attribute("aria-expanded") == "false"
            and page.url.endswith("#/_styleguide"),
        )
        trigger.focus()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Tab")
        check(
            "menu-tab-to-next-control",
            page.locator("#uiStyleguide .ui-popover-host > button").evaluate(
                "e=>e===document.activeElement"
            ),
        )
        tooltip = page.locator("#uiStyleguide .ui-tooltip-host button")
        tooltip.focus()
        page.wait_for_function(
            "getComputedStyle(document.querySelector('#uiStyleguide .ui-tooltip')).opacity==='1'"
        )
        check(
            "tooltip-focus",
            page.locator('#uiStyleguide [role="tooltip"]').evaluate(
                "e=>getComputedStyle(e).visibility==='visible'"
            ),
        )
        audit = page.evaluate(
            "async()=> (await axe.run(document.querySelector('#uiStyleguide .ui-tooltip-host')))"
            ".violations.map(v=>v.id)"
        )
        check("tooltip-open-axe", not audit, audit)
        page.keyboard.press("Escape")
        check(
            "tooltip-escape",
            page.locator('#uiStyleguide [role="tooltip"]').evaluate(
                "e=>getComputedStyle(e).visibility==='hidden'"
            ),
        )
        for action in ["dialog", "drawer", "dialog-error"]:
            opener = page.locator(f'#uiStyleguide [data-ui-action="{action}"]').first
            opener.click()
            modal = page.locator("dialog[data-ui-modal][open]")
            check(action + "-opens", modal.count() == 1)
            page.wait_for_timeout(180)
            page.add_script_tag(content=axe)
            audit = page.evaluate(
                "async()=> (await axe.run(document.querySelector('dialog[data-ui-modal][open]')))"
                ".violations.map(v=>({id:v.id,impact:v.impact}))"
            )
            check(action + "-axe", not audit, audit)
            page.keyboard.press("Shift+Tab")
            check(
                action + "-focus-contained",
                page.evaluate("!!document.activeElement.closest('dialog[data-ui-modal]')"),
            )
            if action == "dialog-error":
                modal.locator('[data-ui-action="confirm-modal"]').click()
                modal.locator("[data-ui-modal-error]").wait_for()
                check(
                    "dialog-failure-preserves-input",
                    modal.locator("input").input_value() == "ệ ữ ặ ẫ ỗ ử"
                    and modal.locator('[data-ui-action="confirm-modal"]').is_enabled(),
                )
            page.screenshot(path=str(args.out / (action + ".png")))
            report["screenshots"].append(action + ".png")
            page.keyboard.press("Escape")
            page.wait_for_function("!document.querySelector('dialog[data-ui-modal]')")
            check(
                action + "-escape-restores-focus",
                opener.evaluate("e=>e===document.activeElement")
                and page.locator("dialog[data-ui-modal]").count() == 0
                and page.url.endswith("#/_styleguide"),
            )
        popover = page.locator("#uiStyleguide .ui-popover-host > button")
        page.evaluate("UI.dialog({title:'Mẫu ngoài'}); UI.dialog({title:'Mẫu trong'})")
        page.keyboard.press("Escape")
        page.wait_for_function("document.querySelectorAll('dialog[data-ui-modal]').length===1")
        check(
            "nested-modal-retains-scroll-lock",
            page.evaluate("document.documentElement.style.overflow==='hidden'"),
        )
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('dialog[data-ui-modal]')")
        check(
            "nested-modal-restores-scroll",
            page.evaluate("document.documentElement.style.overflow!=='hidden'"),
        )
        popover.click()
        page.wait_for_timeout(180)
        check(
            "popover-open",
            page.locator("#uiStyleguide .ui-popover").evaluate('e=>e.matches(":popover-open")'),
        )
        audit = page.evaluate(
            "async()=> (await axe.run(document.querySelector('#uiStyleguide .ui-popover')))"
            ".violations.map(v=>v.id)"
        )
        check("popover-open-axe", not audit, audit)
        page.keyboard.press("Escape")
        check(
            "popover-close-focus",
            popover.evaluate("e=>e===document.activeElement")
            and page.url.endswith("#/_styleguide"),
        )
        log = page.locator("#uiStyleguide .ui-log")
        check(
            "log-font-size",
            float(log.locator("code").evaluate("e=>parseFloat(getComputedStyle(e).fontSize)"))
            >= 12,
        )
        log.locator("[data-ui-log-filter]").fill("Phê duyệt")
        check("log-filter", log.locator("code").inner_text().startswith("Phê duyệt:"))
        log.locator("[data-ui-log-wrap]").uncheck()
        check("log-wrap", log.locator("pre").get_attribute("data-wrap") == "false")
        log.locator('[data-ui-action="copy-log"]').click()
        clipboard = page.evaluate("navigator.clipboard.readText()")
        check("log-copies-full-source", "Dòng dài" in clipboard and "Mẫu / Sample" in clipboard)
        page.locator("[data-ui-copy]").click()
        check(
            "copy-id",
            page.evaluate("navigator.clipboard.readText()") == "sample_mep_0123456789_abcdefgh",
        )
        audit = page.evaluate(
            "async()=> (await axe.run(document.getElementById('uiToastRegion')))"
            ".violations.map(v=>v.id)"
        )
        check("toast-axe", not audit, audit)
        page.locator("#uiToastRegion button").first.click()
        page.locator("#uiToastRegion").evaluate("e=>e.remove()")
        table = page.locator("#uiStyleguide .ui-table")
        table.locator('[data-ui-sort="2"]').click()
        check(
            "table-sort",
            table.locator("tbody tr").first.locator("td").last.inner_text() == "9"
            and table.locator("th").nth(2).get_attribute("aria-sort") == "ascending",
        )
        # French/English etc are not invented; existing locale switch remains VI/EN.
        page.locator('[data-ui-action="language"]').click()
        check(
            "english-components",
            page.locator("#uiStyleguide h1").inner_text() == "Component style guide"
            and page.locator("#uiStyleguide").get_attribute("lang") == "en",
        )
        audit = page.evaluate(
            "async()=> (await axe.run(document.getElementById('uiStyleguide')))"
            ".violations.map(v=>({id:v.id,impact:v.impact}))"
        )
        check("english-axe", not audit, audit)
        page.locator('[data-ui-action="language"]').click()
        for width in [360, 390]:
            page.set_viewport_size({"width": width, "height": 844})
            page.evaluate("UI.hydrate()")
            check(
                f"mobile-{width}-overflow",
                page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )
            audit = page.evaluate(
                "async()=> (await axe.run(document.getElementById('uiStyleguide')))"
                ".violations.map(v=>({id:v.id,impact:v.impact}))"
            )
            check(f"mobile-{width}-axe", not audit, audit)
            file = f"mobile-{width}.png"
            page.screenshot(path=str(args.out / file), full_page=True)
            report["screenshots"].append(file)
            top = f"mobile-{width}-top.png"
            page.screenshot(path=str(args.out / top))
            report["screenshots"].append(top)
        page.locator('#uiStyleguide [data-ui-action="drawer"]').click()
        page.wait_for_timeout(180)
        rect = page.locator("dialog[data-ui-modal]").bounding_box()
        check(
            "mobile-bottom-sheet",
            abs(rect["x"]) < 1
            and abs(rect["width"] - page.viewport_size["width"]) < 1
            and abs(rect["y"] + rect["height"] - 844) < 2,
            rect,
        )
        content_height = page.locator("dialog[data-ui-modal]").evaluate(
            "e=>[...e.children].reduce((h,c)=>h+c.getBoundingClientRect().height,0)"
        )
        check(
            "mobile-sheet-fits-content",
            rect["height"] <= content_height + 3,
            {"height": rect["height"], "content": content_height},
        )
        page.screenshot(path=str(args.out / "mobile-drawer.png"))
        report["screenshots"].append("mobile-drawer.png")
        page.keyboard.press("Escape")
        page.emulate_media(reduced_motion="reduce")
        check(
            "reduced-motion",
            page.evaluate("document.getAnimations().filter(a=>a.playState==='running').length")
            == 0,
        )
        page.set_viewport_size({"width": 720, "height": 900})
        page.evaluate("document.getElementById('uiStyleguide').style.fontSize='28px'")
        check(
            "large-text-overflow", page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        )
        check(
            "preview-does-not-save",
            page.evaluate("localStorage.getItem('onx-ui-preferences-v1')") == saved,
        )
        page.goto(
            args.base + "/api/v1/ui?org=" + args.org + "#/give", wait_until="domcontentloaded"
        )
        page.wait_for_selector("#giveList .row")
        check("existing-work-route", True)
        report["source_sha256"] = {
            str(f): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(Path("src/ai_orchestrator/web").rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts
        }
        context.close()
        touch_context = browser.new_context(
            is_mobile=True, has_touch=True, viewport={"width": 390, "height": 844}
        )
        touch_context.route("**/*", guard)
        touch_page = touch_context.new_page()
        touch_page.goto(
            args.base + "/api/v1/ui?org=" + args.org + "#/_styleguide",
            wait_until="domcontentloaded",
        )
        touch_page.wait_for_selector("#uiStyleguide")
        touch_page.locator('[name="guide-density"]').select_option("compact")
        check(
            "coarse-compact-targets",
            touch_page.evaluate("""()=>
            [...document.querySelectorAll('#uiStyleguide .ui-btn,#uiStyleguide .ui-choice')]
            .filter(el=>el.getClientRects().length)
            .every(el=>el.getBoundingClientRect().height>=44)
        """),
        )
        touch_context.close()
        browser.close()
    check("no-writes-or-external-hosts", not report["blocked_requests"])
    check("no-browser-errors-or-warnings", not report["errors"] and not report["console"])
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "stage-2-gate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "checks": len(report["checks"]),
                "failed": [c for c in report["checks"] if not c["passed"]],
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["gate"] == "passed" else 1)


if __name__ == "__main__":
    main()
