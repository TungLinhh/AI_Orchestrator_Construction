#!/usr/bin/env python
"""Verify configured model providers with a real call.

Reports presence and validity of each credential, never the credential itself.
A provider is reported as `invalid` only when a request to the provider itself
says so — an inference is not evidence, and "we have a key" is not "the key
works".

The default run costs nothing: it checks `/models` or `/key`, which every
provider exposes. Pass `--live` to make one real completion, which is the only
way to prove the account can actually spend.

    uv run python scripts/smoke_test_providers.py
    uv run python scripts/smoke_test_providers.py --live --profile default
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from typing import Any

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.models.gateway import ModelRequest
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import build_providers_from_settings

#: Where each provider's credential is created, and what to ask it.
PROVIDER_INFO: dict[str, dict[str, str]] = {
    "openai": {
        "secret": "OPENAI_API_KEY",
        "where": "https://platform.openai.com/api-keys",
        "check_url": "https://api.openai.com/v1/models",
    },
    "anthropic": {
        "secret": "ANTHROPIC_API_KEY",
        "where": "https://console.anthropic.com/settings/keys",
        "check_url": "https://api.anthropic.com/v1/models",
    },
    "openrouter": {
        "secret": "OPENROUTER_API_KEY",
        "where": "https://openrouter.ai/settings/keys",
        "check_url": "https://openrouter.ai/api/v1/key",
    },
    "google": {
        "secret": "GEMINI_API_KEY",
        "where": "https://aistudio.google.com/apikey",
        "check_url": None,  # no unauthenticated probe; reported as 'unknown'
    },
}

#: Which model to try for a live call, per provider. A cheap one, because this
#: is a smoke test and the goal is to prove the path works, not to benchmark.
LIVE_MODELS: dict[str, str] = {
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-20250514",
    "openrouter": "openai/gpt-4.1-mini",
    "google": "gemini-2.0-flash",
}

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def _colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _probe(name: str, api_key: str, url: str | None) -> str:
    """Ask the provider whether the key is valid. Returns a status word."""
    if url is None:
        return "unknown (no unauthenticated probe endpoint)"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return "valid" if response.status < 400 else f"invalid (HTTP {response.status})"
    except urllib.error.HTTPError as exc:
        # 401/403 means the key was rejected. 404 means the endpoint is wrong,
        # which is not evidence about the key.
        if exc.code in {401, 403}:
            return f"invalid (HTTP {exc.code})"
        return f"unverified (HTTP {exc.code})"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"unreachable ({type(exc).__name__})"


def _key_for(settings: Any, provider: str) -> str:
    attr = {
        "openai": "openai_api_key",
        "anthropic": "anthropic_api_key",
        "openrouter": "openrouter_api_key",
        "google": "gemini_api_key",
    }[provider]
    return getattr(settings, attr).get_secret_value()


def report_providers(*, live: bool, profile: str) -> int:
    settings = get_settings()
    print(f"environment: {settings.environment.value}")
    print(f"default model profile: {settings.model_provider_default}")
    print(f"default embedding provider: {settings.embedding_provider_default}")
    print()

    print(f"{'PROVIDER':<12} {'SECRET':<22} {'CONFIGURED':<11} {'STATUS'}")
    print("-" * 72)

    exit_code = 0
    for provider, info in PROVIDER_INFO.items():
        key = _key_for(settings, provider)
        if not key:
            status = _colour("missing", YELLOW)
            configured = "no"
        else:
            configured = "yes"
            status = _probe(provider, key, info["check_url"])
            if status.startswith("invalid"):
                status = _colour(status, RED)
                exit_code = 1
            else:
                status = _colour(status, GREEN)
        print(f"{provider:<12} {info['secret']:<22} {configured:<11} {status}")
        print(f"{'':<12} {DIM}create at {info['where']}{RESET}")

    print()
    required = settings.available_model_providers()
    if not required:
        print(
            _colour(
                "no provider credential is configured; the deterministic provider "
                "will be used and nothing will be charged",
                YELLOW,
            )
        )
    else:
        print(f"usable providers: {', '.join(required)}")

    if not live:
        print()
        print("re-run with --live to make one real completion call")
        return exit_code

    print()
    print(f"live call against profile {profile!r}:")
    return asyncio.run(_run_live(settings, profile, exit_code))


async def _run_live(settings: Any, profile: str, exit_code: int) -> int:
    from ai_orchestrator.domain.enums import DataClassification
    from ai_orchestrator.models.gateway import ModelCandidate, ModelGateway, ModelProfile
    from ai_orchestrator.models.providers import DeterministicProvider

    profiles = dict(default_profiles())
    candidates = []
    for provider_name, model in LIVE_MODELS.items():
        if not _key_for(settings, provider_name):
            continue
        candidates.append(ModelCandidate(provider=provider_name, model=model))

    if not candidates:
        print("  no credential available; skipping the live call")
        return exit_code

    providers = build_providers_from_settings(settings)
    providers["deterministic"] = DeterministicProvider()
    live_profile = ModelProfile(name=profile, candidates=tuple(candidates))
    # `live_profile` must come last so it wins over the catalogue entry of
    # the same name; otherwise the smoke test silently exercises the
    # deterministic provider and proves nothing about the credential.
    gateway = ModelGateway(providers=providers, profiles={**profiles, profile: live_profile})

    request = ModelRequest(
        profile=profile,
        system_instructions="You are a connectivity probe. Answer with one short word.",
        prompt="Reply with exactly: OK",
        max_output_tokens=20,
        data_classification=DataClassification.INTERNAL,
        output_schema={
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
        },
    )
    try:
        response = await gateway.complete(request)
    except Exception as exc:
        print(f"  {RED}FAILED{RESET}: {type(exc).__name__}: {exc}")
        return 1

    print(f"  model      : {response.model_used}")
    print(f"  provider   : {response.provider}")
    print(f"  routing    : {response.decision.routing_reason if response.decision else 'n/a'}")
    print(
        f"  tokens     : {response.usage.total} "
        f"(in={response.usage.input_tokens} out={response.usage.output_tokens})"
    )
    print(f"  cost       : {response.cost_usd} USD")
    print(f"  latency    : {response.latency_ms} ms")
    print(f"  structured : {response.text[:200]}")
    try:
        parsed = json.loads(response.text)
        print(f"  parsed     : reply={parsed.get('reply')!r}")
    except json.JSONDecodeError, TypeError:
        print("  parsed     : (not valid JSON — structured output was requested but not honoured)")
        exit_code = 1
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="make one real completion call (costs money)",
    )
    parser.add_argument("--profile", default="default", help="profile to use for the live call")
    args = parser.parse_args()
    return report_providers(live=args.live, profile=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
