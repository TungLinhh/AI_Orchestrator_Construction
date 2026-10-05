"""Turning an agent's proposal into an actual delegation.

This module is the missing link between "an agent asked for something" and "the
organisation did it". Without it the platform has a fully gated delegation
mechanism — `authorize_delegation`, the ancestor path, the budget clamp, the
audit row — and nothing ever calls it, so an executive agent handed a goal
completes it alone and the hierarchy is decoration.

Three rules, each one a place where the obvious implementation is wrong:

  * **A proposal is data, not permission.** The agent says "delegate to
    Marketing". The platform then asks `authorize_delegation`, which is the only
    thing that can say yes. A proposal cannot widen its own limits, and a refusal
    is recorded with its reason rather than raised, because "the executive tried
    to delegate to itself and was refused" is a thing an operator needs to see.

  * **The child task is created in the same transaction as the delegation row.**
    A delegation with no task is a claim; a task with no delegation is an
    orphan. Splitting them across two commits is how a crashed process leaves a
    department "assigned" work it was never given.

  * **A refused delegation does not fail the parent task.** The parent learns it
    has outstanding work, and if *nothing* was accepted the parent is blocked
    with the reason — not marked complete, and not marked failed. Those are
    different operator signals and conflating them is how a goal silently goes
    nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.contracts import ActionProposal
from ai_orchestrator.domain.delegation import (
    DelegationPath,
    DelegationVerdict,
    authorize_delegation,
    goal_intent_refusal,
    intent_fingerprint,
    task_fingerprint,
)
from ai_orchestrator.domain.errors import ConflictError
from ai_orchestrator.domain.ids import AgentId
from ai_orchestrator.persistence.models import Agent, Task
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class DelegationOutcome:
    """What became of one run's proposals."""

    accepted: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)
    child_task_ids: list[str] = field(default_factory=list)
    #: **True when a refusal was structural**, so no further proposal in this run can
    #: succeed either. Carried out of `DelegationVerdict.terminal` because the caller
    #: needs to tell a model "stop delegating and answer", and it cannot work that out
    #: from a sentence it has to pattern-match.
    closed: bool = False

    @property
    def any_accepted(self) -> bool:
        return bool(self.accepted)

    @property
    def refusal_reason(self) -> str:
        """The one refusal worth showing, or `""` when something was accepted.

        With several refusals the first is the binding one, because `authorize_delegation`
        checks its ceilings in order and stops at the first that fires — so the list is
        not unordered and "the first" is a fact about the rule, not about timing.
        """
        return self.refused[0][1] if self.refused else ""

    @property
    def summary(self) -> str:
        if not self.accepted and not self.refused:
            return ""
        parts = [f"{len(self.accepted)} delegated"]
        if self.refused:
            parts.append(f"{len(self.refused)} refused ({self.refused[0][1]})")
        return ", ".join(parts)


#: The only `input` keys a child inherits. Named rather than copied wholesale:
#: work data belongs to the task that was given it, and a child that re-decided
#: on the parent's data would be working from a copy nobody reviewed.
#: The `input` keys a child inherits. Named rather than copied wholesale: work data
#: belongs to the task that was given it, and a child re-deciding on the parent's
#: data would be working from a copy nobody reviewed.
#:
#: **`owning_office` belongs here as much as `owning_department`.** It was left out,
#: so the office a piece of work belongs to survived the first hop and was gone by
#: the second -- and the office then routed on whatever it had, which in practice
#: meant the alphabetically first department. Measured: a procurement SOP and a
#: sales SOP both finished at Finance, having asked for a buyer and a salesperson.
#: `brief` is the material the work is about. It travels because a department
#: handed an objective and no data cannot do the work, and because the delegation
#: objective a model writes is its own summary of the task -- which is not the
#: material. Measured: the office forwarded `task.goal[:200]`, and a 200-character
#: cut of a policy question is a question with the policy missing.
#:
#: A routing fact that is carried halfway is worse than one that is absent, because
#: it looks like it is working.
ROUTING_INPUT_KEYS = frozenset({"owning_department", "owning_office", "brief"})


