"""A test-only task runner killed after its independently visible checkpoint."""

import argparse
import asyncio
import json
from pathlib import Path

from ai_orchestrator.application.task_attempt import TaskAttemptRunner
from ai_orchestrator.config.settings import Environment, get_settings
from ai_orchestrator.persistence.session import Database
from tests.integration.test_task_attempt import PausedRuntime


async def main(org: str, task_id: str, ready: Path) -> None:
    settings = get_settings()
    if settings.environment != Environment.TEST or settings.postgres_db != "ai_orchestrator_test":
        raise RuntimeError("Crash fixture is restricted to ai_orchestrator_test")

    class CrashRuntime(PausedRuntime):
        async def execute(self, task, context, *, record_usage=None, **kwargs):
            work = asyncio.create_task(
                super().execute(
                    task,
                    context,
                    record_usage=record_usage,
                    **kwargs,
                )
            )
            await self.started.wait()
            await asyncio.to_thread(
                ready.write_text,
                json.dumps(
                    {
                        "execution_id": str(task.execution_id),
                        "task_id": str(task.task_id),
                    }
                ),
            )
            return await work

    db = Database.from_settings(settings)
    try:
        await TaskAttemptRunner(db, org, runtime=CrashRuntime()).execute_task(task_id)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("org")
    parser.add_argument("task_id")
    parser.add_argument("ready", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.org, args.task_id, args.ready))
