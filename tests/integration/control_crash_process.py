"""Fault-injection child. Refuses non-test databases; no live mail or model calls."""

import asyncio
import json
import sys
import time
from pathlib import Path

from ai_orchestrator.application.connector_actions import ConnectorActions
from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.session import Database


async def main():
    mode, org, root, stage, ready, effect = sys.argv[1:]
    if get_settings().postgres_db != "ai_orchestrator_test":
        raise RuntimeError("Only the isolated test schema is permitted")
    db = Database.from_settings()
    try:
        if mode == "dispatch":

            async def handler(*_):
                await asyncio.to_thread(Path(ready).write_text, json.dumps({"root": root}))
                await asyncio.Event().wait()

            driver = WorkflowDrivers(db, handler=handler, durable=True)
            await driver.submit(org, root, WorkflowKind.BUSINESS)
            await driver.wait(org, root)
        else:

            class Mailbox:
                def send_tests(self, run, cvs):
                    if mode == "after_write":
                        Path(effect).write_text(cvs[0]["text"])
                    Path(ready).write_text(
                        json.dumps({"root": root, "external_write": mode == "after_write"})
                    )
                    time.sleep(300)
                    return {}

            await ConnectorActions(db, org).send_tests(
                root,
                stage,
                Mailbox(),
                [{"name": "Synthetic", "filename": "cv.txt", "text": "MEP source"}],
            )
    finally:
        await db.dispose()


asyncio.run(main())
