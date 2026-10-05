"""Report the native target and the mandatory four-week shadow gate without shortening it."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
from datetime import UTC, datetime
from pathlib import Path

from ai_orchestrator.application.shadow import go_live_readiness
from ai_orchestrator.persistence.session import Database


async def main(org: str, output: Path) -> int:
    db = Database.from_settings()
    try:
        async with db.tenant_session(org) as session:
            readiness = await go_live_readiness(session, org, now=datetime.now(UTC))
        report = {
            "target": "native processes on Ubuntu-24.04 in WSL",
            "system": platform.platform(),
            "python": platform.python_version(),
            "organization_id": org,
            "observed_at": datetime.now(UTC).isoformat(),
            "shadow": readiness.as_dict(),
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if readiness.ready else 1
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.org, args.output)))
