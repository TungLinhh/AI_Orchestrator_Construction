"""Phase 4 browser gate; real GET corpus, isolated fixtures, no operational writes."""

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
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/04-work"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        parser.error("Localhost only")
    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "phase": 4,
        "final_ui_acceptance": False,
        "captured_at": datetime.now(UTC).isoformat(),
        "checks": [],
        "screenshots": [],
        "performance": {},
        "errors": [],
        "blocked_requests": [],
        "scope": "Work list, form and task detail; not all other pages",
        "fixtures": [
            "5000 UI FIXTURE tasks; GET only, no database rows",
            "GET errors/empty and create POST failure responses fulfilled locally",
        ],
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
        context.add_init_script(path="artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js")
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        requests = []
        page.on("request", lambda r: requests.append((r.method, r.url)))
        url = args.base + "/api/v1/ui?org=" + args.org + "#/give"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#giveList .work-row")
        page.evaluate("async()=>{await document.fonts.ready}")
        actual = page.evaluate("registerSnapshot")
        report["actual_corpus"] = {
            k: actual.get(k) for k in ["total", "needs_you", "in_flight", "settled"]
        }

        def shot(name):
            page.screenshot(path=str(args.out / (name + ".png")))
            report["screenshots"].append(name + ".png")

        def audit(name, selector):
            violations = page.evaluate(
                """async s=>{
              const r=await axe.run(document.querySelector(s));
              return r.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>
                ({target:n.target,summary:n.failureSummary}))}));
            }""",
                selector,
            )
            check(name, not violations, violations)

        def stable():
            page.wait_for_function("!document.documentElement.dataset.uiSwitching")
            page.wait_for_timeout(180)

        def clear_filters():
            page.evaluate("document.getElementById('taskSearch').value='';WorkUI.filter('')")

        check("real-corpus-loaded", len(actual["items"]) == actual["total"])
        check(
            "initial-batch-bounded",
            page.locator("#giveList .work-row").count() == min(100, actual["total"]),
        )
        shot("after-list")
        page.locator('#workStats [data-work-filter="you"]').click()
        check("stat-click-filters", page.evaluate("giveFilter") == "you")
        check(
            "waiting-count-api",
            page.locator('#workStats [data-work-filter="you"] strong').inner_text()
            == page.evaluate("num(registerSnapshot.needs_you)"),
        )
        check(
            "waiting-rows",
            page.evaluate(
                "[...document.querySelectorAll('#giveList [data-task]')].every(e=>"
                "registerSnapshot.items.find(t=>t.id===e.dataset.task).waiting_on==='you')"
            ),
        )
        clear_filters()
        before = len([r for r in requests if "/ceo/work?" in r[1]])
        last = actual["items"][-1]
        page.locator("#taskSearch").fill(last["id"])
        page.wait_for_function("document.querySelectorAll('#giveList .work-row').length===1")
        check(
            "search-finds-beyond-first-batch",
            page.locator("#giveList [data-task]").get_attribute("data-task") == last["id"],
        )
        check(
            "filter-search-no-network", len([r for r in requests if "/ceo/work?" in r[1]]) == before
        )
        page.locator("#clearTaskSearch").click()
        page.locator("#taskSearch").fill("UI-no-matching-task-zzzzz")
        page.wait_for_selector("#giveEmpty [data-state=no-results]")
        shot("no-results")
        page.locator("[data-ui-action=clear-work-filters]").click()
        page.locator("#taskSort").select_option("title")
        check(
            "locale-sort",
            page.evaluate(
                "registerSnapshot.items.map(t=>t.title).sort((a,b)=>a.localeCompare(b,state.lang))[0]"
            )
            == page.locator("#giveList .work-open").first.inner_text(),
        )
        page.locator("#taskSort").select_option("newest")
        page.locator("#loadMoreTasks").click()
        check(
            "load-more-increments",
            page.locator("#giveList .work-row").count() == min(200, actual["total"]),
        )
        page.evaluate(
            "window.gateNode=document.querySelector('#giveList .work-row');"
            "window.gateFocus=gateNode.querySelector('.work-open');gateFocus.focus();"
        )
        timings = page.evaluate("""()=>{const t=performance.now();WorkUI.list(registerSnapshot);
          return {milliseconds:performance.now()-t,
            sameNode:gateNode===document.querySelector('#giveList .work-row'),
            focus:document.activeElement===gateFocus};}""")
        report["performance"]["actual_cached_refresh"] = timings
        check("keyed-refresh-preserves-node-focus", timings["sameNode"] and timings["focus"])
        check(
            "no-nested-queue-scroll",
            page.locator("#giveList").evaluate(
                'e=>getComputedStyle(e).maxHeight==="none"'
                ' && getComputedStyle(e).overflowY==="visible"'
            ),
        )
        page.keyboard.press("j")
        check(
            "j-navigates-row",
            page.locator("#giveList .work-open").nth(1).evaluate("e=>e===document.activeElement"),
        )
        page.keyboard.press("k")
        check(
            "k-navigates-row",
            page.locator("#giveList .work-open").first.evaluate("e=>e===document.activeElement"),
        )
        page.keyboard.press("/")
        check("slash-search", page.locator("#taskSearch").evaluate("e=>e===document.activeElement"))
        page.locator("#taskSearch").fill("jkn")
        check("typing-does-not-open-form", not page.locator("#newTaskForm").evaluate("e=>e.open"))
        clear_filters()
        page.locator("#taskSearch").evaluate("e=>e.blur()")
        page.locator("#newTaskBtn").focus()
        page.keyboard.press("n")
        check(
            "n-opens-form",
            page.locator("#newTaskForm").evaluate("e=>e.open")
            and page.locator("#goal").evaluate("e=>e===document.activeElement"),
        )
        page.locator("#goal").fill("")
        page.locator("#runBtn").click()
        check(
            "required-inline-error-no-write",
            page.locator("#goal").get_attribute("aria-invalid") == "true"
            and page.locator("#goalError").is_visible()
            and not report["blocked_requests"],
        )
        audit("form-invalid-axe", "#view-give")
        shot("form-invalid")
        page.locator("#newTaskForm summary").click()
        # One matrix; source and row population stay real, preferences use actual controls.
        for palette in page.evaluate("UIPalettes.items.map(p=>p.id)"):
            for mode in ["light", "dark"]:
                for density in ["comfortable", "compact"]:
                    name = f"{palette}-{mode}-{density}"
                    page.locator("#appearanceBtn").click()
                    page.locator(f'[data-shell-pref="theme"][data-shell-value="{mode}"]').click()
                    page.locator(f'[data-shell-palette="{palette}"]').click()
                    page.locator(
                        f'[data-shell-pref="density"][data-shell-value="{density}"]'
                    ).click()
                    page.keyboard.press("Escape")
                    stable()
                    audit(name + "-axe", "#view-give")
                    check(
                        name + "-overflow",
                        page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                    )
                    check(
                        name + "-zero-readable",
                        page.locator(
                            '#workStats [data-work-filter="in_flight"] strong'
                        ).inner_text()
                        == page.evaluate("num(registerSnapshot.in_flight)"),
                    )
                    shot(name)
        page.locator("#langEnBtn").click()
        stable()
        check("english-locale-number", page.evaluate("num(1192)") == "1,192")
        audit("english-list-axe", "#view-give")
        shot("english-list")
        page.locator("#langBtn").click()
        stable()
        check("vietnamese-locale-number", page.evaluate("num(1192)") == "1.192")
        # Detail, including the original authorized controller links, has no writes.
        page.locator('#workStats [data-work-filter="you"]').click()
        error_row = page.locator("#giveList .work-row:has(details)").first
        if error_row.count():
            error_row.locator("summary").click()
            source = error_row.locator("pre code").inner_text()
            error_row.locator("[data-ui-copy]").last.click()
            check("full-error-copy", page.evaluate("navigator.clipboard.readText()") == source)
            shot("expanded-error")
        task = page.locator("#giveList .work-open").first
        page.evaluate("window.scrollTo(0,650)")
        page.wait_for_timeout(80)
        task.click()
        page.wait_for_function("!!taskReport")
        detail = page.evaluate("taskReport")
        check(
            "detail-id-copy-control",
            page.locator("#taskIdentity [data-ui-copy]").get_attribute("data-ui-copy")
            == detail["task"]["id"],
        )
        audit("detail-overview-axe", "#view-task")
        shot("detail-overview")
        for section in ["steps", "assignment", "decisions", "log"]:
            page.locator('#taskTabs [href$="/' + section + '"]').click()
            page.wait_for_function(
                '!document.querySelector("[data-task-panel=' + section + ']").hidden'
            )
            audit("detail-" + section + "-axe", "#view-task")
            shot("detail-" + section)
        page.locator("#logSearch").fill("no-log-match-zzzzz")
        check("log-empty-filter", page.locator("#taskLogEmpty").is_visible())
        page.locator("#logSearch").fill("")
        log = page.locator("#taskLog .ui-log")
        log.locator("[data-ui-action=copy-log]").click()
        check(
            "log-copy-exact-page",
            page.evaluate("navigator.clipboard.readText()") == log.locator("code").inner_text(),
        )
        check("log-copy-page-scope", "trang" in log.locator("h3").inner_text())
        page.evaluate("go('#/give')")
        page.wait_for_selector("#giveList .work-row")
        page.wait_for_selector("#giveList [data-selected=true]")
        check("return-selection", page.locator("#giveList [data-selected=true]").count() == 1)
        for width in [360, 390, 768]:
            page.set_viewport_size({"width": width, "height": 844})
            stable()
            check(
                f"width-{width}-overflow",
                page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )
            audit(f"width-{width}-list-axe", "#view-give")
            shot(f"width-{width}-list")
            page.locator("#giveList .work-open").first.click()
            page.wait_for_function("!!taskReport")
            audit(f"width-{width}-detail-axe", "#view-task")
            shot(f"width-{width}-detail")
            page.evaluate("go('#/give')")
            page.wait_for_selector("#giveList .work-row")
        page.set_viewport_size({"width": 1440, "height": 900})
        clear_filters()
        # 5000 tasks are locally fulfilled GET fixtures, never persisted.
        statuses = [
            "created",
            "assigned",
            "running",
            "completed",
            "failed",
            "blocked",
            "waiting_for_approval",
            "waiting_for_input",
            "canceled",
            "cancelled",
            "expired",
            "pending",
            "new_status",
        ]
        fixtures = []
        for i in range(5000):
            status = statuses[i % len(statuses)]
            waiting = (
                "you"
                if status
                in {"failed", "blocked", "waiting_for_approval", "waiting_for_input", "pending"}
                else "an_agent"
                if status in {"assigned", "running"}
                else "nobody"
            )
            fixtures.append(
                {
                    **actual["items"][0],
                    "id": f"tsk_ui_fixture_{i:05d}",
                    "title": f"UI FIXTURE MEP Engineer {i:05d}",
                    "status": status,
                    "waiting_on": waiting,
                    "owner_name": "UI FIXTURE HR",
                    "children": i % 3,
                    "created_at": f"2026-10-07T00:{i % 60:02d}:00Z",
                    "last_error": (
                        'UI FIXTURE <img src=x onerror="window.xss=true"> '
                        + ("MEP error detail\n" * 120)
                    )
                    if status == "failed"
                    else None,
                }
            )
        totals = {
            "total": len(fixtures),
            "needs_you": sum(t["waiting_on"] == "you" for t in fixtures),
            "in_flight": sum(t["waiting_on"] == "an_agent" for t in fixtures),
            "settled": sum(t["waiting_on"] == "nobody" for t in fixtures),
        }

        def fixture_queue(route):
            from urllib.parse import parse_qs

            query = parse_qs(urlparse(route.request.url).query)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["200"])[0])
            route.fulfill(json={**totals, "items": fixtures[offset : offset + limit]})

        context.route("**/api/v1/ceo/work?*", fixture_queue)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("registerSnapshot?.total===5000")
        check("5000-initial-dom-bounded", page.locator("#giveList .work-row").count() == 100)
        check(
            "escaped-untrusted-error",
            page.locator("#giveList img").count() == 0 and not page.evaluate("!!window.xss"),
        )
        audit("5000-fixture-status-coverage-axe", "#view-give")
        shot("5000-fixture")
        benchmark = page.evaluate("""async()=>{
          const t=performance.now();document.getElementById('taskSearch').value='04999';
          await renderGive(null,false);const search=performance.now()-t;
          document.getElementById('taskSearch').value='';WorkUI.filter('');
          const u=performance.now();await renderGive(null,false);
          return {search_ms:search,cached_ms:performance.now()-u,
            dom:document.querySelectorAll('#giveList .work-row').length};
        }""")
        report["performance"]["fixture_5000"] = benchmark
        check(
            "5000-cached-latency-budget",
            benchmark["search_ms"] < 500 and benchmark["cached_ms"] < 500,
            benchmark,
        )
        page.locator("#taskSearch").fill("04999")
        page.wait_for_function("document.querySelectorAll('#giveList .work-row').length===1")
        check(
            "5000-last-task-search-result",
            "04999" in page.locator("#giveList .work-open").inner_text(),
        )
        clear_filters()
        # Load 5000 to measure incremental updates after a long session, not only 100.
        full_benchmark = page.evaluate("""()=>{const t=performance.now();let last=0;
          while(!document.getElementById('loadMoreTasks').hidden){
            const s=performance.now();document.getElementById('loadMoreTasks').click();
            last=performance.now()-s;}
          const r=performance.now();WorkUI.list(registerSnapshot);
          return {all_load_ms:performance.now()-t,last_batch_ms:last,
            refresh_ms:performance.now()-r,
            rows:document.querySelectorAll('#giveList .work-row').length};}""")
        report["performance"]["fixture_5000_all_loaded"] = full_benchmark
        check(
            "5000-full-loaded-refresh-budget",
            full_benchmark["refresh_ms"] < 1000 and full_benchmark["last_batch_ms"] < 1000,
            full_benchmark,
        )
        check(
            "5000-final-load-focus",
            page.locator("#giveList").evaluate("e=>e.contains(document.activeElement)"),
        )
        check(
            "no-horizontal-after-5000",
            page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
        )

        # Empty / load error / skeleton use local GET overrides.
        def empty_queue(route):
            route.fulfill(
                json={"total": 0, "needs_you": 0, "in_flight": 0, "settled": 0, "items": []}
            )

        context.unroute("**/api/v1/ceo/work?*", fixture_queue)
        context.route("**/api/v1/ceo/work?*", empty_queue)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#giveEmpty [data-state=empty]")
        audit("empty-axe", "#view-give")
        shot("empty-fixture")
        context.unroute("**/api/v1/ceo/work?*", empty_queue)

        def fail_queue(route):
            route.fulfill(status=503, json={"detail": "UI FIXTURE unavailable"})

        context.route("**/api/v1/ceo/work?*", fail_queue)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#registerError .ui-callout")
        check("error-no-invented-zero", not page.locator("#workStats strong").count())
        audit("load-error-axe", "#view-give")
        shot("load-error-fixture")
        context.unroute("**/api/v1/ceo/work?*", fail_queue)
        page.locator("[data-ui-action=retry-work-list]").click()
        page.wait_for_selector("#giveList .work-row")
        check(
            "retry-recovers-real-data", page.evaluate("registerSnapshot.total") == actual["total"]
        )
        # Authorized form is exercised with local mock POST replies, no requests reach API.
        posts = []

        def fake_create(route):
            posts.append({"path": "/tasks", "payload": route.request.post_data_json})
            route.fulfill(status=503, json={"detail": "UI FIXTURE create refused"})

        context.route("**/api/v1/tasks", fake_create)
        page.locator("#newTaskBtn").click()
        page.locator("#goal").fill("UI FIXTURE MEP hiring brief")
        page.locator("#runBtn").click()
        page.wait_for_selector("#formError:not([hidden])")
        check(
            "create-failure-input-retained",
            page.locator("#goal").input_value() == "UI FIXTURE MEP hiring brief"
            and not page.locator("#runBtn").is_disabled(),
        )
        audit("create-failure-axe", "#view-give")
        shot("create-error-fixture")
        check(
            "create-payload-preserved",
            posts[0]["payload"]["goal"] == "UI FIXTURE MEP hiring brief"
            and posts[0]["payload"]["start_workflow"] is False,
        )
        context.unroute("**/api/v1/tasks", fake_create)
        page.emulate_media(reduced_motion="reduce")
        page.locator("#newTaskForm summary").click()
        page.locator("#newTaskBtn").click()
        check(
            "reduced-motion-form",
            page.locator("#newTaskForm .ui-panel-body").evaluate(
                "e=>getComputedStyle(e).animationName"
            )
            == "none",
        )
        browser.close()
    report["source_sha256"] = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path("src/ai_orchestrator/web").iterdir()
        if p.is_file()
    }
    check("no-runtime-errors", not report["errors"], report["errors"])
    check(
        "no-unapproved-write-or-external-request",
        not report["blocked_requests"],
        report["blocked_requests"],
    )
    report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
    (args.out / "stage-4-gate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "checks": len(report["checks"]),
                "screenshots": len(report["screenshots"]),
                "performance": report["performance"],
                "failed": [c for c in report["checks"] if not c["passed"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(0 if report["gate"] == "passed" else 1)


if __name__ == "__main__":
    main()
