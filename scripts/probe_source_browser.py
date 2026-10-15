"""Real Chromium UI: choose mock, run the real workflow and download the DOCX."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


async def main(args):
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Local API only")
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = {"synthetic": True, "passed": False, "errors": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=args.chromium, headless=True, args=["--no-sandbox"]
        )
        page = await browser.new_page(viewport={"width": 1440, "height": 1100})
        page.on("pageerror", lambda error: receipt["errors"].append(str(error)))
        try:
            await page.goto(
                args.base + "/api/v1/ui?org=" + args.org + "#/give", wait_until="domcontentloaded"
            )
            await page.locator("#newTaskBtn").click()
            await page.locator("#sourceWorkflow").select_option("quote_comparison")
            await page.locator("#sourceChooseMock").check()
            async with page.expect_response(lambda r: "/source-files/mock" in r.url) as response:
                await page.locator("#sourceMock").click()
            assert (await response.value).status == 201
            await page.wait_for_function(
                "document.getElementById('sourceSelectionCount').textContent.includes('1')"
            )
            agents = await page.request.get(
                args.base + "/api/v1/agents?limit=200", headers={"x-organization-id": args.org}
            )
            owner = next(
                a for a in (await agents.json())["items"] if a["name"] == "Procurement Agent"
            )
            await page.locator("#agent").select_option(owner["id"])
            await page.locator("#goal").fill(
                "[SYNTHETIC BROWSER ACCEPTANCE] Compare selected quotes with exact citations, "
                "choose the lowest eligible total VND. No external action."
            )
            await page.screenshot(path=str(args.output / "selection.png"), full_page=True)
            async with page.expect_response(
                lambda r: "/source-workflows" in r.url and r.request.method == "POST"
            ) as response:
                await page.locator("#runBtn").click()
            created = await response.value
            assert created.status == 201, await created.text()
            task = await created.json()
            receipt["task_id"] = task["id"]
            assert task["input"]["source_selection_mode"] == "mock"
            button = page.locator("#sourceDownloads [data-artifact]").filter(has_text="DOCX")
            await button.wait_for(timeout=240000)
            async with page.expect_download() as download:
                await button.click()
            downloaded = await download.value
            await downloaded.save_as(str(args.output / "report.docx"))
            receipt["download_bytes"] = (args.output / "report.docx").stat().st_size
            receipt["passed"] = receipt["download_bytes"] > 1000 and not receipt["errors"]
        except Exception as exc:
            receipt["exception"] = str(exc)[:1500]
        finally:
            await page.screenshot(path=str(args.output / "result.png"), full_page=True)
            await browser.close()
    (args.output / "acceptance.json").write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt))
    if not receipt["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
