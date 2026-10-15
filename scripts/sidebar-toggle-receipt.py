"""Toggle-at-bottom receipt: open/collapsed/peek geometry + no-JS-error proof."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8100"
ORG = "org_01m3ycsehgwz7v1fk2ahswkwhm"
CHROME = "/home/vutun/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
OUT = Path("artifacts/source-intake/2026-10-15/runtime-ui")
OUT.mkdir(parents=True, exist_ok=True)

report = {
    "check": "sidebar-toggle-at-bottom",
    "captured_at": datetime.now(UTC).isoformat(),
    "checks": [],
    "geometry": {},
    "errors": [],
    "screenshots": [],
}


def check(name, passed, detail=None):
    report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.on("pageerror", lambda e: report["errors"].append(str(e)))
    page.goto(f"{BASE}/api/v1/ui?org={ORG}#/give", wait_until="domcontentloaded")
    page.wait_for_function(
        "document.getElementById('workspaceName').textContent==='Autonomous Demo Company'"
    )
    page.wait_for_function("document.querySelector('#sidebar [aria-current=page]')")
    page.wait_for_timeout(400)

    def box():
        return page.locator("#sidebarToggle").bounding_box()

    def shot(name):
        page.screenshot(path=str(OUT / (name + ".png")))
        report["screenshots"].append(name + ".png")

    open_box = box()
    report["geometry"]["open"] = open_box
    shot("sidebar-open")

    sb = page.locator("#sidebar").bounding_box()
    check(
        "toggle sits in the bottom band of the open sidebar",
        open_box["y"] + open_box["height"] >= sb["y"] + sb["height"] - 80,
        f"toggle bottom={open_box['y'] + open_box['height']:.1f} "
        f"sidebar bottom={sb['y'] + sb['height']:.1f}",
    )

    page.locator("#sidebarToggle").click()
    page.wait_for_timeout(350)
    shut_box = box()
    report["geometry"]["collapsed"] = shut_box
    shot("sidebar-collapsed")
    check(
        "collapsed rail is 64px",
        abs(page.locator("#sidebar").bounding_box()["width"] - 64) < 1,
    )
    check(
        "toggle position is unchanged on both axes open vs collapsed",
        abs(open_box["x"] - shut_box["x"]) < 1.5
        and abs(open_box["y"] - shut_box["y"]) < 1.5
        and abs((open_box["y"] + open_box["height"]) - (shut_box["y"] + shut_box["height"])) < 1.5,
        f"open y={open_box['y']:.1f} collapsed y={shut_box['y']:.1f}",
    )
    check(
        "toggle still labelled while collapsed",
        page.locator("#sidebarToggle").get_attribute("aria-expanded") == "false"
        and bool(page.locator("#sidebarToggle").get_attribute("aria-label")),
        page.locator("#sidebarToggle").get_attribute("aria-label"),
    )
    check(
        "collapse persisted",
        page.evaluate('localStorage.getItem("ao-sidebar-collapsed")') == "true",
    )

    # Leave the fixed toggle before re-entering the rail to trigger hover-peek.
    page.mouse.move(1100, 450)
    page.wait_for_timeout(250)
    # Hover-peek: real mouse over the rail, no class poking.
    rail = page.locator("#sidebar").bounding_box()
    page.mouse.move(rail["x"] + rail["width"] / 2, rail["y"] + 300)
    page.wait_for_timeout(350)
    peek_box = box()
    report["geometry"]["peek"] = peek_box
    check(
        "hover peek widens without dropping the collapsed class",
        page.evaluate(
            "document.documentElement.classList.contains('sidebar-collapsed')"
            " && document.documentElement.classList.contains('sidebar-peek')"
        )
        and abs(page.locator("#sidebar").bounding_box()["width"] - 216) < 2,
    )
    shot("sidebar-peek")
    check(
        "toggle position is unchanged on both axes during peek",
        abs(open_box["x"] - peek_box["x"]) < 1.5 and abs(open_box["y"] - peek_box["y"]) < 1.5,
        f"open y={open_box['y']:.1f} peek y={peek_box['y']:.1f}",
    )

    page.mouse.move(1100, 450)
    page.wait_for_timeout(350)
    check(
        "leaving the rail ends the peek",
        not page.evaluate("document.documentElement.classList.contains('sidebar-peek')"),
    )

    page.locator("#sidebarToggle").click()
    page.wait_for_timeout(350)
    check(
        "toggle expands again with aria-expanded=true",
        page.locator("#sidebarToggle").get_attribute("aria-expanded") == "true"
        and abs(page.locator("#sidebar").bounding_box()["width"] - 216) < 1,
    )
    browser.close()

check("zero JS errors", not report["errors"], report["errors"])
report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
(OUT / "sidebar-toggle-bottom.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
)
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["gate"] == "passed" else 1)
