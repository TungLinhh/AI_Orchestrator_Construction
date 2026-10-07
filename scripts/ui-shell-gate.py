"""Read-only Phase 3 shell geometry, preferences and keyboard gate."""

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
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/03-shell"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "scope": "Phase 3 shell only",
        "final_ui_acceptance": False,
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "screenshots": [],
        "errors": [],
        "blocked_requests": [],
        "fixtures": [
            "One browser-only unread notification",
            "Auth-on HTML variant; no credentials submitted",
            "Workspace-name GET failure",
        ],
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(viewport={"width": 1440, "height": 900})

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
        context.add_init_script(path="artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js")
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        url = args.base + "/api/v1/ui?org=" + args.org + "#/give"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_function(
            "document.getElementById('workspaceName').textContent==='Autonomous Demo Company'"
        )
        page.evaluate("async()=>{await document.fonts.ready}")
        page.wait_for_function("document.querySelector('#sidebar [aria-current=page]')")

        def audit(name, selectors):
            violations = page.evaluate(
                """async selectors=>{
              const r=await axe.run({include:selectors.map(s=>[s])});
              return r.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>
                ({target:n.target,summary:n.failureSummary}))}));
            }""",
                selectors,
            )
            check(name, not violations, violations)

        def shot(name):
            page.screenshot(path=str(args.out / (name + ".png")))
            report["screenshots"].append(name + ".png")

        def open_appearance():
            page.locator("#appearanceBtn").click()
            page.wait_for_function(
                "document.getElementById('appearancePanel').matches(':popover-open')"
            )

        def stable():
            page.wait_for_function("!document.documentElement.dataset.uiSwitching")
            page.wait_for_timeout(180)

        def fit(selector):
            return page.locator(selector).evaluate(
                "e=>{const r=e.getBoundingClientRect();"
                "return r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight}"
            )

        for palette in page.evaluate("UIPalettes.items.map(p=>p.id)"):
            for mode in ["light", "dark"]:
                for density in ["comfortable", "compact"]:
                    name = f"{palette}-{mode}-{density}"
                    open_appearance()
                    page.locator(f'[data-shell-pref="theme"][data-shell-value="{mode}"]').click()
                    page.locator(f'[data-shell-palette="{palette}"]').click()
                    page.locator(
                        f'[data-shell-pref="density"][data-shell-value="{density}"]'
                    ).click()
                    stable()
                    check(
                        name + "-saved",
                        page.evaluate('JSON.parse(localStorage.getItem("onx-ui-preferences-v1"))')
                        == page.evaluate("UIPreferences.get()"),
                    )
                    check(
                        name + "-desktop-layout",
                        page.evaluate("""()=>{
                      const nav=document.getElementById('sidebar').getBoundingClientRect();
                      const main=document.querySelector('.main').getBoundingClientRect();
                      return nav.width===216 && main.left===216 && main.width>1000;
                    }"""),
                    )
                    check(name + "-menu-fit", fit("#appearancePanel"))
                    audit(
                        name + "-shell-axe",
                        ["#sidebar", "#crumbs", "#authwarn", "#appearancePanel"],
                    )
                    shot(name)
                    page.keyboard.press("Escape")
                    check(
                        name + "-escape-focus",
                        page.locator("#appearanceBtn").evaluate("e=>e===document.activeElement"),
                    )
                    check(
                        name + "-overflow",
                        page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                    )

        page.locator("#sidebarToggle").click()
        page.wait_for_timeout(250)
        check("collapsed-width", abs(page.locator("#sidebar").bounding_box()["width"] - 64) < 1)
        check(
            "collapsed-persisted",
            page.evaluate('localStorage.getItem("ao-sidebar-collapsed")') == "true",
        )
        page.locator("#sidebar a").first.focus()
        page.wait_for_timeout(220)
        check(
            "collapsed-keyboard-tooltip",
            page.locator(".ui-tooltip-host:focus-within [role=tooltip]").evaluate(
                "e=>getComputedStyle(e).opacity"
            )
            == "1",
        )
        shot("collapsed")
        page.keyboard.press("Escape")
        check("tooltip-escape-keeps-route", page.evaluate("location.hash") == "#/give")
        page.locator("#sidebarToggle").click()
        page.locator('#sidebar [href="#/settings"]').click()
        page.wait_for_function('location.hash==="#/settings"')
        page.wait_for_timeout(280)
        check(
            "sliding-indicator",
            page.evaluate(
                """()=>{
                   const a=document.querySelector('#sidebar [aria-current=page]')
                     .getBoundingClientRect();
                   const b=document.getElementById('navIndicator').getBoundingClientRect();
                   return Math.abs(a.top-b.top)<1&&Math.abs(a.height-b.height)<1&&b.width===2}"""
            ),
        )
        check(
            "one-current-no-double-border",
            page.locator("#sidebar [aria-current=page]").count() == 1
            and page.locator("#sidebar [aria-current=page]").evaluate(
                "e=>getComputedStyle(e).boxShadow"
            )
            == "none",
        )
        page.locator("#backBtn").click()
        page.wait_for_function('location.hash!=="#/settings"')
        page.locator("#langBtn").focus()
        page.keyboard.press("ArrowRight")
        check(
            "keyboard-language-applied",
            page.evaluate("state.lang") == "en"
            and page.locator("#langEnBtn").get_attribute("aria-pressed") == "true",
        )
        check("language-preserves-url-org", args.org in page.url)
        shot("english")
        open_appearance()
        page.locator('[data-shell-pref="theme"][data-shell-value="light"]').focus()
        page.keyboard.press("ArrowRight")
        check("keyboard-appearance-applied", page.evaluate("UIPreferences.get().theme") == "dark")
        page.keyboard.press("Escape")
        page.locator("#authDetails summary").click()
        check(
            "auth-details-security",
            page.locator("#authDetails").get_attribute("open") is not None
            and "service" in page.locator("#authRecommendation").inner_text().lower(),
        )
        shot("auth-expanded")
        page.locator("#authDetails summary").click()
        check("auth-still-visible", page.locator("#authwarn").is_visible())
        page.locator("#workspaceBtn").click()
        stable()
        check(
            "workspace-one-real-entry",
            page.locator(".shell-workspace-current").count() == 1
            and args.org in page.locator("#workspacePanel").inner_text(),
        )
        check("workspace-fit", fit("#workspacePanel"))
        audit("workspace-axe", ["#workspacePanel"])
        shot("workspace")
        page.keyboard.press("Escape")
        page.evaluate(
            "notify({id:'shell-browser-fixture',title:'Browser fixture',"
            "body:'Not a real approval',href:'#/give',silent:true})"
        )
        check(
            "unread-real-render-path",
            page.locator("#notifBtn").get_attribute("data-unread") == "true"
            and page.locator("#notifCount").is_visible(),
        )
        page.locator("#notifBtn").click()
        check(
            "bell-focus",
            page.locator("#notifPanel").evaluate("e=>e.contains(document.activeElement)"),
        )
        shot("notification-fixture")
        page.keyboard.press("Escape")
        check(
            "bell-escape-focus",
            page.locator("#notifPanel").is_hidden()
            and page.locator("#notifBtn").evaluate("e=>e===document.activeElement"),
        )
        page.evaluate("markNotifRead('shell-browser-fixture')")
        page.locator("#langBtn").click()
        page.reload(wait_until="domcontentloaded")
        stable()
        check(
            "reload-language-preferences",
            page.evaluate("state.lang") == "vi"
            and page.evaluate("UIPreferences.get().theme") == "dark",
        )
        check("auth-reload-visible", page.locator("#authwarn").is_visible())
        # Set via real controls before the mobile screenshots.
        open_appearance()
        page.locator('[data-shell-palette="navy"]').click()
        page.locator('[data-shell-pref="theme"][data-shell-value="light"]').click()
        page.keyboard.press("Escape")
        for width in [390, 320]:
            page.set_viewport_size({"width": width, "height": 844})
            stable()
            shot(f"mobile-{width}")
            check(
                f"mobile-{width}-header-fit",
                fit("#crumbs")
                and page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )
            page.locator("#mobileNavBtn").click()
            stable()
            check(f"mobile-{width}-drawer-fit", fit("#sidebarDrawer"))
            check(
                f"mobile-{width}-desktop-collapse-unchanged",
                page.locator("#sidebarToggle").is_hidden(),
            )
            page.locator("#mobileNavClose").focus()
            page.keyboard.press("Shift+Tab")
            check(
                f"mobile-{width}-focus-trap",
                page.locator("#sidebarDrawer").evaluate(
                    "e=>e.contains(document.activeElement)"
                    ' && document.activeElement.id!=="mobileNavClose"'
                ),
            )
            audit(f"mobile-{width}-drawer-axe", ["#sidebarDrawer"])
            shot(f"mobile-{width}-drawer")
            page.locator("#workspaceBtn").click()
            stable()
            check(
                f"mobile-{width}-workspace-in-modal",
                page.locator("#sidebarDrawer #workspacePanel").count() == 1
                and fit("#workspacePanel"),
            )
            page.keyboard.press("Escape")
            check(
                f"mobile-{width}-popover-escape-before-drawer",
                page.locator("#sidebarDrawer").evaluate("e=>e.open"),
            )
            page.keyboard.press("Escape")
            page.wait_for_function(
                'document.querySelector(".app-shell>#sidebar")'
                ' && document.activeElement.id==="mobileNavBtn"'
            )
            check(
                f"mobile-{width}-restore",
                page.locator("#mobileNavBtn").evaluate("e=>e===document.activeElement")
                and page.locator(".app-shell>#sidebar").count() == 1,
            )
            page.locator("#mobileNavBtn").click()
            page.locator('#sidebar [href="#/departments"]').click()
            page.wait_for_function(
                '!document.getElementById("sidebarDrawer").open'
                ' && document.activeElement.id==="mainContent"'
            )
            check(
                f"mobile-{width}-route-closes-drawer",
                page.locator("#sidebarDrawer").evaluate("e=>!e.open")
                and page.locator("#mainContent").evaluate("e=>e===document.activeElement"),
            )
            open_appearance()
            stable()
            check(f"mobile-{width}-appearance-fit", fit("#appearancePanel"))
            shot(f"mobile-{width}-appearance")
            page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(reduced_motion="reduce", color_scheme="dark")
        open_appearance()
        page.locator('[data-shell-pref="theme"][data-shell-value="auto"]').click()
        stable()
        check(
            "auto-follows-system",
            page.evaluate("document.documentElement.dataset.colorMode") == "dark",
        )
        check(
            "reduced-motion",
            page.locator("#navIndicator").evaluate("e=>getComputedStyle(e).transitionDuration")
            == "0s",
        )
        page.keyboard.press("Escape")
        page.emulate_media(color_scheme="light")
        stable()
        check(
            "auto-system-change",
            page.evaluate("document.documentElement.dataset.colorMode") == "light",
        )

        # Security variant has no auth-off callout, and Token remains available.
        def auth_variant(route):
            response = route.fetch()
            route.fulfill(
                response=response,
                body=response.text().replace("const AUTH_OFF = true;", "const AUTH_OFF = false;"),
            )

        context.route("**/api/v1/ui?*", auth_variant)
        page.reload(wait_until="domcontentloaded")
        stable()
        check(
            "auth-on-token",
            page.locator("#authwarn").is_hidden() and page.locator("#tokenBtn").is_visible(),
        )
        page.locator("#sidebarToggle").click()
        page.reload(wait_until="domcontentloaded")
        stable()
        check(
            "collapse-survives-reload",
            page.evaluate('document.documentElement.classList.contains("sidebar-collapsed")'),
        )
        context.route(
            "**/api/v1/organizations/" + args.org,
            lambda r: r.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"Gate fixture: unavailable"}',
            ),
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            'document.getElementById("workspaceName").textContent==="Không gian hiện tại"'
        )
        page.locator("#workspaceBtn").click()
        page.locator("#workspacePanel [role=status]").wait_for()
        check(
            "workspace-failure-honest",
            page.locator("#workspacePanel [role=status]").count() == 1
            and args.org in page.locator("#workspacePanel").inner_text(),
        )
        browser.close()
    report["source_sha256"] = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path("src/ai_orchestrator/web").iterdir()
        if p.is_file()
    }
    check("no-browser-errors", not report["errors"], report["errors"])
    check(
        "no-write-or-external-request", not report["blocked_requests"], report["blocked_requests"]
    )
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "stage-3-gate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "checks": len(report["checks"]),
                "screenshots": len(report["screenshots"]),
                "failed": [c for c in report["checks"] if not c["passed"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(0 if report["gate"] == "passed" else 1)


if __name__ == "__main__":
    main()
