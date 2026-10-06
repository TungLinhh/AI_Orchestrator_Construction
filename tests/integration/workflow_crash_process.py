"""Subprocess fixture: checkpoint a real controller, then wait to be killed.

Used by test_workflow_recovery.py. Refuses the development database and uses
only fake content; no provider, broker, mail or external write is contacted.
"""

import argparse
import asyncio
import json
from pathlib import Path

from ai_orchestrator.application.business_workflow import BusinessWorkflowService
from ai_orchestrator.config.settings import Environment, get_settings
from ai_orchestrator.models.gateway import ModelResponse
from ai_orchestrator.persistence.session import Database
from tests.integration.test_business_workflow import FixtureRuntime


async def main(org: str, root: str, ready: Path) -> None:
    settings = get_settings()
    if settings.environment != Environment.TEST or settings.postgres_db != "ai_orchestrator_test":
        raise RuntimeError("Crash fixture is restricted to ai_orchestrator_test")

    class PausedRuntime(FixtureRuntime):
        async def execute(self, task, context, *, record_usage=None, **kwargs):
            if self.stage.key == "rfq":
                await record_usage(ModelResponse(provider="unit-fake", model_used="crash-fixture"))
                await asyncio.to_thread(
                    ready.write_text,
                    json.dumps(
                        {"task_id": str(task.task_id), "execution_id": str(task.execution_id)}
                    ),
                )
                await asyncio.Event().wait()
            return await super().execute(task, context, record_usage=record_usage, **kwargs)

    db = Database.from_settings(settings)
    try:
        await BusinessWorkflowService(db, org, runtime_factory=PausedRuntime).run(root)
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("org")
    parser.add_argument("root")
    parser.add_argument("ready", type=Path)
    args = parser.parse_args()
    asyncio.run(main(args.org, args.root, args.ready))