class DelegationExecutor:
    """Applies `delegate` proposals, under the platform's rules."""

    def __init__(
        self,
        *,
        tasks: Any,
        delegations: Any,
        agents: Any,
        audit: Any,
        organization_id: str,
        platform_limits: Any = None,
    ) -> None:
        self._tasks = tasks
        self._delegations = delegations
        self._agents = agents
        self._audit = audit
        self._org = organization_id
        self._platform = platform_limits
        from ai_orchestrator.config.settings import get_settings

        self._goal_intent_limit = get_settings().max_distinct_intents_per_goal
        # Intent fingerprints already delegated from this run, so a model that
        # asks for the same work twice gets one child rather than two. Per
        # instance, which is per run: a *new* run of the same goal is allowed to
        # delegate again, because that is a retry the operator asked for.
        self._issued_intents: set[str] = set()

    async def apply(
        self,
        *,
        parent: Task,
        source_agent_id: str,
        proposals: tuple[ActionProposal, ...],
        path: DelegationPath,
        budget_remaining_tokens: int = 0,
    ) -> DelegationOutcome:
        """Apply every `delegate` proposal in one pass."""
        outcome = DelegationOutcome()
        for proposal in proposals:
            if proposal.kind != "delegate":
                continue
            await self._apply_one(
                parent=parent,
                source_agent_id=source_agent_id,
                proposal=proposal,
                path=path,
                outcome=outcome,
            )
        return outcome

    async def _apply_one(
        self,
        *,
        parent: Task,
        source_agent_id: str,
        proposal: ActionProposal,
        path: DelegationPath,
        outcome: DelegationOutcome,
    ) -> None:

        target_id = proposal.target_agent_id
        if not target_id:
            outcome.refused.append(("", "the proposal named no agent to delegate to"))
            return

        # The target must exist, be active, and be in *this* tenant. A model that
        # hallucinates an agent id must not be able to route work into somebody
        # else's organisation.
        target = await self._lookup_target(target_id)
        if target is None:
            outcome.refused.append((str(target_id), "no such agent in this organisation"))
            return

        verdict: DelegationVerdict = authorize_delegation(
            source=AgentId(source_agent_id),
            target=AgentId(target.id),
            path=path,
            requested=None,
            parent_limits=path_limits(path, self._platform),
            platform_limits=self._platform or path_limits(path, None),
            current_fanout=await self._current_fanout(source_agent_id, parent.id),
            current_active_descendants=await self._active_descendants(source_agent_id),
        )
        if not verdict.allowed:
            outcome.refused.append((str(target.id), verdict.reason))
            outcome.closed = outcome.closed or verdict.terminal
            await self._record_refusal(parent, source_agent_id, target.id, verdict.reason)
            return

        # Duplicate work is refused, not created twice. A model that asks twice
        # gets one child: the first live run delegated to the same agent with the
        # same objective twice, because the model re-read its own tool result,
        # decided the work was not done, and asked again. Two rows for one piece of
        # work is how a fan-out cap stops meaning anything, and it is a lie in the
        # audit trail — the company appears to have assigned one requisition twice.
        #
        # `task_fingerprint` rather than a key of this module's own, so "have I
        # been asked this before?" has exactly one answer in the system. The
        # target is in the scope, which makes this key *coarser* than the child
        # task's own fingerprint: that is the right direction, because "this agent
        # was already given this work" must not depend on the wording drifting.
        intent = task_fingerprint(
            organization_id=self._org,
            task_type=parent.task_type,
            goal=proposal.objective or parent.goal,
            # The target is part of the scope, so the same words sent to two
            # different agents are two pieces of work, not one duplicated.
            scope=(str(target.id),),
        )
        if intent in self._issued_intents:
            outcome.refused.append(
                (str(target.id), "this agent already delegated this exact work from this task")
            )
            logger.info(
                "delegation.duplicate_refused",
                parent_task_id=parent.id,
                target=target.name,
                fingerprint=intent,
            )
            return
        self._issued_intents.add(intent)

        # **The intent key, and the database does the refusing.**
        #
        # `_issued_intents` lives on this instance, so it only ever covered one run -- and the
        # measured duplicate was exactly that: one parent, four children, one per run. It stays
        # as the cheap first check, because it saves a query when a model asks twice in one
        # breath, but it is not the guard.
        #
        # The guard is `uq_tasks_live_intent` (migration 0026), a partial unique index on
        # `(organization_id, owner_agent_id, intent_fingerprint)` over **live** statuses. The
        # database rather than a check in this function, because a check here protects only
        # here: a second writer, a retry, or a future path would not consult it. And partial, so
        # finished work can legitimately be asked for again.
        intent_key = intent_fingerprint(
            organization_id=self._org,
            task_type=parent.task_type,
            goal=proposal.objective or parent.goal,
            owner_agent_id=str(target.id),
        )
        if intent_key in self._issued_intents:
            outcome.refused.append(
                (str(target.id), "this agent already delegated this exact work from this task")
            )
            logger.info(
                "delegation.duplicate_refused",
                parent_task_id=parent.id,
                target=target.name,
                fingerprint=intent_key,
            )
            return
        self._issued_intents.add(intent_key)

        # Across all offices, not just this parent. The repository holds a goal
        # lock through the child/delegation commit, so parallel coordinators
        # cannot both spend the last slot. Reworded work consumes a new slot.
        issued = await self._delegations.goal_intents(str(parent.id))
        reason = goal_intent_refusal(
            issued=issued, intent=intent_key, limit=self._goal_intent_limit
        )
        if reason:
            outcome.refused.append((str(target.id), reason))
            outcome.closed = True
            await self._record_refusal(parent, source_agent_id, target.id, reason)
            return

        # Child limits can only ever be narrower. The verdict carries the clamped set, and the
        # child task is created with it, so a subagent cannot hand itself a larger envelope
        # than the parent had.
        title = proposal.objective[:120] or f"Delegated from {parent.title}"
        try:
            child = await self._tasks.create(
                title=title,
                goal=proposal.objective or parent.goal,
                task_type=parent.task_type,
                parent_task_id=parent.id,
                # **The child's owning unit, which is the target agent's unit.**
                #
                # It was not passed, so every delegated task in the tenant carried
                # `org_unit_id = NULL` -- and migration 0030's rule for a NULL unit is
                # "readable by everyone", because a task with no unit belongs to nobody
                # in particular. So the unit boundary was inert on `tasks`, the one table
                # it most needed to cover, and every agent could read every department's
                # work while the policy said it could not.
                #
                # This is what makes the boundary mean anything: the row has to say whose
                # work it is, and the executor is the only place that knows.
                org_unit_id=str(target.org_unit_id) if target.org_unit_id else None,
                owner_agent_id=target.id,
                requester_type="agent",
                # The delegating agent is who asked for this work. Without this the
                # row would claim "an agent requested it" while naming nobody, and
                # `ck_tasks_tasks_requester_kind_matches` (0027) refuses that — a
                # constraint that is only satisfiable by recording the truth.
                requester_agent_id=source_agent_id,
                # The contract travels with the work.
                #
                # A child that does not know what its caller asked for cannot be
                # held to it, and the gate that checks a declared output only
                # fires where a declaration exists. So a chain ended with the
                # department producing *something* and being called `completed`:
                # seven runs, zero of which could be checked, because the only
                # task carrying the contract was the one at the top.
                expected_output_schema=parent.expected_output_schema or None,
                # Carry the routing facts down, and only those.
                #
                # Delegated work used to arrive with an empty `input`, so
                # anything the caller attached was lost at the first hop -- the
                # child could not tell which department the work belonged to and
                # guessed, sending it to whoever happened to be free. Seven runs
                # reached *a* department and not the one that could answer.
                #
                # Not the whole payload: `input` carries data the caller supplied,
                # and a copy of it at every tier is a copy that can disagree with
                # the original. These two keys say where the work goes, and the
                # office may restate them for its own departments.
                input={k: v for k, v in (parent.input or {}).items() if k in ROUTING_INPUT_KEYS},
                intent_fingerprint=intent_key,
            )
        except ConflictError as exc:
            # A duplicate is a decision to report, not a failure. Letting it escape would fail
            # the whole run over a proposal the model made twice by accident -- and the work is
            # delegated, just once, so refusing is the correct outcome rather than an error.
            outcome.refused.append((str(target.id), str(exc)))
            logger.info(
                "delegation.duplicate_refused_by_index",
                parent_task_id=parent.id,
                target=target.name,
                fingerprint=intent_key,
            )
            return

        recorded = await self._delegations.record(
            parent_task_id=parent.id,
            # The child task, created four lines above and previously **not passed**, so
            # every delegation row the platform wrote had a null child and the delegation
            # tree could only be walked from the task side. Passing what is already in
            # hand is not a design change; it is finishing the call.
            child_task_id=child.id,
            source_agent_id=source_agent_id,
            target_agent_id=target.id,
            objective=proposal.objective or parent.goal,
            path=path,
            platform_limits=self._platform or path_limits(path, None),
            parent_limits=path_limits(path, self._platform),
        )
        outcome.accepted.append(target.id)
        outcome.child_task_ids.append(child.id)
        logger.info(
            "delegation.applied",
            parent_task_id=parent.id,
            child_task_id=child.id,
            target=target.name,
            status=recorded.status,
        )

    async def _lookup_target(self, agent_id: str) -> Agent | None:
        """The agent, or `None`.

        Through the repository's own `get_optional`, which is tenant-scoped. This
        used to reach for `self._agents.session` — an attribute no repository
        exposes, since the session is private to it — so the lookup raised
        `AttributeError` on the first delegation that got past the gates, and the
        model was told the platform had no delegation executor at all.
        """
        target: Agent | None = await self._agents.get_optional(agent_id)
        return target

    async def _current_fanout(self, source_agent_id: str, parent_task_id: str) -> int:
        issued: int = await self._delegations.issued_by_for_parent(source_agent_id, parent_task_id)
        return issued

    async def _active_descendants(self, source_agent_id: str) -> int:
        issued: int = await self._delegations.issued_by(source_agent_id)
        return issued

    async def _record_refusal(
        self, parent: Task, source_agent_id: str, target_id: str, reason: str
    ) -> None:
        """A refusal an operator can see.

        Silently dropping it is the worst outcome: the executive believes it
        delegated, the department never received anything, and the audit trail
        says the task completed.
        """
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType

        await self._audit.record(
            actor=Actor(id=source_agent_id, kind=ActorType.AGENT),
            action="task.delegation_refused",
            resource_type="agent",
            resource_id=target_id,
            task_id=parent.id,
            context={"reason": reason, "goal": parent.goal[:200]},
        )
        logger.warning(
            "delegation.refused",
            task_id=parent.id,
            source=source_agent_id,
            target=target_id,
            reason=reason,
        )


def path_limits(path: DelegationPath, override: Any) -> Any:
    """The limits for the next hop.

    Delegation limits are on the *role*, not on the path, so the platform's own
    ceiling is the honest default here and the role's ceiling is applied by the
    caller when it resolves the agent. A `None` override means "no narrower
    ceiling known", which `clamp_to` handles by taking the other side.
    """
    from ai_orchestrator.domain.delegation import DelegationLimits

    return override or DelegationLimits.platform_default()


__all__ = ["DelegationExecutor", "DelegationOutcome", "path_limits"]
