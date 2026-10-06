"""Compare a feedback candidate to baseline without binding it to production.

Gold stays outside task inputs. Both arms run through TaskExecutionService. Mock
runs verify plumbing only and cannot satisfy the publication gate. Actual corpus
review by a department expert is recorded separately at publication.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from scripts.evaluate_organization import DATASET, run_cases
from scripts.organization_eval import corpus_hash, load_corpus
from sqlalchemy import select

from ai_orchestrator.application.business_workflow import payload_hash
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.persistence.models import SkillVersion, Task
from ai_orchestrator.persistence.session import Database


async def compare(db, org, version_id, cases, runtime, rounds=2):
    if rounds < 1 or rounds > 10:
        raise ValueError("Choose 1 to 10 evaluation rounds")
    async with db.tenant_session(org) as session:
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.organization_id == org,
                SkillVersion.id == version_id,
            )
        )
        if (
            version is None
            or version.is_published
            or version.derived_from.get("lesson_scope") != "proposal_only"
        ):
            raise PreconditionError("Select an unpublished feedback candidate")
        root = await session.get(Task, version.derived_from["workflow_root"])
        departments = {"mep_hiring": "hr", "procurement": "procurement"}
        if root is None or root.input.get("business_workflow") not in departments:
            raise PreconditionError("Candidate must reference an existing business campaign")
        department = departments[root.input["business_workflow"]]
        selected = [case for case in cases if case.department == department]
        if {case.split for case in selected} != {"train", "holdout"}:
            raise PreconditionError("Provide both train and holdout cases for this department")
        instructions = version.instructions
    digest = corpus_hash(selected)
    reports, tasks, regressions = [], [], []
    for iteration in range(rounds):
        # Alternate arm order to reduce the effect of changing provider availability.
        arms = ["baseline", "candidate"] if iteration % 2 == 0 else ["candidate", "baseline"]
        outcomes = {}
        for arm in arms:
            report = await run_cases(
                db,
                org,
                selected,
                runtime,
                dataset_digest=digest,
                feedback_candidate=(version_id, instructions) if arm == "candidate" else None,
            )
            reports.append({"round": iteration, "arm": arm, "report": report})
            outcomes[arm] = {row["case_id"]: row for row in report["results"]}
            for row in report["results"]:
                tasks.append(
                    {
                        "round": iteration,
                        "arm": arm,
                        "case_id": row["case_id"],
                        "split": row["split"],
                        "task_id": row["task_id"],
                        "passed": row["passed"],
                        "output_hash": payload_hash(row["output"]),
                        "agent_id": row["agent_id"],
                    }
                )
        for case in selected:
            before, after = outcomes["baseline"][case.id], outcomes["candidate"][case.id]
            if before["passed"] and not after["passed"]:
                regressions.append(
                    {"round": iteration, "case_id": case.id, "failures": after["failures"]}
                )
    candidate = [row for row in tasks if row["arm"] == "candidate"]
    passed = not regressions and all(row["passed"] for row in candidate)
    receipt = {
        "protocol": 1,
        "runtime": runtime,
        "rounds": rounds,
        "corpus_sha256": digest,
        "instructions_hash": payload_hash(instructions),
        "agent_id": tasks[0]["agent_id"],
        "tasks": tasks,
        "regressions": regressions,
        "passed": passed,
    }
    async with db.committing_tenant_session(org) as session:
        version = await session.scalar(
            select(SkillVersion)
            .where(SkillVersion.organization_id == org, SkillVersion.id == version_id)
            .with_for_update()
        )
        if (
            version.is_published
            or payload_hash(version.instructions) != receipt["instructions_hash"]
        ):
            raise PreconditionError(
                "Candidate changed during evaluation; keep reports and reevaluate"
            )
        version.test_results = {"passed": passed, "paired_evaluation": receipt}
        await AuditService(session, org).record(
            actor=Actor(id="feedback-skill-evaluator", kind=ActorType.SYSTEM),
            action="evaluation.paired",
            resource_type="skill_version",
            resource_id=version_id,
            context={"receipt_hash": payload_hash(receipt), "receipt": receipt},
        )
        await session.commit()
    return {
        "receipt": receipt,
        "reports": reports,
        "production_ready": False,
        "human_expert_review_required": True,
        "model_quality_measured": any(row["report"]["model_quality_measured"] for row in reports),
    }


async def main(args):
    if args.runtime == "live" and get_settings().model_provider_default != "openrouter":
        raise SystemExit(
            "Live evaluation requires AO_MODEL_PROVIDER_DEFAULT=openrouter; "
            "no tasks were created. Use uv run -m scripts.evaluate_feedback_skill ..."
        )
    db = Database.from_settings()
    try:
        report = await compare(
            db, args.org, args.version, load_corpus(args.dataset), args.runtime, args.rounds
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(
            f"Candidate passed: {report['receipt']['passed']}; "
            f"publication remains human-gated; {args.output}"
        )
        return int(not report["receipt"]["passed"])
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--runtime", choices=["mock", "live"], default="mock")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument(
        "--output", type=Path, default=Path(".devdata/reports/feedback-skill-eval.json")
    )
    raise SystemExit(asyncio.run(main(parser.parse_args())))
