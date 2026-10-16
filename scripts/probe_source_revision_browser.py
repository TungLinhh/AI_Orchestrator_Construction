"""Real Chromium UI probe: explicit source-revision flow on terminal tasks.

Covers desktop 1440x900 AND mobile 390x844. Default fixture creates and
cancels owned synthetic tasks without any model call (/run is never called);
when --task-id is supplied, that prior terminal task (for example a completed
test task) is used as the main revision original instead. The probe creates
revision drafts through the real UI (POST /tasks/{id}/source-revisions) and
asserts they start no run (zero executions). Synthetic org and local API only.
Technical artifact validation here does not imply human/business acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright

TERMINAL = {"completed", "failed", "canceled", "cancelled", "expired"}
VIEWPORTS = [
    {"name": "desktop", "width": 1440, "height": 900},
    {"name": "mobile", "width": 390, "height": 844},
]
REASON = "[SYNTHETIC BROWSER REVISION] Replace the mock quote with seed 2 output."
EMPTY_REASON = "[SYNTHETIC BROWSER REVISION] Empty terminal task revision draft."
UPLOAD_CSV = (
    "quote_id,item,quantity,unit_price,delivery,certified\n"
    "QSYN1,synthetic widget,10,100000,VND,yes\n"
    "QSYN2,synthetic gadget,5,250000,VND,yes\n"
)


# --- harness helpers: receipt accounting, HTTP/console triage ------------------
# Pure (or tmp-filesystem only) so unit tests can exercise them without
# a browser, API, model, or network.

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SETUP_ERROR = 2

#: HTTP statuses the probe deliberately induces through its own negative
#: checks (idempotent-replay conflict, missing-key / active-work rejection).
#: They are recorded as check evidence, never as transport failures.
#: Every other non-2xx (including 403/5xx) is unexpected and fails the run.
EXPECTED_NEGATIVE_STATUS = frozenset({409, 422})

CONSOLE_BODY_LIMIT = 500
HTTP_BODY_LIMIT = 400


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def summarize_checks(checks: dict) -> dict:
    """Separate SKIP entries from real results without dropping them."""
    skipped = sorted(k for k, v in checks.items() if v == "SKIP")
    return {
        "total": len(checks),
        "skipped": skipped,
        "passed_with_skip": bool(skipped),
    }


def finalize_viewport(out: dict) -> dict:
    """Compute passed/skipped flags from errors plus SKIP-valued checks."""
    summary = summarize_checks(out.get("checks", {}))
    out["skipped"] = summary["skipped"]
    out["passed"] = not out["errors"] and not out.get("exception")
    out["passed_with_skip"] = out["passed"] and summary["passed_with_skip"]
    return out


def finalize_receipt(receipt: dict) -> dict:
    """A partial viewport loop or a saved exception can never be a pass."""
    viewports = receipt.get("viewports", {})
    receipt["passed"] = (
        not receipt.get("errors")
        and not receipt.get("setup_error")
        and set(viewports) == {vp["name"] for vp in VIEWPORTS}
        and all(
            result.get("passed") is True
            and not result.get("errors")
            and not result.get("exception")
            for result in viewports.values()
        )
    )
    receipt["passed_with_skip"] = receipt["passed"] and any(
        result.get("passed_with_skip") for result in viewports.values()
    )
    return receipt


def classify_http_status(status: int, expected=EXPECTED_NEGATIVE_STATUS) -> str:
    if 200 <= status < 300:
        return "ok"
    if status in expected:
        return "expected_negative"
    return "unexpected"


def record_http(out, *, kind, url, status, body="", stage="") -> dict:
    entry = {
        "kind": kind,
        "url": url,
        "status": status,
        "body": (body or "")[:HTTP_BODY_LIMIT],
        "stage": stage,
    }
    out["http"].append(entry)
    if kind == "unexpected_http":
        out["errors"].append(f"unexpected HTTP {status} at {stage or '?'}: {url}"[:500])
    return entry


def classify_console(message_type: str) -> str:
    lowered = (message_type or "").lower()
    if lowered == "error":
        return "fail"
    if lowered == "warning":
        return "warn"
    return "info"


def record_console(
    out: dict,
    message_type: str,
    text: str,
    *,
    location_url: str = "",
    expected_aborts: dict[str, int] | None = None,
) -> None:
    entry = f"console.{message_type}: {text}"[:CONSOLE_BODY_LIMIT]
    kind = classify_console(message_type)
    if (
        kind == "fail"
        and text == "Failed to load resource: net::ERR_FAILED"
        and expected_aborts is not None
        and expected_aborts.get(location_url, 0) > 0
    ):
        expected_aborts[location_url] -= 1
        out.setdefault("expected_console", []).append(
            {"message": entry, "url": location_url, "reason": "probe-injected one-shot abort"}
        )
        return
    if kind == "fail":
        out["errors"].append(entry)
    elif kind == "warn":
        out.setdefault("warnings", []).append(entry)


def prepare_output_dir(output: Path) -> dict | None:
    """Create a fresh output dir; refuse overwrite with a structured error."""
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return {
            "phase": "output_dir",
            "reason": f"refusing to overwrite existing output dir: {output}",
        }
    except OSError as exc:
        return {
            "phase": "output_dir",
            "reason": f"cannot create output dir {output}: {exc}",
        }
    return None


def append_stage(output: Path, event: dict) -> dict:
    record = {"ts": utc_now_iso(), **event}
    with (output / "stages.log").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    return record


def write_receipt(output: Path, receipt: dict) -> None:
    (output / "acceptance.json").write_text(json.dumps(receipt, indent=2))


def receipt_exit_code(receipt: dict) -> int:
    if receipt.get("setup_error"):
        return EXIT_SETUP_ERROR
    return EXIT_OK if receipt.get("passed") else EXIT_FAIL


def base_receipt() -> dict:
    return {
        "synthetic": True,
        "passed": False,
        "viewports": {},
        "errors": [],
        "stages": [],
        "human_review_required": True,
        "note": "Technical browser validation only; human review still required.",
    }


def shot(page, output: Path, name: str):
    return page.screenshot(path=str(output / name), full_page=False)


def shot_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


async def ensure_canceled_fixture(client: httpx.AsyncClient) -> dict:
    """Create then cancel a synthetic mock-source task. Never calls /run."""
    agents = (await client.get("/agents?limit=200")).json()["items"]
    active = [a for a in agents if a.get("lifecycle_status", "active") == "active"]
    owner = active[0] if active else agents[0]
    files = (await client.post("/source-files/mock?workflow=quote_comparison")).json()["items"]
    assert files, "mock fixture produced no files"
    task = (
        await client.post(
            "/source-workflows",
            json={
                "title": "[SYNTHETIC REVISION FIXTURE] quote comparison",
                "goal": "Compare synthetic quotes with exact citations. No external actions.",
                "workflow": "quote_comparison",
                "owner_agent_id": owner["id"],
                "selection": {"mode": "mock", "file_ids": [f["id"] for f in files]},
            },
        )
    ).json()
    sources = (await client.get(f"/tasks/{task['id']}/sources")).json()
    assert sources.get("manifest"), "fixture workflow produced no manifest"
    canceled = (await client.post(f"/tasks/{task['id']}/cancel", json={})).json()
    assert canceled["status"] in TERMINAL, canceled["status"]
    executions = (await client.get(f"/tasks/{task['id']}/executions")).json()["items"]
    assert executions == [], f"fixture started work: {executions}"
    return canceled


async def ensure_empty_terminal_task(client: httpx.AsyncClient) -> dict:
    """Canceled synthetic source task with no manifest. Never calls /run."""
    agents = (await client.get("/agents?limit=200")).json()["items"]
    active = [a for a in agents if a.get("lifecycle_status", "active") == "active"]
    owner = active[0] if active else agents[0]
    task = (
        await client.post(
            "/source-workflows",
            json={
                "title": "[SYNTHETIC REVISION FIXTURE] empty terminal task",
                "goal": "Synthetic empty source task for revision eligibility.",
                "workflow": "quote_comparison",
                "owner_agent_id": owner["id"],
                "selection": {"mode": "sources", "file_ids": []},
            },
        )
    ).json()
    sources = (await client.get(f"/tasks/{task['id']}/sources")).json()
    assert sources.get("manifest") is None, "empty fixture unexpectedly has a manifest"
    canceled = (await client.post(f"/tasks/{task['id']}/cancel", json={})).json()
    assert canceled["status"] in TERMINAL, canceled["status"]
    return canceled


async def check_viewport(browser, args, vp: dict, original: dict, empty_task: dict, client) -> dict:
    out: dict = {
        "viewport": vp["name"],
        "passed": False,
        "errors": [],
        "warnings": [],
        "http": [],
        "screenshots": {},
        "checks": {},
        "skipped": [],
        "passed_with_skip": False,
        "human_review_required": True,
    }
    output: Path = args.output
    context = await browser.new_context(viewport={"width": vp["width"], "height": vp["height"]})
    page = await context.new_page()
    expected_aborts: dict[str, int] = {}
    page.on("pageerror", lambda e: out["errors"].append(f"pageerror: {e}"))
    page.on(
        "console",
        lambda msg: record_console(
            out,
            msg.type,
            msg.text,
            location_url=msg.location.get("url", ""),
            expected_aborts=expected_aborts,
        ),
    )
    stage = {"current": "init"}

    def set_stage(name: str) -> None:
        stage["current"] = name

    async def _on_response(response) -> None:
        try:
            url = response.url
            status = response.status
        except Exception:
            return
        if "/api/" not in url or 200 <= status < 300:
            return
        try:
            body = await response.text()
        except Exception:
            body = ""
        verdict = classify_http_status(status)
        kind = "expected_negative_http" if verdict == "expected_negative" else "unexpected_http"
        record_http(out, kind=kind, url=url, status=status, body=body, stage=stage["current"])

    page.on("response", _on_response)
    base = args.base

    def log_http(kind: str, url: str, status: int, body: str = ""):
        record_http(out, kind=kind, url=url, status=status, body=body, stage=stage["current"])

    async def snap(name: str):
        if name == "form":
            await page.locator("#sourceRevisionForm fieldset").scroll_into_view_if_needed()
        p = output / f"{vp['name']}_{name}.png"
        await page.screenshot(path=str(p), full_page=False)
        out["screenshots"][name] = {"file": p.name, "sha256_16": shot_hash(p)}

    async def active_id() -> str:
        return await page.evaluate(
            "() => (document.activeElement && document.activeElement.id) || ''"
        )

    async def global_picker_state() -> dict:
        return await page.evaluate(
            "() => ({mode: (document.querySelector("
            "'input[name=\"sourceChoice\"]:checked') || {}).value || null,"
            " count: (document.getElementById('sourceSelectionCount') || {})"
            ".textContent || null,"
            " resumeHidden: (document.getElementById('sourceResume') || {})"
            ".hidden ?? null})"
        )

    manifest = (await client.get(f"/tasks/{original['id']}/sources")).json()["manifest"]
    assert manifest, "original terminal task has no manifest for the main flow"
    manifest_sha = (manifest.get("sha256") or "")[:16]
    out["checks"]["manifest_sha_prefix"] = manifest_sha

    async def wait_fresh_original():
        await page.wait_for_function(
            "() => { const p = document.getElementById('sourceTaskPanel');"
            " return p && !p.hidden; }",
            timeout=30000,
        )
        await page.wait_for_function(
            f"() => (document.getElementById('sourceTaskFiles') || {{}})"
            f".textContent.includes('{manifest_sha}')",
            timeout=30000,
        )

    try:
        # --- 1. terminal original renders revision entry point ---
        set_stage("render_original")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{original['id']}",
            wait_until="domcontentloaded",
        )
        await wait_fresh_original()
        history_before = await page.text_content("#sourceTaskFiles")
        assert history_before and history_before.strip(), "prior manifest/history empty"
        assert manifest_sha in (history_before or ""), "manifest sha not rendered"
        out["checks"]["history_visible"] = True
        picker_before = await global_picker_state()
        out["checks"]["global_picker_before"] = picker_before
        start_visible = await page.is_visible("#sourceRevisionStart")
        assert start_visible, "revise action not offered on terminal root source task"
        out["checks"]["start_offered"] = True
        await snap("top")

        # --- 2. open via keyboard; focus lands on the visible radio ---
        set_stage("open_form")
        await page.focus("#sourceRevisionStart")
        assert await active_id() == "sourceRevisionStart", "could not focus start button"
        await page.keyboard.press("Enter")
        await page.wait_for_selector("#sourceRevisionForm:not([hidden])", timeout=10000)
        start_hidden = await page.is_hidden("#sourceRevisionStart")
        out["checks"]["start_hidden_after_open"] = start_hidden
        assert start_hidden, "start button should hide while the form is open"
        focus_after_open = await active_id()
        out["checks"]["focus_after_open"] = focus_after_open
        assert focus_after_open == "revisionChooseSources", (
            f"focus target wrong: {focus_after_open!r}"
        )

        # --- 3. explicit choice, no pre-selected default (correct awaits) ---
        set_stage("explicit_choice")
        radios = await page.query_selector_all('input[name="revisionChoice"]')
        assert len(radios) == 2, f"expected 2 revision radios, got {len(radios)}"
        checked = [await r.is_checked() for r in radios]
        assert not any(checked), f"revision mode pre-selected: {checked}"
        out["checks"]["no_implicit_default"] = True

        # --- 4. inline radio styles, hit targets, accessible labels ---
        set_stage("radio_styles")
        for rid in ("revisionChooseSources", "revisionChooseMock"):
            handle = await page.query_selector(f"#{rid}")
            assert handle is not None, f"missing radio {rid}"
            assert await handle.is_visible(), f"radio not visible: {rid}"
            rects = await page.evaluate(
                "() => { const i = document.getElementById('"
                + rid
                + "'); const l = i.closest('label');"
                + " const ir = i.getBoundingClientRect();"
                + " const lr = l.getBoundingClientRect();"
                + " return {iw: ir.width, ih: ir.height, iy: ir.y,"
                + " lx: lr.x, ly: lr.y, lw: lr.width, lh: lr.height,"
                + " cls: l.className, txt: l.textContent}; }"
            )
            out["checks"][f"radio_rect_{rid}"] = rects
            assert "ui-choice" in (rects.get("cls") or ""), f"radio label missing ui-choice: {rid}"
            assert (rects.get("txt") or "").strip(), f"radio label has no text: {rid}"
            assert rects["iw"] >= 12 and rects["ih"] >= 12, f"radio too small: {rects}"
            assert rects["lh"] >= 32, f"radio hit target too short: {rects}"
            # getBoundingClientRect y is viewport-relative; recompute in page
            aligned = await page.evaluate(
                "() => { const i = document.getElementById('"
                + rid
                + "'); const l = i.closest('label');"
                + " const ir = i.getBoundingClientRect();"
                + " const lr = l.getBoundingClientRect();"
                + " const mid = ir.y + ir.height / 2;"
                + " return lr.y - 2 <= mid && mid <= lr.y + lr.height + 2; }"
            )
            assert aligned, f"radio and label misaligned: {rid} {rects}"
        out["checks"]["radio_styles"] = True
        bounds = await page.evaluate("""() => {
          const elements = [document.getElementById('sourceRevisionForm'),
            ...document.querySelectorAll(
              '#sourceRevisionForm fieldset, #sourceRevisionForm .ui-choice')];
          return elements.map(el => {const rect = el.getBoundingClientRect();
            return {id: el.id || el.tagName, left: rect.left, right: rect.right};});
        }""")
        out["checks"]["responsive_bounds"] = bounds
        assert all(item["left"] >= -1 and item["right"] <= vp["width"] + 1 for item in bounds), (
            bounds
        )

        # --- 5. keyboard order from the visible radio ---
        # Radios form one group: Tab exits the group (arrow keys move inside).
        set_stage("focus_order")
        await page.focus("#revisionChooseSources")
        seen: list[str] = []
        focusable_count = await page.locator(
            "#sourceRevisionForm input:not([disabled]), "
            "#sourceRevisionForm button:not([disabled]), "
            "#sourceRevisionForm textarea, #sourceRevisionForm select, "
            '#sourceRevisionForm [tabindex="0"]'
        ).count()
        out["checks"]["focusable_controls"] = focusable_count
        for _ in range(focusable_count + 5):
            await page.keyboard.press("Tab")
            seen.append(await active_id())
            if seen[-1] == "sourceRevisionCreate":
                break
        order = ["sourceRevisionReason", "sourceRevisionCreate"]
        positions = [next((i for i, s in enumerate(seen) if s == want), None) for want in order]
        assert all(p is not None for p in positions), f"focus never reached form: {seen}"
        assert positions == sorted(positions), f"focus order wrong: {seen}"
        assert "revisionFileList" in seen, f"tab never left radio group: {seen}"
        out["checks"]["focus_order"] = [s for s in seen if s]

        # --- 6. revision has its own upload/refresh/more controls ---
        set_stage("own_controls")
        for cid in (
            "#revisionUploadFiles",
            "#revisionClassification",
            "#sourceRevisionUpload",
            "#sourceRevisionRefresh",
            "#sourceRevisionMore",
            "#revisionFileList",
            "#revisionSelectionCount",
        ):
            assert await page.query_selector(cid) is not None, f"missing revision control {cid}"
        out["checks"]["revision_own_controls"] = True
        await snap("form")

        # --- 7. required reason validation: empty, then too short, then no mode ---
        set_stage("reason_validation")
        await page.click("#sourceRevisionCreate")
        await page.wait_for_function(
            "() => document.getElementById('sourceRevisionNotice').textContent.includes('10')",
            timeout=10000,
        )
        await page.fill("#sourceRevisionReason", "short")
        await page.click("#sourceRevisionCreate")
        await page.wait_for_function(
            "() => document.getElementById('sourceRevisionNotice').textContent.includes('10')",
            timeout=10000,
        )
        out["checks"]["reason_validation"] = True

        # --- 8. isolation: revision choice must not touch the ordinary picker ---
        set_stage("choice_isolation")
        await page.check("#revisionChooseMock")
        picker_after_choice = await global_picker_state()
        out["checks"]["global_picker_after_revision_choice"] = picker_after_choice
        assert picker_after_choice["mode"] == picker_before["mode"], (
            f"revision choice leaked into ordinary mode: {picker_after_choice}"
        )
        assert picker_after_choice["count"] == picker_before["count"], (
            f"revision choice leaked into ordinary count: {picker_after_choice}"
        )

        # --- 9. mock replacement via the revision control ---
        set_stage("mock_replacement")
        async with page.expect_response(
            lambda r: "/source-files/mock" in r.url, timeout=30000
        ) as mock_response:
            await page.click("#sourceRevisionMock")
        mock_resp = await mock_response.value
        log_http("mock", mock_resp.url, mock_resp.status)
        assert mock_resp.status == 201, await mock_resp.text()
        await page.wait_for_function(
            "() => { const el = document.getElementById('revisionSelectionCount'); "
            "return el && /[1-9]/.test(el.textContent); }",
            timeout=15000,
        )
        picker_after_mock = await global_picker_state()
        out["checks"]["global_picker_after_mock"] = picker_after_mock
        assert picker_after_mock["mode"] == picker_before["mode"], (
            "mock generate leaked into ordinary picker"
        )
        first_box = page.locator("#revisionFileList [data-revision-source-id]").first
        await first_box.uncheck()
        count_after = await page.text_content("#revisionSelectionCount")
        assert count_after and "0" in count_after, f"uncheck did not change count: {count_after}"
        await first_box.check()
        out["checks"]["mock_selection_change"] = True

        # --- 10. uploaded replacement via the revision control ---
        set_stage("uploaded_replacement")
        await page.check("#revisionChooseSources")
        upload_path = output / f"{vp['name']}_synthetic_upload.csv"
        upload_path.write_text(UPLOAD_CSV, encoding="utf-8")
        await page.set_input_files("#revisionUploadFiles", str(upload_path))
        async with page.expect_response(
            lambda r: r.url.endswith("/source-files") and r.request.method == "POST",
            timeout=30000,
        ) as upload_response:
            await page.click("#sourceRevisionUpload")
        upload_resp = await upload_response.value
        log_http("upload", upload_resp.url, upload_resp.status)
        assert upload_resp.status == 201, await upload_resp.text()
        await page.wait_for_function(
            "() => { const el = document.getElementById('revisionSelectionCount'); "
            "return el && /[1-9]/.test(el.textContent); }",
            timeout=15000,
        )
        out["checks"]["uploaded_replacement"] = True
        await page.wait_for_function(
            "() => /Đã đọc file thay thế|Replacement files are ready/.test("
            "document.getElementById('sourceRevisionNotice').textContent)",
            timeout=15000,
        )
        out["checks"]["upload_settled_notice"] = True
        picker_after_upload = await global_picker_state()
        assert picker_after_upload["mode"] == picker_before["mode"], (
            "upload leaked into ordinary picker"
        )
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await snap("middle")

        # --- 11. pagination: refresh and load-more preserve revision selection ---
        set_stage("pagination")
        checked_js = (
            "() => [...document.querySelectorAll("
            "'#revisionFileList [data-revision-source-id]:checked')]"
            ".map(e => e.getAttribute('data-revision-source-id')).sort()"
        )
        count_js = (
            "() => document.querySelectorAll('#revisionFileList [data-revision-source-id]').length"
        )
        selected_before = await page.evaluate(checked_js)
        assert selected_before, "no revision selection to preserve across pages"
        catalog = (await client.get("/source-files", params={"limit": 1, "offset": 0})).json()
        out["checks"]["source_total"] = catalog.get("total")
        await page.click("#sourceRevisionRefresh")
        await page.wait_for_function(count_js + " > 0", timeout=15000)
        selected_after_refresh = await page.evaluate(checked_js)
        assert selected_after_refresh == selected_before, (
            f"refresh lost revision selection: {selected_before} vs {selected_after_refresh}"
        )
        more_hidden = await page.is_hidden("#sourceRevisionMore")
        out["checks"]["revision_more_hidden"] = more_hidden
        if not more_hidden:
            n_before = await page.evaluate(count_js)
            await page.click("#sourceRevisionMore")
            await page.wait_for_function(count_js + f" > {n_before}", timeout=15000)
            selected_after_more = await page.evaluate(checked_js)
            assert selected_after_more == selected_before, "load-more lost revision selection"
            out["checks"]["pagination_preserves_selection"] = True
        else:
            out["checks"]["pagination_preserves_selection"] = "SKIP"

        # --- 12. cancel clears ONLY revision state ---
        set_stage("cancel_form")
        await page.click("#sourceRevisionCancel")
        await page.wait_for_selector("#sourceRevisionStart:not([hidden])", timeout=10000)
        assert await active_id() == "sourceRevisionStart", "cancel did not return focus"
        picker_after_cancel_form = await global_picker_state()
        out["checks"]["global_picker_after_cancel_form"] = picker_after_cancel_form
        assert picker_after_cancel_form["mode"] == picker_before["mode"], (
            "cancel altered ordinary mode"
        )
        await page.goto(f"{base}/api/v1/ui?org={args.org}#/give", wait_until="domcontentloaded")
        await page.wait_for_function(
            "() => !!document.getElementById('sourceSelectionCount')", timeout=15000
        )
        await page.wait_for_timeout(500)
        picker_after_cancel = await global_picker_state()
        out["checks"]["global_picker_after_cancel"] = picker_after_cancel
        assert picker_after_cancel["mode"] == picker_before["mode"], (
            f"cancel leaked revision mode: {picker_after_cancel}"
        )
        assert picker_after_cancel["count"] == picker_before["count"], (
            f"cancel leaked revision count: {picker_after_cancel}"
        )

        # --- 13. reopen; transient failure keeps a stable idempotency key ---
        set_stage("transient_retry")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{original['id']}",
            wait_until="domcontentloaded",
        )
        await wait_fresh_original()
        await page.wait_for_selector("#sourceRevisionStart", timeout=15000)
        await page.click("#sourceRevisionStart")
        await page.wait_for_selector("#sourceRevisionForm:not([hidden])", timeout=10000)
        await page.check("#revisionChooseMock")
        async with page.expect_response(
            lambda r: "/source-files/mock" in r.url, timeout=30000
        ) as mock2:
            await page.click("#sourceRevisionMock")
        assert (await mock2.value).status == 201
        await page.wait_for_function(
            "() => { const el = document.getElementById('revisionSelectionCount'); "
            "return el && /[1-9]/.test(el.textContent); }",
            timeout=15000,
        )
        await page.fill("#sourceRevisionReason", REASON)
        seen_keys: list[str] = []

        async def abort_once(route):
            headers = await route.request.all_headers()
            key = next((v for k, v in headers.items() if k.lower() == "idempotency-key"), None)
            seen_keys.append(key)
            if len(seen_keys) == 1:
                url = route.request.url
                expected_aborts[url] = expected_aborts.get(url, 0) + 1
                out.setdefault("injected_faults", []).append(
                    {"kind": "network_abort", "url": url, "count": 1}
                )
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/source-revisions", abort_once)
        await page.click("#sourceRevisionCreate")
        await page.wait_for_function(
            "() => (document.getElementById('sourceRevisionNotice') || {}).textContent.length > 0",
            timeout=15000,
        )
        await page.wait_for_function(
            "() => !document.getElementById('sourceRevisionCreate').disabled", timeout=15000
        )
        async with page.expect_response(
            lambda r: "/source-revisions" in r.url and r.request.method == "POST",
            timeout=30000,
        ) as revision_response:
            await page.click("#sourceRevisionCreate")
        created_response = await revision_response.value
        await page.unroute("**/source-revisions")
        headers = await created_response.request.all_headers()
        retry_key = next((v for k, v in headers.items() if k.lower() == "idempotency-key"), None)
        seen_keys.append(retry_key)
        out["checks"]["transient_retry_keys"] = seen_keys
        assert seen_keys[0] and retry_key, "idempotency key missing on retry"
        assert seen_keys[0] == retry_key, f"retry key changed: {seen_keys}"
        body_text = await created_response.text()
        log_http("revision_create", created_response.url, created_response.status, body_text)
        out["checks"]["idempotency_key_sent"] = bool(retry_key)
        assert created_response.status == 201, body_text
        created = json.loads(body_text)
        assert created.get("id"), "revision response has no task id"
        assert created.get("status") == "assigned", created.get("status")
        prev_id = (created.get("input") or {}).get("source_revision", {}).get("previous_task_id")
        assert prev_id == original["id"], "new task does not point at the original"
        out["revision_id"] = created["id"]
        executions = (await client.get(f"/tasks/{created['id']}/executions")).json()["items"]
        assert executions == [], f"revision started work: {executions}"
        out["checks"]["no_run_started"] = True

        # --- 14. idempotent replay: same key/same body replays, changed body conflicts ---
        set_stage("idempotent_replay")
        sent_body = json.loads(created_response.request.post_data or "{}")
        replayed = await client.post(
            f"/tasks/{original['id']}/source-revisions",
            json=sent_body,
            headers={"Idempotency-Key": retry_key},
        )
        out["checks"]["replay_status"] = replayed.status_code
        assert replayed.status_code == 201, replayed.text
        assert replayed.json().get("id") == created["id"], "idempotent replay changed task"
        assert replayed.json().get("idempotent_replay") is True, "replay flag missing"
        conflict_body = {**sent_body, "reason": sent_body.get("reason", "") + " changed"}
        conflicted = await client.post(
            f"/tasks/{original['id']}/source-revisions",
            json=conflict_body,
            headers={"Idempotency-Key": retry_key},
        )
        out["checks"]["conflict_status"] = conflicted.status_code
        assert conflicted.status_code == 409, conflicted.text
        out["checks"]["idempotent_retry"] = True

        # --- 15. prior-task link survives reload ---
        set_stage("prior_link")
        await page.wait_for_selector("#sourceRevisionPriorLink", timeout=15000)
        href = await page.get_attribute("#sourceRevisionPriorLink", "href")
        assert href and original["id"] in href, f"prior link wrong: {href}"
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await snap("bottom")
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("#sourceRevisionPriorLink", timeout=15000)
        href2 = await page.get_attribute("#sourceRevisionPriorLink", "href")
        assert href2 and original["id"] in href2, "prior link lost after reload"
        out["checks"]["prior_link_survives_reload"] = True

        # --- 16. original history untouched (fresh-render wait keeps the invariant) ---
        set_stage("original_history")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{original['id']}",
            wait_until="domcontentloaded",
        )
        await wait_fresh_original()
        history_after = await page.text_content("#sourceTaskFiles")
        assert history_after == history_before, "original history changed"
        out["checks"]["original_history_untouched"] = True

        # --- 17. active work gets no revise action (rejected server-side too) ---
        set_stage("active_rejection")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{created['id']}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector("#sourceRevisionPriorLink", timeout=15000)
        assert await page.query_selector("#sourceRevisionStart") is None, (
            "revise action offered on active work"
        )
        out["checks"]["no_revise_on_active"] = True
        missing = await client.post(
            f"/tasks/{created['id']}/source-revisions",
            json={"selection": {"mode": "mock", "file_ids": []}, "reason": REASON},
        )
        out["checks"]["active_missing_key_status"] = missing.status_code
        rejected = await client.post(
            f"/tasks/{created['id']}/source-revisions",
            json={"selection": {"mode": "mock", "file_ids": []}, "reason": REASON},
            headers={"Idempotency-Key": f"probe-active-rejected-{vp['name']}"},
        )
        out["checks"]["active_with_key_status"] = rejected.status_code
        assert missing.status_code == 422, missing.text
        assert rejected.status_code == 409, rejected.text

        # --- 18. canceled revision copy stays factual after reload ---
        # Check the rendered status copy, not page.content() (which inlines
        # the JS bundle and always contains every copy string).
        set_stage("canceled_copy")
        cancel_resp = await client.post(f"/tasks/{created['id']}/cancel", json={})
        assert cancel_resp.status_code == 200, cancel_resp.text
        await page.reload(wait_until="domcontentloaded")
        await page.wait_for_selector("#sourceRevisionPriorLink", timeout=15000)
        await page.wait_for_selector("p[data-revision-status-copy]", timeout=15000)
        status_copy = await page.text_content("p[data-revision-status-copy]")
        status_attr = await page.get_attribute(
            "p[data-revision-status-copy]", "data-revision-status-copy"
        )
        out["checks"]["prior_copy_after_cancel"] = (status_copy or "").strip()[:300]
        out["checks"]["revision_status_after_cancel"] = status_attr
        out["checks"]["says_unstarted_after_cancel"] = ("Unstarted" in (status_copy or "")) or (
            "chưa chạy" in (status_copy or "")
        )
        assert "Unstarted" not in (status_copy or ""), (
            f"stale Unstarted copy on canceled revision: {status_copy}"
        )
        assert "chưa chạy" not in (status_copy or ""), (
            f"stale unstarted copy on canceled revision: {status_copy}"
        )
        assert (status_attr or "").lower() in TERMINAL, (
            f"revision status copy not factual: {status_attr} / {status_copy}"
        )

        # --- 19. terminal task without a manifest still offers revision ---
        set_stage("empty_terminal")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{empty_task['id']}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_function(
            "() => { const p = document.getElementById('sourceTaskPanel'); "
            "return p && !p.hidden; }",
            timeout=30000,
        )
        assert await page.query_selector("#sourcePickForTask") is None, (
            "terminal empty task wrongly offers attach"
        )
        assert await page.query_selector("#sourceAttach") is None, (
            "terminal empty task wrongly offers resume"
        )
        await page.wait_for_selector("#sourceRevisionStart", timeout=15000)
        out["checks"]["empty_terminal_offers_revision"] = True
        await snap("empty_top")
        await page.click("#sourceRevisionStart")
        await page.wait_for_selector("#sourceRevisionForm:not([hidden])", timeout=10000)
        await page.check("#revisionChooseMock")
        async with page.expect_response(
            lambda r: "/source-files/mock" in r.url, timeout=30000
        ) as empty_mock:
            await page.click("#sourceRevisionMock")
        assert (await empty_mock.value).status == 201
        await page.wait_for_function(
            "() => { const el = document.getElementById('revisionSelectionCount'); "
            "return el && /[1-9]/.test(el.textContent); }",
            timeout=15000,
        )
        await page.fill("#sourceRevisionReason", EMPTY_REASON)
        async with page.expect_response(
            lambda r: "/source-revisions" in r.url and r.request.method == "POST",
            timeout=30000,
        ) as empty_revision_response:
            await page.click("#sourceRevisionCreate")
        empty_created_response = await empty_revision_response.value
        assert empty_created_response.status == 201, await empty_created_response.text()
        empty_created = await empty_created_response.json()
        assert (empty_created.get("input") or {}).get("source_revision", {}).get(
            "previous_task_id"
        ) == empty_task["id"], "empty revision does not point at its original"
        out["empty_revision_id"] = empty_created.get("id")
        out["checks"]["empty_revision_created"] = True
        await page.wait_for_selector("#sourceRevisionPriorLink", timeout=15000)

        # --- 20. back to original: fresh Start offered again, no leaked form ---
        set_stage("no_leaked_form")
        await page.goto(
            f"{base}/api/v1/ui?org={args.org}#/give/{original['id']}",
            wait_until="domcontentloaded",
        )
        await page.wait_for_selector("#sourceRevisionStart", timeout=15000)
        leaked_form = await page.query_selector("#sourceRevisionForm:not([hidden])")
        out["checks"]["no_leaked_open_form"] = leaked_form is None
        assert leaked_form is None, "revision form leaked across navigation"

        finalize_viewport(out)
    except Exception as exc:
        out["exception"] = str(exc)[:1500]
        out["errors"].append(f"probe exception: {type(exc).__name__}: {exc}"[:500])
        with contextlib.suppress(Exception):
            await snap("failure")
        finalize_viewport(out)
    finally:
        await context.close()
    return out


async def main(args):
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Local API only")
    setup_error = prepare_output_dir(args.output)
    if setup_error is not None:
        receipt = {**base_receipt(), "setup_error": setup_error, "stages": ["refused_overwrite"]}
        print(json.dumps(receipt))
        raise SystemExit(EXIT_SETUP_ERROR)
    receipt = base_receipt()

    def save(stage: str) -> None:
        receipt["stages"].append(stage)
        append_stage(args.output, {"stage": stage, "passed": receipt["passed"]})
        write_receipt(args.output, receipt)

    save("init")
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(
        base_url=args.base + "/api/v1",
        headers={"x-organization-id": args.org},
        timeout=timeout,
    ) as client:
        try:
            if args.task_id:
                original = (await client.get(f"/tasks/{args.task_id}")).json()
                assert original["status"] in TERMINAL, original["status"]
            else:
                original = await ensure_canceled_fixture(client)
            receipt["original_task_id"] = original["id"]
            receipt["original_status"] = original["status"]
            empty_task = await ensure_empty_terminal_task(client)
            receipt["empty_task_id"] = empty_task["id"]
            save("fixture")
        except Exception as exc:
            receipt["errors"].append(f"fixture: {exc}"[:500])
            write_receipt(args.output, receipt)
            append_stage(args.output, {"stage": "fixture_failed", "passed": False})
            print(json.dumps(receipt))
            raise SystemExit(receipt_exit_code(receipt)) from exc
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=args.chromium, headless=True, args=["--no-sandbox"]
            )
            try:
                for vp in VIEWPORTS:
                    try:
                        result = await check_viewport(
                            browser, args, vp, original, empty_task, client
                        )
                    except Exception as exc:
                        result = {
                            "viewport": vp["name"],
                            "passed": False,
                            "errors": [f"viewport crashed: {exc}"[:500]],
                            "warnings": [],
                            "http": [],
                            "screenshots": {},
                            "checks": {},
                            "skipped": [],
                            "passed_with_skip": False,
                        }
                    receipt["viewports"][vp["name"]] = result
                    save(f"viewport:{vp['name']}")
            except Exception as exc:
                receipt["errors"].append(f"viewport loop: {exc}"[:500])
            finally:
                await browser.close()
    finalize_receipt(receipt)
    write_receipt(args.output, receipt)
    append_stage(args.output, {"stage": "done", "passed": receipt["passed"]})
    print(json.dumps(receipt))
    if not receipt["passed"]:
        raise SystemExit(receipt_exit_code(receipt))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8112")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--deadline", type=int, default=300)
    asyncio.run(main(parser.parse_args()))
