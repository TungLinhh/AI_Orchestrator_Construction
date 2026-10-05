"""Measure live goal completion, queue drainage and department contracts.

Writes each result as it finishes, including failures and actual provider usage.
Exit status counts unexpected outcomes. Required human reviews are reported separately
from autonomous completions and must stop before later contracted stages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_pipeline import _create_root
from sqlalchemy import select

from ai_orchestrator.application.pipeline import PipelineOutcome, run_pipeline
from ai_orchestrator.application.scenarios import SCENARIOS, Scenario, agent_for
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.models import Agent, ModelUsage, Task
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


def expected_review_pause(
    scenario: Scenario, outcome: PipelineOutcome, owned: list[dict], statuses: dict
) -> bool:
    """A required review needs the owning draft and no work past the gate."""
    field = scenario.human_review_field
    if not field or not outcome.waiting_for_human or not outcome.human_exceptions:
        return False
    waiting = set(outcome.waiting_for_human)
    drafts = {row["task_id"]: row for row in owned if row["status"] == "waiting_for_approval"}
    if waiting != set(drafts) or set(drafts) != {row["task_id"] for row in owned}:
        return False
    if any(
        status not in {"running", "waiting_for_approval", "completed"}
        for status in statuses.values()
    ):
        return False
    if any(
        not row.get("draft_review_ready") or row.get("work_past_review") for row in drafts.values()
    ):
        return False
    return {item["task_id"] for item in outcome.human_exceptions} == waiting and all(
        item["class"] == "agent_request" and item["reason"].strip()
        for item in outcome.human_exceptions
    )


async def measure(db: Database, org: str, scenario: Scenario) -> dict:
    marker = f"[lần chạy {uuid.uuid4().hex[:8]}]"
    root_id = await _create_root(
        db,
        org,
        f"{scenario.objective}\n\n{marker}",
        dict(scenario.expected_output),
        {"owning_department": agent_for(scenario.department), "owning_office": scenario.office},
        f"{scenario.goal}\n\n{marker}",
    )
    print(f"START {scenario.key} root={root_id}", flush=True)
    started = time.monotonic()
    outcome = await run_pipeline(db, org, root_id, auto_approve=False)
    async with db.tenant_session(org) as session:
        statuses = await TaskRepository(session, org).subtree_statuses(outcome.root_task_id)
        rows = (
            await session.execute(
                select(Task, Agent.name)
                .outerjoin(Agent, Agent.id == Task.owner_agent_id)
                .where(Task.organization_id == org, Task.id.in_(statuses))
            )
        ).all()
        usage = (
            await session.execute(
                select(ModelUsage.provider, ModelUsage.model_used, ModelUsage.status).where(
                    ModelUsage.organization_id == org, ModelUsage.task_id.in_(statuses)
                )
            )
        ).all()
        models = Counter(f"{provider}/{model} ({status})" for provider, model, status in usage)
        real_calls = sum(
            status == "ok" and provider not in {"fake", "scripted", "deterministic"}
            for provider, _model, status in usage
        )
        fake_calls = sum(
            status == "ok" and provider in {"fake", "scripted", "deterministic"}
            for provider, _model, status in usage
        )
        owned = []
        for task, owner in rows:
            if owner != agent_for(scenario.department):
                continue
            required = (task.expected_output_schema or {}).get("required", [])
            owned.append(
                {
                    "task_id": str(task.id),
                    "status": str(task.status),
                    "required_keys": required,
                    "returned_keys": sorted(task.output or {}),
                    "full_contract": bool(required) and set(required) <= set(task.output or {}),
                    "draft_review_ready": bool(
                        (task.output or {}).get(scenario.human_review_field)
                    ),
                    "work_past_review": bool(scenario.human_review_field)
                    and any(
                        bool((task.output or {}).get(key))
                        for key in required
                        if key not in {scenario.human_review_field, "approvals_needed"}
                    ),
                }
            )
        errors = [
            {
                "task_id": str(task.id),
                "owner": owner,
                "category": task.failure_category,
                "reason": task.last_error,
            }
            for task, owner in rows
            if task.status == "failed"
        ]
    open_tasks = sum(
        status not in {"completed", "failed", "canceled", "expired"} for status in statuses.values()
    )
    complete_owned = [row for row in owned if row["status"] == "completed" and row["full_contract"]]
    completed = (
        outcome.finished
        and not open_tasks
        and bool(complete_owned)
        and real_calls > 0
        and not fake_calls
        and not outcome.human_exceptions
    )
    review_pause = (
        real_calls > 0
        and not fake_calls
        and expected_review_pause(scenario, outcome, owned, statuses)
    )
    passed = review_pause if scenario.human_review_field else completed
    result = {
        "scenario": scenario.key,
        "department": scenario.department,
        "root_task_id": outcome.root_task_id,
        "initial_root_task_id": root_id,
        "seconds": round(time.monotonic() - started, 2),
        "passed": passed,
        "person_free": completed,
        "expected_human_review": bool(scenario.human_review_field),
        "waiting_as_expected": review_pause,
        "queue_drained": open_tasks == 0,
        "open_tasks": open_tasks,
        "outcome": asdict(outcome),
        "department_tasks": owned,
        "real_model_calls": real_calls,
        "fake_model_calls": fake_calls,
        "models": dict(models),
        "errors": errors,
        "url": f"http://127.0.0.1:8100/api/v1/ui?org={org}#/give/{outcome.root_task_id}",
    }
    print(
        f"{'PASS' if passed else 'FAIL'} {scenario.key}: root={outcome.root_status}, "
        f"open={open_tasks}, department_contracts={len(complete_owned)}/{len(owned)}, "
        f"real_calls={real_calls}, fake_calls={fake_calls}, seconds={result['seconds']}",
        flush=True,
    )
    return result


async def main(org: str, keys: list[str], output: Path) -> int:
    settings = get_settings()
    if settings.model_provider_default in {"fake", "scripted", "deterministic", "null", "none"}:
        raise ValueError("set AO_MODEL_PROVIDER_DEFAULT to a live provider before measuring")
    db = Database.from_settings()
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "organization_id": org,
        "provider_setting": settings.model_provider_default,
        "concurrency": settings.pipeline_concurrency,
        "intent_limit": settings.max_distinct_intents_per_goal,
        "scenarios": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        for scenario in SCENARIOS:
            if keys and scenario.key not in keys:
                continue
            result = await measure(db, org, scenario)
            report["scenarios"].append(result)
            await asyncio.to_thread(
                output.write_text,
                json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            )
    finally:
        await db.dispose()
    failed = sum(not result["passed"] for result in report["scenarios"])
    completed_count = sum(result["person_free"] for result in report["scenarios"])
    review_count = sum(result["waiting_as_expected"] for result in report["scenarios"])
    print(
        f"{completed_count} autonomous completions, {review_count} required review pauses, "
        f"{failed} unexpected outcomes; "
        f"report: {output}",
        flush=True,
    )
    return failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument(
        "--only", action="append", choices=[scenario.key for scenario in SCENARIOS], default=[]
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.org, args.only, args.output)))
