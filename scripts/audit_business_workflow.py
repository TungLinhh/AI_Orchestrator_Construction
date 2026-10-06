"""Read-only acceptance audit of persisted steps, files and independent source records."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from ai_orchestrator.application.business_workflow import BusinessWorkflowService, payload_hash
from ai_orchestrator.domain.business_workflow import LEVEL_FACTORS, RUBRIC_SPEC, WORKFLOWS
from ai_orchestrator.integrations.recruitment_mail import DEV_DATA_DIR
from ai_orchestrator.persistence.models import (
    Approval,
    AuditLog,
    Event,
    Execution,
    ModelUsage,
    Task,
)
from ai_orchestrator.persistence.session import Database


async def audit(org: str, root_id: str, output: Path) -> None:
    db = Database.from_settings()
    checks: list[str] = []

    def require(ok: bool, label: str) -> None:
        if not ok:
            raise ValueError("Acceptance refused: " + label)
        checks.append(label)

    try:
        report = await BusinessWorkflowService(db, org).report(root_id)
        require(report["status"] == "completed", "root completed")
        require(report["mode"] == "simulation", "explicit simulation mode")
        stages = {s["key"]: s for s in report["stages"]}
        expected = WORKFLOWS[report["kind"]]
        require(list(stages) == [s.key for s in expected], "all ordered stages exist")
        require(all(s["status"] == "completed" for s in stages.values()), "all stages completed")
        async with db.tenant_session(org) as session:
            root = await session.get(Task, root_id)
            brief = root.input["brief"]
            for stage in expected:
                row = stages[stage.key]
                executions = (
                    (
                        await session.execute(
                            select(Execution).where(
                                Execution.organization_id == org,
                                Execution.task_id == row["id"],
                                Execution.status == "completed",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                require(bool(executions), stage.key + ": completed execution")
                events = (
                    await session.execute(
                        select(Event.id).where(
                            Event.organization_id == org,
                            Event.subject == row["id"],
                        )
                    )
                ).all()
                audits = (
                    await session.execute(
                        select(AuditLog.id).where(
                            AuditLog.organization_id == org,
                            AuditLog.task_id == row["id"],
                        )
                    )
                ).all()
                require(bool(events) and bool(audits), stage.key + ": events and audit")
                if stage.key != "close":
                    require(
                        report["summary"]["stage_evidence_hashes"][stage.key]
                        == payload_hash(row["output"]),
                        stage.key + ": artifact hash",
                    )
                if stage.kind == "model":
                    origin = row["reused_from"] or row["id"]
                    model_task = await session.get(Task, origin)
                    require(
                        not {
                            "test_cvs",
                            "interview_technical",
                            "interview_hr",
                            "offer_acceptance",
                            "onboarding_evidence",
                            "delivery",
                        }.intersection(model_task.input.get("brief", {})),
                        stage.key + ": future fixture inputs withheld",
                    )
                    calls = (
                        (
                            await session.execute(
                                select(ModelUsage).where(
                                    ModelUsage.organization_id == org,
                                    ModelUsage.task_id == origin,
                                    ModelUsage.status == "ok",
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    require(
                        bool(calls)
                        and all(
                            c.provider
                            not in {
                                "fake",
                                "unit-fake",
                                "scripted",
                                "deterministic",
                            }
                            for c in calls
                        ),
                        stage.key + ": real model provenance",
                    )
                if stage.kind == "gate":
                    result = row["output"]
                    require(
                        result["decision"] == "simulated_approved"
                        and result["human_decision"] is False,
                        stage.key + ": honest simulated review",
                    )
            ids = [root_id, *(s["id"] for s in stages.values())]
            real_reviews = (
                await session.execute(
                    select(Approval.id).where(
                        Approval.organization_id == org,
                        Approval.task_id.in_(ids),
                    )
                )
            ).all()
            require(not real_reviews, "no forged human approval rows")
        if report["kind"] == "mep_hiring":
            cvs = {c["candidate_id"]: c for c in stages["cv_intake"]["output"]["cvs"]}
            sent = {r["sha256"] for r in stages["test_mail"]["output"]["sent"]}
            require(set(cvs) == sent and len(cvs) == 3, "actual sent/received CV hashes match")
            scored = stages["scoring"]["output"]["candidates"]
            require({c["candidate_id"] for c in scored} == set(cvs), "every CV evaluated")
            # Independent expectations for the versioned synthetic exercise;
            # these outcomes are not supplied to the model as its answers.
            benchmark = {cvs[c["candidate_id"]]["filename"]: c["score"] for c in scored}
            threshold = stages["rubric"]["output"]["threshold"]
            require(
                benchmark["mep-test-a.txt"]
                >= threshold
                > benchmark["mep-test-b.txt"]
                > benchmark["mep-test-c.txt"],
                "benchmark: strong shortlisted; weak/injection below threshold",
            )
            for row in scored:
                cv = cvs[row["candidate_id"]]
                files = list(
                    (DEV_DATA_DIR / "recruitment" / org / root_id).glob(cv["sha256"] + ".*")
                )
                require(
                    len(files) == 1
                    and hashlib.sha256(files[0].read_bytes()).hexdigest() == cv["sha256"],
                    "downloaded CV hash: " + cv["filename"],
                )
                criteria = row["criteria"]
                require(
                    {c["key"] for c in criteria} == dict(RUBRIC_SPEC).keys()
                    and len(criteria) == len(RUBRIC_SPEC),
                    "full rubric: " + cv["filename"],
                )
                require(
                    all(
                        (c["evidence_quote"] in cv["text"] and bool(c["evidence_quote"]))
                        if c["level"] != "missing"
                        else not c["evidence_quote"]
                        for c in criteria
                    ),
                    "exact source quotations: " + cv["filename"],
                )
                require(
                    all(
                        "no evidence" not in c["evidence_quote"].lower()
                        for c in criteria
                        if c["level"] != "missing"
                    ),
                    "explicit missing evidence receives zero: " + cv["filename"],
                )
                total = sum(
                    dict(RUBRIC_SPEC)[c["key"]] * LEVEL_FACTORS[c["level"]] for c in criteria
                )
                require(total == row["score"], "independently recomputed score: " + cv["filename"])
            onboarding = stages["onboarding_setup"]["output"]
            require(len(onboarding["actions"]) == 5, "five sandbox onboarding actions")
            folder = DEV_DATA_DIR / "recruitment" / org / root_id / "onboarding"
            for action in onboarding["actions"]:
                file = folder / (action["action"] + ".json")
                require(
                    file.is_file()
                    and hashlib.sha256(file.read_bytes()).hexdigest() == action["artifact_sha256"],
                    "onboarding file: " + action["action"],
                )
            require(
                all(v == "scheduled" for v in onboarding["future_reviews"].values())
                and not report["summary"]["future_day_30_60_90_completed"],
                "future reviews not fabricated",
            )
        else:
            po = stages["po"]["output"]["lines"]
            require(
                {r["material_id"] for r in po} == {r["material_id"] for r in brief["boq"]},
                "every BOQ material awarded",
            )
            legal = {s["supplier_id"] for s in brief["suppliers"] if s["legal_valid"]}
            require(all(r["supplier_id"] in legal for r in po), "no red supplier awarded")
            delivery = stages["delivery"]["output"]
            for po_line in po:
                for source_name, output_name in (
                    ("grn_lines", "grn"),
                    ("invoice_lines", "invoice"),
                ):
                    source = [
                        r
                        for r in brief["delivery"][source_name]
                        if (r["material_id"], r["supplier_id"])
                        == (po_line["material_id"], po_line["supplier_id"])
                    ]
                    received = [
                        r
                        for r in delivery[output_name]
                        if r["material_id"] == po_line["material_id"]
                    ]
                    require(
                        source == received and len(source) == 1,
                        "independent " + output_name + ": " + po_line["material_id"],
                    )
                    require(source[0]["quantity"] == po_line["quantity"], "quantity matches source")
                    if output_name == "invoice":
                        require(
                            source[0]["unit_price"] == po_line["unit_price"], "price matches source"
                        )
            total = sum(r["quantity"] * r["unit_price"] for r in po)
            require(
                total == report["summary"]["total_vnd"], "independently recomputed purchase total"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text,
            json.dumps(
                {"root": root_id, "mode": "simulation", "passed": True, "checks": checks},
                ensure_ascii=False,
                indent=2,
            ),
        )
        print(f"PASS {root_id}: {len(checks)} independently inspected checks")
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(audit(args.org, args.root, args.output))
