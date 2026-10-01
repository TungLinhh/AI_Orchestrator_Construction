"""Authority model.

Answers, for one question: *may this actor perform this action on this
resource, right now?*

Three rules shape the implementation:

1. **Default deny.** No grant means no access. There is no wildcard capability
   and no "admin by default"; a role that forgets to list a capability denies
   it, which is the correct failure direction.
2. **Identity is not authority.** `authority_profile` is data attached by the
   control plane. An agent claiming in its prompt to be the CEO gets nothing,
   because the claim is not read.
3. **Self-approval is impossible.** The separation-of-duties check is
   structural: it compares the requesting actor's id against the required
   approver's, and it cannot be satisfied by an agent at any autonomy level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import (
    ActorType,
    AuthorityScope,
    AutonomyLevel,
    DataClassification,
    EffectClass,
    PolicyDecisionType,
    RiskLevel,
    ToolRisk,
)
from ai_orchestrator.domain.errors import AuthorizationError

#: Effect classes that always need a human, regardless of policy tuning.
#: Taken from the failure evidence in the O-Nexus audit: an agent that is
#: allowed to decide or to send externally is a governance incident waiting.
_ALWAYS_APPROVAL_EFFECTS: frozenset[EffectClass] = frozenset(
    {EffectClass.DECIDE, EffectClass.EXTERNAL_SEND, EffectClass.PRIVILEGED, EffectClass.DESTRUCTIVE}
)

#: Tool risk that is gated no matter what an agent definition claims.
_ALWAYS_APPROVAL_TOOL_RISK: frozenset[ToolRisk] = frozenset(
    {ToolRisk.PRIVILEGED, ToolRisk.DESTRUCTIVE}
)


class AuthorityAction(StrEnum):
    """What an actor is trying to do. Kept separate from `EffectClass`:
    the effect describes the consequence, the action describes the verb."""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    DELEGATE = "delegate"
    SPAWN_SUBAGENT = "spawn_subagent"
    CALL_TOOL = "call_tool"
    GRANT_PERMISSION = "grant_permission"
    APPROVE = "approve"
    DEPLOY = "deploy"
    CHANGE_POLICY = "change_policy"
    CHANGE_MODEL_POLICY = "change_model_policy"
    RAISE_BUDGET = "raise_budget"
    RETIRE_AGENT = "retire_agent"
    EXPORT_DATA = "export_data"
    EXECUTE_CODE = "execute_code"


@dataclass(frozen=True, slots=True)
class AuthorityGrant:
    """One capability in a role's authority profile."""

    action: AuthorityAction
    scope: AuthorityScope
    resource_type: str = "*"
    resource_id: str | None = None
    # Effects the grant is allowed to produce. An empty tuple means "this
    # action cannot produce an effect at all" rather than "any effect".
    allowed_effects: frozenset[EffectClass] = frozenset()
    max_risk: RiskLevel = RiskLevel.HIGH
    data_classification_ceiling: DataClassification = DataClassification.INTERNAL

    def covers(self, action: AuthorityAction, resource_type: str) -> bool:
        """Action and type match only. Resource-id binding is done by the
        profile's `grants_for`, which has the id to compare against."""
        if self.action is not action:
            return False
        return self.resource_type in ("*", resource_type)


@dataclass(frozen=True, slots=True)
class AuthorityProfile:
    """A role's complete authority. Absence of a grant is a denial."""

    role_id: str
    grants: frozenset[AuthorityGrant] = field(default_factory=frozenset)
    # Hard cap independent of any grant. Belt and braces.
    max_autonomy: AutonomyLevel = AutonomyLevel.L2_PARENT_REVIEW
    may_delegate_to_peers: bool = False
    may_spawn_subagents: bool = False
    max_delegation_depth: int = 2
    # Named so it can carry a default: a resource-specific grant binds to one id.
    resource: Resource | None = None

    def grants_for(
        self, action: AuthorityAction, resource_type: str, resource_id: str | None = None
    ) -> list[AuthorityGrant]:
        """Grants matching an action.

        A grant pinned to a specific `resource_id` only matches that id, so a
        grant issued for one task cannot be replayed against another.
        """
        matches: list[AuthorityGrant] = []
        for g in self.grants:
            if not g.covers(action, resource_type):
                continue
            if g.resource_id not in (None, "*") and g.resource_id != resource_id:
                continue
            matches.append(g)
        return matches


