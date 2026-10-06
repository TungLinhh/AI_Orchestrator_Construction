"""Execute a complete, explicit synthetic workflow using a real configured model."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ai_orchestrator.application.business_workflow import BusinessWorkflowService, create_workflow
from ai_orchestrator.application.workflow_fixtures import hiring_fixture, procurement_fixture
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.session import Database


async def main(
    org: str,
    kind: str,
    output: Path,
    resume: str | None,
    retry: str | None = None,
    packet: Path | None = None,
) -> int:
    if get_settings().model_provider_default in {"fake", "scripted", "deterministic"}:
        raise SystemExit(
            "Configure a real provider before creating an acceptance run: "
            "AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python "
            "scripts/run_business_workflow.py ..."
        )
    if packet and (resume or retry):
        raise ValueError("A source packet creates a fresh run; it cannot change an existing run")
    source_brief = None
    if packet:
        from scripts.workflow_acceptance_packet import load_brief

        source_brief = load_brief(packet, kind)
    db = Database.from_settings()
    try:
        root = resume
        if not root:
            async with db.tenant_session(org) as session:
                brief = source_brief or (
                    hiring_fixture() if kind == "mep_hiring" else procurement_fixture()
                )
                if retry:
                    from ai_orchestrator.persistence.repositories.task import TaskRepository

                    old = await TaskRepository(session, org).get(retry)
                    if old.input.get("business_workflow") != kind or old.status != "failed":
                        raise ValueError("Retry requires a failed workflow of the same kind")
                    brief = old.input["brief"]
                root = await create_workflow(
                    session, org, kind, "simulation", brief, reuse_source=retry
                )

        print("workflow root:", root, flush=True)
        report = await BusinessWorkflowService(db, org).run(root)
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text,
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(
            report["status"],
            report["completed"],
            "/",
            report["total"],
            "mode=",
            report["mode"],
            flush=True,
        )
        print(
            "Open: http://127.0.0.1:8100/api/v1/ui?org=" + org + "#/processes/workflows/" + root,
            flush=True,
        )
        return (
            0 if report["status"] == "completed" and report["completed"] == report["total"] else 1
        )
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--kind", choices=["mep_hiring", "procurement"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--resume")
    source.add_argument("--retry")
    source.add_argument(
        "--packet", type=Path, help="Verified synthetic source manifest; fresh run only"
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.org, args.kind, args.output, args.resume, args.retry, args.packet))
    )
