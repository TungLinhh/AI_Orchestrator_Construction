import asyncio
from unittest.mock import MagicMock

from ai_orchestrator.application.workflow_drivers import WorkflowDrivers
from ai_orchestrator.domain.workflow_lifecycle import WorkflowKind
from ai_orchestrator.persistence.session import Database


async def test_committed_gate_wakeup_is_not_lost_while_driver_is_still_exiting():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(org, root, kind):
        calls.append(root)
        if len(calls) == 1:
            entered.set()
            await release.wait()

    drivers = WorkflowDrivers(MagicMock(spec=Database), handler=handler)
    assert drivers.start("org", "root", WorkflowKind.AGENT)
    await asyncio.wait_for(entered.wait(), timeout=1)
    # Simulate the approval request arriving after its decision commits.
    assert not drivers.start("org", "root", WorkflowKind.AGENT)
    assert not drivers.start("org", "root", WorkflowKind.AGENT)
    release.set()
    await asyncio.wait_for(drivers.wait("org", "root"), timeout=1)
    assert calls == ["root", "root"]
    assert not drivers.is_running("org", "root")
    await drivers.shutdown()


async def test_concurrency_limit_and_shutdown_do_not_start_queued_work():
    entered = asyncio.Event()
    calls = []

    async def handler(org, root, kind):
        calls.append(root)
        entered.set()
        await asyncio.Event().wait()

    drivers = WorkflowDrivers(MagicMock(spec=Database), handler=handler, concurrency=1)
    assert drivers.start("org", "first", WorkflowKind.BUSINESS)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert drivers.start("org", "queued", WorkflowKind.BUSINESS)
    await asyncio.wait_for(drivers.shutdown(), timeout=1)
    assert calls == ["first"]
    assert not drivers.is_running("org", "queued")
    assert not drivers.start("org", "late", WorkflowKind.BUSINESS)


async def test_applications_do_not_share_driver_registries():
    entered = asyncio.Event()

    async def handler(org, root, kind):
        entered.set()
        await asyncio.Event().wait()

    first = WorkflowDrivers(MagicMock(spec=Database), handler=handler)
    second = WorkflowDrivers(MagicMock(spec=Database), handler=handler)
    assert first.start("org", "root", WorkflowKind.AGENT)
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert not second.is_running("org", "root")
    await second.shutdown()
    assert first.is_running("org", "root")
    await first.shutdown()
