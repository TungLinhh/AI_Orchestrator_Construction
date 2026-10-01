"""Policy engine.

A rule engine, not a hard-coded `if`. The reason: approval requirements must be
tunable per organization without a deploy, and an operator has to be able to
read the rule set and predict what it will do.

The decision vocabulary is deliberately four-valued, not two-valued. Collapsing
`REQUIRE_APPROVAL` into `DENY` is the most common way a platform like this
becomes useless — the agent cannot act, so it either bypasses the gate or stops
working, and both outcomes teach the operator that the gate is advisory.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ai_orchestrator.domain.authority import (
    Action,
    AuthorityAction,
    PolicyContext,
    PolicyDecision,
    Resource,
)
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import (
    AutonomyLevel,
    PolicyDecisionType,
    RiskLevel,
    risk_at_least,
)


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """One rule. Evaluated in priority order; first non-ALLOW decision wins.

    Priority exists so that a specific deny can override a broad allow without
    the rule author needing to know the whole set.
    """

    rule_id: str
    description: str
    priority: int
    applies_to_actions: frozenset[AuthorityAction] | None = None
    applies_to_effects: frozenset[str] | None = None
    applies_to_risks: frozenset[RiskLevel] | None = None
    applies_to_tools: frozenset[str] | None = None
    applies_to_orgs: frozenset[str] | None = None
    decision: PolicyDecisionType = PolicyDecisionType.ALLOW
    reason: str = ""
    required_approver_roles: frozenset[str] = field(default_factory=frozenset)
    approval_ttl_s: int | None = None
    enabled: bool = True

    def matches(
        self, *, actor: Actor, action: Action, context: PolicyContext, tool_name: str | None
    ) -> bool:
        if not self.enabled:
            return False
        if self.applies_to_actions and action.action not in self.applies_to_actions:
            return False
        if self.applies_to_effects and action.effect.value not in self.applies_to_effects:
            return False
        if self.applies_to_risks and action.risk not in self.applies_to_risks:
            return False
        if self.applies_to_tools and (tool_name is None or tool_name not in self.applies_to_tools):
            return False
        return not (self.applies_to_orgs and context.organization_id not in self.applies_to_orgs)


@dataclass(frozen=True, slots=True)
class PolicySet:
    rules: tuple[PolicyRule, ...] = ()

    def __post_init__(self) -> None:
        ids = [r.rule_id for r in self.rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            msg = f"duplicate policy rule_id: {sorted(dupes)}"
            raise ValueError(msg)

    def ordered(self) -> list[PolicyRule]:
        return sorted(self.rules, key=lambda r: (-r.priority, r.rule_id))


@runtime_checkable
class PolicyEngine(Protocol):
    """The seam an organization swaps to change governance behaviour."""

    async def evaluate(
        self,
        actor: Actor,
        action: Action,
        resource: Resource,
        context: PolicyContext,
    ) -> PolicyDecision: ...


class RuleBasedPolicyEngine:
    """Default engine: a priority-ordered rule set with a fail-closed fallback.

    Fail-closed matters more than it looks. If evaluation itself throws, the
    answer must be DENY. An engine that fails open is a single-point bypass.
    """

    def __init__(self, rules: Iterable[PolicyRule] = ()) -> None:
        self._rules = PolicySet(tuple(rules))

    @property
    def rules(self) -> PolicySet:
        return self._rules

    async def evaluate(
        self,
        actor: Actor,
        action: Action,
        resource: Resource,
        context: PolicyContext,
    ) -> PolicyDecision:
        for rule in self._rules.ordered():
            if not rule.matches(actor=actor, action=action, context=context, tool_name=None):
                continue
            if rule.decision is PolicyDecisionType.ALLOW:
                return PolicyDecision(
                    decision=PolicyDecisionType.ALLOW,
                    reason=rule.reason or f"allowed by rule {rule.rule_id}",
                    rule_id=rule.rule_id,
                    effect=action.effect,
                    risk=action.risk,
                )
            return PolicyDecision(
                decision=rule.decision,
                reason=rule.reason or f"{rule.decision.value} by rule {rule.rule_id}",
                rule_id=rule.rule_id,
                effect=action.effect,
                risk=action.risk,
                required_approver_roles=rule.required_approver_roles,
                expires_at_seconds=rule.approval_ttl_s,
            )
        # No rule matched. Deny: an unconfigured action is not an allowed one.
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            reason=(
                f"no policy rule matched {action.action.value} "
                f"(risk={action.risk.value}, effect={action.effect.value}); "
                f"the platform denies unmatched actions by default"
            ),
            rule_id="POLICY_NO_MATCH",
            effect=action.effect,
            risk=action.risk,
        )


def default_policy_set(*, approval_ttl_s: int = 3600) -> PolicySet:
    """The baseline rule set. Explicit, so an operator can read what governs them."""
    return PolicySet(
        (
            # --- explicit denials, highest priority -----------------------------
            PolicyRule(
                rule_id="DENY_DESTRUCTIVE_WITHOUT_HUMAN",
                description="destructive actions are never autonomous",
                priority=100,
                applies_to_effects=frozenset({"destructive"}),
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="destructive action requires human approval",
                required_approver_roles=frozenset({"org_admin"}),
                approval_ttl_s=approval_ttl_s,
            ),
            PolicyRule(
                rule_id="DENY_EXTERNAL_SEND",
                description="anything that leaves the system needs a human",
                priority=95,
                applies_to_effects=frozenset({"external_send"}),
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="external side effect requires human approval",
                required_approver_roles=frozenset({"org_admin"}),
                approval_ttl_s=approval_ttl_s,
            ),
            PolicyRule(
                rule_id="DENY_POLICY_CHANGE",
                description="no actor but a privileged human may change policy",
                priority=90,
                applies_to_actions=frozenset(
                    {
                        AuthorityAction.CHANGE_POLICY,
                        AuthorityAction.CHANGE_MODEL_POLICY,
                        AuthorityAction.GRANT_PERMISSION,
                    }
                ),
                decision=PolicyDecisionType.DENY,
                reason="policy and permission changes are human-only",
            ),
            PolicyRule(
                rule_id="DENY_DEPLOY",
                description="production deployment is human-only",
                priority=90,
                applies_to_actions=frozenset({AuthorityAction.DEPLOY}),
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="deployment requires human approval",
                required_approver_roles=frozenset({"org_admin"}),
                approval_ttl_s=approval_ttl_s,
            ),
            PolicyRule(
                rule_id="APPROVAL_RAISE_BUDGET",
                description="an agent may not increase its own budget",
                priority=90,
                applies_to_actions=frozenset({AuthorityAction.RAISE_BUDGET}),
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="budget increases require human approval",
                required_approver_roles=frozenset({"org_admin"}),
                approval_ttl_s=approval_ttl_s,
            ),
            # --- risk bands ------------------------------------------------------
            # Note: there is deliberately no "gate everything" rule here. A rule
            # with no filters matches every action and would shadow the allow
            # rules below it. The per-autonomy-level floors live in
            # `apply_autonomy_gate`, which cannot be edited away by a policy set.
            PolicyRule(
                rule_id="RISK_CRITICAL_APPROVAL",
                description="critical and privileged risk needs a human",
                priority=70,
                applies_to_risks=frozenset({RiskLevel.CRITICAL, RiskLevel.PRIVILEGED}),
                decision=PolicyDecisionType.REQUIRE_APPROVAL,
                reason="critical/privileged risk requires human approval",
                required_approver_roles=frozenset({"org_admin"}),
                approval_ttl_s=approval_ttl_s,
            ),
            PolicyRule(
                rule_id="RISK_MEDIUM_HIGH_PARENT_REVIEW",
                description="medium and high risk go to the parent's review",
                priority=60,
                applies_to_risks=frozenset({RiskLevel.MEDIUM, RiskLevel.HIGH}),
                decision=PolicyDecisionType.ESCALATE,
                reason=f"{RiskLevel.MEDIUM.value}+ risk requires parent review",
            ),
            # --- autonomous band -------------------------------------------------
            PolicyRule(
                rule_id="ALLOW_LOW_RISK_AUTONOMOUS",
                description="low-risk reads and preparations run unattended",
                priority=10,
                applies_to_risks=frozenset({RiskLevel.LOW}),
                applies_to_actions=frozenset(
                    {AuthorityAction.READ, AuthorityAction.CALL_TOOL, AuthorityAction.CREATE}
                ),
                decision=PolicyDecisionType.ALLOW,
                reason="low-risk action within autonomy level",
            ),
        )
    )


def apply_autonomy_gate(
    decision: PolicyDecision, *, actor_kind: str, autonomy: AutonomyLevel
) -> PolicyDecision:
    """Second-stage gate applied after rule evaluation.

    Kept separate from the rules so that a rule set cannot accidentally grant an
    L0 agent the ability to act. A policy editor can change the rules; this
    floor cannot be edited away.
    """
    if actor_kind != "agent":
        return decision
    if autonomy is AutonomyLevel.L0_SUGGEST and decision.allowed:
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            reason="autonomy level L0 permits suggestions only",
            rule_id="AUTONOMY_FLOOR_L0",
            effect=decision.effect,
            risk=decision.risk,
        )
    if autonomy is AutonomyLevel.L3_HUMAN_APPROVAL and decision.allowed:
        return PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason="autonomy level L3 requires human approval for every action",
            rule_id="AUTONOMY_CEILING_L3",
            effect=decision.effect,
            risk=decision.risk,
            required_approver_roles=frozenset({"org_admin"}),
        )
    if risk_at_least(decision.risk, RiskLevel.PRIVILEGED) and decision.allowed:
        return PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason="privileged risk is never autonomous",
            rule_id="RISK_FLOOR_PRIVILEGED",
            effect=decision.effect,
            risk=decision.risk,
            required_approver_roles=frozenset({"org_admin"}),
        )
    return decision


__all__ = [
    "PolicyEngine",
    "PolicyRule",
    "PolicySet",
    "RuleBasedPolicyEngine",
    "apply_autonomy_gate",
    "default_policy_set",
]
