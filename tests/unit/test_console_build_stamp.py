"""Console metadata must not block concurrent health and API requests."""

import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest

from ai_orchestrator.api import stream


@pytest.mark.unit
def test_build_stamp_queries_alembic_once(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout="abc123 (head)\n")

    stream._build_stamp.cache_clear()
    monkeypatch.setattr(subprocess, "run", run)
    try:
        assert stream._build_stamp() == "schema abc123"
        assert stream._build_stamp() == "schema abc123"
        assert len(calls) == 1
    finally:
        stream._build_stamp.cache_clear()


@pytest.mark.unit
async def test_first_console_request_does_not_block_event_loop(monkeypatch):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_stamp():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=3), "the event loop could not release the worker"
        return "schema test"

    monkeypatch.setattr(stream, "_build_stamp", slow_stamp)
    request = asyncio.create_task(stream.ui(org=None))
    try:
        async with asyncio.timeout(2):
            await started.wait()
        assert not request.done()
        release.set()
        response = await request
        assert b"schema test" in response.body
    finally:
        release.set()
        if not request.done():
            await request
