"""Worker entry point. `make dev-worker` runs this."""

from __future__ import annotations

import asyncio

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.telemetry.logging import configure_logging, get_logger
from ai_orchestrator.telemetry.setup import configure_telemetry
from ai_orchestrator.worker_runtime import run_worker

logger = get_logger(__name__)


async def _run() -> None:
    settings = get_settings()
    settings.validate_for_startup()
    configure_logging(settings)
    telemetry = configure_telemetry(settings)
    logger.info("worker.starting", **settings.redacted_summary())
    try:
        await run_worker(settings)
    finally:
        if telemetry is not None:
            telemetry.shutdown()


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
