"""Submit a real custom HR task through Chromium and retain an acceptance receipt.

This is an explicit live-model operation, never part of the automatic test suite.
The goal and optional clarification must contain authorized data. Expected phrases
are checked after execution and are never sent to the model.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


async def main(args: argparse.Namespace) -> None:
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Local API only")
    args.output.mkdir(parents=True, exist_ok=False)
    goal = args.goal.read_text()
    receipt: dict = {
        "passed": False,
        "kind": "live_custom_hr_browser",
        "goal_sha256": hashlib.sha256(goal.encode()).hexdigest(),
        "page_errors": [],
        "states": [],
        "requests": [],
    }
    headers = {"x-organization-id": args.org}
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(executable_path=args.chromium, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: receipt["page_errors"].append(str(error)))

        def record_request(request):
            if request.method == "POST" and "/api/v1/tasks" in request.url:
                receipt["requests"].append({"method": request.method, "url": request.url})

        page.on("request", record_request)
        try:
            url = args.base + "/api/v1/ui?org=" + args.org
            await page.goto(url + "#/give", wait_until="domcontentloaded")
            await page.locator("#newTaskBtn").click()
            await page.wait_for_function(
                "[...document.querySelectorAll('#agent option')]"
                ".some(o=>o.textContent.includes('HR Agent'))"
            )
            owner = await page.locator("#agent option").evaluate_all(
                "options=>options.find(o=>o.textContent.includes('HR Agent')).value"
            )
            await page.locator("#scenario").select_option("")
            await page.locator("#sourceWorkflow").select_option("")
            await page.locator("#agent").select_option(owner)
            await page.locator("#taskType").select_option("analysis")
            await page.locator("#goal").fill(goal)
            await page.screenshot(path=str(args.output / "submitted-form.png"), full_page=True)
            async with page.expect_response(
                lambda response: (
                    response.url.endswith("/api/v1/tasks") and response.request.method == "POST"
                )
            ) as pending:
                await page.locator("#runBtn").click()
            response = await pending.value
            assert response.status == 201, await response.text()
            made = await response.json()
            task_id = made["id"]
            receipt.update(task_id=task_id, owner_agent_id=owner)
            assert made["goal"] == goal.strip()
            assert made["owner_agent_id"] == owner
            assert made["task_type"] == "analysis"
            assert not made.get("input", {}).get("source_workflow")
            clarified = False
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                result = await page.request.get(
                    args.base + "/api/v1/tasks/" + task_id, headers=headers
                )
                assert result.status == 200
                task = await result.json()
                state = task["status"]
                if not receipt["states"] or receipt["states"][-1] != state:
                    receipt["states"].append(state)
                if state == "waiting_for_input" and args.clarification and not clarified:
                    receipt["question"] = task.get("last_error")
                    await page.goto(url + "#/give/" + task_id, wait_until="domcontentloaded")
                    await page.locator("#taskClarificationText").wait_for(timeout=30000)
                    await page.screenshot(
                        path=str(args.output / "waiting-for-input.png"), full_page=True
                    )
                    await page.locator("#taskClarificationText").fill(
                        args.clarification.read_text()
                    )
                    async with page.expect_response(
                        lambda response: (
                            response.url.endswith("/" + task_id + "/inputs")
                            and response.request.method == "POST"
                        )
                    ) as saved:
                        await page.locator("#taskClarificationSave").click()
                    assert (await saved.value).status == 200
                    await page.locator("#runThisTask").wait_for(state="visible")
                    await page.locator("#runThisTask").click()
                    clarified = True
                if state in {"completed", "failed", "canceled", "expired", "blocked"}:
                    receipt["task"] = task
                    break
                await asyncio.sleep(2)
            else:
                raise TimeoutError("Task did not reach a terminal state before acceptance deadline")
            assert task["status"] == "completed", task.get("last_error")
            output = task.get("output")
            assert output, "Completed task has no output"
            serialized = json.dumps(output, ensure_ascii=False).casefold()
            receipt["quality_checks"] = [
                {"phrase": phrase, "passed": phrase.casefold() in serialized}
                for phrase in args.expect
            ]
            assert all(item["passed"] for item in receipt["quality_checks"])
            if args.clarification:
                assert clarified, "Missing-input/resume cycle was not exercised"
            await page.goto(url + "#/give/" + task_id, wait_until="domcontentloaded")
            # The shell already contains taskBanner while its report is loading.
            # Accept only the rendered answer, including the full-text interaction.
            await page.locator("#taskAnswerCopy").wait_for(state="visible", timeout=30000)
            await page.wait_for_function(
                "document.querySelector('#taskAnswer')?.textContent.trim().length > 0"
            )
            await page.screenshot(path=str(args.output / "result.png"), full_page=True)
            full_answer = page.locator("#taskAnswer [data-answertext]")
            if await full_answer.count():
                await full_answer.first.click()
                await page.locator("#sheet[open]").wait_for()
                visible_answer = await page.locator("#sheetBody").inner_text()
                await page.screenshot(path=str(args.output / "full-answer.png"), full_page=True)
            else:
                visible_answer = await page.locator("#taskAnswer").inner_text()
            receipt["ui_answer"] = visible_answer
            receipt["ui_answer_checks"] = [
                {"phrase": phrase, "passed": phrase.casefold() in visible_answer.casefold()}
                for phrase in args.expect
            ]
            assert all(item["passed"] for item in receipt["ui_answer_checks"])
            receipt["copy_button_visible"] = True
            assert not receipt["page_errors"], receipt["page_errors"]
            receipt["passed"] = True
        except Exception as exc:
            receipt["exception"] = str(exc)[:2000]
            await page.screenshot(path=str(args.output / "failure.png"), full_page=True)
        finally:
            await browser.close()
            (args.output / "receipt.json").write_text(
                json.dumps(receipt, indent=2, ensure_ascii=False)
            )
    print(
        json.dumps({key: receipt.get(key) for key in ("passed", "task_id", "states", "exception")})
    )
    if not receipt["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--goal", type=Path, required=True)
    parser.add_argument("--clarification", type=Path)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    asyncio.run(main(parser.parse_args()))