#: A deliberately minimal profile. Used for subagents before the spawn request
#: is evaluated, and as the floor that nothing may go below.
SUBAGENT_FLOOR_PROFILE = AuthorityProfile(
    role_id="subagent",
    grants=frozenset(
        {
            AuthorityGrant(
                action=AuthorityAction.READ,
                scope=AuthorityScope.TASK,
                allowed_effects=frozenset({EffectClass.READ}),
                max_risk=RiskLevel.LOW,
            ),
            AuthorityGrant(
                action=AuthorityAction.CALL_TOOL,
                scope=AuthorityScope.TASK,
                allowed_effects=frozenset({EffectClass.READ, EffectClass.PREPARE}),
                max_risk=RiskLevel.MEDIUM,
            ),
        }
    ),
    max_autonomy=AutonomyLevel.L1_LOW_RISK_AUTONOMOUS,
    may_delegate_to_peers=False,
    may_spawn_subagents=False,
    max_delegation_depth=0,
)


@dataclass(frozen=True, slots=True)
class Resource:
    """What is being acted upon. Scoped so policy can be specific."""

    resource_type: str
    resource_id: str
    organization_id: str
    org_unit_id: str | None = None
    owner_agent_id: str | None = None
    classification: DataClassification = DataClassification.INTERNAL


