"""Read-only integration snapshot from a running local API and its dependencies."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx


async def main(args):
    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("This probe only accepts a local API")
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "GET and socket probes only",
        "checks": [],
    }
    async with httpx.AsyncClient(
        base_url=args.base, timeout=30, headers={"x-organization-id": args.org}
    ) as client:
        for path in (
            "/health",
            "/ready",
            "/api/v1/model-profiles",
            "/api/v1/agents?limit=200",
            "/api/v1/tools?limit=200",
            "/api/v1/skills?limit=200",
            "/api/v1/documents/summary",
            "/api/v1/stream/status",
        ):
            try:
                response = await client.get(path)
                response.raise_for_status()
                report["checks"].append({"path": path, "passed": True, "body": response.json()})
            except Exception as exc:
                report["checks"].append(
                    {"path": path, "passed": False, "error": type(exc).__name__ + ": " + str(exc)}
                )
    for port in (55432, 4222, 7233):
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
            writer.close()
            await writer.wait_closed()
            report["checks"].append({"port": port, "passed": True, "scope": "socket only"})
        except (OSError, TimeoutError) as exc:
            report["checks"].append({"port": port, "passed": False, "error": str(exc)})
    report["passed"] = all(check["passed"] for check in report["checks"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "checks": len(report["checks"]),
                "output": str(args.output),
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
