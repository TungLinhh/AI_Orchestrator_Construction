"""Telemetry bootstrap.

One place that knows how to configure tracing and metrics, so a service name or
a sampling rate is never decided twice. The exporter is OTLP over HTTP, which
works unchanged against a local collector, Jaeger, Tempo or any managed vendor —
the platform is not coupled to an observability product.

Two decisions worth stating:

  * Sampling is parent-based. A workflow that starts a task and then runs a
    model call produces one trace, and the trace is kept or dropped as a whole.
    Sampling individual spans would leave orphaned fragments that look like
    separate incidents.
  * Secrets are never attributes. A span attribute ends up in a log index and in
    a vendor's storage; the exporter scrubs anything that looks like a
    credential before it leaves the process.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ai_orchestrator.config.settings import Settings
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Attribute names that must never be exported. Matched case-insensitively
#: against the full attribute key.
_FORBIDDEN_ATTRIBUTE_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|authorization|auth[_-]?header|"
    r"credential|private[_-]?key|session[_-]?id|cookie|set-cookie)"
)

#: The same names as span attribute keys, redacted by value inspection too.
_FORBIDDEN_VALUE_PATTERN = re.compile(r"(?i)^(sk-|sk-or-|Bearer\s|ghp_|gho_|xox[baprs]-)")


def scrub_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop anything that looks like a credential.

    Applied at the exporter boundary rather than at each call site, because the
    failure mode is a developer adding one attribute and forgetting. The cost is
    that a legitimately named attribute containing the word "token" is dropped
    too; that is the correct trade for a value that must never leave the process.
    """
    return {
        key: value
        for key, value in attributes.items()
        if not _FORBIDDEN_ATTRIBUTE_PATTERN.search(key)
        and not (isinstance(value, str) and _FORBIDDEN_VALUE_PATTERN.match(value.strip()))
    }


@dataclass(frozen=True, slots=True)
class TelemetryHandles:
    tracer_provider: Any
    meter_provider: Any
    shutdown: Any


def configure_telemetry(settings: Settings) -> TelemetryHandles | None:
    """Initialise tracing and metrics. Returns None when disabled or unavailable.

    Returns `None` rather than a no-op object so a caller cannot accidentally
    treat an unconfigured pipeline as a working one.

    Missing telemetry dependencies degrade to a warning rather than failing
    startup. That is a deliberate difference from the database checks in the app
    lifespan: an unreachable database means requests cannot be served correctly,
    while a missing exporter means requests can be served and are merely harder
    to debug. Failing the process for the second would make observability a
    prerequisite for availability.
    """
    if not settings.otel_enabled:
        return None

    try:
        return _configure_telemetry(settings)
    except ImportError as exc:
        logger.warning(
            "otel.dependencies_missing",
            error=str(exc),
            hint="install the optional extra: uv sync --extra telemetry",
        )
        return None
    except Exception as exc:
        # An unreachable collector is also not a reason to refuse to start.
        logger.warning("otel.configuration_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def _configure_telemetry(settings: Settings) -> TelemetryHandles:
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.otel_service_version,
            "deployment.environment": settings.environment.value,
        }
    )

    # ParentBased(ALWAYS_ON) for local and test: a partial trace is worse than a
    # large one when you are debugging. Production uses a ratio so cost stays
    # bounded, but still parent-based so a sampled-out trace is not left with
    # children that reference a parent nobody has.
    sampler = (
        ParentBased(ALWAYS_ON)
        if not settings.is_production
        else ParentBased(TraceIdRatioBased(0.1))
    )

    tracer_provider = TracerProvider(resource=resource, sampler=sampler)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
            )
        )
    )
    trace.set_tracer_provider(tracer_provider)

    meter_provider: Any = None
    if settings.metrics_enabled:
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/metrics"
                    ),
                    export_interval_millis=15_000,
                )
            ],
        )
        metrics.set_meter_provider(meter_provider)

    def shutdown() -> None:
        tracer_provider.shutdown()
        if meter_provider is not None:
            meter_provider.shutdown()

    return TelemetryHandles(
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        shutdown=shutdown,
    )


# -------------------------------------------------------- domain constants --
# Metric and span names are declared once so a dashboard and the code that
# feeds it cannot drift apart on a typo.
ORGANIZATION_ID = "ai_orchestrator.organization_id"
TASK_ID = "ai_orchestrator.task_id"
TASK_STATUS = "ai_orchestrator.task_status"
AGENT_ID = "ai_orchestrator.agent_id"
AGENT_VERSION = "ai_orchestrator.agent_version"
SKILL_ID = "ai_orchestrator.skill_id"
TOOL_ID = "ai_orchestrator.tool_id"
MODEL = "ai_orchestrator.model"
EXECUTION_ID = "ai_orchestrator.execution_id"
WORKFLOW_ID = "ai_orchestrator.workflow_id"
DELEGATION_DEPTH = "ai_orchestrator.delegation_depth"
SPAWN_COUNT = "ai_orchestrator.spawn_count"
ERROR_CATEGORY = "ai_orchestrator.error_category"
ERROR_KIND = "ai_orchestrator.error_kind"
APPROVAL_ID = "ai_orchestrator.approval_id"
POLICY_DECISION = "ai_orchestrator.policy_decision"
POLICY_RULE = "ai_orchestrator.policy_rule"
ROUTING_REASON = "ai_orchestrator.routing_reason"

#: Exception types worth a dedicated span, as opposed to noise.
SPAN_TASK_EXECUTE = "task.execute"
SPAN_AGENT_EXECUTE = "agent.execute"
SPAN_MODEL_CALL = "model.call"
SPAN_TOOL_INVOKE = "tool.invoke"
SPAN_A2A_CALL = "a2a.call"
SPAN_DELEGATE = "delegation.authorize"
SPAN_POLICY_EVALUATE = "policy.evaluate"
SPAN_MEMORY_SEARCH = "memory.search"
SPAN_OUTBOX_PUBLISH = "outbox.publish"


__all__ = [
    "AGENT_ID",
    "AGENT_VERSION",
    "APPROVAL_ID",
    "DELEGATION_DEPTH",
    "ERROR_CATEGORY",
    "ERROR_KIND",
    "EXECUTION_ID",
    "MODEL",
    "ORGANIZATION_ID",
    "POLICY_DECISION",
    "POLICY_RULE",
    "ROUTING_REASON",
    "SKILL_ID",
    "SPAN_A2A_CALL",
    "SPAN_AGENT_EXECUTE",
    "SPAN_DELEGATE",
    "SPAN_MEMORY_SEARCH",
    "SPAN_MODEL_CALL",
    "SPAN_OUTBOX_PUBLISH",
    "SPAN_POLICY_EVALUATE",
    "SPAN_TASK_EXECUTE",
    "SPAN_TOOL_INVOKE",
    "SPAWN_COUNT",
    "TASK_ID",
    "TASK_STATUS",
    "TOOL_ID",
    "WORKFLOW_ID",
    "TelemetryHandles",
    "configure_telemetry",
    "scrub_attributes",
]
