"""Bounded read-only concurrent campaign/pool probe; no model or connector writes."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx
from sqlalchemy import text

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.session import Database


def percentiles(values):
    ordered = sorted(values)
    return {
        f"p{p}_ms": round(ordered[math.ceil(len(ordered) * p / 100) - 1], 3) for p in (50, 95, 99)
    }


async def measure(args):
    settings = get_settings()
    db = Database.from_settings(settings)
    semaphore = asyncio.Semaphore(args.concurrency)
    try:
        async with httpx.AsyncClient(
            base_url=args.base, timeout=30, headers={"x-organization-id": args.org}
        ) as client:
            response = await client.get("/api/v1/workflows", params={"limit": 100})
            response.raise_for_status()
            roots = [row["id"] for row in response.json()["items"]][: args.campaigns]
            if not roots:
                raise ValueError(
                    "Create recorded campaigns before measuring; this probe never fabricates work"
                )
            # Warm cache and pool separately, so cold startup is not disguised as queue wait.
            for root in roots:
                response = await client.get("/api/v1/workflows/" + root)
                response.raise_for_status()
            async with db.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))

            async def sample(index):
                async with semaphore:
                    root = roots[index % len(roots)]
                    start = time.perf_counter()
                    response = await client.get("/api/v1/workflows/" + root)
                    response.raise_for_status()
                    duration = (time.perf_counter() - start) * 1000
                    start = time.perf_counter()
                    async with db.engine.connect() as connection:
                        acquire = (time.perf_counter() - start) * 1000
                        await connection.execute(text("SELECT 1"))
                    return {"campaign": root, "api_ms": duration, "pool_acquire_ms": acquire}

            rows = await asyncio.gather(*(sample(i) for i in range(args.samples)))
        report = {
            "scope": "read-only existing campaign reports and separate application-role pool",
            "concurrency": args.concurrency,
            "campaigns": roots,
            "samples": len(rows),
            "api": percentiles([r["api_ms"] for r in rows]),
            "pool_acquire": percentiles([r["pool_acquire_ms"] for r in rows]),
            "pool_size": settings.db_pool_size,
            "pool_max_overflow": settings.db_max_overflow,
            "errors": 0,
            "rows": rows,
            "limitations": [
                "Pool acquisition includes checkout/pre-ping and cold new connections, "
                "not pure queue wait.",
                "The pool probe is separate from the API pool; "
                "it does not measure API internal queue wait.",
                "Read concurrency does not certify concurrent model campaign throughput "
                "or production SLA.",
            ],
        }
        report["passed"] = report["api"]["p95_ms"] <= args.max_p95_ms
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(json.dumps({key: report[key] for key in ("passed", "api", "pool_acquire", "errors")}))
        return int(not report["passed"])
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--org", required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=4)
    parser.add_argument("--campaigns", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--samples", type=int, choices=range(1, 201), default=24)
    parser.add_argument("--max-p95-ms", type=float, default=2000)
    parser.add_argument("--output", type=Path, default=Path(".devdata/reports/console-load.json"))
    raise SystemExit(asyncio.run(measure(parser.parse_args())))
