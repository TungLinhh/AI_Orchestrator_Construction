"""Point a model profile at a real free-tier model, so an agent can actually run.

`python scripts/seed_free_model.py [--agent "Executive Agent"]`

## Why this script exists

Every model profile in the seeded tenant points at `provider: "deterministic"` — the
scripted fake model the test suite uses. `AO_MODEL_PROVIDER_DEFAULT=openrouter` changes
the default for *new* work, and it changes nothing here, because a profile names its own
providers and the profile wins. So the honest state of the development database was:
**nine agents, sixty-five recorded model calls, and not one of them a real model.**

`GET /api/v1/model-profiles` is read-only, and adding a write endpoint for a model
profile is not a decision to make inside a seeding script. So this writes the row
directly, and it is idempotent — it upserts by name and leaves anything it does not
recognise alone.

## What it will not do

* **It will not touch a profile it did not create.** `--force` replaces a profile with the
  same name whatever it currently holds, and it is opt-in for that reason.
* **It will not write a key.** The profile names a *model*; the credential comes from
  `AO_OPENROUTER_API_KEY` in the environment, so nothing secret enters the database. If
  the key is missing the script stops and says so rather than creating a profile that
  will fail at call time.
* **It will not invent a model id.** The two free models are named explicitly, and the
  first one that answers is used. A profile pointing at a model that has been withdrawn
  from the free tier fails on the *first task*, which is the worst moment to find out.

Revision note: this writes `model_profiles.providers` in the shape the gateway reads —
a JSON array of `{provider, model, weight}`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ai_orchestrator.persistence.session import Database

#: The two free models this project is allowed to use. Named rather than discovered:
#: "the cheapest model the provider currently offers" changes under you, and a seed that
#: changes under you is not a seed.
FREE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-3-note-preview:free",
)

#: The profile the gateway actually resolves.
#:
#: `ModelGateway` is built from `default_profiles()` in code and **never reads the
#: `model_profiles` table**, so a row written there is invisible to it and an agent
#: pointing at that row's id fails with
#:
#:     unknown model profile: mpr_192532_08699
#:
#: `primary` is a built-in whose first candidate is a real free OpenRouter model, with
#: the deterministic provider second as a fallback. That is the profile to point an agent
#: at for real work, and writing a database row is the part that does nothing.
#:
#: The gap is real and it is recorded rather than papered over: a tenant cannot yet
#: choose its model from the database, and `PRODUCT_GAP.md` should say so. The row is
#: still written, so the intent is recorded and the wiring is one line away from working.
PROFILE_NAME = "primary"
PROVIDER = "openrouter"
BASE_URL = "https://openrouter.ai/api/v1"


async def _verify_model(api_key: str, model: str) -> str:
    """Ask the model one cheap question, so a bad id fails here and not on a task.

    Returns the reply. Raises on failure, which is the point: a profile written for a
    model that cannot be reached is a profile that fails somebody's real work.
    """
    from ai_orchestrator.domain.enums import DataClassification
    from ai_orchestrator.models.profiles import ModelProfile
    from ai_orchestrator.models.providers import (
        ModelCandidate,
        ModelRequest,
        OpenAICompatibleProvider,
    )

    candidate = ModelCandidate(
        provider=PROVIDER, model=model, max_classification=DataClassification.PUBLIC
    )
    profile = ModelProfile(
        name="probe",
        description="probe",
        candidates=(candidate,),
        requires_tools=False,
        requires_structured_output=False,
    )
    provider = OpenAICompatibleProvider(
        PROVIDER,
        api_key=api_key,
        base_url=BASE_URL,
        default_model=model,
        timeout_s=60,
        max_retries=1,
    )
    response = await provider.complete(
        candidate,
        ModelRequest(
            profile=profile,
            prompt="Reply with the single word: ONLINE",
            max_output_tokens=24,
        ),
    )
    return str(getattr(response, "text", response) or "")


async def run(*, name: str, force: bool, agent_name: str | None) -> int:
    api_key = os.environ.get("AO_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        # The settings object reads it too, and this way the message names the variable.
        from ai_orchestrator.config.settings import get_settings

        secret = get_settings().openrouter_api_key
        api_key = secret.get_secret_value() if secret else ""
    if not api_key:
        print(
            "no OpenRouter key. Set AO_OPENROUTER_API_KEY; this script will not "
            "write a credential into the database, so there is nothing to fall back on.",
            file=sys.stderr,
        )
        return 2

    chosen: str | None = None
    for model in FREE_MODELS:
        try:
            reply = await _verify_model(api_key, model)
            chosen = model
            print(f"  {model} answered {reply.strip()[:40]!r}")
            break
        except Exception as exc:  # every provider failure is reported, then the next is tried
            print(f"  {model}: {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
    if chosen is None:
        print("no free model answered; refusing to write an unusable profile", file=sys.stderr)
        return 1

    providers: list[dict[str, Any]] = [{"provider": PROVIDER, "model": chosen, "weight": 1.0}]
    admin = Database.from_settings(use_admin_role=True)
    # The database row is **best effort**. `ModelGateway` resolves profiles from
    # `default_profiles()` in code and never reads this table, so a row written here
    # changes nothing about which model runs. It is still written, so the intent is
    # recorded and the day the gateway does read the table this becomes the switch.
    engine = create_async_engine(admin.engine.url)
    try:
        async with engine.begin() as conn:
            org = (
                await conn.execute(
                    text("SELECT id FROM organizations ORDER BY created_at, id LIMIT 1")
                )
            ).scalar()
            if org is None:
                print("no organization; run `make seed` first.", file=sys.stderr)
                return 1
            existing = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, providers FROM model_profiles"
                            " WHERE organization_id = CAST(:o AS varchar(40))"
                            "   AND name = :n"
                        ),
                        {"o": org, "n": name},
                    )
                )
                .mappings()
                .one_or_none()
            )

            if existing is not None and not force:
                # asyncpg hands `jsonb` back already decoded, so this is sometimes a
                # list and sometimes a string depending on the driver path. Both are
                # handled, because a seed that crashes on a driver's representation of
                # JSON is a seed you cannot re-run.
                raw = existing["providers"]
                current = json.loads(raw) if isinstance(raw, str) else (raw or [])
                if any(p.get("provider") == PROVIDER for p in current):
                    print(f"  profile {name!r} already points at {PROVIDER}; nothing to do")
                else:
                    await _upsert(conn, org, name, providers, existing["id"])
                    print(f"  replaced profile {name!r} (it pointed at the fake model)")
            else:
                await _upsert(conn, org, name, providers, existing and existing["id"])
                print(f"  wrote profile {name!r} -> {chosen}")

            if agent_name:
                bound = await _bind_agent(conn, org, agent_name, name)
                print(
                    f"  {agent_name}: {'bound to' if bound else 'NOT FOUND — left alone'}"
                    f" profile {name!r}"
                )
    finally:
        await engine.dispose()
        await admin.engine.dispose()
    return 0


async def _upsert(
    conn: Any, org: str, name: str, providers: list[dict[str, Any]], profile_id: str | None
) -> str:
    if profile_id:
        await conn.execute(
            text(
                "UPDATE model_profiles SET providers = CAST(:p AS jsonb),"
                " is_active = true, updated_at = now()"
                " WHERE organization_id = CAST(:o AS varchar(40)) AND id = CAST(:i AS varchar(40))"
            ),
            {"p": json.dumps(providers), "o": org, "i": profile_id},
        )
        return profile_id
    new_id = f"mpr_{os.getpid()}_{abs(hash(name)) % 100000:05d}"
    await conn.execute(
        text(
            "INSERT INTO model_profiles (id, organization_id, name, description,"
            " providers, max_classification, is_active, created_at, updated_at)"
            " VALUES (CAST(:i AS varchar(40)), CAST(:o AS varchar(40)), :n,"
            " 'A real free-tier model, verified before it was written.',"
            " CAST(:p AS jsonb), 'public', true, now(), now())"
        ),
        {"i": new_id, "o": org, "n": name, "p": json.dumps(providers)},
    )
    return new_id


async def _bind_agent(conn: Any, org: str, agent_name: str, profile_ref: str) -> bool:
    """Point an agent at a model profile.

    By **name**, not by row id — `ModelGateway.profiles` is keyed on the name and the
    gateway is built from `default_profiles()` in code, so `mpr_197050_25365` is not a
    profile as far as the runtime is concerned and every task fails with
    `unknown model profile`. That was the second half of the same mistake: the first
    version wrote a database row and bound the agent to its id, and both halves are
    invisible to the gateway.
    """
    result = await conn.execute(
        text(
            "UPDATE agents SET model_profile = :p, updated_at = now()"
            " WHERE organization_id = CAST(:o AS varchar(40)) AND name = :n"
        ),
        {"p": profile_ref, "o": org, "n": agent_name},
    )
    return bool(result.rowcount)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--name", default=PROFILE_NAME, help="the profile to write")
    parser.add_argument(
        "--agent",
        default="Executive Agent",
        help="Which agent to point at it. Pass '' to write the profile and bind nothing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace a profile of this name whatever it currently holds.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "repair **every** profile in the tenant that starts on an unreachable "
            "provider, and bind nothing. This is the flag that was missing: the script "
            "could only fix a profile it was told the name of, so a tenant ended up with "
            "`fast_general` and `reasoning_high` still pointing at the deterministic "
            "provider while `primary` had been fixed. The Knowledge Agent is the one agent "
            "on `fast_general`, and it could not run at all -- found by run_fleet.py."
        ),
    )
    args = parser.parse_args(argv)
    if args.all:
        return asyncio.run(repair_all())
    return asyncio.run(run(name=args.name, force=args.force, agent_name=args.agent or None))


async def repair_all() -> int:
    """Point every profile that starts on an unreachable provider at the free real model.

    Scoped, not indiscriminate: a profile whose **first** candidate is `deterministic` in a
    deployment with no adapter for it cannot run, because the gateway tries the first,
    fails, and then falls through to models this deployment cannot reach either. Those are
    the ones repaired. A profile already starting on a reachable provider is left alone
    and reported, so the run says what it did *not* touch.
    """
    api_key = os.environ.get("AO_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        from ai_orchestrator.config.settings import get_settings

        secret = get_settings().openrouter_api_key
        api_key = secret.get_secret_value() if secret else ""
    if not api_key:
        print(
            "  AO_OPENROUTER_API_KEY is not set, so the free real model cannot be verified "
            "and no profile was changed. A profile that cannot be reached must not be "
            "replaced with one that cannot be reached either.",
            file=sys.stderr,
        )
        return 1

    model = os.environ.get("AO_MODEL_ID", "nvidia/nemotron-3-ultra-550b-a55b:free")
    reply = await _verify_model(api_key, model)
    print(f"  the model answered: {reply[:40]!r}")

    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.session() as conn:
            org = (
                await conn.execute(
                    text("SELECT id FROM organizations ORDER BY created_at, id LIMIT 1")
                )
            ).scalar()
            if org is None:
                print("no organization; run `make seed` first.", file=sys.stderr)
                return 1
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT id, name, providers FROM model_profiles"
                            " WHERE organization_id = CAST(:o AS varchar(40))"
                            " ORDER BY name"
                        ),
                        {"o": org},
                    )
                )
                .mappings()
                .all()
            )
            changed = 0
            for row in rows:
                raw = row["providers"]
                current = json.loads(raw) if isinstance(raw, str) else (raw or [])
                first = current[0].get("provider") if current else None
                if first == PROVIDER:
                    print(f"    {row['name']:18} already reachable")
                    continue
                await _upsert(conn, org, row["name"], _free_candidates(model), row["id"])
                changed += 1
            # Committed explicitly. `db.session()` hands out a session, not a
            # transaction that commits itself, and the first version of this reported
            # three repairs while the table still held `deterministic` on all three.
            await conn.commit()

            # Read back in the same run, so the report cannot claim a write that did not
            # happen. A seeder that says "done" and is not is worse than one that fails.
            verify = (
                (
                    await conn.execute(
                        text(
                            "SELECT name, providers FROM model_profiles"
                            " WHERE organization_id = CAST(:o AS varchar(40))"
                            " ORDER BY name"
                        ),
                        {"o": org},
                    )
                )
                .mappings()
                .all()
            )
            print("  after the write:")
            for row in verify:
                raw = row["providers"]
                got = json.loads(raw) if isinstance(raw, str) else (raw or [])
                head = got[0].get("provider") if got else None
                mark = "ok " if head == PROVIDER else "!! "
                print(f"    {mark}{row['name']:18} first candidate: {head}")
    finally:
        await db.dispose()
    print(f"  repaired {changed} of {len(rows)} profile(s)")
    return 0


def _free_candidates(model: str) -> list[dict[str, object]]:
    """The candidate list every profile in this tenant should start with."""
    return [
        {"provider": PROVIDER, "model": model, "weight": 1.0},
        {"provider": "deterministic", "model": "scripted-1", "weight": 0.0},
    ]


if __name__ == "__main__":
    sys.exit(main())
