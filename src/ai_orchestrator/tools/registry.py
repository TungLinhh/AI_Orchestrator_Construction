"""Tool registry and the invocation path.

A tool is a capability with a side effect, and the whole design is about the gap
between "an agent wants to do this" and "this happened". The agent proposes; the
platform decides; the platform executes; the platform records.

Order is not arbitrary. Authority, then policy, then rate limit, then budget,
then validation, then execution, then audit. A tool that skips to execution
because its inputs looked fine has skipped the only part that matters.

`ToolRegistry` is the seam an MCP-backed tool implements. The in-process tools
and the MCP tools are the same interface, so a runtime cannot tell them apart and
therefore cannot grow a special case for the dangerous one.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.contracts import ToolContract
from ai_orchestrator.domain.enums import (
    DataClassification,
    EffectClass,
    RunMode,
    ToolRisk,
    tool_risk_exceeds,
)
from ai_orchestrator.domain.errors import (
    ToolUnavailable,
    ValidationError,
)


@dataclass(slots=True)
class ToolResult:
    """The outcome of one tool call.

    `ok=False` with an `error_kind` rather than an exception for expected
    failures, so a tool that cannot find something is a normal result and only a
    platform fault is exceptional.

    Mutable rather than frozen because the gateway stamps `duration_ms` and
    tightens `idempotent` after a call returns — both are facts the handler
    cannot know. It is never mutated after the result is handed back, so a
    caller that holds a reference is not exposed to a later change.
    """

    ok: bool
    output: Any = None
    error_kind: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    # Whether it is safe to call this tool again. A non-idempotent tool that
    # partially succeeded must never be retried blindly.
    idempotent: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, kind: str, message: str, **kwargs: Any) -> ToolResult:
        return cls(ok=False, error_kind=kind, error_message=message, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "idempotent": self.idempotent,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class ToolDefinition:
    """Everything the gateway needs to gate and run a tool."""

    tool_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    # Required, and declared before every defaulted field: a dataclass cannot
    # have a non-default field after a default one, and a tool without a handler
    # is not a tool.
    handler: Callable[[dict[str, Any], ToolExecutionContext], Awaitable[ToolResult]]
    output_schema: dict[str, Any] | None = None
    risk: ToolRisk = ToolRisk.LOW_RISK_WRITE
    effect_class: EffectClass = EffectClass.MUTATE_INTERNAL
    is_idempotent: bool = True
    requires_approval: bool = False
    timeout_seconds: int = 30
    rate_limit_per_minute: int = 60
    data_classification: DataClassification = DataClassification.INTERNAL
    # Recorded for audit: 'in_process' or the mcp server id. Never the code path.
    served_by: str = "in_process"
    version: str = "1"
    # Payload logging is opt-in. A tool argument is the most likely place for
    # prompt-injected user data to end up in a log index.
    log_payload: bool = False
    required_permissions: tuple[str, ...] = ()

    def to_contract(self) -> ToolContract:
        return ToolContract(
            tool_id=self.tool_id,
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            risk=self.risk,
            effect_class=self.effect_class,
            requires_approval=self.requires_approval,
            timeout_s=self.timeout_seconds,
            data_classification=self.data_classification,
            mcp_server=self.served_by if self.served_by != "in_process" else None,
        )

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


@dataclass(slots=True)
class ToolExecutionContext:
    """What a tool handler is allowed to know.

    Deliberately not the agent and not the task. A tool receives an id, a run
    mode and a logger. Handing it the task payload would let any tool read
    anything the agent could see, which defeats per-tool data scoping.
    """

    organization_id: str
    task_id: str | None = None
    execution_id: str | None = None
    agent_id: str | None = None
    run_mode: RunMode = RunMode.LIVE
    timeout_seconds: int = 30
    attributes: dict[str, Any] = field(default_factory=dict)


class ToolRegistry:
    """Name -> definition. Default-deny: an unregistered name does not exist."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> ToolDefinition:
        if definition.name in self._tools:
            msg = f"tool already registered: {definition.name}"
            raise ValidationError(msg, details={"tool": definition.name})
        self._tools[definition.name] = definition
        return definition

    def get(self, name: str) -> ToolDefinition:
        definition = self._tools.get(name)
        if definition is None:
            # Enumerating the available tools in the error is a deliberate leak
            # of capability names to an agent, so only the count is disclosed.
            msg = f"unknown tool: {name}"
            raise ToolUnavailable(msg, details={"tool": name, "registered_count": len(self._tools)})
        return definition

    def all(self) -> list[ToolDefinition]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def contracts_for(
        self, names: list[str], *, max_risk: ToolRisk = ToolRisk.PRIVILEGED
    ) -> tuple[ToolContract, ...]:
        """The contracts an agent may see, filtered by a risk ceiling.

        A tool above the ceiling is omitted rather than refused at call time, so
        the agent never learns it exists. Surfacing it and then denying every
        attempt teaches the model to keep trying.
        """
        contracts: list[ToolContract] = []
        for name in names:
            try:
                definition = self.get(name)
            except ToolUnavailable:
                continue
            if tool_risk_exceeds(definition.risk, max_risk):
                # Omitted rather than refused at call time, so the agent never
                # learns a forbidden tool exists. Showing it and denying every
                # attempt teaches the model to keep trying.
                continue
            contracts.append(definition.to_contract())
        return tuple(contracts)

    def openai_schemas(self, names: list[str]) -> list[dict[str, Any]]:
        return [self.get(name).to_openai_schema() for name in names if name in self._tools]

    async def aclose(self) -> None:
        """Release any resources held by MCP-backed tools."""


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """Validate tool arguments against the declared input schema.

    A hand-rolled validator rather than a dependency: the subset of JSON Schema
    that tool inputs actually use is small, and a tool that accepts an
    unvalidated payload is a much larger problem than the missing dependency.

    Deliberately *not* lenient. An unknown property is an error, because a
    misspelled argument that is silently dropped produces a tool call that
    appears to succeed and did nothing.
    """
    properties = schema.get("properties") or {}
    required = set(schema.get("required") or ())
    additional = schema.get("additionalProperties", False)

    for key in required - set(arguments):
        msg = f"missing required argument: {key}"
        raise ValidationError(msg, details={"argument": key, "required": sorted(required)})

    for key, value in arguments.items():
        if key not in properties and not additional:
            msg = f"unknown argument: {key}"
            raise ValidationError(msg, details={"argument": key, "accepted": sorted(properties)})
        spec = properties.get(key) or {}
        _check_type(key, value, spec)

    if additional is False and len(arguments) > len(properties):
        extra = set(arguments) - set(properties)
        msg = f"unknown arguments: {sorted(extra)}"
        raise ValidationError(msg, details={"arguments": sorted(extra)})


