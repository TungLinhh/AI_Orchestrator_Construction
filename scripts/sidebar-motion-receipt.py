"""Measure sidebar anchors on every animation frame, with real pointer/keyboard input."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

GEOMETRY = """() => {
  const box = n => { const r=n.getBoundingClientRect();
    return {x:r.x,y:r.y,width:r.width,height:r.height}; };
  return {toggle:box(document.querySelector('#sidebarToggle')),
    icons:[...document.querySelectorAll('#sidebar .ic')].map(box),
    rail:box(document.querySelector('#sidebar')),
    overflow:document.documentElement.scrollWidth > innerWidth};
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8110")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "errors": [],
        "screenshots": [],
        "transitions": [],
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"])
        for width, height in [(1440, 900), (1920, 1080), (1024, 640)]:
            page = browser.new_page(viewport={"width": width, "height": height})
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            page.goto(f"{args.base}/api/v1/ui?org={args.org}#/give", wait_until="domcontentloaded")
            page.wait_for_function("document.querySelector('#sidebar [aria-current=page]')")
            page.wait_for_timeout(400)
            initial = page.evaluate(GEOMETRY)

            def shot(name, width=width, page=page):
                filename = f"{width}-{name}.png"
                page.screenshot(path=str(args.out / filename))
                report["screenshots"].append(filename)

            def transition(
                name, action, expected_width, page=page, width=width, initial=initial, shot=shot
            ):
                page.evaluate(
                    """() => { window.sidebarFrames=[]; window.sidebarCapture=true;
                  const capture=()=>{ if(!window.sidebarCapture) return;
                    window.sidebarFrames.push((GEOMETRY)()); requestAnimationFrame(capture); };
                  requestAnimationFrame(capture); }""".replace("GEOMETRY", GEOMETRY)
                )
                action()
                page.wait_for_timeout(420)
                frames = page.evaluate("window.sidebarCapture=false; window.sidebarFrames")
                report["transitions"].append({"viewport": width, "name": name, "frames": frames})
                current = page.evaluate(GEOMETRY)
                anchor_error = max(
                    abs(f["toggle"][axis] - initial["toggle"][axis])
                    for f in frames
                    for axis in ["x", "y"]
                )
                icon_error = max(
                    abs(icon[axis] - initial["icons"][i][axis])
                    for f in frames
                    for i, icon in enumerate(f["icons"])
                    for axis in ["x", "y"]
                )
                check(
                    f"{width} {name}: toggle XY fixed throughout transition",
                    anchor_error < 1,
                    anchor_error,
                )
                check(
                    f"{width} {name}: every icon XY fixed throughout transition",
                    icon_error < 1,
                    icon_error,
                )
                check(
                    f"{width} {name}: target width",
                    abs(current["rail"]["width"] - expected_width) < 1,
                    current["rail"]["width"],
                )
                check(
                    f"{width} {name}: no horizontal page overflow",
                    not any(f["overflow"] for f in frames),
                )
                shot(name)
                return frames

            shot("expanded")

            def collapse(page=page, width=width):
                page.locator("#sidebarToggle").click()
                page.mouse.move(width - 50, 400)
                page.locator("#mainContent").focus()

            frames = transition("collapse", collapse, 64)
            check(
                f"{width}: animation has intermediate widths",
                any(65 < f["rail"]["width"] < 215 for f in frames),
            )
            check(
                f"{width}: toggle state and persistence",
                page.locator("#sidebarToggle").get_attribute("aria-expanded") == "false"
                and page.evaluate('localStorage.getItem("ao-sidebar-collapsed")') == "true",
            )
            transition("hover-peek", lambda page=page: page.mouse.move(30, 220), 216)
            transition(
                "hover-leave", lambda page=page, width=width: page.mouse.move(width - 50, 400), 64
            )
            transition(
                "keyboard-peek",
                lambda page=page: page.locator("#sidebar a[data-nav]").first.focus(),
                216,
            )
            transition("keyboard-blur", lambda page=page: page.locator("#mainContent").focus(), 64)
            transition("expand", lambda page=page: page.locator("#sidebarToggle").click(), 216)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(100)
            shot("page-bottom")
            check(
                f"{width}: toggle remains anchored on page scroll",
                abs(page.locator("#sidebarToggle").bounding_box()["y"] - initial["toggle"]["y"])
                < 1,
            )
            page.evaluate("window.scrollTo(0,0)")
            page.emulate_media(reduced_motion="reduce")
            transition("reduced-motion", collapse, 64)
            check(
                f"{width}: reduced motion disables width transition",
                page.locator(".app-shell").evaluate("(n)=>getComputedStyle(n).transitionDuration")
                == "0s",
            )
            rail = page.locator("#sidebar").bounding_box()
            workspace = page.locator("#workspaceBtn").bounding_box()
            check(
                f"{width}: collapsed workspace hit area stays inside rail",
                workspace["x"] + workspace["width"] <= rail["x"] + rail["width"],
            )
            page.locator("#workspaceBtn").click()
            check(
                f"{width}: workspace opens using its real click target",
                page.locator("#workspacePanel").evaluate("n=>n.matches(':popover-open')"),
            )
            page.keyboard.press("Escape")
            page.close()

        for width in [390, 360]:
            page = browser.new_page(
                viewport={"width": width, "height": 844}, is_mobile=True, has_touch=True
            )
            page.on("pageerror", lambda e: report["errors"].append(str(e)))
            page.add_init_script("localStorage.setItem('ao-sidebar-collapsed','true')")
            page.goto(f"{args.base}/api/v1/ui?org={args.org}#/give", wait_until="domcontentloaded")
            page.wait_for_timeout(600)
            page.locator("#mobileNavBtn").click()
            page.wait_for_timeout(250)
            check(
                f"{width}: mobile drawer open and labels visible",
                page.locator("#sidebarDrawer").evaluate("(n)=>n.open")
                and page.locator('#sidebar [data-i18n="nav.give"]').is_visible(),
            )
            check(
                f"{width}: mobile toggle stays out of drawer",
                not page.locator("#sidebarToggle").is_visible(),
            )
            for name, end in [("drawer-top", 0), ("drawer-bottom", 10000)]:
                page.locator("#navItems").evaluate("(n,v)=>n.scrollTop=v", end)
                filename = f"{width}-{name}.png"
                page.screenshot(path=str(args.out / filename))
                report["screenshots"].append(filename)
            page.locator("#mobileNavClose").click()
            check(
                f"{width}: drawer closes and focus returns",
                not page.locator("#sidebarDrawer").evaluate("(n)=>n.open")
                and page.locator("#mobileNavBtn").evaluate("(n)=>n===document.activeElement"),
            )
            check(
                f"{width}: no mobile horizontal overflow",
                page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )
            page.close()
        browser.close()
    check("zero JavaScript errors", not report["errors"], report["errors"])
    report["passed"] = all(c["passed"] for c in report["checks"])
    (args.out / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": len(report["checks"]),
                "failed": [c for c in report["checks"] if not c["passed"]],
                "screenshots": len(report["screenshots"]),
            },
            ensure_ascii=False,
        )
    )
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
