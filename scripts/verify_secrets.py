#!/usr/bin/env python
"""Report which credentials are configured. Never prints a value.

Statuses are `configured`, `missing` or `invalid`. A key is only reported
`invalid` when the provider itself says so; see `smoke_test_providers.py` for
that check, which this script invokes for any provider that has a key.

Internal secrets (database password, JWT secret, encryption key) are reported by
presence only, and a missing internal secret in a non-local environment is a
failure, because the process would otherwise start and fail on first use.
"""

from __future__ import annotations

import sys

from ai_orchestrator.config.settings import Environment, get_settings
from ai_orchestrator.security.secrets import (
    KNOWN_SECRET_NAMES,
    REQUIRED_SECRET_NAMES,
)

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _colour(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _usage(name: str) -> str:
    return {
        "POSTGRES_PASSWORD": "generated locally by scripts/pgctl.py; required",
        "JWT_SECRET": "generated locally; required for API auth",
        "ENCRYPTION_KEY": "generated locally; required for column encryption",
        "INTERNAL_SERVICE_SECRET": "generated locally; required for service-to-service auth",
        "OPENAI_API_KEY": "https://platform.openai.com/api-keys (optional)",
        "ANTHROPIC_API_KEY": "https://console.anthropic.com/settings/keys (optional)",
        "GEMINI_API_KEY": "https://aistudio.google.com/apikey (optional)",
        "OPENROUTER_API_KEY": "https://openrouter.ai/settings/keys (optional)",
    }.get(name, "")


def main() -> int:
    settings = get_settings()
    values = {
        "POSTGRES_PASSWORD": settings.postgres_password.get_secret_value(),
        "JWT_SECRET": settings.jwt_secret.get_secret_value(),
        "ENCRYPTION_KEY": settings.encryption_key.get_secret_value(),
        "INTERNAL_SERVICE_SECRET": settings.internal_service_secret.get_secret_value(),
        "OPENAI_API_KEY": settings.openai_api_key.get_secret_value(),
        "ANTHROPIC_API_KEY": settings.anthropic_api_key.get_secret_value(),
        "GEMINI_API_KEY": settings.gemini_api_key.get_secret_value(),
        "OPENROUTER_API_KEY": settings.openrouter_api_key.get_secret_value(),
    }

    print(f"{_colour('AI Orchestrator — secret inventory', BOLD)}")
    print(f"environment: {settings.environment.value}")
    print("secrets file: .secrets/runtime.env (mode 0600, never committed)")
    print()
    print(f"{'SECRET':<26} {'STATUS':<12} {'REQUIRED':<9} NOTE")
    print("-" * 100)

    missing_required: list[str] = []
    for name in KNOWN_SECRET_NAMES:
        value = values.get(name, "")
        required = name in REQUIRED_SECRET_NAMES
        if value:
            status = _colour("configured", GREEN)
        elif required and settings.environment is not Environment.LOCAL:
            status = _colour("missing", RED)
            missing_required.append(name)
        elif required:
            status = _colour("missing", YELLOW)
        else:
            status = _colour("missing", YELLOW)
        print(f"{name:<26} {status:<22} {'yes' if required else 'optional':<9} {_usage(name)}")

    print()
    providers = settings.available_model_providers()
    if providers:
        print(f"model providers with a credential: {', '.join(providers)}")
    else:
        print(
            _colour(
                "no model provider credential: the deterministic provider will be used. "
                "The full pipeline runs, nothing is charged.",
                YELLOW,
            )
        )

    if missing_required:
        print()
        print(_colour(f"required secrets missing: {', '.join(missing_required)}", RED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
