"""Preview or apply the free model chain to explicitly named tenant profiles.

Uses the tenant application role. Repeated application converges to the same
configuration. Never changes the proposer, credentials, approvals or agent bindings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from ai_orchestrator.application.model_profiles import load_profiles_from_session
from ai_orchestrator.domain.ids import make_id
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.persistence.session import Database


async def main(args):
    reference = default_profiles()["primary"]
    candidates = [
        {
            "provider": c.provider,
            "model": c.model,
            "max_input_tokens": c.max_input_tokens,
            "supports_tools": c.supports_tools,
            "supports_structured_output": c.supports_structured_output,
            "supports_vision": c.supports_vision,
            "max_classification": args.max_classification,
        }
        for c in reference.candidates
        if c.provider == "openrouter" and c.model.endswith(":free")
    ]
    names = args.profile or ["primary", "reasoning", "default", "fast_general", "reasoning_high"]
    if "proposer" in names:
        raise SystemExit("The independent proposer must remain disjoint from the subject models")
    report = {
        "observed_at": datetime.now(UTC).isoformat(),
        "organization_id": args.org,
        "applied": args.apply,
        "max_classification": args.max_classification,
        "changes": [],
    }
    db = Database.from_settings()
    try:
        async with db.tenant_session(args.org) as session:
            for name in names:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT id, providers, max_latency_ms, max_retries, "
                                "requires_structured_output FROM model_profiles "
                                "WHERE organization_id = :org AND name = :name FOR UPDATE"
                            ),
                            {"org": args.org, "name": name},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                report["changes"].append(
                    {"name": name, "before": dict(row) if row else None, "after": candidates}
                )
                if not args.apply:
                    continue
                if row:
                    await session.execute(
                        text(
                            "UPDATE model_profiles SET providers = CAST(:providers AS jsonb), "
                            "max_latency_ms = :deadline, max_retries = 0, "
                            "max_classification = :classification, "
                            "requires_structured_output = false, requires_tool_calling = true, "
                            "is_active = true, updated_at = now() "
                            "WHERE organization_id = :org AND id = :id"
                        ),
                        {
                            "org": args.org,
                            "id": row["id"],
                            "providers": json.dumps(candidates),
                            "deadline": args.deadline_ms,
                            "classification": args.max_classification,
                        },
                    )
                else:
                    await session.execute(
                        text(
                            "INSERT INTO model_profiles (id, organization_id, name, providers, "
                            "max_latency_ms, max_retries, requires_structured_output, "
                            "requires_tool_calling, max_classification, is_active) "
                            "VALUES (:id, :org, :name, CAST(:providers AS jsonb), :deadline, 0, "
                            "false, true, :classification, true)"
                        ),
                        {
                            "id": make_id("mpf"),
                            "org": args.org,
                            "name": name,
                            "providers": json.dumps(candidates),
                            "deadline": args.deadline_ms,
                            "classification": args.max_classification,
                        },
                    )
            if args.apply:
                loaded = await load_profiles_from_session(session, args.org)
                for name in names:
                    actual = [c.model for c in loaded.profiles[name].candidates]
                    if actual != [c["model"] for c in candidates]:
                        raise ValueError("Runtime read-back differs for " + name)
                report["read_back_verified"] = True
        # Verify committed values through a fresh tenant connection.
        if args.apply:
            async with db.tenant_session(args.org) as session:
                loaded = await load_profiles_from_session(session, args.org)
                report["effective"] = {
                    name: [c.model for c in loaded.profiles[name].candidates] for name in names
                }
                assert all(
                    models == [c["model"] for c in candidates]
                    for models in report["effective"].values()
                )
    finally:
        await db.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {"org": args.org, "applied": args.apply, "profiles": names, "output": str(args.output)}
        )
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-classification", choices=["public", "internal"], default="public")
    parser.add_argument("--deadline-ms", type=int, choices=range(1000, 120001), default=60000)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
