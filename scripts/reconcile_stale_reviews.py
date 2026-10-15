"""Preview/apply only reviews blocked by a terminal ancestor, preserving history."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_orchestrator.application.stale_reviews import reconcile_stale_reviews
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.session import Database


async def main(args):
    db = Database.from_settings()
    try:
        async with db.tenant_session(args.org) as session:
            rows = await reconcile_stale_reviews(
                session,
                args.org,
                Actor(id="review-reconciliation", kind=ActorType.SYSTEM),
                apply=args.apply,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"organization_id": args.org, "apply": args.apply, "findings": rows}, indent=2
            )
            + "\n"
        )
        print(json.dumps({"findings": len(rows), "applied": sum(r["applied"] for r in rows)}))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
