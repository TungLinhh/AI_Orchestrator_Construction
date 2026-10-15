"""Read tenant task counts and waiting work without changing tasks or running agents."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from ai_orchestrator.persistence.session import Database


async def main(args):
    db = Database.from_settings()
    try:
        async with db.tenant_session(args.org) as session:
            counts = (
                (
                    await session.execute(
                        text(
                            "SELECT status, count(*) AS tasks FROM tasks "
                            "WHERE organization_id = :org GROUP BY status ORDER BY status"
                        ),
                        {"org": args.org},
                    )
                )
                .mappings()
                .all()
            )
            waiting = (
                (
                    await session.execute(
                        text(
                            "SELECT id, title, status, parent_task_id, owner_agent_id, "
                            "created_at, last_error, failure_category FROM tasks "
                            "WHERE organization_id = :org "
                            "AND status NOT IN ('completed', 'failed', 'canceled', 'expired') "
                            "ORDER BY created_at, id LIMIT 50"
                        ),
                        {"org": args.org},
                    )
                )
                .mappings()
                .all()
            )
            running = (
                (
                    await session.execute(
                        text(
                            "SELECT id, task_id, started_at FROM executions "
                            "WHERE organization_id = :org AND status = 'running' "
                            "ORDER BY started_at LIMIT 50"
                        ),
                        {"org": args.org},
                    )
                )
                .mappings()
                .all()
            )
            dependencies = (
                (
                    await session.execute(
                        text(
                            "SELECT t.status, (t.parent_task_id IS NULL) AS is_root, "
                            "p.status AS parent_status, count(*) AS tasks FROM tasks t "
                            "LEFT JOIN tasks p ON p.id = t.parent_task_id "
                            "AND p.organization_id = t.organization_id "
                            "WHERE t.organization_id = :org AND t.status NOT IN "
                            "('completed', 'failed', 'canceled', 'expired') "
                            "GROUP BY t.status, (t.parent_task_id IS NULL), p.status "
                            "ORDER BY t.status, is_root, parent_status"
                        ),
                        {"org": args.org},
                    )
                )
                .mappings()
                .all()
            )
            roots = (
                (
                    await session.execute(
                        text(
                            "SELECT id, title, status, created_at, last_error, failure_category, "
                            "constraints->'workflow_lifecycle' AS lifecycle FROM tasks "
                            "WHERE organization_id = :org AND parent_task_id IS NULL "
                            "AND status NOT IN ('completed', 'failed', 'canceled', 'expired') "
                            "ORDER BY created_at, id LIMIT 50"
                        ),
                        {"org": args.org},
                    )
                )
                .mappings()
                .all()
            )
        report = {
            "observed_at": datetime.now(UTC).isoformat(),
            "organization_id": args.org,
            "scope": "Tenant application role, SELECT only; no automatic cleanup or retry",
            "counts": [dict(row) for row in counts],
            "oldest_nonterminal_tasks": [dict(row) for row in waiting],
            "running_executions": [dict(row) for row in running],
            "nonterminal_dependencies": [dict(row) for row in dependencies],
            "nonterminal_roots": [dict(row) for row in roots],
            "sample_limit": 50,
            "limitations": "Terminal includes failure. Waiting work is not necessarily abandoned.",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        print(json.dumps({"counts": report["counts"], "running": len(running)}))
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
