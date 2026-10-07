"""Read-only UI baseline. Browser writes and non-local requests are always blocked.

Run with uv run --with playwright python scripts/ui-shots.py --help.
Baseline records existing defects; --strict makes those defects fail the run.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src/ai_orchestrator/web"
ROUTES = [
    "give",
    "console",
    "work/issues",
    "work/approvals",
    "work",
    "departments",
    "agent",
    "processes",
    "processes/workflows",
    "processes/catalogue",
    "processes/hiring",
    "processes/definitions",
    "processes/provision",
    "library/skills",
    "library/tools",
    "library/memory",
    "library",
    "business/projects",
    "business/documents",
    "business",
    "operations/models",
    "operations/usage",
    "operations/events",
    "operations/decisions",
    "operations/audit",
    "operations/governance",
    "operations/system",
    "operations",
    "settings",
    "settings/appearance",
    "settings/organization",
    "settings/units",
    "settings/roles",
]


def source_audit() -> dict:
    """Static candidates are evidence to inspect, never proof that CSS is dead."""
    files = []
    for path in sorted(WEB.iterdir()):
        if path.is_file():
            raw = path.read_text()
            files.append(
                {
                    "file": str(path.relative_to(ROOT)),
                    "bytes": path.stat().st_size,
                    "lines": len(raw.splitlines()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    css = "\n".join(p.read_text() for p in WEB.glob("*.css"))
    selectors = re.findall(r"([^{}]+)\{", css)
    repeats = Counter(s.strip() for s in selectors if not s.strip().startswith("@"))
    enums = {}
    tree = ast.parse((ROOT / "src/ai_orchestrator/domain/enums.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            values = [
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ]
            if values:
                enums[node.name] = values
    return {
        "files": files,
        "enums": enums,
        "css": {
            "important": len(re.findall(r"!important", css)),
            "literal_colors": len(re.findall(r"#[\da-fA-F]{3,8}\b|rgba?\(", css)),
            "pixel_literals": len(re.findall(r"-?\d+(?:\.\d+)?px\b", css)),
            "z_index_values": re.findall(r"z-index:\s*([^;\n}]+)", css),
            "repeated_selectors": {k: v for k, v in repeats.items() if v > 1},
            "layers": len(re.findall(r"@layer\b", css)),
        },
    }


def pattern(route: str) -> str:
    return re.sub(
        r"(?:tsk|agt|apr|adef|sop|spd|skl|sklv|tool|toolv|evt|dec|unit|role|prj|doc)_\w+",
        ":id",
        route,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts/ui-shots/00-baseline")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Only localhost/127.0.0.1 may be audited")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "time_utc": datetime.now(UTC).isoformat(),
        "base": args.base,
        "organization": args.org,
        "source": source_audit(),
        "routes": [],
        "captures": [],
        "errors": [],
        "blocked_writes": [],
        "external_requests": [],
        "console": [],
        "palette_matrix": [],
        "fixtures": [],
        "performance": {},
    }
    axe = args.out / "font-study/axe-axe.min.js"
    if not axe.is_file():
        parser.error("Missing local axe-core asset; see docs/ui-refresh/00-audit.md")
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium, headless=True)
        report["browser"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="vi-VN")
        page = context.new_page()

        def guard(route):
            request = route.request
            if urlparse(request.url).hostname not in {"127.0.0.1", "localhost"}:
                report["external_requests"].append(request.url)
                route.abort()
            elif request.method not in {"GET", "HEAD", "OPTIONS"}:
                report["blocked_writes"].append({"method": request.method, "url": request.url})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        page.on(
            "pageerror", lambda e: report["errors"].append({"route": page.url, "error": str(e)})
        )
        page.on(
            "console",
            lambda m: (
                report["console"].append({"route": page.url, "type": m.type, "text": m.text})
                if m.type in {"warning", "error"}
                else None
            ),
        )
        page.add_init_script("""localStorage.setItem('ao-lang-v1','vi');
          localStorage.setItem('onx-ui-preferences-v1',JSON.stringify({theme:'light',palette:'navy',
            density:'comfortable',motion:'reduced',home:'campaigns'}));
          window.__longTasks=[]; new PerformanceObserver(list=>window.__longTasks.push(
            ...list.getEntries().map(e=>({start:e.startTime,duration:e.duration}))
          )).observe({type:'longtask',buffered:true});""")
        url = f"{args.base}/api/v1/ui?org={args.org}"
        started = time.perf_counter()
        page.goto(url + "#/give", wait_until="domcontentloaded")
        page.wait_for_function(
            "typeof registerSnapshot !== 'undefined' && registerSnapshot !== null"
        )
        report["performance"]["initial_work_ms"] = round((time.perf_counter() - started) * 1000, 1)
        report["register"] = page.evaluate("""() => ({
          total:registerSnapshot.total,needs_you:registerSnapshot.needs_you,
          in_flight:registerSnapshot.in_flight,settled:registerSnapshot.settled,
          statuses:[...new Set(registerSnapshot.items.map(t=>t.status))],
          types:[...new Set(registerSnapshot.items.map(t=>t.task_type))],
          rows:document.querySelectorAll('#giveList [data-task]').length,
          list_style:{maxHeight:getComputedStyle(document.querySelector('#giveList')).maxHeight,
            overflow:getComputedStyle(document.querySelector('#giveList')).overflow},
          task_id:registerSnapshot.items[0]?.id
        })""")
        # Establish representative record IDs using GET only, without saving full business payloads.
        ids = page.evaluate("""async () => {
          const result={};
          for(const kind of ['approvals','agents','workflows','skills','tools']) {
            const data=await apiGet('/'+kind,{limit:50});
            result[kind]=(data.items||[]).map(x=>({id:x.id,kind:x.kind,name:x.name}));
          } return result;
        }""")
        task = report["register"]["task_id"]
        routes = list(ROUTES)
        routes += [
            f"give/{task}/{section}"
            for section in ("overview", "steps", "assignment", "decisions", "log")
        ]
        routes += [f"give/{task}", f"task/{task}", f"give/{task}/log/page/200"]
        for kind, prefix in (
            ("approvals", "approval"),
            ("agents", "agent"),
            ("skills", "library/skills"),
            ("tools", "library/tools"),
        ):
            if ids[kind]:
                routes.append(prefix + "/" + ids[kind][0]["id"])
        seen_kinds = set()
        for workflow in ids["workflows"]:
            if workflow["kind"] not in seen_kinds:
                routes.append("processes/workflows/" + workflow["id"])
                seen_kinds.add(workflow["kind"])

        def visit(route: str):
            # Approval routes open a shared native dialog. Its lifetime is not
            # tied to hash routing: dismiss the previous route's overlay first.
            page.locator("#sheet").evaluate("el=>{if(el.open)el.close()}")
            page.evaluate("r => go('#/'+r)", route)
            page.evaluate(
                "async () => { await route(); if(watchTimer) clearInterval(watchTimer);"
                "if(givePoll) clearInterval(givePoll); }"
            )
            page.evaluate("window.scrollTo(0,0)")
            page.wait_for_timeout(100)

        # Discover one actual linked record per URL shape, not every task/event record.
        known = {pattern(r) for r in routes}
        i = 0
        while i < len(routes):
            route = routes[i]
            visit(route)
            anchors = page.locator('main a[href^="#/"]').evaluate_all(
                "els=>els.filter(e=>e.getClientRects().length).map(e=>e.getAttribute('href').slice(2))"
            )
            for anchor in anchors:
                key = pattern(anchor)
                if key not in known and "/page/" not in anchor:
                    routes.append(anchor)
                    known.add(key)
            if len(routes) > 95:
                raise RuntimeError("Unexpected route inventory growth; inspect route normalization")
            report["routes"].append(
                {
                    "route": route,
                    "pattern": pattern(route),
                    "title": page.locator("main h1:visible").all_text_contents(),
                    "page_error": page.locator("#pageError").inner_text()
                    if page.locator("#pageError").is_visible()
                    else None,
                }
            )
            i += 1

        client = context.new_cdp_session(page)
        client.send("DOM.enable")
        client.send("CSS.enable")
        client.send("CSS.startRuleUsageTracking")

        def capture(name: str, axe_check: bool = True):
            page.screenshot(path=str(args.out / (name + ".png")))
            item = {
                "file": name + ".png",
                "route": page.url.split("#")[-1],
                "viewport": page.viewport_size,
                "open_dialogs": page.locator("dialog[open]").evaluate_all("els=>els.map(e=>e.id)"),
                "horizontal_overflow": page.evaluate(
                    "document.documentElement.scrollWidth>document.documentElement.clientWidth+1"
                ),
            }
            if axe_check:
                page.add_script_tag(path=str(axe))
                item["axe"] = page.evaluate("""async () => (await axe.run(document,{
                  runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}
                })).violations.map(v=>({id:v.id,impact:v.impact,help:v.help,
                  nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary})).slice(0,12),
                  total_nodes:v.nodes.length}))""")
            report["captures"].append(item)

        for mode in ("light", "dark"):
            page.evaluate(
                "m=>UIPreferences.save({theme:m,palette:'navy',density:'comfortable',motion:'reduced'})",
                mode,
            )
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for index, route in enumerate(routes):
                    visit(route)
                    capture(f"route-{index:02d}-{mode}-{width}")
                # Read-only auxiliary UI states: no submit/approval/run actions.
                visit("give")
                page.locator("#notifBtn").click()
                capture(f"notifications-{mode}-{width}")
                page.locator("#notifBtn").click()
                page.locator("#authwarn summary").click()
                capture(f"auth-warning-{mode}-{width}")
                page.locator("#authwarn summary").click()
                disclosures = page.locator("#view-give details:visible")
                if disclosures.count():
                    disclosures.first.evaluate("el=>el.open=true")
                    capture(f"create-work-{mode}-{width}")
                    disclosures.first.evaluate("el=>el.open=false")
                visit(f"give/{task}/steps")
                # Sheet content comes from the real visible task; invoking its existing
                # presentation function avoids a potentially mutating button.
                page.evaluate(
                    "openSheet(document.querySelector('#taskTitle').textContent,"
                    "document.querySelector('#taskSteps').innerHTML)"
                )
                capture(f"task-sheet-{mode}-{width}")
                page.locator("#sheet").evaluate("el=>el.close()")
                page.locator("#sidebarToggle").click()
                capture(f"sidebar-collapsed-{mode}-{width}")
                page.locator("#sidebarToggle").click()
            (args.out / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )

        page.set_viewport_size({"width": 1440, "height": 900})
        visit("give")
        for mode in ("light", "dark"):
            for palette in page.evaluate("UIPalettes.items.map(p=>p.id)"):
                for density in ("compact", "comfortable"):
                    page.evaluate(
                        "v=>UIPreferences.save(v)",
                        {
                            "theme": mode,
                            "palette": palette,
                            "density": density,
                            "motion": "reduced",
                        },
                    )
                    capture(f"work-{palette}-{mode}-{density}", axe_check=False)
                    report["palette_matrix"].append(
                        page.evaluate("""() => {
                      const rgb=s=>s.match(/[\\d.]+/g).slice(0,3).map(Number);
                      const lum=s=>rgb(s).map(v=>{v/=255;
                        return v<=.04045?v/12.92:((v+.055)/1.055)**2.4})
                        .reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
                      const ratio=(a,b)=>(Math.max(lum(a),lum(b))+.05)
                        /(Math.min(lum(a),lum(b))+.05);
                      const check=(selector,background)=>{
                        const el=document.querySelector(selector),s=getComputedStyle(el);
                        return {selector,color:s.color,background,ratio:ratio(s.color,background)};
                      };
                      return {preferences:UIPreferences.get(),checks:[
                        check('body',getComputedStyle(document.body).backgroundColor),
                        check('#giveList .s',getComputedStyle(
                          document.querySelector('.card')).backgroundColor),
                        check('.btn.primary',getComputedStyle(document.querySelector('.btn.primary')).backgroundColor)]};
                    }""")
                    )
        page.evaluate(
            "UIPreferences.save({theme:'light',palette:'navy',density:'comfortable',motion:'reduced'})"
        )
        for width, height in ((768, 1024), (360, 800), (1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            capture(f"work-extra-{width}")
        page.set_viewport_size({"width": 1440, "height": 900})
        report["performance"]["real_rows"] = report["register"]["rows"]
        page.evaluate("window.__longTasks=[]")
        scroll = page.evaluate("""async () => {
          const list=document.querySelector('#giveList'), start=performance.now();
          const samples=[];
          for(let i=0;i<60;i++) {
            const now=performance.now(); list.scrollTop=i/59*(list.scrollHeight-list.clientHeight);
            window.scrollTo(0,i/59*(document.body.scrollHeight-innerHeight));
            await new Promise(requestAnimationFrame);samples.push(performance.now()-now);
          } return {elapsed_ms:performance.now()-start,frames:samples,
            long_tasks:window.__longTasks,list_height:list.scrollHeight,viewport_height:list.clientHeight};
        }""")
        report["performance"]["scroll"] = scroll
        # Explicit fixture: browser-local only, using the real schema and repeated rows.
        # No API writes and no fixture is persisted to the organization.
        for fixture in (
            "empty",
            "no-results",
            "long-error",
            "long-title",
            "all-zero",
            "error",
            "loading",
        ):
            visit("give")

            def fake_register(route, _request, state=fixture):
                if state == "error":
                    route.fulfill(status=503, json={"detail": "UI fixture: service unavailable"})
                    return
                if state == "loading":
                    route.fulfill(status=200, json={"items": [], "total": 0})
                    return
                data = route.fetch().json()
                if state in {"empty", "all-zero"}:
                    data.update(items=[], total=0, needs_you=0, in_flight=0, settled=0)
                elif state in {"long-error", "long-title"} and data.get("items"):
                    data["items"][0]["last_error" if state == "long-error" else "title"] = (
                        "UI fixture · ệ ữ ặ ẫ ỗ ử · " * 70
                    )
                route.fulfill(json=data)

            page.route("**/api/v1/ceo/work?*", fake_register)
            held_requests = []
            if fixture == "loading":
                # Hold GET in the browser; capture while renderer awaits the response.
                page.route(
                    "**/api/v1/ceo/work?*",
                    lambda r, _request, pending=held_requests: pending.append(r),
                )
                page.evaluate("() => {registerSnapshot=null; void renderGive()}")
                page.wait_for_timeout(200)
            else:
                page.evaluate("async()=>{registerSnapshot=null;await renderGive()}")
            if fixture == "no-results":
                page.locator("#taskSearch").fill("ui-fixture-no-matching-task-836412")
                assert page.locator("#giveEmpty").is_visible()
            elif fixture in {"empty", "all-zero"}:
                assert page.evaluate(
                    "registerSnapshot.total===0 && registerSnapshot.items.length===0"
                )
            elif fixture in {"long-title", "long-error"}:
                field = "title" if fixture == "long-title" else "last_error"
                assert page.evaluate(
                    "f=>registerSnapshot.items[0][f].includes('UI fixture')", field
                )
            elif fixture == "loading":
                assert held_requests, "Loading fixture must hold an actual GET"
            elif fixture == "error":
                assert page.locator("#pageError").is_visible()
            capture("fixture-" + fixture, axe_check=False)
            report["fixtures"].append({"state": fixture, "browser_only": True, "verified": True})
            for held in held_requests:
                held.fulfill(json={"items": [], "total": 0})
            page.unroute("**/api/v1/ceo/work?*")
            page.locator("#taskSearch").fill("")
        page.evaluate("UIPreferences.save({motion:'auto'})")
        page.emulate_media(reduced_motion="reduce")
        report["reduced_motion_transform_animations"] = page.evaluate("""document.getAnimations()
          .filter(a=>a.playState==='running' && a.effect.getKeyframes().some(k=>k.transform))
          .map(a=>a.animationName||a.id)""")
        report["css_rule_usage"] = client.send("CSS.stopRuleUsageTracking")["ruleUsage"]
        browser.close()
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "routes": len(report["routes"]),
                "screenshots": len(report["captures"]),
                "errors": len(report["errors"]),
                "console": len(report["console"]),
                "blocked_writes": len(report["blocked_writes"]),
                "report": str(args.out / "report.json"),
            }
        )
    )
    if report["external_requests"] or report["blocked_writes"]:
        raise SystemExit("Read-only/local-only boundary violated; see report")
    if args.strict and (
        report["errors"]
        or report["console"]
        or len(report["palette_matrix"]) != 24
        or any(check["ratio"] < 4.5 for p in report["palette_matrix"] for check in p["checks"])
        or report.get("reduced_motion_transform_animations", [])
        or any(
            c["horizontal_overflow"]
            or any(v["impact"] in {"serious", "critical"} for v in c.get("axe", []))
            for c in report["captures"]
        )
    ):
        raise SystemExit("UI acceptance violations; see report")


if __name__ == "__main__":
    main()
