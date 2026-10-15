"""Create a synthetic HTTP task, run it, and independently grade the persisted output.

Exercises the ordinary API/local runner/PydanticAI path without mail or orders.
The probe cancels its own task if the bounded wait expires. Source data is JSON
in the request; this is not an upload or source-picker acceptance test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


async def main(args):
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Local API only")
    report = {"observed_at": datetime.now(UTC).isoformat(), "synthetic": True, "passed": False}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    async with httpx.AsyncClient(
        base_url=args.base, headers={"x-organization-id": args.org}, timeout=30
    ) as client:

        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        agents = await request("GET", "/api/v1/agents?limit=200")
        owner = next(a for a in agents["items"] if a["name"] == "Procurement Agent")
        task = await request(
            "POST",
            "/api/v1/tasks",
            json={
                "title": "[SYNTHETIC MODEL READINESS] tender artifact",
                "goal": (
                    "Analyze only the synthetic quotes in input. Need 12 valves with mandatory "
                    "certification. Select the cheapest eligible quote and compute total including "
                    "delivery. Do not send RFQ, place orders, contact people or delegate. Return "
                    "one JSON object with winner as the quote id and total_vnd as a number. "
                    "This is a draft calculation, not an external transaction."
                ),
                "task_type": "analysis",
                "owner_agent_id": owner["id"],
                "allow_parallel": True,
                "start_workflow": False,
                "input": {
                    "synthetic": True,
                    "quantity": 12,
                    "quotes": [
                        {"id": "A", "unit_vnd": 120000, "delivery_vnd": 80000, "certified": True},
                        {"id": "B", "unit_vnd": 110000, "delivery_vnd": 0, "certified": False},
                    ],
                },
                "expected_output_schema": {
                    "type": "object",
                    "properties": {"winner": {"type": "string"}, "total_vnd": {"type": "number"}},
                    "required": ["winner", "total_vnd"],
                },
                "budget_limit_tokens": 12000,
            },
        )
        report["task_id"] = task["id"]
        save()
        report["run_handle"] = await request("POST", f"/api/v1/tasks/{task['id']}/run")
        deadline = time.monotonic() + args.deadline_s
        while time.monotonic() < deadline:
            task = await request("GET", f"/api/v1/tasks/{task['id']}")
            if task["status"] in {
                "completed",
                "failed",
                "canceled",
                "expired",
                "waiting_for_approval",
            }:
                break
            await asyncio.sleep(1)
        else:
            report["timeout"] = True
            report["cancellation"] = await request("POST", f"/api/v1/tasks/{task['id']}/cancel")
        report["task"] = await request("GET", f"/api/v1/tasks/{task['id']}")
        report["executions"] = await request("GET", f"/api/v1/tasks/{task['id']}/executions")
        report["model_usage"] = await request("GET", f"/api/v1/model/usage?task_id={task['id']}")
        output = report["task"]["output"] or {}
        report["passed"] = (
            report["task"]["status"] == "completed"
            and output.get("winner") == "A"
            and output.get("total_vnd") == 1520000
            and bool(report["executions"]["items"])
            and bool(report["model_usage"]["items"])
            and all(
                row["provider"] == "openrouter" and row["model_used"].endswith(":free")
                for row in report["model_usage"]["items"]
            )
            and all(
                e["model_used"] and e["model_used"].endswith(":free")
                for e in report["executions"]["items"]
            )
        )
        save()
    print(
        json.dumps(
            {"passed": report["passed"], "task_id": task["id"], "status": report["task"]["status"]}
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--deadline-s", type=int, default=180)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
