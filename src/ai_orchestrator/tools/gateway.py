"""The tool gateway.

The only path from an agent's intent to an executed tool. Every gate in
`_gate` runs before `_execute`, and the order is chosen so that a cheap,
decisive check runs first:

    1. authorisation  — is this agent allowed this tool at all
    2. policy         — does the effect need approval or is it denied
    3. autonomy       — the non-editable floor for the agent's level
    4. run mode       — simulation refuses to touch anything external
    5. rate limit     — protect the provider before spending a call
    6. budget         — refuse before the spend, not after
    7. validation     — arguments, before they reach a handler

A gate returning "requires approval" is not an error: it becomes a pause. That
distinction is the difference between a platform that can ask a human and one
whose agents either bypass the gate or stop working.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.authority import (
    Action,
    AuthorityAction,
    AuthorityProfile,
    PolicyContext,
    PolicyDecision,
    Resource,
    enforce_authority,
)
from ai_orchestrator.domain.budget import BudgetState, Money
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import (
    ActorType,
    AutonomyLevel,
    EffectClass,
    RiskLevel,
    RunMode,
    ToolRisk,
    tool_risk_exceeds,
)
from ai_orchestrator.domain.errors import (
    AuthorizationError,
    BudgetExceeded,
    PlatformError,
    ToolUnavailable,
    ValidationError,
)
from ai_orchestrator.domain.policy import PolicyEngine, apply_autonomy_gate
from ai_orchestrator.tools.registry import (
    RateLimiter,
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    input_hash,
    validate_arguments,
)

#: Called when a gated tool needs a human decision. Returning None means "no
#: approval mechanism is wired up", which the gateway treats as a denial rather
#: than proceeding: an action that requires approval and cannot be approved must
#: not run.
ApprovalRequester = Callable[["ToolGateResult"], Awaitable[str | None]]


@dataclass(slots=True)
class ToolGateResult:
    """The outcome of gating, before anything executes."""

    allowed: bool
    decision: PolicyDecision | None
    reason: str
    rule_id: str
    requires_approval: bool = False
    approval_id: str | None = None
    risk: ToolRisk = ToolRisk.LOW_RISK_WRITE
    estimated_cost_usd: Money = field(default_factory=lambda: Money("0"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "requires_approval": self.requires_approval,
            "approval_id": self.approval_id,
            "risk": self.risk.value,
            "decision": self.decision.to_dict() if self.decision else None,
        }


@dataclass(slots=True)
class ToolInvocation:
    """The record of one attempt. Written to the audit log either way."""

    tool_name: str
    agent_id: str
    task_id: str | None
    execution_id: str | None
    organization_id: str
    arguments_hash: str
    gate: ToolGateResult
    result: ToolResult | None = None
    started_at: float = 0.0
    duration_ms: int = 0
    trace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "execution_id": self.execution_id,
            "organization_id": self.organization_id,
            "arguments_hash": self.arguments_hash,
            "gate": self.gate.to_dict(),
            "result": self.result.to_dict() if self.result else None,
            "duration_ms": self.duration_ms,
        }


class ToolGateway:
    """Gates and runs tool calls."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        policy_engine: PolicyEngine,
        rate_limiter: RateLimiter | None = None,
        approval_requester: ApprovalRequester | None = None,
        audit: Callable[[ToolInvocation], Awaitable[None]] | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy_engine
        self._rate_limiter = rate_limiter or RateLimiter()
        self._approval_requester = approval_requester
        self._audit = audit
        #: The most recent invocations, for the execution timeline.
        self.history: list[ToolInvocation] = []

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    async def check(
        self,
        *,
        actor: Actor,
        tool_name: str,
        organization_id: str,
        autonomy_level: AutonomyLevel = AutonomyLevel.L1_LOW_RISK_AUTONOMOUS,
        profile: AuthorityProfile | None = None,
        run_mode: RunMode = RunMode.LIVE,
        max_risk: ToolRisk | None = None,
        arguments: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> ToolGateResult:
        """Evaluate every gate without executing anything.

        Exposed separately from `invoke` because the workflow needs to know
        whether approval is required *before* the tool runs, and because a UI
        that wants to show "this would need approval" should not have to call it.
        """
        try:
            definition = self._registry.get(tool_name)
        except ToolUnavailable as exc:
            return ToolGateResult(
                allowed=False, decision=None, reason=exc.message, rule_id="TOOL_UNKNOWN"
            )

        resource = Resource(
            resource_type="tool",
            resource_id=definition.tool_id,
            organization_id=organization_id,
            classification=definition.data_classification,
            owner_agent_id=str(actor.id),
        )
        action = Action(
            action=AuthorityAction.CALL_TOOL,
            effect=definition.effect_class,
            # A tool's registered risk is the platform's assessment, not the
            # agent's. Using the agent's `self_assessed_risk` would let a
            # miscalibrated model authorise itself out of every gate.
            risk=_risk_to_level(definition.risk),
            tool_risk=definition.risk,
            resource=resource,
            autonomy_level=autonomy_level,
        )
        context = PolicyContext(
            organization_id=organization_id, task_id=task_id, run_mode=run_mode.value
        )

        # 1. Authorisation. Raises on deny; approval is not a denial here.
        if profile is not None:
            try:
                enforce_authority(actor=actor, action=action, profile=profile, context=context)
            except AuthorizationError as exc:
                return ToolGateResult(
                    allowed=False,
                    decision=None,
                    reason=exc.message,
                    rule_id=str(exc.details.get("rule", "AUTH_DENIED")),
                    risk=definition.risk,
                )

        # 2. Policy.
        decision = await self._policy.evaluate(actor, action, resource, context)

        # 3. The autonomy floor. Applied after policy and not editable by it.
        decision = apply_autonomy_gate(
            decision, actor_kind=actor.kind.value, autonomy=autonomy_level
        )

        # 4. A per-binding risk ceiling, when the operator set one narrower than
        #    the tool's own risk.
        if max_risk is not None and tool_risk_exceeds(definition.risk, max_risk):
            return ToolGateResult(
                allowed=False,
                decision=decision,
                reason=(
                    f"tool risk {definition.risk.value} exceeds the ceiling "
                    f"{max_risk.value} granted to this agent"
                ),
                rule_id="TOOL_RISK_CEILING",
                risk=definition.risk,
            )

        # 5. Simulation. A simulation run must not touch anything external, and
        #    this is the last point at which that can be guaranteed.
        if run_mode is RunMode.SIMULATION and _has_external_effect(definition):
            return ToolGateResult(
                allowed=False,
                decision=decision,
                reason=(
                    f"tool {tool_name!r} has an external effect and simulation mode "
                    f"forbids executing it"
                ),
                rule_id="SIMULATION_NO_SIDE_EFFECT",
                risk=definition.risk,
            )

        requires_approval = definition.requires_approval or decision.needs_approval

        if decision.denied:
            return ToolGateResult(
                allowed=False,
                decision=decision,
                reason=decision.reason,
                rule_id=decision.rule_id,
                risk=definition.risk,
            )

        if requires_approval and actor.kind is ActorType.AGENT:
            return ToolGateResult(
                allowed=False,
                decision=decision,
                reason=f"{tool_name} requires human approval: {decision.reason}",
                rule_id=decision.rule_id,
                requires_approval=True,
                risk=definition.risk,
            )

        if requires_approval and actor.kind is not ActorType.AGENT:
            # A human acting directly still has to be a privileged one, and the
            # policy engine already said so; reaching here means the policy
            # allowed a non-privileged human, which is a misconfiguration.
            return ToolGateResult(
                allowed=False,
                decision=decision,
                reason=f"{tool_name} requires an explicitly privileged human",
                rule_id=decision.rule_id,
                requires_approval=True,
                risk=definition.risk,
            )

        return ToolGateResult(
            allowed=True,
            decision=decision,
            reason=decision.reason,
            rule_id=decision.rule_id,
            risk=definition.risk,
        )

    async def invoke(
        self,
        *,
        actor: Actor,
        tool_name: str,
        arguments: dict[str, Any],
        organization_id: str,
        task_id: str | None = None,
        execution_id: str | None = None,
        autonomy_level: AutonomyLevel = AutonomyLevel.L1_LOW_RISK_AUTONOMOUS,
        profile: AuthorityProfile | None = None,
        run_mode: RunMode = RunMode.LIVE,
        max_risk: ToolRisk | None = None,
        budget: BudgetState | None = None,
        estimated_cost_usd: Money | None = None,
        trace_id: str | None = None,
        context_attributes: dict[str, Any] | None = None,
    ) -> ToolInvocation:
        """Gate, execute, and record. The whole path, in one call."""
        invocation = ToolInvocation(
            tool_name=tool_name,
            agent_id=str(actor.id),
            task_id=task_id,
            execution_id=execution_id,
            organization_id=organization_id,
            arguments_hash=input_hash(arguments),
            gate=ToolGateResult(allowed=False, decision=None, reason="not evaluated", rule_id=""),
            started_at=time.monotonic(),
            trace_id=trace_id,
        )

        definition: ToolDefinition | None = None
        try:
            definition = self._registry.get(tool_name)
        except ToolUnavailable as exc:
            invocation.gate = ToolGateResult(
                allowed=False, decision=None, reason=exc.message, rule_id="TOOL_UNKNOWN"
            )
            return await self._finish(invocation, ToolResult.failure("TOOL_UNKNOWN", exc.message))

        try:
            gate = await self.check(
                actor=actor,
                tool_name=tool_name,
                organization_id=organization_id,
                autonomy_level=autonomy_level,
                profile=profile,
                run_mode=run_mode,
                max_risk=max_risk,
                arguments=arguments,
                task_id=task_id,
            )
        except PlatformError as exc:
            invocation.gate = ToolGateResult(
                allowed=False, decision=None, reason=exc.message, rule_id=exc.kind.value
            )
            return await self._finish(invocation, ToolResult.failure(exc.kind.value, exc.message))
        invocation.gate = gate

        if not gate.allowed:
            kind = "TOOL_DENIED" if gate.requires_approval is False else "APPROVAL_REQUIRED"
            return await self._finish(
                invocation,
                ToolResult.failure(
                    kind,
                    gate.reason,
                    metadata={"rule_id": gate.rule_id, "requires_approval": gate.requires_approval},
                ),
            )

        assert definition is not None
        # 5. Rate limit, before the budget is touched.
        try:
            self._rate_limiter.check(
                f"{organization_id}:{tool_name}", definition.rate_limit_per_minute
            )
        except ToolUnavailable as exc:
            return await self._finish(invocation, ToolResult.failure("RATE_LIMITED", exc.message))

        # 6. Budget, before the call.
        cost = estimated_cost_usd or Money("0")
        if budget is not None:
            try:
                budget.check(est_cost_usd=cost)
            except BudgetExceeded as exc:
                return await self._finish(
                    invocation, ToolResult.failure("BUDGET_EXCEEDED", exc.message)
                )

        # 7. Validation, before the handler.
        try:
            validate_arguments(definition.input_schema, arguments)
        except ValidationError as exc:
            return await self._finish(
                invocation, ToolResult.failure("INVALID_ARGUMENTS", exc.message)
            )

        return await self._finish(
            invocation,
            await self._execute(
                definition,
                arguments,
                organization_id,
                task_id,
                execution_id,
                actor,
                run_mode,
                cost,
                context_attributes,
            ),
        )

    async def _execute(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
        organization_id: str,
        task_id: str | None,
        execution_id: str | None,
        actor: Actor,
        run_mode: RunMode,
        reserved: Money,
        context_attributes: dict[str, Any] | None = None,
    ) -> ToolResult:
        import asyncio

        context = ToolExecutionContext(
            organization_id=organization_id,
            task_id=task_id,
            execution_id=execution_id,
            agent_id=str(actor.id),
            run_mode=run_mode,
            timeout_seconds=definition.timeout_seconds,
            # The caller's attributes, or the handler sees an empty dict. This was
            # the single missing line behind "the Executive never delegates": the
            # application passed its delegation callable in here, the parameter
            # was accepted, and the `ToolExecutionContext` was built without it —
            # so `delegate_to_agent` read `attributes.get("delegate")`, found
            # `None`, and correctly reported `DELEGATION_UNAVAILABLE` on every
            # call. A dropped keyword argument is indistinguishable from a broken
            # feature, which is why it survived a model change, a prompt audit
            # and two live runs.
            attributes=context_attributes or {},
        )
        started = time.monotonic()
        try:
            async with asyncio.timeout(definition.timeout_seconds):
                result = await definition.handler(arguments, context)
        except TimeoutError:
            return ToolResult.failure(
                "TOOL_TIMEOUT",
                f"tool {definition.name!r} exceeded {definition.timeout_seconds}s",
                idempotent=definition.is_idempotent,
            )
        except PlatformError as exc:
            return ToolResult.failure(
                exc.kind.value, exc.message, idempotent=definition.is_idempotent
            )
        except Exception as exc:
            # A tool that raises unexpectedly is a tool fault, not a platform
            # fault, and must not be reported as an internal error.
            return ToolResult.failure(
                "TOOL_FAULT", f"{type(exc).__name__}: {exc}", idempotent=definition.is_idempotent
            )
        # A fresh instance rather than a mutation: the handler may have handed
        # back a result it also holds a reference to.
        return ToolResult(
            ok=result.ok,
            output=result.output,
            error_kind=result.error_kind,
            error_message=result.error_message,
            duration_ms=int((time.monotonic() - started) * 1000),
            idempotent=result.idempotent and definition.is_idempotent,
            metadata=result.metadata,
        )

    async def _finish(self, invocation: ToolInvocation, result: ToolResult) -> ToolInvocation:
        invocation.result = result
        invocation.duration_ms = int((time.monotonic() - invocation.started_at) * 1000)
        self.history.append(invocation)
        if len(self.history) > 500:
            del self.history[:-500]
        if self._audit is not None:
            await self._audit(invocation)
        return invocation

    async def request_approval(self, gate: ToolGateResult) -> str | None:
        """Ask the configured mechanism for a human decision.

        Returns None when no mechanism is wired up, which the caller must treat
        as a denial. Silently proceeding is the failure this guards against.
        """
        if self._approval_requester is None:
            return None
        return await self._approval_requester(gate)


_RISK_TO_LEVEL = {
    ToolRisk.READ_ONLY: "low",
    ToolRisk.LOW_RISK_WRITE: "low",
    ToolRisk.EXTERNAL_SIDE_EFFECT: "high",
    ToolRisk.PRIVILEGED: "privileged",
    ToolRisk.DESTRUCTIVE: "critical",
}


def _risk_to_level(risk: ToolRisk) -> RiskLevel:
    return RiskLevel(_RISK_TO_LEVEL[risk])


#: Effects that make simulation mode refuse the call. READ and PREPARE are
#: excluded because they change nothing outside the process, which is exactly
#: what a simulation run is for.
_SIMULATION_FORBIDDEN_EFFECTS = frozenset(
    {
        EffectClass.MUTATE_INTERNAL,
        EffectClass.EXTERNAL_SEND,
        EffectClass.PRIVILEGED,
        EffectClass.DESTRUCTIVE,
    }
)


def _has_external_effect(definition: ToolDefinition) -> bool:
    return definition.effect_class in _SIMULATION_FORBIDDEN_EFFECTS


__all__ = [
    "ApprovalRequester",
    "ToolGateResult",
    "ToolGateway",
    "ToolInvocation",
]