def _check_type(name: str, value: Any, spec: dict[str, Any]) -> None:
    expected = spec.get("type")
    if expected is None:
        return
    types = expected if isinstance(expected, list) else [expected]
    ok = any(
        (t == "string" and isinstance(value, str))
        or (t == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (t == "number" and isinstance(value, int | float) and not isinstance(value, bool))
        or (t == "boolean" and isinstance(value, bool))
        or (t == "array" and isinstance(value, list))
        or (t == "object" and isinstance(value, dict))
        or (t == "null" and value is None)
        for t in types
    )
    if not ok:
        msg = f"argument {name!r} must be of type {expected}, got {type(value).__name__}"
        raise ValidationError(msg, details={"argument": name, "expected": expected})


def input_hash(arguments: dict[str, Any]) -> str:
    """Stable hash of a tool's arguments, for audit and dedup.

    Sorted keys, so a caller that reorders arguments produces the same hash and
    a retry is recognised as a retry.
    """
    return hashlib.sha256(
        json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


class RateLimiter:
    """Per-tool sliding window, in process memory.

    In-process is the honest choice at this scale: a distributed limiter needs a
    shared store, and adding one for a limit that protects a single provider key
    would be over-engineering. When the API is scaled horizontally the limit
    becomes per-instance, which is documented in `docs/SECURITY.md` as a known
    limit rather than pretended away.
    """

    def __init__(self) -> None:
        self._calls: dict[str, list[float]] = {}

    def check(self, key: str, limit_per_minute: int) -> None:
        now = time.monotonic()
        window = [t for t in self._calls.get(key, []) if now - t < 60.0]
        if len(window) >= limit_per_minute:
            self._calls[key] = window
            retry_after = int(60.0 - (now - window[0])) + 1
            msg = f"rate limit exceeded for {key}; retry in {retry_after}s"
            raise ToolUnavailable(
                msg, details={"tool": key, "limit": limit_per_minute, "retry_after_s": retry_after}
            )
        window.append(now)
        self._calls[key] = window

    def reset(self) -> None:
        self._calls.clear()


__all__ = [
    "RateLimiter",
    "ToolDefinition",
    "ToolExecutionContext",
    "ToolRegistry",
    "ToolResult",
    "input_hash",
    "validate_arguments",
]
