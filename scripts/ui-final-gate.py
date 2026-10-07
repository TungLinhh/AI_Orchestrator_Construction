"""Whole-app read-only UI acceptance; fixtures never reach the operational API."""

import argparse
import hashlib
import json
import re
import runpy
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/07-final"))
    parser.add_argument("--scan", action="store_true", help="One desktop pass for development")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    source_before = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").rglob("*")
        if f.is_file() and "__pycache__" not in f.parts
    }
    result = {
        "gate": "failed",
        "checks": [],
        "captures": [],
        "errors": [],
        "console": [],
        "blocked_requests": [],
        "routes": [],
        "performance": {},
        "fixtures": [],
    }

    def check(name, passed, detail=None):
        result["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            print("FAIL", name, json.dumps(detail, ensure_ascii=False)[:1200], flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        result["browser"] = browser.version
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )

        def guard(route):
            r = route.request
            if urlparse(r.url).hostname not in {"127.0.0.1", "localhost"} or r.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                result["blocked_requests"].append({"method": r.method, "url": r.url})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        context.add_init_script(path="artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js")
        context.add_init_script("""localStorage.setItem('ao-lang-v1','vi');
            window.__longTasks=[];new PerformanceObserver(l=>__longTasks.push(
            ...l.getEntries().map(e=>e.duration))).observe({type:'longtask',buffered:true});""")
        page = context.new_page()
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        page.on(
            "console",
            lambda m: (
                result["console"].append({"type": m.type, "text": m.text, "url": page.url})
                if m.type in {"warning", "error"}
                else None
            ),
        )
        url = "http://127.0.0.1:8100/api/v1/ui?org=" + args.org
        started = time.perf_counter()
        page.goto(url + "#/give", wait_until="domcontentloaded")
        page.wait_for_function("typeof registerSnapshot!=='undefined' && registerSnapshot!==null")
        page.evaluate("async()=>await document.fonts.ready")
        result["performance"]["initial_work_ms"] = (time.perf_counter() - started) * 1000
        result["performance"]["corpus"] = page.evaluate("registerSnapshot.total")
        routes = list(runpy.run_path("scripts/ui-shots.py")["ROUTES"])
        task = page.evaluate("registerSnapshot.items[0].id")
        routes += [
            f"give/{task}/{s}" for s in ["overview", "steps", "assignment", "decisions", "log"]
        ]
        routes += [f"task/{task}", f"give/{task}", f"give/{task}/log/page/200"]
        ids = page.evaluate(
            """async()=>{const r={};
              for(const k of ['approvals','agents','skills','tools','workflows']){
                const d=await apiGet('/'+k,{limit:50});r[k]=d.items || [];}return r;}"""
        )
        for kind, prefix in [
            ("approvals", "approval"),
            ("agents", "agent"),
            ("skills", "library/skills"),
            ("tools", "library/tools"),
        ]:
            if ids[kind]:
                routes.append(prefix + "/" + ids[kind][0]["id"])
        seen = set()
        for w in ids["workflows"]:
            if w["kind"] not in seen:
                seen.add(w["kind"])
                routes.append("processes/workflows/" + w["id"])

        def visit(route):
            page.evaluate(
                """async r=>{document.querySelectorAll('dialog[open]').forEach(d=>d.close());
              history.replaceState(history.state,'','#/'+r);await window.route();
              if(watchTimer)clearInterval(watchTimer);if(givePoll)clearInterval(givePoll);
              window.scrollTo(0,0);}""",
                route,
            )
            page.wait_for_timeout(100)

        def signature(route):
            return re.sub(
                r"(?:tsk|agt|apr|adef|sop|spd|skl|sklv|tool|toolv|evt|dec|unit|role|prj|doc)_\w+",
                ":id",
                route,
            )

        known = {signature(r) for r in routes}
        for r in routes:
            visit(r)
            for link in page.locator('main a[href^="#/"]').evaluate_all(
                "els=>els.filter(e=>e.getClientRects().length).map(e=>e.getAttribute('href').slice(2))"
            ):
                key = signature(link)
                if key not in known and "/page/" not in link:
                    routes.append(link)
                    known.add(key)
            if len(routes) > 110:
                raise RuntimeError("Unexpected route inventory growth")
        result["routes"] = routes

        def audit(name, shot=True):
            violations = page.evaluate(
                """async()=>{const r=await axe.run(document,{runOnly:{type:'tag',
                  values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});
                  return r.violations.filter(v=>['serious','critical'].includes(v.impact))
                    .map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>
                      ({target:n.target,summary:n.failureSummary})).slice(0,8)}));}"""
            )
            check(name + "-axe", not violations, violations)
            overflow = page.evaluate(
                "document.documentElement.scrollWidth>document.documentElement.clientWidth+1"
            )
            check(name + "-overflow", not overflow)
            error = page.locator("#pageError")
            check(
                name + "-route",
                error.is_hidden(),
                error.inner_text() if error.is_visible() else None,
            )
            if shot:
                filename = name + ".png"
                page.screenshot(path=str(args.out / filename))
                result["captures"].append(
                    {"file": filename, "url": page.url, "viewport": page.viewport_size}
                )

        combinations = (
            [("light", 1440, 900)]
            if args.scan
            else [(m, w, h) for m in ["light", "dark"] for w, h in [(1440, 900), (390, 844)]]
        )
        for mode, width, height in combinations:
            page.set_viewport_size({"width": width, "height": height})
            page.evaluate(
                "m=>UIPreferences.save({...UIPreferences.get(),theme:m,palette:'navy',motion:'reduced'})",
                mode,
            )
            for i, r in enumerate(routes):
                visit(r)
                audit(f"route-{i:02d}-{mode}-{width}", shot=not args.scan)
            print("routes checked", mode, width, len(routes), flush=True)
        if not args.scan:
            page.set_viewport_size({"width": 360, "height": 800})
            for i, r in enumerate(routes):
                visit(r)
                check(
                    f"all-routes-360-{i:02d}",
                    page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                )
            page.set_viewport_size({"width": 1440, "height": 900})
            visit("give")
            result["performance"]["bounded_initial_rows"] = page.locator(
                "#giveList .work-row"
            ).count()
            result["performance"]["cached_render_ms"] = page.evaluate("""async()=>{
              const start=performance.now();await renderGive();return performance.now()-start;}""")
            page.evaluate("""()=>{while(!document.getElementById('loadMoreTasks').hidden)
              document.getElementById('loadMoreTasks').click();window.__longTasks=[];}""")
            result["performance"]["loaded_rows"] = page.locator("#giveList .work-row").count()
            result["performance"]["scroll"] = page.evaluate("""async()=>{
              const start=performance.now(),frames=[];for(let i=0;i<60;i++){
                const t=performance.now();scrollTo(0,i/59*(document.body.scrollHeight-innerHeight));
                await new Promise(requestAnimationFrame);frames.push(performance.now()-t);}
              return {elapsed_ms:performance.now()-start,frames,long_tasks:__longTasks};}""")
            check(
                "full-real-register-scroll",
                result["performance"]["loaded_rows"]
                == page.evaluate("registerSnapshot.items.length"),
            )
            page.evaluate("window.scrollTo(0,0)")
        result["source_hashes"] = {
            str(f): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in Path("src/ai_orchestrator/web").rglob("*")
            if f.is_file() and "__pycache__" not in f.parts
        }
        check("source-frozen-during-gate", result["source_hashes"] == source_before)
        check("no-js-errors", not result["errors"], result["errors"])
        check("no-console-warnings-errors", not result["console"], result["console"])
        check(
            "no-external-or-mutating-requests",
            not result["blocked_requests"],
            result["blocked_requests"],
        )
        result["gate"] = "passed" if all(c["passed"] for c in result["checks"]) else "failed"
        (args.out / "report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        browser.close()
    print(
        json.dumps(
            {
                "gate": result["gate"],
                "checks": len(result["checks"]),
                "routes": len(result["routes"]),
            }
        )
    )
    if result["gate"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
