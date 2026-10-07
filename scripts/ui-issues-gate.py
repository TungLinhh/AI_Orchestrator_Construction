"""Issues page browser gate; read-only real corpus and browser-local failure fixtures."""

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/05-issues"))
    args = parser.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    result = {
        "gate": "failed",
        "phase": 5,
        "page": "issues",
        "checks": [],
        "screenshots": [],
        "errors": [],
        "console_errors": [],
        "expected_fixture_console_errors": [],
        "blocked_requests": [],
        "final_ui_acceptance": False,
    }

    def check(name, passed, detail=None):
        result["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )

        def guard(route):
            r = route.request
            if urlparse(r.url).hostname not in {"localhost", "127.0.0.1"} or r.method != "GET":
                result["blocked_requests"].append({"method": r.method, "url": r.url})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        context.add_init_script(path="artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js")
        page = context.new_page()
        page.on("pageerror", lambda error: result["errors"].append(str(error)))
        expected_error_urls = set()

        def console(message):
            if message.type not in {"warning", "error"}:
                return
            item = {"type": message.type, "text": message.text, "location": message.location}
            if message.location.get("url") in expected_error_urls and message.text.startswith(
                "Failed to load resource"
            ):
                result["expected_fixture_console_errors"].append(item)
            else:
                result["console_errors"].append(item)

        page.on("console", console)
        requests = []
        page.on("request", lambda r: requests.append((r.method, r.url)))
        url = "http://127.0.0.1:8100/api/v1/ui?org=" + args.org + "#/work/issues"

        def shot(name):
            page.screenshot(path=str(out / (name + ".png")))
            if name + ".png" not in result["screenshots"]:
                result["screenshots"].append(name + ".png")

        def audit(name):
            violations = page.evaluate("""async()=>{const r=await axe.run('#view-work');
              return r.violations.map(v=>({id:v.id,impact:v.impact,
                targets:v.nodes.map(n=>n.target)}));}""")
            check(name + "-axe", not violations, violations)
            check(
                name + "-overflow",
                page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector("#issueList [data-task]")
        page.evaluate("async()=>{await document.fonts.ready}")
        actual = page.evaluate("""async()=>{const q=await loadRegister();return q;}""")
        issue_items = [
            t
            for t in actual["items"]
            if t["status"] in {"failed", "blocked", "canceled", "cancelled"}
            or (t["status"] == "created" and t["waiting_on"] == "you")
        ]
        result["actual_corpus"] = {"tasks": actual["total"], "issues": len(issue_items)}
        check(
            "bounded-initial-rows",
            page.locator("#issueList [data-task]").count() == min(100, len(issue_items)),
        )
        check(
            "metric-count-from-register",
            page.locator("#needStats strong").first.inner_text()
            == page.evaluate("num(" + str(len(issue_items)) + ")"),
        )
        check("issues-route-hides-approvals", page.locator("#approvalsCard").is_hidden())
        shot("after-desktop")
        for palette in ["navy", "teal", "indigo", "forest", "copper", "graphite"]:
            for mode in ["light", "dark"]:
                page.evaluate(
                    "p=>UIPreferences.save({...UIPreferences.get(),...p})",
                    {"palette": palette, "theme": mode},
                )
                page.wait_for_timeout(230)
                check(
                    palette + "-" + mode + "-applied",
                    page.evaluate("document.documentElement.dataset.colorMode") == mode,
                )
                audit(palette + "-" + mode)
                if palette == "navy":
                    shot("desktop-" + mode)
        page.evaluate("UIPreferences.save({...UIPreferences.get(),palette:'navy',theme:'light'})")
        page.locator('#issueFilter [data-i="failed"]').click()
        expected = sum(t["status"] == "failed" for t in issue_items)
        check(
            "filter-count",
            page.locator("#issueSub").inner_text().split(" / ")[0]
            == page.evaluate("num(" + str(expected) + ")"),
        )
        before = sum("/ceo/work" in u for _, u in requests)
        page.locator("#issueSearch").fill(issue_items[-1]["id"])
        page.locator('#issueFilter [data-i=""]').click()
        page.wait_for_timeout(160)
        check("search-entire-corpus", page.locator("#issueList [data-task]").count() == 1)
        check("filter-search-no-network", sum("/ceo/work" in u for _, u in requests) == before)
        page.locator("#issueSearch").fill("no such UI FIXTURE issue")
        page.wait_for_selector('#issueEmpty [data-state="no-results"]')
        audit("no-results")
        shot("no-results")
        page.locator('[data-ui-action="clear-issue-filters"]').click()
        page.locator("#issueList summary").first.click()
        row = page.locator("#issueList [data-task]").first
        code = row.locator("code").inner_text()
        row.locator('[data-ui-action="copy-log"]').click()
        check("full-error-copy", page.evaluate("navigator.clipboard.readText()") == code)
        audit("expanded")
        shot("expanded-error")
        stable = page.evaluate("""()=>{const row=document.querySelector('#issueList [data-task]');
          window.savedIssue=row;row.querySelector('summary').focus();return row.dataset.task;}""")
        page.evaluate("async()=>await IssuesUI.load()")
        page.wait_for_function("!document.getElementById('refreshIssues').disabled")
        check(
            "refresh-preserves-node-open-focus",
            page.evaluate("""()=>savedIssue===document.querySelector('#issueList [data-task]') &&
          savedIssue.querySelector('details').open &&
          savedIssue.contains(document.activeElement)"""),
        )
        if len(issue_items) > 100:
            page.locator("#loadMoreIssues").click()
            check(
                "incremental-load",
                page.locator("#issueList [data-task]").count() == min(200, len(issue_items)),
            )
        page.locator("#langEnBtn").click()
        page.wait_for_function(
            "document.getElementById('issueSearchLabel').textContent==='Search issues'"
        )
        audit("english")
        shot("english")
        page.locator("#langBtn").click()
        for width in [360, 390, 768]:
            page.set_viewport_size({"width": width, "height": 844})
            for mode in ["light", "dark"]:
                page.evaluate("m=>UIPreferences.save({...UIPreferences.get(),theme:m})", mode)
                page.wait_for_timeout(230)
                audit(str(width) + "-" + mode)
                shot(str(width) + "-" + mode)
        page.set_viewport_size({"width": 1440, "height": 900})

        # Retry failure: keep handler/endpoint, fulfill POST locally and preserve the row.
        retry_calls = []

        def failed_retry(route):
            expected_error_urls.add(route.request.url)
            retry_calls.append(route.request.post_data_json)
            check("retry-disabled-while-sending", row.locator("[data-retry]").is_disabled())
            route.fulfill(
                status=409, json={"error": {"message": "UI FIXTURE workflow retry refused"}}
            )

        context.route("**/api/v1/tasks/*/retry", failed_retry)
        row = page.locator('#issueList [data-task="' + stable + '"]')
        if not row.locator("details").evaluate("e=>e.open"):
            row.locator("summary").click()
        row.locator("[data-retry]").click()
        page.wait_for_function(
            "[...document.querySelectorAll('[data-retry]')].every(b=>!b.disabled)"
        )
        check(
            "retry-failure-retained",
            retry_calls == [{}] and page.locator("#issueList [data-task]").count() > 0,
        )
        check(
            "retry-refusal-visible",
            "UI FIXTURE workflow retry refused" in page.locator(".ui-toast").last.inner_text(),
        )
        context.unroute("**/api/v1/tasks/*/retry", failed_retry)

        def fail_queue(route):
            expected_error_urls.add(route.request.url)
            route.fulfill(
                status=503, json={"error": {"message": "UI FIXTURE register unavailable"}}
            )

        context.route("**/api/v1/ceo/work?*", fail_queue)
        page.locator("#refreshIssues").click()
        page.wait_for_selector("#issueError:not([hidden])")
        check("refresh-error-retains-data", page.locator("#issueList [data-task]").count() > 0)
        page.locator('#issueFilter [data-i="failed"]').click()
        check("cached-filter-keeps-stale-warning", page.locator("#issueError").is_visible())
        audit("stale-error")
        shot("stale-error-fixture")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#issueError:not([hidden])")
        check("initial-error-no-zero-metrics", not page.locator("#needStats strong").count())
        audit("initial-error")
        shot("initial-error-fixture")
        context.unroute("**/api/v1/ceo/work?*", fail_queue)
        page.locator('[data-ui-action="reload-issues"]').click()
        page.wait_for_selector("#issueList [data-task]")

        def empty_queue(route):
            route.fulfill(
                json={"items": [], "total": 0, "needs_you": 0, "in_flight": 0, "settled": 0}
            )

        context.route("**/api/v1/ceo/work?*", empty_queue)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector('#issueEmpty [data-state="empty"]')
        audit("empty")
        shot("empty-fixture")
        context.unroute("**/api/v1/ceo/work?*", empty_queue)

        def pending_queue(route):
            page.wait_for_selector("#issueLoading:not([hidden])")
            check(
                "loading-aria-busy", page.locator("#issueList").get_attribute("aria-busy") == "true"
            )
            shot("loading-fixture")
            route.continue_()

        context.route("**/api/v1/ceo/work?*", pending_queue)
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#issueList [data-task]")
        context.unroute("**/api/v1/ceo/work?*", pending_queue)
        page.evaluate("go('#/work/approvals')")
        page.wait_for_selector("#approvalsCard")
        page.wait_for_function("!!document.querySelector('#needStats .ui-metric-strip')")
        check(
            "approvals-route-preserved",
            page.locator("#issuesCard").is_hidden()
            and page.locator("#needStats [data-ui-metric]").count() == 2
            and page.evaluate(
                "Number(document.querySelector('#needStats [data-ui-metric=all] strong')"
                ".textContent.replace(/[^0-9]/g,''))===state.inbox.length"
            ),
        )
        page.evaluate("go('#/work')")
        page.wait_for_function("!!document.querySelector('#needStats .stat')")
        check("legacy-combined-route-preserved", page.locator("#needStats .stat").count() == 4)
        page.evaluate("go('#/give')")
        page.wait_for_selector("#giveList .work-row")
        check("work-page-regression", page.locator("#workStats strong").count() == 4)
        page.evaluate("go('#/_styleguide')")
        page.wait_for_selector("#uiStyleguide .ui-metric-strip")
        page.locator('#uiStyleguide [data-ui-metric="1"]').click()
        check(
            "guide-metric-selection",
            page.locator('#uiStyleguide [data-ui-metric="1"]').get_attribute("aria-pressed")
            == "true",
        )
        violations = page.evaluate(
            "async()=> (await axe.run('#uiStyleguide .ui-metric-strip')).violations.map(v=>v.id)"
        )
        check("guide-new-metrics-axe", not violations, violations)
        browser.close()
    check("no-unexpected-console-errors", not result["console_errors"], result["console_errors"])
    check("no-runtime-errors", not result["errors"], result["errors"])
    check(
        "no-external-or-operational-write",
        not result["blocked_requests"],
        result["blocked_requests"],
    )
    result["source_sha256"] = {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path("src/ai_orchestrator/web").iterdir()
        if path.is_file()
    }
    result["gate"] = "passed" if all(c["passed"] for c in result["checks"]) else "failed"
    (out / "gate.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "gate": result["gate"],
                "checks": len(result["checks"]),
                "screenshots": len(result["screenshots"]),
                "failed": [c for c in result["checks"] if not c["passed"]],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(result["gate"] != "passed")


if __name__ == "__main__":
    main()
