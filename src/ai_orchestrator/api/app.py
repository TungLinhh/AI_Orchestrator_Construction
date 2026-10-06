"""FastAPI application factory.

One place that assembles the app, so wiring is inspectable rather than spread
across module imports. The lifespan is where the platform proves it is safe to
serve traffic before it accepts any: the database must be reachable, the
connection must be the least-privilege role, and the configuration must be valid.

A process that starts and then fails on the first business transaction has
already accepted a request it cannot serve. Refusing to start is better.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ai_orchestrator.config.settings import Settings, get_settings
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import configure_logging, get_logger
from ai_orchestrator.telemetry.setup import configure_telemetry

logger = get_logger(__name__)

API_TITLE = "AI Orchestrator Control Plane"
API_VERSION = "1.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    settings.validate_for_startup()
    configure_logging(settings)
    telemetry = configure_telemetry(settings)
    app.state.telemetry = telemetry

    database = Database.from_settings(settings)
    app.state.db = database

    if not await database.healthcheck():
        await database.dispose()
        msg = "database is not reachable; refusing to start"
        raise RuntimeError(msg)

    # The check that matters most and is easiest to skip: if the application
    # connects as the owner, every RLS policy is inert and the platform leaks
    # across tenants while looking correctly configured.
    try:
        await database.assert_app_role_is_least_privilege()
    except PermissionError:
        await database.dispose()
        raise

    rls = await database.rls_status()
    if rls["rls_enabled"] < rls["tables"] - 3:  # organizations, consumer_offsets, alembic_version
        logger.warning(
            "database.rls_incomplete",
            enabled=rls["rls_enabled"],
            tables=rls["tables"],
            unprotected=rls["unprotected"],
        )

    logger.info("api.started", **settings.redacted_summary(), rls_protected=rls["rls_enabled"])

    # The browser stream's feed. Started here rather than on the first connection
    # because a pump that starts per connection is a pump per tab, and the fan-out
    # already exists to make one reader serve many.
    from ai_orchestrator.api.stream_pump import start_stream_pump, stop_stream_pump

    start_stream_pump(app, database)
    from ai_orchestrator.api.business_workflows import start_mail_monitor

    start_mail_monitor(database)
    try:
        yield
    finally:
        # Subscribers before the database: a browser tab should learn the platform is
        # going away while the database can still answer, not after it has gone.
        await stop_stream_pump(app)
        # In-flight local runs next, and **awaited rather than cancelled**. A run cancelled
        # halfway leaves a task in `assigned` with an execution that never finished and no
        # worker to pick it up -- the exact state `local_runner` exists to escape, created by
        # the shutdown path. Shutdown is where a few more seconds is available.
        from ai_orchestrator.application.local_runner import shutdown as stop_local_runs

        await stop_local_runs()
        from ai_orchestrator.api.business_workflows import stop_business_runs

        await stop_business_runs()
        await database.dispose()
        if telemetry is not None:
            telemetry.shutdown()
        logger.info("api.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=lifespan,
        # Schema is served: a client generated from it is the only way to keep
        # the API contract honest.
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    _install_middleware(app, settings)
    _install_error_handlers(app)
    _install_routes(app)
    return app


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    from fastapi.middleware.cors import CORSMiddleware

    from ai_orchestrator.security.rate_limit import RateLimitMiddleware

    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        # Explicit origins, never '*'. A wildcard with credentials is refused by
        # browsers anyway, so allowing it just signals that CORS was not thought
        # about.
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["authorization", "content-type", "x-organization-id", "idempotency-key"],
    )


def _install_error_handlers(app: FastAPI) -> None:
    """One error shape for the whole API.

    Every handler emits the platform's normalised error envelope, so a client
    parses one thing. Crucially, an unexpected exception is a 500 with a generic
    message and the detail is logged, not returned: a stack trace in an API
    response is a disclosure of the implementation.
    """
    from ai_orchestrator.domain.errors import PlatformError

    @app.exception_handler(PlatformError)
    async def _platform_error(_request: Any, exc: PlatformError) -> JSONResponse:
        status = exc.http_status
        body: dict[str, Any] = {
            "error": {
                "kind": exc.kind.value,
                "category": exc.category.value,
                "message": exc.message,
                "details": exc.details,
                "retryable": exc.is_retryable,
            }
        }
        if exc.resource_type:
            body["error"]["resource"] = {
                "type": exc.resource_type,
                "id": exc.resource_id,
            }
        if status >= 500:
            logger.error("api.error", kind=exc.kind.value, message=exc.message)
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(ValueError)
    async def _value_error(_request: Any, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "kind": "VALIDATION",
                    "category": "validation_error",
                    "message": str(exc),
                    "details": {},
                    "retryable": False,
                }
            },
        )


def _install_routes(app: FastAPI) -> None:
    from ai_orchestrator.api.agents import router as agents_router
    from ai_orchestrator.api.agents_control import router as agents_control_router
    from ai_orchestrator.api.approvals import router as approvals_router
    from ai_orchestrator.api.business_workflows import router as business_workflows_router
    from ai_orchestrator.api.console import router as console_router
    from ai_orchestrator.api.construction import router as construction_router
    from ai_orchestrator.api.documents import router as documents_router
    from ai_orchestrator.api.events_audit import router as events_audit_router
    from ai_orchestrator.api.health import router as health_router
    from ai_orchestrator.api.organizations import router as organizations_router
    from ai_orchestrator.api.runtime import router as runtime_router
    from ai_orchestrator.api.scenarios import router as scenarios_router
    from ai_orchestrator.api.skills_tools import router as skills_tools_router
    from ai_orchestrator.api.stream import router as stream_router
    from ai_orchestrator.api.tasks import router as tasks_router

    # Health lives at the root, not under the versioned prefix. A probe does not
    # know or care which API version is deployed, and `/api/v1/health` returning
    # 404 while the service is perfectly healthy is a false alarm that gets
    # somebody paged.
    app.include_router(health_router)

    # Order is load-bearing, not alphabetical. Starlette matches in registration
    # order, so a router that declares `/approvals/inbox` **after** `approvals_router`
    # has already registered `/approvals/{approval_id}` would have its own endpoint
    # captured by the parameterised one and answer 404 forever.
    #
    # `construction_router` therefore goes before `approvals_router`. The same rule
    # applies *within* a router, and is why `/approvals/stats` and `/events/stats` are
    # declared above their `{id}` siblings rather than below them — see F123.
    for router in (
        console_router,
        business_workflows_router,
        organizations_router,
        agents_router,
        tasks_router,
        skills_tools_router,
        construction_router,
        documents_router,
        agents_control_router,
        approvals_router,
        events_audit_router,
        runtime_router,
        scenarios_router,
        stream_router,
    ):
        app.include_router(router, prefix="/api/v1")


app = create_app()
