#!/usr/bin/env python3
"""Review/apply a tenant-scoped prompt revision without reseeding the company.

Dry-run is default. Use --expected-sha256 from a reviewed current definition;
--prompt-file carries explicitly reviewed replacement text, including custom text.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ai_orchestrator.application.prompt_revision import revise_agent_prompt
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.session import Database


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = Database.from_settings()
    try:
        async with db.tenant_session(args.org) as session:
            result = await revise_agent_prompt(
                session,
                args.org,
                agent_id=args.agent,
                instructions=args.prompt_file.read_text(encoding="utf-8"),
                revision=args.revision,
                expected_hash=args.expected_sha256,
                actor=Actor(id="prompt-revision-operator", kind=ActorType.SYSTEM),
                apply=args.apply,
            )
        print(json.dumps(result, ensure_ascii=False))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
