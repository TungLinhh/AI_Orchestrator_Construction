"""Capture read-only states missing from the live UI catalogue (browser GET fixtures only)."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/00-baseline"))
    args = parser.parse_args()
    if urlparse(args.base).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("Only local workspaces are supported")
    report = {"captures": [], "blocked_requests": [], "errors": [], "fixture_only": True}
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        context = browser.new_context(locale="vi-VN")

        def guard(route):
            request = route.request
            if urlparse(request.url).hostname not in {
                "localhost",
                "127.0.0.1",
            } or request.method not in {"GET", "HEAD", "OPTIONS"}:
                report["blocked_requests"].append({"method": request.method, "url": request.url})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        context.add_init_script("""localStorage.setItem('ao-lang-v1','vi');
          localStorage.setItem('onx-ui-preferences-v1',JSON.stringify({theme:'light',palette:'navy',
            density:'comfortable',motion:'auto'}));""")
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        page.goto(f"{args.base}/api/v1/ui?org={args.org}#/give", wait_until="domcontentloaded")
        page.wait_for_function("typeof registerSnapshot!=='undefined' && registerSnapshot!==null")
        task_id = page.evaluate("""async () => {
          for(const t of registerSnapshot.items.filter(t=>!t.parent_task_id &&
            !['completed','failed','canceled','expired'].includes(t.status)).slice(0,20)) {
            const r=await apiGet('/tasks/'+t.id+'/report');
            if(r.available_actions.includes('cancel'))return t.id;
          } throw Error('No live root permits cancellation-dialog observation');
        }""")
        departments = page.evaluate("apiGet('/departments')")
        unit_keys = [
            departments["chief"]["key"],
            departments["second_tier"][0]["key"],
            departments["offices"][0]["key"],
        ]

        def visit(hash_route):
            page.locator("#sheet").evaluate("el=>{if(el.open)el.close()}")
            page.evaluate("r=>go('#/'+r)", hash_route)
            page.evaluate(
                "async()=>{await route();if(watchTimer)clearInterval(watchTimer);"
                "if(givePoll)clearInterval(givePoll)}"
            )
            page.evaluate("window.scrollTo(0,0)")

        def capture(name, fixture=False):
            page.screenshot(path=str(args.out / (name + ".png")))
            report["captures"].append(
                {
                    "file": name + ".png",
                    "route": page.url,
                    "viewport": page.viewport_size,
                    "browser_get_fixture": fixture,
                    "page_error": page.locator("#pageError").inner_text()
                    if page.locator("#pageError").is_visible()
                    else None,
                }
            )

        project = "prj_ui_fixture"
        document = "doc_ui_fixture"
        fixtures = {
            f"projects/{project}": {
                "id": project,
                "name": "[UI FIXTURE] Dự án kiểm tra bố cục",
                "code": "UI-ONLY",
                "status": "active",
                "address": "Mô phỏng trong browser",
                "planned_completion": None,
            },
            f"projects/{project}/workspace": {"late": [], "unmeasured": []},
            f"projects/{project}/wbs": {"items": []},
            f"documents/{document}": {
                "id": document,
                "title": "[UI FIXTURE] Biểu mẫu kiểm tra bố cục",
                "code": "UI-DOC",
                "classification": "internal",
                "live_version_no": None,
                "compliance": {"required": 0, "acknowledged": 0, "outstanding": 0},
                "versions": [],
                "distributions": [],
            },
        }
        for path, payload in fixtures.items():
            page.route(f"**/api/v1/{path}", lambda r, _request, data=payload: r.fulfill(json=data))
        for mode in ("light", "dark"):
            page.evaluate("m=>UIPreferences.save({theme:m,motion:'auto'})", mode)
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for alias in ("work", "library", "business", "operations"):
                    visit(alias)
                    capture(f"alias-{alias}-{mode}-{width}")
                for name, route in (
                    ("project-detail", f"business/projects/{project}"),
                    ("document-detail", f"business/documents/{document}"),
                ):
                    visit(route)
                    capture(f"fixture-{name}-{mode}-{width}", fixture=True)
                for key in unit_keys:
                    visit("dept/" + key)
                    capture(f"unit-{key}-{mode}-{width}")
                visit(f"give/{task_id}")
                page.locator("#cancelTask").click()
                capture(f"cancel-confirmation-{mode}-{width}")
                page.locator("#sheet").evaluate("el=>el.close()")
                visit("work/issues")
                row = page.locator("#issueList details").first
                row.evaluate("el=>el.open=true")
                capture(f"issue-expanded-{mode}-{width}")
                visit("give")
                page.locator("#giveList").scroll_into_view_if_needed()
                capture(f"work-list-scrolled-{mode}-{width}")
        visit("give")
        page.set_viewport_size({"width": 1440, "height": 900})
        source = page.evaluate("registerSnapshot")
        for state in ("empty", "all-zero", "no-results", "long-title", "long-error", "error"):
            visit("give")
            data = copy.deepcopy(source)
            if state in {"empty", "all-zero"}:
                data.update(items=[], total=0, needs_you=0, in_flight=0, settled=0)
            elif state in {"long-title", "long-error"}:
                data["items"][0]["title" if state == "long-title" else "last_error"] = (
                    "UI fixture · ệ ữ ặ ẫ ỗ ử · " * 70
                )

            def respond(route, request, payload=data, fixture=state):
                if fixture == "error":
                    route.fulfill(status=503, json={"detail": "UI fixture: service unavailable"})
                    return
                query = parse_qs(urlparse(request.url).query)
                start = int(query.get("offset", ["0"])[0])
                size = int(query.get("limit", ["200"])[0])
                route.fulfill(json={**payload, "items": payload["items"][start : start + size]})

            page.route("**/api/v1/ceo/work?*", respond)
            page.evaluate("async()=>{registerSnapshot=null;await renderGive()}")
            if state == "no-results":
                page.locator("#taskSearch").fill("ui-fixture-no-match-836412")
                assert page.locator("#giveEmpty").is_visible()
            elif state in {"empty", "all-zero"}:
                assert page.evaluate(
                    "registerSnapshot.total===0&&registerSnapshot.items.length===0"
                )
            elif state in {"long-title", "long-error"}:
                field = "title" if state == "long-title" else "last_error"
                assert page.evaluate(
                    "f=>registerSnapshot.items[0][f].includes('UI fixture')", field
                )
            else:
                assert page.locator("#pageError").is_visible()
            capture("state-fixture-" + state, fixture=True)
            report["captures"][-1]["expected_page_error"] = state == "error"
            report["captures"][-1]["state"] = state
            page.unroute("**/api/v1/ceo/work?*")
            page.locator("#taskSearch").fill("")
        visit("give")
        page.set_viewport_size({"width": 1440, "height": 900})
        held = []
        page.route("**/api/v1/ceo/work?*", lambda r: held.append(r))
        page.evaluate("()=>{registerSnapshot=null;void renderGive()}")
        page.wait_for_timeout(200)
        assert held, "Loading probe must hold an actual pending GET"
        capture("fixture-loading-held-get", fixture=True)
        report["captures"][-1]["state"] = "loading"
        for request in held:
            request.fulfill(json={"items": [], "total": 0})
        page.unroute("**/api/v1/ceo/work?*")
        page.emulate_media(reduced_motion="reduce")
        report["os_reduced_motion_running_transforms"] = page.evaluate("""document.getAnimations()
          .filter(a=>a.playState==='running' && a.effect.getKeyframes().some(k=>k.transform))
          .map(a=>a.animationName||a.id)""")
        page.evaluate("document.body.style.zoom='2'")
        capture("zoom-200-percent")
        report["zoom_overflow"] = page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth+1"
        )
        # Native/browser prompt dialogs cannot be included in a page screenshot.
        # Inventory them in the audit instead of triggering a potentially mutating flow.
        browser.close()
    (args.out / "state-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    if (
        report["blocked_requests"]
        or report["errors"]
        or any(c["page_error"] and not c.get("expected_page_error") for c in report["captures"])
    ):
        raise SystemExit("State probe failed; inspect state-report.json")
    print(f"{len(report['captures'])} additional states captured; zero API writes.")


if __name__ == "__main__":
    main()
