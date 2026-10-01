"""Refuse to run the e2e suite with a dependency missing.

The event-pipeline tests skip themselves when NATS is unreachable, and the MCP
tests skip when `openai` is not installed. Both are correct for a developer
working on an unrelated module, and both are wrong for a gate: a run that
reports `16 passed, 5 skipped` reads as success while five scenarios did not
execute, and nobody reading the summary counts the skips.

So the skip stays where it belongs — in the test, for the developer — and the
*gate* refuses to start. This script is the gate.

Deliberately a script rather than a fixture: a fixture cannot fail the run before
collection has already reported its skips.
"""

from __future__ import annotations

import socket
import sys

from ai_orchestrator.config.settings import get_settings


def _port_open(host: str, port: int, *, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    settings = get_settings()
    problems: list[str] = []

    host, _, nats_port = settings.nats_url.rpartition("://")[2].partition(":")
    if not _port_open(host or "127.0.0.1", int(nats_port or 4222)):
        problems.append(
            f"NATS is not reachable at {settings.nats_url} — the event-pipeline "
            f"scenarios would skip. Start it with `make dev-nats`."
        )

    temporal_host, _, temporal_port = settings.temporal_address.partition(":")
    if not _port_open(temporal_host or "127.0.0.1", int(temporal_port or 7233)):
        problems.append(
            f"Temporal is not reachable at {settings.temporal_address} — the "
            f"workflow scenarios would skip. Start it with `make dev-temporal`."
        )

    if not settings.async_database_url:
        problems.append("no database URL is configured; the suite cannot run at all.")

    if problems:
        print("preflight failed:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("preflight ok: postgres, nats, temporal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
