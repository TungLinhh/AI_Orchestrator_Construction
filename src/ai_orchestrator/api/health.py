"""Health, readiness and metrics.

`/health` is liveness: the process is up. It must not touch a dependency, or a
database blip restarts every pod.

`/ready` is readiness: this instance can serve traffic. It checks the dependency
that actually matters — the database connection is reachable *and* is running as
the least-privilege role, because an instance connected as the owner would serve
traffic with tenant isolation switched off.

`/metrics` exposes Prometheus text. The metric names are declared next to the
counters so a dashboard and the code that feeds it cannot drift on a typo.
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Request, Response

from ai_orchestrator.telemetry.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)

_STARTED_AT = time.monotonic()

#: Process-level counters. In-memory by design: they are reset on restart, which
#: is what a Prometheus counter expects, and a durable version would need a
#: scrape-time write on the hot path.
# `float`, not `int`: `model_cost_usd_sum` is a USD amount. Typing this as
# `int` to make the literal fit needed a `type: ignore` on the float writer
# below, which hid the same mismatch a third time.
COUNTERS: dict[str, float] = {
    "tasks_created_total": 0,
    "tasks_completed_total": 0,
    "tasks_failed_total": 0,
    "delegations_created_total": 0,
    "delegations_blocked_total": 0,
    "subagents_spawned_total": 0,
    "tool_invocations_total": 0,
    "tool_denials_total": 0,
    "approvals_requested_total": 0,
    "approvals_decided_total": 0,
    "agent_executions_total": 0,
    "agent_execution_latency_ms_sum": 0,
    "model_tokens_total": 0,
    # A float among the integers: the cost sum is a float, not an int, and
    # annotating the dict as `dict[str, int]` to make it fit would be a lie
    # the type system is there to prevent.
    "model_cost_usd_sum": 0.0,
    "retries_total": 0,
    "cycle_blocks_total": 0,
    "duplicate_task_blocks_total": 0,
}


def bump(name: str, amount: float = 1) -> None:
    """Increment a counter. Unknown names are created, so a new metric does not
    need a coordinated change here and in the exporter."""
    COUNTERS[name] = COUNTERS.get(name, 0.0) + amount


def add(name: str, amount: float) -> None:
    COUNTERS[name] = COUNTERS.get(name, 0.0) + amount


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Deliberately dependency-free."""
    return {"status": "ok", "uptime_s": round(time.monotonic() - _STARTED_AT, 1)}


@router.get("/ready")
async def ready(request: Request) -> Response:
    """Readiness. Checks the things that make serving unsafe."""
    checks: dict[str, Any] = {}

    db = getattr(request.app.state, "db", None)
    if db is None:
        return Response(
            content='{"status":"not_ready","reason":"database not initialised"}',
            status_code=503,
            media_type="application/json",
        )

    reachable = await db.healthcheck()
    checks["database_reachable"] = reachable
    if reachable:
        try:
            await db.assert_app_role_is_least_privilege()
            checks["least_privilege_role"] = True
        except PermissionError as exc:
            # Not ready, and loud about why. An instance in this state is a
            # cross-tenant breach waiting to happen.
            checks["least_privilege_role"] = False
            checks["error"] = str(exc)
        checks["row_level_security"] = await db.rls_status()

    ready_now = reachable and checks.get("least_privilege_role", False)
    return Response(
        content=json.dumps(
            {"status": "ready" if ready_now else "not_ready", "checks": checks},
            default=str,
        ),
        status_code=200 if ready_now else 503,
        media_type="application/json",
    )


@router.get("/metrics")
async def metrics() -> Response:
    """Prometheus text exposition."""
    lines: list[str] = [
        "# HELP ao_uptime_seconds Process uptime.",
        "# TYPE ao_uptime_seconds gauge",
        f"ao_uptime_seconds {time.monotonic() - _STARTED_AT:.3f}",
    ]
    for name, value in sorted(COUNTERS.items()):
        metric = f"ao_{name}"
        kind = "gauge" if name.endswith(("_ms_sum", "_usd_sum")) else "counter"
        lines.append(f"# HELP {metric} {name}")
        lines.append(f"# TYPE {metric} {kind}")
        lines.append(f"{metric} {value}")
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/system/secrets")
async def secret_status() -> dict[str, Any]:
    """Which credentials are configured. Never a value.

    Exposed unauthenticated on purpose: an operator needs to see at a glance that
    a credential is missing, and presence is not sensitive. It is deliberately
    limited to configured/missing — no provider probes, no error detail, nothing
    that would help someone probing the endpoint learn which keys are valid.
    """
    from ai_orchestrator.security.secrets import KNOWN_SECRET_NAMES, EnvFileSecretProvider

    provider = EnvFileSecretProvider()
    described = await provider.describe()
    return {name: described.get(name, "missing") for name in KNOWN_SECRET_NAMES}


__all__ = ["COUNTERS", "add", "bump", "router"]
