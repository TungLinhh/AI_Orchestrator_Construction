"""Phase 5/6 interaction acceptance. Browser fixtures are labelled and never written."""

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
    args = parser.parse_args()
    out = Path("artifacts/ui-shots/06-interactions")
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "gate": "failed",
        "checks": [],
        "captures": [],
        "errors": [],
        "console": [],
        "expected_fixture_console": [],
        "blocked_requests": [],
        "fixture_posts": [],
    }

    def check(name, passed, detail=None):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        if not passed:
            print("FAIL", name, str(detail)[:800], flush=True)

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
                report["blocked_requests"].append({"method": r.method, "url": r.url})
                route.abort()
            else:
                route.continue_()

        context.route("**/*", guard)
        context.add_init_script(path="artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js")
        context.add_init_script("localStorage.setItem('ao-lang-v1','vi')")
        context.add_init_script("""window.__introAnimations=[];
          const originalAnimate=Element.prototype.animate;
          Element.prototype.animate=function(frames,options){
            window.__introAnimations.push({parent:this.parentElement?.id,
              duration:options?.duration,delay:options?.delay || 0});
            return originalAnimate.call(this,frames,options);
          };""")
        page = context.new_page()
        page.on("pageerror", lambda e: report["errors"].append(str(e)))
        expected = set()

        def console(m):
            if m.type not in {"warning", "error"}:
                return
            item = {"text": m.text, "location": m.location}
            if m.location.get("url") in expected and m.text.startswith("Failed to load resource"):
                report["expected_fixture_console"].append(item)
            else:
                report["console"].append(item)

        page.on("console", console)
        url = "http://127.0.0.1:8100/api/v1/ui?org=" + args.org
        page.goto(url + "#/give", wait_until="domcontentloaded")
        page.wait_for_selector("#giveList .work-row")
        page.evaluate("async()=>await document.fonts.ready")
        task = page.evaluate("registerSnapshot.items[0]")
        intro = page.evaluate(
            "__introAnimations.filter(a=>['giveList','workStats'].includes(a.parent))"
        )
        check("intro-bounded-rows", 0 < sum(a["parent"] == "giveList" for a in intro) <= 12)
        check("intro-bounded-stats", 0 < sum(a["parent"] == "workStats" for a in intro) <= 4)
        check("intro-finishes-within-400ms", all(a["duration"] + a["delay"] <= 400 for a in intro))
        report["intro_animations"] = intro

        def shot(name):
            page.screenshot(path=str(out / (name + ".png")))
            report["captures"].append(name + ".png")

        def audit(name):
            # Await the actual entrance, rather than assuming a timer equals painted frames.
            page.evaluate("""async()=>{await Promise.all(document.getAnimations()
              .filter(a=>a.effect.getTiming().iterations!==Infinity)
              .map(a=>a.finished.catch(()=>{})));
              await new Promise(requestAnimationFrame);}""")
            bad = page.evaluate("""async()=>{const r=await axe.run(document,{runOnly:{type:'tag',
              values:['wcag2a','wcag2aa','wcag21aa','wcag22aa']}});return r.violations
              .filter(v=>['critical','serious'].includes(v.impact)).map(v=>({id:v.id,
                nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}));}""")
            check(name + "-axe", not bad, bad)
            check(
                name + "-overflow",
                page.evaluate("document.documentElement.scrollWidth<=innerWidth"),
            )

        page.locator("#commandBtn").focus()
        page.keyboard.press("Control+k")
        page.wait_for_selector("#commandInput")
        check("combobox-in-modal", page.locator("dialog[open] #commandInput").count() == 1)
        page.wait_for_function(
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill(task["id"])
        check("search-id", page.locator("#commandResults [role=option]").count() == 1)
        check(
            "active-descendant",
            page.locator("#commandInput").get_attribute("aria-activedescendant")
            == "command-option-0",
        )
        audit("palette-task")
        shot("palette-task")
        page.keyboard.press("Escape")
        page.wait_for_function("document.activeElement.id==='commandBtn'")
        check("escape-restores-focus", page.locator("dialog[open]").count() == 0)
        page.keyboard.press("Meta+k")
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill(task.get("owner_name") or task["title"])
        check("search-owner-title", page.locator("#commandResults [role=option]").count() > 0)
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill(task["id"])
        page.keyboard.press("Enter")
        page.wait_for_function(
            "location.hash.includes('"
            + task["id"]
            + "') && !document.getElementById('view-task').hidden"
        )
        check("command-opens-selected-task", task["id"] in page.url)
        page.locator("#commandBtn").click()
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill("Màu nhấn: Than chì")
        page.keyboard.press("Enter")
        page.wait_for_function("UIPreferences.get().palette==='graphite'")
        check("palette-command", True)
        page.locator("#commandBtn").click()
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill("Mật độ: Gọn")
        page.keyboard.press("Enter")
        page.wait_for_function("UIPreferences.get().density==='compact'")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("typeof UICommands!=='undefined'")
        check(
            "command-preferences-persist",
            page.evaluate(
                "UIPreferences.get().palette==='graphite' && "
                "UIPreferences.get().density==='compact'"
            ),
        )
        page.locator("#commandBtn").focus()
        page.keyboard.press("?")
        page.wait_for_selector("dialog[open] .ui-shortcut-list")
        audit("shortcut-help")
        shot("shortcut-help")
        page.keyboard.press("Tab")
        page.keyboard.press("Shift+Tab")
        check(
            "help-focus-contained",
            page.evaluate("!!document.activeElement.closest('dialog[open]')"),
        )
        page.keyboard.press("Escape")
        page.wait_for_function("!document.querySelector('dialog[data-ui-modal]')")
        page.evaluate("go('#/give')")
        page.wait_for_selector("#taskSearch:visible")
        page.locator("#taskSearch").focus()
        page.keyboard.press("Control+k")
        page.keyboard.type("?n")
        check(
            "shortcuts-disabled-in-field",
            page.locator("dialog[open]").count() == 0
            and page.locator("#taskSearch").input_value() == "?n",
        )
        page.locator("#taskSearch").fill("")
        check(
            "intro-not-replayed-on-navigation",
            page.evaluate(
                "__introAnimations.filter(a=>['giveList','workStats'].includes(a.parent)).length"
            )
            == 0,
        )
        page.locator("#commandBtn").focus()
        page.locator("#commandBtn").click()
        page.wait_for_function(
            "document.querySelector('dialog[open] #commandInput') && "
            "!document.getElementById('commandStatus').textContent.includes('Đang tải')"
        )
        page.locator("#commandInput").fill("Giao việc mới")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "document.getElementById('newTaskForm').open && document.activeElement.id==='goal'"
        )
        check("new-task-command-focus", True)
        # Appearance changes are previews until saved and must not leak to later pages.
        page.evaluate("go('#/settings/appearance')")
        page.wait_for_selector("#ui-preferences")
        saved = page.evaluate("UIPreferences.get()")
        page.locator("input[name=palette][value=copper]").check()
        check("appearance-live-preview", page.evaluate("UIPreferences.get().palette==='copper'"))
        page.locator("#ui-preferences button").last.click()
        check("appearance-reset", page.evaluate("UIPreferences.get()") == saved)
        page.locator("input[name=palette][value=teal]").check()
        page.evaluate("go('#/give')")
        page.wait_for_selector("#giveList .work-row")
        check("navigation-reverts-unsaved-preview", page.evaluate("UIPreferences.get()") == saved)
        page.evaluate("go('#/settings/appearance')")
        page.wait_for_selector("#ui-preferences")
        page.locator("input[name=palette][value=forest]").check()
        page.locator("select[name=density]").select_option("comfortable")
        page.locator("#ui-preferences button[type=submit]").click()
        page.wait_for_selector("#ui-preferences .form-result.success")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#ui-preferences")
        check(
            "appearance-save-reload",
            page.evaluate(
                "UIPreferences.get().palette==='forest' && "
                "UIPreferences.get().density==='comfortable'"
            ),
        )
        # Local approval contract fixtures. No approval, mail or model is executed.
        fixture = {
            "approval_id": "apr_UI_FIXTURE",
            "task_title": "UI FIXTURE: Tuyển kỹ sư MEP",
            "action_type": "hiring.hire",
            "effect_class": "external_write",
            "risk_level": "high",
            "requested_by": "UI FIXTURE HR",
            "assigned_approver_id": "UI FIXTURE Boss",
            "waiting_days": 2,
            "actionable": True,
            "expired": False,
        }
        inbox = {"items": [fixture], "count": 1}
        page.route("**/api/v1/approvals/inbox", lambda r: r.fulfill(json=inbox))
        page.route(
            "**/api/v1/approvals/apr_UI_FIXTURE",
            lambda r: r.fulfill(
                json={
                    "id": "apr_UI_FIXTURE",
                    "task_id": task["id"],
                    "status": "pending",
                    "action_type": "hiring.hire",
                    "reason": "UI FIXTURE: chỉ thử giao diện",
                    "action_payload": {"candidate": "UI FIXTURE"},
                    "expires_at": None,
                }
            ),
        )
        pending = []

        def decision(r):
            report["fixture_posts"].append({"url": r.request.url, "body": r.request.post_data_json})
            pending.append(r)

        page.route("**/api/v1/approvals/apr_UI_FIXTURE/*", decision)
        page.evaluate("go('#/work/approvals')")
        page.wait_for_selector("[data-approval=apr_UI_FIXTURE]")
        audit("approval-inbox")
        shot("approval-inbox")
        page.locator("[data-do=approve][data-id=apr_UI_FIXTURE]").click()
        page.wait_for_selector("dialog[open] [data-ui-action=confirm-modal]")
        audit("approval-confirm")
        shot("approval-confirm")
        page.locator("dialog[open] [data-ui-action=confirm-modal]").click()
        page.wait_for_timeout(120)
        check(
            "approval-confirm-busy",
            page.locator("dialog[open] [data-ui-action=confirm-modal]").is_disabled(),
        )
        check(
            "approval-exact-payload",
            len(pending) == 1 and report["fixture_posts"][-1]["body"] == {},
        )
        expected.add(pending[-1].request.url)
        pending.pop().fulfill(
            status=403, json={"detail": "UI FIXTURE: reviewer permission required"}
        )
        page.wait_for_selector("dialog[open] [data-ui-modal-error]")
        check(
            "approval-error-visible-retryable",
            page.locator("dialog[open] [data-ui-action=confirm-modal]").is_enabled(),
        )
        audit("approval-server-refusal")
        shot("approval-server-refusal")
        page.keyboard.press("Escape")
        page.locator("[data-do=reject][data-id=apr_UI_FIXTURE]").click()
        page.locator("dialog[open] [data-ui-action=confirm-modal]").click()
        page.wait_for_selector("dialog[open] [data-ui-modal-error]")
        check("reject-requires-note", not pending)
        page.locator("dialog[open] textarea").fill("UI FIXTURE: thiếu chứng cứ")
        page.locator("dialog[open] [data-ui-action=confirm-modal]").click()
        page.wait_for_timeout(120)
        check(
            "reject-note-contract",
            report["fixture_posts"][-1]["body"] == {"note": "UI FIXTURE: thiếu chứng cứ"},
        )
        inbox.update(items=[], count=0)
        pending.pop().fulfill(json={"status": "rejected"})
        page.wait_for_function("!document.querySelector('dialog[open]')")
        page.wait_for_selector("#workEmpty:visible")
        check(
            "decision-refreshes-inbox", page.locator("[data-approval=apr_UI_FIXTURE]").count() == 0
        )
        inbox.update(items=[fixture], count=1)
        page.evaluate("go('#/approval/apr_UI_FIXTURE')")
        page.wait_for_selector("dialog[open] [data-do=ask]")
        audit("approval-drawer")
        page.locator("dialog[open] [data-do=ask]").click()
        page.locator("dialog[open]").last.locator("textarea").fill("UI FIXTURE: cần bổ sung CV")
        page.locator("dialog[open]").last.locator("[data-ui-action=confirm-modal]").click()
        page.wait_for_timeout(120)
        check(
            "ask-information-contract",
            report["fixture_posts"][-1]["body"]
            == {"needs_information": True, "note": "UI FIXTURE: cần bổ sung CV"},
        )
        pending.pop().fulfill(json={"status": "needs_information"})
        page.wait_for_timeout(250)
        # Exercise the current agent form. Every attempted mutation stays in this browser.
        agent = page.evaluate("async()=> (await apiGet('/agents',{limit:1})).items[0]")
        agent_id = agent["id"]
        control = page.evaluate("async id=>await apiGet('/agents/'+id+'/control')", agent_id)
        page.route(
            "**/api/v1/agents/" + agent_id + "/control",
            lambda r: r.fulfill(json={**control, "kill_switch": False}),
        )
        agent_posts = []

        def stop_agent(r):
            agent_posts.append(r.request.post_data_json)
            report["fixture_posts"].append({"url": r.request.url, "body": r.request.post_data_json})
            expected.add(r.request.url)
            r.fulfill(
                status=403, json={"error": {"message": "UI FIXTURE: stop permission refused"}}
            )

        page.route("**/api/v1/agents/" + agent_id + "/kill", stop_agent)
        page.evaluate("id=>go('#/agent/'+id)", agent_id)
        page.wait_for_selector("#agent-stop textarea")
        page.locator("#agent-stop textarea").fill("   ")
        page.locator("#agent-stop button[type=submit]").click()
        page.wait_for_selector("#agent-stop .form-result.error:visible")
        check("agent-stop-rejects-whitespace-before-request", not agent_posts)
        page.locator("#agent-stop textarea").fill("  UI FIXTURE: stop reason  ")
        page.locator("#agent-stop button[type=submit]").click()
        page.wait_for_selector("#agent-stop .form-result.error:visible")
        page.wait_for_timeout(120)
        check("agent-stop-trims-reason", agent_posts == [{"reason": "UI FIXTURE: stop reason"}])
        check(
            "agent-stop-refusal-is-visible",
            "UI FIXTURE" in page.locator("#agent-stop .form-result.error").inner_text(),
        )
        audit("agent-stop-refusal")
        shot("agent-stop-refusal")
        page.evaluate("go('#/give')")
        page.wait_for_selector("#giveList .work-row")
        for width, height in [(360, 800), (390, 844), (768, 1024), (1920, 1080)]:
            page.set_viewport_size({"width": width, "height": height})
            audit("work-" + str(width))
            shot("work-" + str(width))
            if width <= 390:
                page.locator("#commandBtn").click()
                page.wait_for_selector("#commandInput")
                check(
                    "palette-fullscreen-" + str(width),
                    page.locator("dialog[open]").bounding_box()["height"] >= height - 1,
                )
                audit("palette-" + str(width))
                shot("palette-" + str(width))
                page.keyboard.press("Escape")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.emulate_media(reduced_motion="reduce")
        page.evaluate("UIPreferences.save({...UIPreferences.get(),motion:'auto'})")
        page.evaluate("sessionStorage.removeItem('onx-work-intro-v1')")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#giveList .work-row")
        check(
            "reduced-skips-first-visit-intro",
            not page.evaluate(
                "__introAnimations.filter(a=>['giveList','workStats'].includes(a.parent))"
            ),
        )
        page.locator("#commandBtn").click()
        check(
            "reduced-no-transform-animation",
            page.evaluate(
                "document.getAnimations().every(a=>!a.effect.getKeyframes().some(k=>k.transform && "
                "k.transform!=='none'))"
            ),
        )
        audit("reduced-palette")
        page.keyboard.press("Escape")
        page.emulate_media(forced_colors="active")
        audit("forced-colors")
        shot("forced-colors")
        # 200% desktop zoom is represented by the corresponding CSS viewport.
        page.emulate_media(forced_colors="none")
        page.set_viewport_size({"width": 720, "height": 450})
        audit("zoom-200-reflow")
        shot("zoom-200-reflow")
        page.evaluate("""()=>{UIPreferences.save({...UIPreferences.get(),theme:'dark'});
          UI.toast('UI FIXTURE: notification with a link',{duration:0,
            actionLabel:'Open task',actionHref:'#/give'});} """)
        audit("dark-toast-action")
        shot("dark-toast-action")
        check("no-js-errors", not report["errors"], report["errors"])
        check("no-unexpected-console", not report["console"], report["console"])
        check(
            "no-server-writes-external-hosts",
            not report["blocked_requests"],
            report["blocked_requests"],
        )
        report["source_hashes"] = {
            str(f): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in Path("src/ai_orchestrator/web").glob("*")
            if f.is_file()
        }
        report["gate"] = "passed" if all(c["passed"] for c in report["checks"]) else "failed"
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        browser.close()
    print(json.dumps({"gate": report["gate"], "checks": len(report["checks"])}))
    if report["gate"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
