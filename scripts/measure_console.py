"""Measure actual console read latency at bounded local concurrency."""

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

import httpx


async def main(args) -> int:
    semaphore = asyncio.Semaphore(args.concurrency)
    endpoints = ["/health", "/api/v1/ui?org=" + args.org, "/api/v1/departments"]

    async with httpx.AsyncClient(
        base_url=args.base_url, timeout=15, headers={"x-organization-id": args.org}
    ) as client:

        async def sample(path):
            async with semaphore:
                start = time.monotonic()
                try:
                    response = await client.get(path)
                    response.raise_for_status()
                    return {
                        "path": path,
                        "ms": round((time.monotonic() - start) * 1000, 2),
                        "bytes": len(response.content),
                        "error": None,
                    }
                except httpx.HTTPError as exc:
                    return {
                        "path": path,
                        "ms": round((time.monotonic() - start) * 1000, 2),
                        "bytes": 0,
                        "error": type(exc).__name__,
                    }

        rows = await asyncio.gather(
            *(sample(path) for _ in range(args.samples) for path in endpoints)
        )
    summaries = []
    for path in endpoints:
        values = [r for r in rows if r["path"] == path]
        times = sorted(r["ms"] for r in values)
        summaries.append(
            {
                "path": path,
                "requests": len(values),
                "errors": sum(r["error"] is not None for r in values),
                "p50_ms": times[math.ceil(len(times) * 0.5) - 1],
                "p95_ms": times[math.ceil(len(times) * 0.95) - 1],
                "max_bytes": max(r["bytes"] for r in values),
            }
        )
    report = {
        "concurrency": args.concurrency,
        "results": summaries,
        "samples": rows,
        "limitations": "Local read benchmark only; not a production workload or SLA.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summaries, indent=2))
    return int(any(r["errors"] for r in summaries))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--samples", type=int, choices=range(1, 101), default=10)
    parser.add_argument("--concurrency", type=int, choices=range(1, 21), default=3)
    parser.add_argument(
        "--output", type=Path, default=Path(".devdata/reports/console-latency.json")
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
