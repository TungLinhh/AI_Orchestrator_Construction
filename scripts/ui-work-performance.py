"""Read-only before/after list measurements and browser-local create/run fixtures."""

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--before", default="7a7c860")
    parser.add_argument("--out", type=Path, default=Path("artifacts/ui-shots/04-work"))
    args = parser.parse_args()
    base = "http://127.0.0.1:8100"
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    results = {
        "baseline_revision": args.before,
        "measurements": {},
        "checks": [],
        "scope": "Single Chromium sample; local POST fixtures are not real execution",
        "screenshots": [],
    }
    sources = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").glob("*")
        if f.is_file()
    }
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as p:
        root = Path(folder)
        paths = subprocess.check_output(
            ["git", "ls-tree", "--name-only", args.before + ":src/ai_orchestrator/web"], text=True
        ).splitlines()
        for name in paths:
            if "." in name:
                (root / name).write_bytes(
                    subprocess.check_output(
                        ["git", "show", args.before + ":src/ai_orchestrator/web/" + name]
                    )
                )
        spec = importlib.util.spec_from_file_location("baseline_page", root / "page.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        old_html = module.render_console(root)
        served = urlopen(base + "/api/v1/ui?org=" + args.org).read().decode()
        old_html = old_html.replace("__ORG_ID__", args.org).replace(
            "__AUTH_OFF__", re.search(r"const AUTH_OFF = (true|false)", served).group(1)
        )
        for key in ["BUILD", "PROVIDER"]:
            value = re.search(r"const " + key + r' = "([^"\n]*)"', served).group(1)
            old_html = old_html.replace("__" + key + "__", value)
        browser = p.chromium.launch(executable_path=args.chromium)
        for label in ["before", "after"]:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            blocked = []

            def guard(route, request, blocked=blocked):
                r = route.request
                if urlparse(r.url).hostname not in {"localhost", "127.0.0.1"} or r.method != "GET":
                    blocked.append(r.url)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.add_init_script("""window.longTasks=[];
              new PerformanceObserver(l=>longTasks.push(...l.getEntries().map(e=>
                ({start:e.startTime,duration:e.duration})))).observe({type:'longtask',buffered:true});""")
            if label == "before":
                context.route(
                    "**/api/v1/ui?*",
                    lambda route: route.fulfill(body=old_html, content_type="text/html"),
                )
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.goto(base + "/api/v1/ui?org=" + args.org + "#/give")
            try:
                page.wait_for_selector("#giveList .row", state="attached", timeout=10000)
            except Exception:
                print(
                    {
                        "label": label,
                        "errors": errors,
                        "blocked": blocked,
                        "html": page.locator("#giveList").inner_html(),
                        "hash": page.evaluate("location.hash"),
                    },
                    flush=True,
                )
                raise
            page.evaluate("async()=>{await document.fonts.ready}")
            initial = page.evaluate("""()=>({corpus:registerSnapshot.total,
              rows:document.querySelector('#giveList').children.length,
              navigation_to_rows_ms:performance.now(),startup_long_tasks:longTasks.slice()})""")
            cached = page.evaluate("""async()=>{const t=performance.now();
              await renderGive(null,false);
              return performance.now()-t;}""")
            if label == "after":
                page.evaluate("""()=>{while(!document.getElementById('loadMoreTasks').hidden)
                  document.getElementById('loadMoreTasks').click();}""")
            scroll = page.evaluate("""async()=>{const list=document.getElementById('giveList');
              const inner=getComputedStyle(list).maxHeight!=='none';
              const start=performance.now(),frames=[];let prev=start;
              for(let i=0;i<=60;i++){await new Promise(requestAnimationFrame);
                const now=performance.now();frames.push(now-prev);prev=now;
                if(inner)list.scrollTop=(list.scrollHeight-list.clientHeight)*i/60;
                else window.scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*i/60);}
              await new Promise(r=>setTimeout(r,100));
              frames.sort((a,b)=>a-b);
              return {duration_ms:performance.now()-start,
                frame_p95_ms:frames[Math.floor(frames.length*.95)],
                long_tasks:longTasks.filter(e=>e.start>=start),rows:list.children.length,nested_scroll:inner};}""")
            results["measurements"][label] = {
                "initial": initial,
                "cached_render_ms": cached,
                "scroll": scroll,
            }
            results["checks"].append(
                {
                    "name": label + "-read-only-no-errors",
                    "passed": not blocked and not errors,
                    "blocked": blocked,
                    "errors": errors,
                }
            )
            if label == "after":
                task_id = "ui_fixture_create_run"
                report = {
                    "task": {
                        "id": task_id,
                        "title": "UI FIXTURE create/run",
                        "goal": "UI FIXTURE brief",
                        "status": "running",
                        "task_type": "analysis",
                    },
                    "summary": {},
                    "tree": [],
                    "delegations": [],
                    "events": [],
                    "approvals": [],
                    "tree_executions": [],
                    "available_actions": [],
                }
                context.route(
                    "**/api/v1/tasks/" + task_id + "/report?*",
                    lambda route, request, report=report: route.fulfill(json=report),
                )
                calls = []

                def create(route, request, calls=calls, page=page, task_id=task_id):
                    calls.append({"kind": "create", "payload": route.request.post_data_json})
                    results["checks"].append(
                        {
                            "name": "create-busy-state",
                            "passed": page.locator("#runBtn").is_disabled()
                            and page.locator("#runBtn").get_attribute("aria-busy") == "true",
                        }
                    )
                    route.fulfill(json={"task_id": task_id})

                def run(route, request, calls=calls):
                    calls.append({"kind": "run", "payload": route.request.post_data_json})
                    route.fulfill(json={"started": True, "already_running": False})

                context.route("**/api/v1/tasks", create)
                context.route("**/api/v1/tasks/" + task_id + "/run", run)
                page.locator("#newTaskBtn").click()
                page.locator("#goal").fill("UI FIXTURE brief")
                page.locator("#runBtn").click()
                page.wait_for_function("taskReport?.task.id==='ui_fixture_create_run'")
                results["checks"].append(
                    {
                        "name": "create-then-run-and-detail",
                        "passed": [c["kind"] for c in calls] == ["create", "run"]
                        and calls[0]["payload"]["start_workflow"] is False
                        and calls[1]["payload"] == {}
                        and not page.locator("#runBtn").is_disabled(),
                    }
                )
                page.screenshot(path=str(out / "create-success-fixture.png"))
                results["screenshots"].append("create-success-fixture.png")
                page.evaluate("go('#/give')")
                page.wait_for_selector("#giveList .work-row")
                context.unroute("**/api/v1/tasks/" + task_id + "/run", run)
                report["task"]["status"] = "created"
                context.route(
                    "**/api/v1/tasks/" + task_id + "/run",
                    lambda route: route.fulfill(
                        status=503, json={"error": {"message": "UI FIXTURE run unavailable"}}
                    ),
                )
                page.locator("#newTaskBtn").click()
                page.locator("#goal").fill("UI FIXTURE brief")
                page.locator("#runBtn").click()
                page.wait_for_function("taskReport?.task.id==='ui_fixture_create_run'")
                page.wait_for_selector(".ui-toast[data-tone=danger]")
                results["checks"].append(
                    {
                        "name": "created-but-run-failed-visible",
                        "passed": "UI FIXTURE run unavailable"
                        in page.locator(".ui-toast[data-tone=danger]").inner_text(),
                    }
                )
                page.screenshot(path=str(out / "run-error-fixture.png"))
                results["screenshots"].append("run-error-fixture.png")
                page.evaluate("go('#/give')")
                page.wait_for_selector("#giveList .work-row")
                page.set_viewport_size({"width": 1920, "height": 1080})
                page.screenshot(path=str(out / "width-1920-list.png"))
                results["screenshots"].append("width-1920-list.png")
                results["checks"].append(
                    {
                        "name": "width-1920-no-overflow",
                        "passed": page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
                    }
                )

                def pending_queue(route, request, page=page):
                    page.wait_for_selector("#registerLoading:not([hidden])")
                    results["checks"].append(
                        {
                            "name": "pending-get-loading-state",
                            "passed": page.locator("#giveList").get_attribute("aria-busy")
                            == "true",
                        }
                    )
                    page.screenshot(path=str(out / "loading-fixture.png"))
                    results["screenshots"].append("loading-fixture.png")
                    route.continue_()

                context.route("**/api/v1/ceo/work?*", pending_queue)
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#giveList .work-row")
                context.unroute("**/api/v1/ceo/work?*", pending_queue)
                results["checks"].append(
                    {
                        "name": "fixture-writes-local-only",
                        "passed": not blocked and not errors,
                        "blocked": blocked,
                        "errors": errors,
                    }
                )
            context.close()
        browser.close()
    results["source_sha256"] = {
        str(f): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in Path("src/ai_orchestrator/web").glob("*")
        if f.is_file()
    }
    results["checks"].append(
        {"name": "source-frozen", "passed": sources == results["source_sha256"]}
    )
    results["gate"] = "passed" if all(c["passed"] for c in results["checks"]) else "failed"
    (out / "performance-and-form.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    raise SystemExit(results["gate"] != "passed")


if __name__ == "__main__":
    main()
