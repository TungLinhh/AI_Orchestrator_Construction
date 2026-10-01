"""Worker composition root.

The place where the runtime adapter is chosen, the database is built and the
Temporal worker is started. Kept separate from the workflow module so that
importing workflow code — which Temporal's sandbox restricts — does not pull in a
database engine, an HTTP client and every model provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_orchestrator.config.settings import Settings, get_settings
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class RuntimeComponents:
    """Everything a worker needs, built once."""

    database: Database
    runtime: Any
    settings: Settings

    async def close(self) -> None:
        await self.database.dispose()


def build_runtime(settings: Settings | None = None, *, database: Database | None = None) -> Any:
    """Choose the agent runtime adapter.

    The name comes from configuration, not from a hard-coded class, so an operator
    can move an organization from one framework to another by changing a setting
    rather than by deploying different code. `custom:` is the escape hatch for an
    adapter that is not in this table.
    """
    settings = settings or get_settings()
    name = settings.model_provider_default

    from ai_orchestrator.agent_runtime import NullRuntime, PydanticAIRuntime, ScriptedRuntime

    if name in {"fake", "scripted", "deterministic"}:
        return ScriptedRuntime()
    if name in {"pydantic_ai", "pydanticai", "default"}:
        return PydanticAIRuntime()
    if name in {"null", "none"}:
        return NullRuntime()
    # Anything else names a **model provider**, not a runtime.
    #
    # The table above answers "how does an agent run", and a provider name answers
    # "which model does it call". `openrouter` was not in the table, so it fell through
    # to `NullRuntime` and every task failed with
    #
    #     no runtime adapter is available for Executive Agent
    #
    # which is a confident, specific, and wrong message: the adapter was fine, the
    # agent had a real OpenRouter profile, and the factory had decided otherwise because
    # it was being asked the wrong question. `AIMODEL_PROVIDER_DEFAULT=openrouter` is
    # how you say "use a real model", and it silently gave you an agent that could not
    # run at all.
    #
    # So a real provider now selects the real runtime. The name is still configuration
    # and not a hard-coded class, and `custom:` remains the escape hatch.
    return PydanticAIRuntime()


def build_execution_service(
    organization_id: str,
    *,
    run_mode: str = "live",
    database: Database | None = None,
    settings: Settings | None = None,
) -> Any:
    """Build a `TaskExecutionService` bound to one tenant.

    Used by workflow activities, which receive a session factory rather than a
    session, because an activity must not hold a database transaction open across
    a retry.
    """
    from ai_orchestrator.domain.enums import RunMode

    settings = settings or get_settings()
    db = database or _shared_database()
    runtime = build_runtime(settings)

    # The service needs a session; the caller supplies one through the context
    # variable set below. Kept as a function so both the worker and the API build
    # the same object.
    return _ServiceFactory(
        db=db,
        runtime=runtime,
        organization_id=organization_id,
        run_mode=RunMode(run_mode) if isinstance(run_mode, str) else run_mode,
        settings=settings,
    )


@dataclass(slots=True)
class _ServiceFactory:
    """Builds a service with a fresh session per call.

    A workflow activity can be retried, and a reused session would carry a
    transaction that was already aborted. One session per call is the only
    correct shape here.
    """

    db: Database
    runtime: Any
    organization_id: str
    run_mode: Any
    settings: Settings

    def __call__(self) -> Any:
        from ai_orchestrator.application.task_execution import TaskExecutionService

        return TaskExecutionService(
            self.db.session_factory(),
            self.organization_id,
            runtime=self.runtime,
            run_mode=self.run_mode,
        )

    # The workflow activity calls `.execute_task` directly; forward it through a
    # real session rather than making the workflow know about sessions.
    async def execute_task(self, *args: Any, **kwargs: Any) -> Any:
        async with self.db.tenant_session(self.organization_id) as session:
            from ai_orchestrator.application.task_execution import TaskExecutionService

            service = TaskExecutionService(
                session,
                self.organization_id,
                runtime=self.runtime,
                run_mode=self.run_mode,
            )
            return await service.execute_task(*args, **kwargs)


_shared: Database | None = None


def _shared_database() -> Database:
    global _shared
    if _shared is None:
        _shared = Database.from_settings()
    return _shared


async def run_worker(settings: Settings | None = None) -> None:
    """Start the Temporal worker. Blocks until cancelled."""
    from temporalio import activity
    from temporalio.client import Client
    from temporalio.worker import Worker

    from ai_orchestrator.workflows.task_workflow import (
        TASK_QUEUE,
        DelegationWorkflow,
        TaskExecutionWorkflow,
        TaskWorkflowInput,
    )

    settings = settings or get_settings()
    if not settings.temporal_enabled:
        logger.info("worker.temporal_disabled")
        return

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    db = Database.from_settings()
    runtime = build_runtime(settings)

    @activity.defn(name="execute_task")
    async def _execute_task_activity(data: Any) -> Any:
        """Rebuild the service per activity invocation.

        A fresh session per call: an activity is retried, and a session whose
        transaction was already rolled back cannot be reused.

        **The payload is validated here rather than by annotating the parameter**, and
        that is deliberate. Temporal's converter reads the annotation to decide what to
        deserialise into, so `data: TaskWorkflowInput` would fix the symptom — but the
        type must be resolvable in the function's *module* globals, and importing it at
        module level is exactly what this module's docstring forbids. A name imported
        inside `run_worker` is a local, so the annotation raises `NameError` at
        decoration time instead.

        Validating at the boundary is the better answer anyway. The activity is where
        data crosses from the wire, and a boundary that trusts the shape it was handed
        is a boundary that breaks the first time the payload is not what was expected.
        The `Any` version did exactly that: `AttributeError: 'dict' object has no
        attribute 'organization_id'`, five times, once per retry — fast enough each
        time to read as a crash rather than as a contract mismatch.
        """
        from ai_orchestrator.application.task_execution import TaskExecutionService
        from ai_orchestrator.domain.enums import RunMode

        if isinstance(data, dict):
            data = TaskWorkflowInput.model_validate(data)

        async with db.tenant_session(data.organization_id) as session:
            service = TaskExecutionService(
                session,
                data.organization_id,
                runtime=runtime,
                run_mode=RunMode(data.run_mode),
            )
            outcome = await service.execute_task(
                data.task_id,
                agent_id=data.agent_id,
                attempt=data.attempt,
                input_override=None,
                retrieved_context=data.retrieved_context,
            )
        from ai_orchestrator.workflows.task_workflow import ExecutionResult

        return ExecutionResult(
            task_id=outcome.task_id,
            status=outcome.status.value,
            summary=outcome.summary,
            execution_id=outcome.execution_id,
            agent_id=outcome.agent_id,
            needs_approval=outcome.needs_approval,
            blocked_reason=outcome.blocked_reason,
            failure_category=outcome.failure_category,
            cost_usd=str(outcome.cost_usd),
            tokens=outcome.tokens,
            duration_ms=outcome.duration_ms,
            proposed_actions=[a.model_dump(mode="json") for a in outcome.proposed_actions],
        )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[TaskExecutionWorkflow, DelegationWorkflow],
        activities=[_execute_task_activity],
    )
    logger.info(
        "worker.started",
        address=settings.temporal_address,
        queue=TASK_QUEUE,
        build_id=settings.temporal_build_id,
    )
    try:
        await worker.run()
    finally:
        await db.dispose()


__all__ = [
    "RuntimeComponents",
    "build_execution_service",
    "build_runtime",
    "run_worker",
]