@dataclass(frozen=True, slots=True)
class Action:
    """A proposed action, fully described so policy needs no guessing."""

    action: AuthorityAction
    effect: EffectClass
    risk: RiskLevel
    resource: Resource
    tool_risk: ToolRisk | None = None
    justification: str = ""
    autonomy_level: AutonomyLevel = AutonomyLevel.L1_LOW_RISK_AUTONOMOUS


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Ambient facts that influence a decision."""

    organization_id: str
    run_mode: str = "live"
    task_id: str | None = None
    parent_agent_id: str | None = None
    # The human who requested the work, when there is one. Used to decide
    # whether an approval can be routed to them.
    on_behalf_of: str | None = None
    is_approval_requester: bool = False
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """A decision plus the reason, so the audit row is self-explanatory."""

    decision: PolicyDecisionType
    reason: str
    rule_id: str
    effect: EffectClass
    risk: RiskLevel
    required_approver_roles: frozenset[str] = field(default_factory=frozenset)
    expires_at_seconds: int | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is PolicyDecisionType.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.decision is PolicyDecisionType.REQUIRE_APPROVAL

    @property
    def denied(self) -> bool:
        return self.decision is PolicyDecisionType.DENY

    def to_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "effect": self.effect.value,
            "risk": self.risk.value,
            "required_approver_roles": sorted(self.required_approver_roles),
        }


def _deny(reason: str, rule_id: str, action: Action) -> PolicyDecision:
    return PolicyDecision(
        decision=PolicyDecisionType.DENY,
        reason=reason,
        rule_id=rule_id,
        effect=action.effect,
        risk=action.risk,
    )


def evaluate_authority(
    *, actor: Actor, action: Action, profile: AuthorityProfile, context: PolicyContext
) -> PolicyDecision:
    """Check the authority profile. Pure. No I/O, no clock, no database.

    This is the only place an action is matched against a grant, so the answer
    to "why was this denied" is always a rule id.
    """
    if actor.organization_id is not None and str(actor.organization_id) != context.organization_id:
        return _deny(
            f"actor belongs to organization {actor.organization_id}, "
            f"resource belongs to {context.organization_id}",
            "AUTH_TENANT_MISMATCH",
            action,
        )

    if action.effect in _ALWAYS_APPROVAL_EFFECTS:
        return PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=(
                f"effect {action.effect.value} always requires human approval "
                f"regardless of the agent's authority profile"
            ),
            rule_id="AUTH_EFFECT_ALWAYS_APPROVAL",
            effect=action.effect,
            risk=action.risk,
            required_approver_roles=frozenset({"org_admin"}),
        )

    if action.tool_risk is not None and action.tool_risk in _ALWAYS_APPROVAL_TOOL_RISK:
        return PolicyDecision(
            decision=PolicyDecisionType.REQUIRE_APPROVAL,
            reason=f"tool risk {action.tool_risk.value} is always approval-gated",
            rule_id="AUTH_TOOL_RISK_ALWAYS_APPROVAL",
            effect=action.effect,
            risk=action.risk,
            required_approver_roles=frozenset({"org_admin"}),
        )

    if actor.kind is ActorType.AGENT and action.autonomy_level is AutonomyLevel.L0_SUGGEST:
        return PolicyDecision(
            decision=PolicyDecisionType.DENY,
            reason="agent runs at autonomy level L0: it may only suggest, not act",
            rule_id="AUTH_AUTONOMY_L0",
            effect=action.effect,
            risk=action.risk,
        )

    matching = profile.grants_for(
        action.action, action.resource.resource_type, action.resource.resource_id
    )
    if not matching:
        return _deny(
            f"no grant for {action.action.value} on "
            f"{action.resource.resource_type}:{action.resource.resource_id} "
            f"in role {profile.role_id}",
            "AUTH_NO_GRANT",
            action,
        )

    # Narrowest matching grant wins, so a resource-pinned grant can carry a
    # tighter limit than the org-wide one.
    best = min(
        matching,
        key=lambda g: (0 if g.resource_id not in (None, "*") else 1, _RISK_ORDER[g.max_risk]),
    )
    if _RISK_ORDER[action.risk] > _RISK_ORDER[best.max_risk]:
        return _deny(
            f"risk {action.risk.value} exceeds the maximum {best.max_risk.value} "
            f"granted to role {profile.role_id}",
            "AUTH_RISK_TOO_HIGH",
            action,
        )

    if action.effect not in best.allowed_effects and best.allowed_effects:
        return _deny(
            f"effect {action.effect.value} is not permitted for "
            f"{action.action.value} by role {profile.role_id}",
            "AUTH_EFFECT_NOT_GRANTED",
            action,
        )

    if _classification_rank(action.resource.classification) > _classification_rank(
        best.data_classification_ceiling
    ):
        return _deny(
            f"resource classification {action.resource.classification.value} exceeds the "
            f"ceiling {best.data_classification_ceiling.value} for role {profile.role_id}",
            "AUTH_CLASSIFICATION_TOO_HIGH",
            action,
        )

    if action.action is AuthorityAction.SPAWN_SUBAGENT and not profile.may_spawn_subagents:
        return _deny(f"role {profile.role_id} may not spawn subagents", "AUTH_NO_SPAWN", action)

    if action.action is AuthorityAction.DELEGATE and (
        action.resource.owner_agent_id is not None
        and action.resource.owner_agent_id != str(actor.id)
        and not profile.may_delegate_to_peers
    ):
        return _deny(
            f"role {profile.role_id} may not delegate to peers", "AUTH_NO_PEER_DELEGATION", action
        )

    return PolicyDecision(
        decision=PolicyDecisionType.ALLOW,
        reason="permitted by role authority profile",
        rule_id="AUTH_GRANT",
        effect=action.effect,
        risk=action.risk,
    )


_CLASSIFICATION_RANK = {
    DataClassification.PUBLIC: 0,
    DataClassification.INTERNAL: 1,
    DataClassification.CONFIDENTIAL: 2,
    DataClassification.RESTRICTED: 3,
    DataClassification.SECRET: 4,
}

#: Shared with the enums module. The authority layer needs the numeric order to
#: compare magnitudes; a string comparison of "low"/"medium" would be wrong.
_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
    RiskLevel.PRIVILEGED: 4,
}


def _classification_rank(value: DataClassification) -> int:
    return _CLASSIFICATION_RANK[value]


def enforce_authority(
    *, actor: Actor, action: Action, profile: AuthorityProfile, context: PolicyContext
) -> PolicyDecision:
    """`evaluate_authority` that raises on deny.

    Approval-required is *not* raised: it is a legitimate outcome the caller
    turns into a pause, and treating it as an exception is what produces the
    "agent crashed when it needed permission" class of bug.
    """
    decision = evaluate_authority(actor=actor, action=action, profile=profile, context=context)
    if decision.denied:
        raise AuthorizationError(decision.reason, details=decision.to_dict())
    return decision


def require_separate_approver(
    *, requester: Actor, approver_id: str, approver_is_agent: bool
) -> None:
    """Maker-checker. Structural, so no configuration can route around it.

    An agent is never an acceptable approver, and never its own requester.
    """
    if approver_is_agent:
        msg = "an agent cannot approve an action; a human approver is required"
        raise AuthorizationError(msg, details={"rule": "APPROVER_MUST_BE_HUMAN"})
    if str(requester.id) == str(approver_id):
        msg = "the requester cannot approve their own request"
        raise AuthorizationError(msg, details={"rule": "SELF_APPROVAL_FORBIDDEN"})


__all__ = [
    "SUBAGENT_FLOOR_PROFILE",
    "Action",
    "AuthorityAction",
    "AuthorityGrant",
    "AuthorityProfile",
    "PolicyContext",
    "PolicyDecision",
    "Resource",
    "enforce_authority",
    "evaluate_authority",
    "require_separate_approver",
]
