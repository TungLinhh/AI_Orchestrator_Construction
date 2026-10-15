"""Draft sandbox skills, then compare baseline and candidate on frozen holdout.

Each arm uses the real task executor and free provider. Never publishes skills,
claims expert approval, or treats synthetic cases as production shadow activity.
Run as `python -m scripts.run_readiness_learning`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.evaluate_organization import run_cases
from scripts.organization_eval import corpus_hash, load_corpus

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.session import Database


async def main(args):
    if get_settings().model_provider_default != "openrouter":
        raise SystemExit("Explicit OpenRouter mode is required")
    cases = load_corpus(args.dataset)
    train = [case for case in cases if case.split == "train"]
    holdout = [case for case in cases if case.split == "holdout"]
    if not train or not holdout:
        raise ValueError("Training and independent holdout are both required")
    digest = corpus_hash(cases)
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "synthetic": True,
        "corpus_sha256": digest,
        "production_ready": False,
        "skills_published": False,
        "weight_training_performed": False,
        "rounds": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")

    db = Database.from_settings()
    try:
        report["training"] = await run_cases(
            db, args.org, train, "live", lessons=True, dataset_digest=digest
        )
        save()
        for iteration in range(args.rounds):
            arms = ["baseline", "candidate"] if iteration % 2 == 0 else ["candidate", "baseline"]
            pair = {"round": iteration + 1}
            report["rounds"].append(pair)
            for arm in arms:
                pair[arm] = await run_cases(
                    db,
                    args.org,
                    holdout,
                    "live",
                    candidate_lessons=arm == "candidate",
                    dataset_digest=digest,
                )
                save()
        report["passed"] = report["training"]["passed"] == len(train) and all(
            pair[arm]["passed"] == len(holdout)
            for pair in report["rounds"]
            for arm in ("baseline", "candidate")
        )
        report["limitations"] = (
            "Development corpus has no new expert review. Candidate lessons originate from "
            "train cases. A passing comparison alone does not authorize publication."
        )
        save()
    except Exception as exc:
        report.update(passed=False, error_type=type(exc).__name__, error=str(exc))
        save()
        raise
    finally:
        await db.dispose()
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "rounds": len(report["rounds"]),
                "output": str(args.output),
            }
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--rounds", type=int, choices=range(2, 6), default=2)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
