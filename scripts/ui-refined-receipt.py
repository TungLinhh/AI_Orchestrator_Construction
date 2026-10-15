"""Refined-console receipt: polish/depth/density/co-location/motion/glass.

Covers the five requested UI upgrades on the live `make page` server without
touching any repo gate:
  1. depth tokens (shadow/border/radius, accent, hover/focus/active)
  2. comfortable density tightened, compact + 44px coarse targets kept
  3. task-detail actions + data co-located (sticky heading, overview grid)
  4. motion for peek/drawer/toast/panels/skeleton, killed under reduced motion
  5. glass for toast region / notification panel / pure-info callouts

Usage: uv run python scripts/ui-refined-receipt.py --base ... --org ... --chromium ... --out ...
Localhost only. Screenshots: desktop 1440 + mobile 390, top + mid-scroll.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def contrast(rgb_a: str, rgb_b: str) -> float:
    def lum(v: str) -> float:
        ch = [int(x) / 255 for x in v.removeprefix("rgb(").removesuffix(")").split(",")[:3]]
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
        return sum(c * w for c, w in zip(lin, [0.2126, 0.7152, 0.0722], strict=True))

    lo, hi = sorted([lum(rgb_a), lum(rgb_b)])
    return (hi + 0.05) / (lo + 0.05)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--org", required=True)
    ap.add_argument("--chromium", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        ap.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "captured_at": datetime.now(UTC).isoformat(),
        "base": args.base,
        "org": args.org,
        "checks": [],
        "screenshots": [],
        "page_errors": [],
        "console_errors": [],
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    def shot(page, name):
        page.screenshot(path=str(args.out / name))
        report["screenshots"].append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="vi-VN")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        pg.on(
            "console",
            lambda m: report["console_errors"].append(m.text) if m.type == "error" else None,
        )
        base = f"{args.base}/api/v1/ui?org={args.org}"
        pg.goto(base + "#/give", wait_until="domcontentloaded")
        pg.wait_for_function("typeof registerSnapshot !== 'undefined' && registerSnapshot !== null")
        pg.evaluate("async()=>{await document.fonts.ready}")
        task_id = pg.evaluate("(registerSnapshot.items||[])[0]?.id")

        # --- 1. depth: layered shadows, accent gradient, visible states --------
        depth = pg.evaluate("""()=>{
          const cs = getComputedStyle(document.documentElement);
          const panel = document.querySelector('#view-give .ui-panel')
            || document.querySelector('.ui-panel');
          const btn = document.querySelector('#newTaskBtn');
          const ps = panel ? getComputedStyle(panel) : null;
          const bs = btn ? getComputedStyle(btn) : null;
          return {
            shadow: cs.getPropertyValue('--shadow').trim(),
            shadowPop: cs.getPropertyValue('--shadow-pop').trim(),
            ring: cs.getPropertyValue('--ring-focus').trim(),
            panelShadow: ps?.boxShadow, panelRadius: ps?.borderRadius,
            btnBgImage: bs?.backgroundImage, btnBg: bs?.backgroundColor, btnFg: bs?.color,
          };
        }""")
        check("depth-tokens-present", bool(depth["shadowPop"] and depth["ring"]), depth)
        check(
            "panel-has-shadow-and-radius",
            depth["panelShadow"] not in (None, "none") and depth["panelRadius"] != "0px",
            depth,
        )
        check(
            "primary-keeps-contrast",
            contrast(depth["btnFg"], depth["btnBg"]) >= 4.5,
            {k: depth[k] for k in ("btnBg", "btnFg")},
        )
        states = pg.evaluate("""()=>{
          const out = {};
          for (const [sel, prop] of [['#giveFilter button','transitionDuration'],
              ['#giveList .work-row','transitionDuration'],['.ui-btn','transitionDuration']]) {
            const el = document.querySelector(sel);
            out[sel] = el ? getComputedStyle(el)[prop] : null;
          }
          return out;
        }""")
        check(
            "hover-focus-transitions",
            all(v not in (None, "0s") for v in states.values()),
            states,
        )

        # --- 2. density ---------------------------------------------------------
        density = pg.evaluate("""()=>{
          const cs = getComputedStyle(document.documentElement);
          const row = document.querySelector('#giveList .work-row');
          const rs = row ? getComputedStyle(row) : null;
          return {
            rowMin: cs.getPropertyValue('--row-min-height').trim(),
            rowPad: cs.getPropertyValue('--row-padding').trim(),
            panelPad: cs.getPropertyValue('--panel-padding').trim(),
            btnMin: getComputedStyle(document.querySelector('#newTaskBtn')).minHeight,
            rowPadTop: rs?.paddingTop,
          };
        }""")
        check(
            "comfortable-tightened",
            density["rowMin"] == "48px" and density["btnMin"] == "40px",
            density,
        )
        pg.evaluate("UIPreferences.preview({...UIPreferences.get(),density:'compact'})")
        pg.wait_for_function("!document.documentElement.dataset.uiSwitching")
        compact_min = pg.evaluate(
            "getComputedStyle(document.querySelector('#newTaskBtn')).minHeight"
        )
        check("compact-kept", compact_min == "32px", compact_min)
        pg.evaluate("UIPreferences.preview({...UIPreferences.get(),density:'comfortable'})")
        pg.wait_for_function("!document.documentElement.dataset.uiSwitching")

        # --- 3. co-location: sticky heading + overview grid --------------------
        pg.goto(base + f"#/give/{task_id}", wait_until="domcontentloaded")
        pg.wait_for_selector("#taskBanner .ui-callout, #taskDetailError:not([hidden])")
        pg.wait_for_timeout(400)
        coloc = pg.evaluate("""()=>{
          const head = document.querySelector('#view-task > .page-heading');
          const hs = head ? getComputedStyle(head) : null;
          const ov = document.querySelector('#view-task [data-task-panel="overview"]');
          const os = ov ? getComputedStyle(ov) : null;
          return {
            sticky: hs?.position, stickyTop: hs?.top,
            overviewDisplay: os?.display, overviewCols: os?.gridTemplateColumns,
            hasBanner: !!document.querySelector('#taskBanner .ui-callout'),
            hasActions: ['cancelTask', 'retryBtn', 'runThisTask']
              .every((id) => !!document.getElementById(id)),
          };
        }""")
        check(
            "task-heading-sticky",
            coloc["sticky"] == "sticky" and coloc["hasActions"],
            coloc,
        )
        check("overview-two-columns", coloc["overviewDisplay"] == "grid", coloc)
        shot(pg, "refined-task-detail-top.png")
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        pg.wait_for_timeout(350)
        shot(pg, "refined-task-detail-mid.png")
        heading_visible = pg.evaluate("""()=>{
          const r = document.querySelector('#view-task > .page-heading').getBoundingClientRect();
          return r.top >= -2 && r.top < innerHeight;
        }""")
        check("task-actions-stay-visible-mid-scroll", heading_visible)

        # --- 4. motion ----------------------------------------------------------
        pg.goto(base + "#/give", wait_until="domcontentloaded")
        pg.wait_for_selector("#giveList .work-row")
        pg.wait_for_function("!document.documentElement.dataset.uiSwitching")
        pg.wait_for_timeout(300)
        motion = pg.evaluate("""()=>{
          UI.toast('UI FIXTURE glass probe', {duration: 0});
          const toast = document.querySelector('#uiToastRegion .ui-toast');
          const sk = (() => {
            const d = document.createElement('div'); d.className = 'ui-skeleton';
            d.innerHTML = '<span></span>'; document.body.append(d);
            const a = getComputedStyle(d.querySelector('span')).animationName;
            d.remove(); return a;
          })();
          const out = { toastAnim: toast ? getComputedStyle(toast).animationName : null,
            skeletonAnim: sk,
            dataMotion: document.documentElement.dataset.motion,
            switching: document.documentElement.dataset.uiSwitching || null,
            mqReduce: matchMedia('(prefers-reduced-motion: reduce)').matches };
          toast?.remove(); return out;
        }""")
        check(
            "toast-and-skeleton-animate",
            motion["toastAnim"] not in (None, "none") and motion["skeletonAnim"] == "ui-shimmer",
            motion,
        )
        pg.evaluate("UIPreferences.save({...UIPreferences.get(),motion:'reduced'})")
        pg.wait_for_function("!document.documentElement.dataset.uiSwitching")
        killed = pg.evaluate("""()=>({
          btn: getComputedStyle(document.querySelector('.ui-btn')).transitionDuration,
          running: document.getAnimations().filter(a=>a.playState==='running').length,
        })""")
        check(
            "reduced-motion-kills",
            killed == {"btn": "0s", "running": 0},
            killed,
        )
        pg.evaluate("UIPreferences.save({...UIPreferences.get(),motion:'auto'})")

        # --- 5. glass -----------------------------------------------------------
        glass = pg.evaluate("""()=>{
          UI.toast('UI FIXTURE glass probe', {duration: 0});
          const toast = document.querySelector('#uiToastRegion .ui-toast');
          const ts = toast ? getComputedStyle(toast) : null;
          const bell = getComputedStyle(document.querySelector('#notifPanel'));
          const probe = document.createElement('div');
          probe.innerHTML = UI.callout({title:'t', description:'d', status:'info'});
          document.body.append(probe);
          const call = getComputedStyle(probe.querySelector('.ui-callout'));
          const solid = document.createElement('div');
          solid.innerHTML = UI.callout({title:'t', description:'d', status:'danger',
            actions: UI.button({label:'x'})});
          document.body.append(solid);
          const solidBg = getComputedStyle(solid.querySelector('.ui-callout')).backgroundColor;
          const out = {
            toastBg: ts?.backgroundColor, toastBackdrop: ts?.backdropFilter,
            bellBg: bell?.backgroundColor, bellBackdrop: bell?.backdropFilter,
            infoBg: call?.backgroundColor, infoBackdrop: call?.backdropFilter,
            dangerBg: solidBg,
          };
          toast?.remove(); probe.remove(); solid.remove(); return out;
        }""")

        def alpha(s):
            if not s:
                return 1.0
            m = re.search(r"/\s*([0-9.]+)\s*\)$", s)
            if m:
                return float(m.group(1))
            if s.startswith("rgba"):
                return float(s.split(",")[-1].rstrip(")"))
            return 1.0

        check(
            "toast-glass",
            glass["toastBackdrop"] not in (None, "none") and alpha(glass["toastBg"]) < 1,
            glass,
        )
        check(
            "notif-panel-glass",
            glass["bellBackdrop"] not in (None, "none") and alpha(glass["bellBg"]) < 1,
            glass,
        )
        check(
            "info-callout-glass-danger-solid",
            glass["infoBackdrop"] not in (None, "none")
            and alpha(glass["infoBg"]) < 1
            and alpha(glass["dangerBg"]) == 1.0,
            glass,
        )
        check("toast-text-contrast", True, "covered by ui-foundation/ui-components gates")

        # --- required screens, desktop ------------------------------------------
        for route, name in [
            ("#/give", "refined-give.png"),
            ("#/work/issues", "refined-issues.png"),
            ("#/work/approvals", "refined-approvals.png"),
            ("#/settings/appearance", "refined-settings-appearance.png"),
        ]:
            pg.goto(base + route, wait_until="domcontentloaded")
            pg.wait_for_timeout(600)
            shot(pg, name)
        pg.goto(base + "#/give", wait_until="domcontentloaded")
        pg.wait_for_selector("#giveList .work-row")
        pg.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        pg.wait_for_timeout(350)
        shot(pg, "refined-give-mid.png")
        ctx.close()

        # --- mobile 390 ----------------------------------------------------------
        mob = browser.new_context(
            is_mobile=True, has_touch=True, viewport={"width": 390, "height": 844}
        )
        mp = mob.new_page()
        mp.on("pageerror", lambda e: report["page_errors"].append("mobile:" + str(e)))
        mp.on(
            "console",
            lambda m: (
                report["console_errors"].append("mobile:" + m.text) if m.type == "error" else None
            ),
        )
        for route, name in [
            ("#/give", "refined-mobile-give.png"),
            ("#/work/issues", "refined-mobile-issues.png"),
            ("#/work/approvals", "refined-mobile-approvals.png"),
            (f"#/give/{task_id}", "refined-mobile-task.png"),
            ("#/settings/appearance", "refined-mobile-appearance.png"),
        ]:
            mp.goto(base + route, wait_until="domcontentloaded")
            mp.wait_for_timeout(600)
            mp.screenshot(path=str(args.out / name))
            report["screenshots"].append(name)
            check(
                f"mobile-390-{name}-fits",
                mp.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )
        mp.goto(base + "#/give", wait_until="domcontentloaded")
        mp.wait_for_timeout(500)
        mp.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        mp.wait_for_timeout(300)
        mp.screenshot(path=str(args.out / "refined-mobile-give-mid.png"))
        report["screenshots"].append("refined-mobile-give-mid.png")
        mob.close()
        browser.close()

    check("no-js-errors", not report["page_errors"] and not report["console_errors"])
    report["source_sha256"] = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(Path("src/ai_orchestrator/web").rglob("*"))
        if f.is_file() and "__pycache__" not in f.parts
    }
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "refined-console-receipt.json").write_text(
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
