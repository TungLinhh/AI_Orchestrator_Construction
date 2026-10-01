"""Temporal client wrapper.

Kept separate from the workflow module so the API can talk to Temporal without
importing workflow code — which Temporal's sandbox restricts, and which would
otherwise pull `pydantic` and the activity machinery into every request path.

Every call is a no-op that reports success when Temporal is disabled. That is a
deliberate choice: a deployment with no workflow engine still creates and tracks
tasks; the work simply does not run until someone starts the worker. Failing
task creation because the orchestrator is down would be a worse product than
degrading the execution path.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import Any

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

_client: Any = None


async def get_client() -> Any:
    """The shared Temporal client, connected on first use."""
    global _client
    if _client is not None:
        return _client
    from temporalio.client import Client

    settings = get_settings()
    if not settings.temporal_enabled:
        return None
    try:
        _client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
    except Exception as exc:
        # A missing workflow engine degrades execution, it does not break the
        # control plane. The task row and its event are already committed.
        logger.warning(
            "temporal.unavailable", address=settings.temporal_address, error=type(exc).__name__
        )
        return None
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):
            await _client.close()
    _client = None


@dataclass(slots=True)
class StartResult:
    started: bool
    workflow_id: str | None
    run_id: str | None
    reason: str = ""


async def start_task_workflow(data: dict[str, Any]) -> StartResult:
    """Start a durable workflow for a task."""
    from ai_orchestrator.workflows.task_workflow import (
        TASK_QUEUE,
        WORKFLOW_TASK_EXECUTION,
        TaskWorkflowInput,
    )

    client = await get_client()
    if client is None:
        return StartResult(False, None, None, "temporal is disabled or unreachable")

    task_id = data["task_id"]
    workflow_id = f"task:{task_id}"
    try:
        handle = await client.start_workflow(
            WORKFLOW_TASK_EXECUTION,
            TaskWorkflowInput.model_validate(data),
            id=workflow_id,
            task_queue=TASK_QUEUE,
        )
        return StartResult(True, handle.id, handle.run_id)
    except Exception as exc:
        # An already-running workflow is not an error: the client retried a
        # creation that had already succeeded.
        if "already started" in str(exc).lower() or "WorkflowExecutionAlreadyStarted" in str(exc):
            logger.info("temporal.already_started", workflow_id=workflow_id)
            return StartResult(True, workflow_id, None, "already running")
        logger.warning("temporal.start_failed", workflow_id=workflow_id, error=str(exc)[:200])
        return StartResult(False, None, None, str(exc)[:200])


async def cancel_workflow(workflow_id: str, *, reason: str = "") -> bool:
    client = await get_client()
    if client is None:
        return False
    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.cancel(reason=reason or None)
        return True
    except Exception as exc:
        logger.warning("temporal.cancel_failed", workflow_id=workflow_id, error=str(exc)[:200])
        return False


async def signal_approval_decision(
    workflow_id: str, *, approval_id: str, approved: bool, decided_by: str, note: str = ""
) -> bool:
    """Resume a workflow that is waiting on a human.

    This is the other half of the approval round trip: the approval service
    records the decision, and this delivers it. Both are required — recording a
    decision without signalling leaves the workflow waiting forever.
    """
    from ai_orchestrator.workflows.task_workflow import ApprovalSignal

    client = await get_client()
    if client is None:
        return False
    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(
            "approval_decision",
            ApprovalSignal(
                approval_id=approval_id, approved=approved, decided_by=decided_by, note=note
            ),
        )
        return True
    except Exception as exc:
        logger.warning(
            "temporal.signal_failed",
            workflow_id=workflow_id,
            approval_id=approval_id,
            error=str(exc)[:200],
        )
        return False


async def describe_workflow(workflow_id: str) -> dict[str, Any] | None:
    client = await get_client()
    if client is None:
        return None
    try:
        handle = client.get_workflow_handle(workflow_id)
        described = await handle.describe()
        return {
            "workflow_id": described.id,
            "run_id": described.run_id,
            "status": described.status.name if described.status else None,
            "start_time": described.start_time.isoformat() if described.start_time else None,
        }
    except Exception as exc:
        logger.debug("temporal.describe_failed", workflow_id=workflow_id, error=str(exc)[:120])
        return None


async def query_workflow_state(workflow_id: str) -> dict[str, Any] | None:
    """Ask a running workflow what it is waiting for."""
    client = await get_client()
    if client is None:
        return None
    try:
        handle = client.get_workflow_handle(workflow_id)
        state: dict[str, Any] | None = await handle.query("state")
        return state
    except Exception as exc:
        logger.debug("temporal.query_failed", workflow_id=workflow_id, error=str(exc)[:120])
        return None


__all__ = [
    "StartResult",
    "cancel_workflow",
    "close_client",
    "describe_workflow",
    "get_client",
    "query_workflow_state",
    "signal_approval_decision",
    "start_task_workflow",
]
