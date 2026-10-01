"""Organization, agent registry and capability repositories.

Every query here is scoped by `organization_id` explicitly, in addition to the
RLS policy. The duplication is deliberate: RLS is a backstop for a forgotten
predicate, not a substitute for one. A repository that relied on RLS alone would
work correctly until somebody ran a diagnostic query as the owner role, and then
it would leak.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import ARRAY, Select, String, and_, cast, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.enums import AgentLifecycleStatus
from ai_orchestrator.domain.errors import (
    ConflictError,
    NotFoundError,
    PreconditionError,
    ValidationError,
)
from ai_orchestrator.domain.ids import (
    AgentDefinitionId,
    AgentId,
    OrganizationId,
    OrgUnitId,
    RoleId,
    ToolId,
)
from ai_orchestrator.domain.state_machines import Transition, next_agent_status
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import (
    Agent,
    AgentDefinition,
    AgentRelationship,
    AgentSkillBinding,
    AgentToolBinding,
    Organization,
    OrgUnit,
    Role,
    Skill,
    Tool,
)


class OrganizationRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        name: str,
        slug: str,
        description: str = "",
        settings: dict[str, Any] | None = None,
        spend_cap_usd: float | None = None,
    ) -> Organization:
        if not name.strip():
            msg = "organization name must not be empty"
            raise ValidationError(msg, details={"field": "name"})
        if not slug.strip() or " " in slug:
            msg = f"slug {slug!r} must be a non-empty token without spaces"
            raise ValidationError(msg, details={"field": "slug"})

        org = Organization(
            id=str(OrganizationId.create()),
            name=name.strip(),
            slug=slug.strip().lower(),
            description=description,
            settings=settings or {},
            spend_cap_usd=spend_cap_usd,
        )
        self._session.add(org)
        await self._session.flush()
        return org

    async def get(self, org_id: str) -> Organization:
        org = await self._session.get(Organization, org_id)
        if org is None:
            msg = f"organization not found: {org_id}"
            raise NotFoundError(msg, resource_type="organization", resource_id=org_id)
        return org

    async def get_by_slug(self, slug: str) -> Organization:
        result = await self._session.execute(
            select(Organization).where(Organization.slug == slug.lower())
        )
        org = result.scalar_one_or_none()
        if org is None:
            msg = f"no organization with slug {slug!r}"
            raise NotFoundError(msg, resource_type="organization", resource_id=slug)
        return org

    async def list_all(self) -> Sequence[Organization]:
        result = await self._session.execute(select(Organization).order_by(Organization.name))
        rows: Sequence[Organization] = result.scalars().all()
        return rows

    async def update_settings(self, org_id: str, patch: dict[str, Any]) -> Organization:
        org = await self.get(org_id)
        for key, value in patch.items():
            if not hasattr(org, key):
                msg = f"unknown organization field: {key}"
                raise ValidationError(msg, details={"field": key})
            setattr(org, key, value)
        org.updated_at = utcnow()
        await self._session.flush()
        return org


class OrgUnitRepository:
    """The organization tree.

    A node is not an agent. `Sales Department` is a unit; `Sales Director Agent`
    is an agent that leads it. Conflating them produces a structure where a
    vacant department is indistinguishable from a department with no director.
    """

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        name: str,
        slug: str,
        parent_id: str | None = None,
        unit_type: str = "department",
        purpose: str = "",
        head_agent_id: str | None = None,
    ) -> OrgUnit:
        if not name.strip():
            msg = "organizational unit name must not be empty"
            raise ValidationError(msg, details={"field": "name"})

        depth = 0
        parent_path = "/"
        if parent_id is not None:
            parent = await self.get(parent_id)
            depth = parent.depth + 1
            parent_path = parent.path
            if depth > 12:
                msg = f"organizational tree is deeper than 12 levels at {parent_id}"
                raise PreconditionError(msg, details={"max_depth": 12})

        unit = OrgUnit(
            id=str(OrgUnitId.create()),
            organization_id=self._org,
            parent_id=parent_id,
            name=name.strip(),
            slug=slug.strip().lower(),
            unit_type=unit_type,
            purpose=purpose,
            head_agent_id=head_agent_id,
            depth=depth,
            # Materialised path. Subtree queries are then a single LIKE rather
            # than a recursive CTE, which matters once the tree is deep.
            path=f"{parent_path.rstrip('/')}/{slug.strip().lower()}",
        )
        self._session.add(unit)
        await self._session.flush()
        return unit

    async def get(self, unit_id: str) -> OrgUnit:
        result = await self._session.execute(
            select(OrgUnit).where(and_(OrgUnit.id == unit_id, OrgUnit.organization_id == self._org))
        )
        unit = result.scalar_one_or_none()
        if unit is None:
            msg = f"organizational unit not found: {unit_id}"
            raise NotFoundError(msg, resource_type="org_unit", resource_id=unit_id)
        return unit

    async def list_children(self, parent_id: str | None) -> Sequence[OrgUnit]:
        stmt = select(OrgUnit).where(OrgUnit.organization_id == self._org)
        if parent_id is None:
            stmt = stmt.where(OrgUnit.parent_id.is_(None))
        else:
            stmt = stmt.where(OrgUnit.parent_id == parent_id)
        result = await self._session.execute(stmt.order_by(OrgUnit.name))
        return result.scalars().all()

    async def subtree(self, root_id: str) -> Sequence[OrgUnit]:
        root = await self.get(root_id)
        result = await self._session.execute(
            select(OrgUnit)
            .where(
                and_(
                    OrgUnit.organization_id == self._org,
                    OrgUnit.path.startswith(f"{root.path.rstrip('/')}/"),
                )
            )
            .order_by(OrgUnit.path)
        )
        return result.scalars().all()

    async def tree(self) -> Sequence[OrgUnit]:
        result = await self._session.execute(
            select(OrgUnit).where(OrgUnit.organization_id == self._org).order_by(OrgUnit.path)
        )
        return result.scalars().all()

    async def reparent(self, unit_id: str, new_parent_id: str | None) -> OrgUnit:
        """Move a unit, rewriting the materialised path of the whole subtree.

        Rejected when the new parent is inside the moved subtree: that would
        detach the branch from the tree and leave agents pointing at a unit with
        no path to the root.
        """
        unit = await self.get(unit_id)
        if new_parent_id == unit_id:
            msg = "a unit cannot be its own parent"
            raise PreconditionError(msg, resource_type="org_unit", resource_id=unit_id)

        if new_parent_id is None:
            new_path, new_depth = "/", 0
        else:
            new_parent = await self.get(new_parent_id)
            if new_parent.path.startswith(f"{unit.path.rstrip('/')}/"):
                msg = "cannot move a unit beneath one of its own descendants"
                raise PreconditionError(msg, details={"unit": unit_id, "new_parent": new_parent_id})
            new_path = f"{new_parent.path.rstrip('/')}/{unit.slug}"
            new_depth = new_parent.depth + 1

        descendants = await self.subtree(unit_id)
        old_prefix = unit.path.rstrip("/")
        for node in (unit, *descendants):
            node.path = new_path + node.path[len(old_prefix) :]
            node.depth = new_depth + (node.depth - unit.depth)
        unit.parent_id = new_parent_id
        unit.updated_at = utcnow()
        await self._session.flush()
        return unit

    async def set_head(self, unit_id: str, agent_id: str | None) -> OrgUnit:
        unit = await self.get(unit_id)
        unit.head_agent_id = agent_id
        unit.updated_at = utcnow()
        await self._session.flush()
        return unit


class RoleRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        name: str,
        description: str = "",
        authority_profile: dict[str, Any] | None = None,
        parent_role_id: str | None = None,
        max_autonomy: str = "l2_parent_review",
        may_delegate_to_peers: bool = False,
        may_spawn_subagents: bool = False,
        max_delegation_depth: int = 2,
        allowed_capabilities: list[str] | None = None,
        is_system_role: bool = False,
    ) -> Role:
        role = Role(
            id=str(RoleId.create()),
            organization_id=self._org,
            name=name.strip(),
            description=description,
            authority_profile=authority_profile or {},
            parent_role_id=parent_role_id,
            max_autonomy=max_autonomy,
            may_delegate_to_peers=may_delegate_to_peers,
            may_spawn_subagents=may_spawn_subagents,
            max_delegation_depth=max_delegation_depth,
            allowed_capabilities=allowed_capabilities or [],
            is_system_role=is_system_role,
        )
        self._session.add(role)
        await self._session.flush()
        return role

    async def get(self, role_id: str) -> Role:
        result = await self._session.execute(
            select(Role).where(and_(Role.id == role_id, Role.organization_id == self._org))
        )
        role = result.scalar_one_or_none()
        if role is None:
            msg = f"role not found: {role_id}"
            raise NotFoundError(msg, resource_type="role", resource_id=role_id)
        return role

    async def list(self) -> Sequence[Role]:
        result = await self._session.execute(
            select(Role).where(Role.organization_id == self._org).order_by(Role.name)
        )
        return result.scalars().all()


class AgentRepository:
    """The agent registry. The canonical control-plane entity for agents."""

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        name: str,
        role_id: str,
        definition_id: str | None = None,
        org_unit_id: str | None = None,
        parent_agent_id: str | None = None,
        description: str = "",
        autonomy_level: str = "l2_parent_review",
        runtime_adapter: str = "pydantic_ai",
        model_profile: str = "default",
        budget_limit_usd: float | None = None,
        budget_limit_tokens: int | None = None,
        capabilities: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Agent:
        # A parent must exist, be in this org, and be an ancestor. Validating here
        # rather than at the FK keeps the error message useful.
        if parent_agent_id is not None:
            parent = await self.get(parent_agent_id)
            if parent.parent_agent_id == parent_agent_id:
                msg = "agent cannot be its own parent"
                raise PreconditionError(msg, resource_type="agent", resource_id=parent_agent_id)
        if org_unit_id is not None:
            unit = await self._session.execute(
                select(OrgUnit).where(
                    and_(OrgUnit.id == org_unit_id, OrgUnit.organization_id == self._org)
                )
            )
            if unit.scalar_one_or_none() is None:
                msg = f"organizational unit not found in this organization: {org_unit_id}"
                raise NotFoundError(msg, resource_type="org_unit", resource_id=org_unit_id)

        agent = Agent(
            id=str(AgentId.create()),
            organization_id=self._org,
            org_unit_id=org_unit_id,
            definition_id=definition_id or "",
            role_id=role_id,
            parent_agent_id=parent_agent_id,
            name=name.strip(),
            description=description,
            autonomy_level=autonomy_level,
            runtime_adapter=runtime_adapter,
            model_profile=model_profile,
            budget_limit_usd=budget_limit_usd,
            budget_limit_tokens=budget_limit_tokens,
            capabilities=capabilities or [],
            metadata_=metadata or {},
        )
        self._session.add(agent)
        await self._session.flush()
        return agent

    async def get(self, agent_id: str) -> Agent:
        result = await self._session.execute(
            select(Agent).where(and_(Agent.id == agent_id, Agent.organization_id == self._org))
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            msg = f"agent not found: {agent_id}"
            raise NotFoundError(msg, resource_type="agent", resource_id=agent_id)
        return agent

    async def get_optional(self, agent_id: str) -> Agent | None:
        result = await self._session.execute(
            select(Agent).where(and_(Agent.id == agent_id, Agent.organization_id == self._org))
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        lifecycle_status: str | None = None,
        org_unit_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Agent]:
        stmt: Select[Any] = select(Agent).where(Agent.organization_id == self._org)
        if lifecycle_status is not None:
            stmt = stmt.where(Agent.lifecycle_status == lifecycle_status)
        if org_unit_id is not None:
            stmt = stmt.where(Agent.org_unit_id == org_unit_id)
        result = await self._session.execute(stmt.order_by(Agent.name).limit(limit).offset(offset))
        rows: Sequence[Agent] = result.scalars().all()
        return rows

    async def children(self, parent_agent_id: str) -> Sequence[Agent]:
        result = await self._session.execute(
            select(Agent)
            .where(
                and_(
                    Agent.organization_id == self._org,
                    Agent.parent_agent_id == parent_agent_id,
                )
            )
            .order_by(Agent.name)
        )
        return result.scalars().all()

    async def ancestors(self, agent_id: str) -> Sequence[Agent]:
        """Walk up the parent chain.

        Bounded at 32 hops. A cycle in the parent column would otherwise hang
        the request, and a cycle there means the data is already corrupt.
        """
        chain: list[Agent] = []
        current_id: str | None = agent_id
        for _ in range(32):
            if current_id is None:
                break
            result = await self._session.execute(
                select(Agent).where(
                    and_(Agent.id == current_id, Agent.organization_id == self._org)
                )
            )
            agent = result.scalar_one_or_none()
            if agent is None or agent.parent_agent_id is None:
                break
            chain.append(agent)
            current_id = agent.parent_agent_id
        return chain

    async def transition(self, agent_id: str, event: Transition) -> Agent:
        """Move the lifecycle, using the state machine.

        The transition map is the single source of truth, so the API, the CLI
        and the workflow all reject the same illegal jumps.
        """
        agent = await self.get(agent_id)
        target = next_agent_status(
            AgentLifecycleStatus(agent.lifecycle_status),
            event,
        )
        agent.lifecycle_status = target.value
        if target.value == "retired":
            agent.retired_at = utcnow()
        agent.updated_at = utcnow()
        await self._session.flush()
        return agent

    async def set_runtime_status(
        self, agent_id: str, runtime_status: str, health: str | None = None
    ) -> Agent:
        """Operational status, separate from lifecycle.

        An agent can be ACTIVE as a business fact and BUSY as an operational one.
        Merging them produces the nonsense pair "agent ready, task blocked", which
        is neither true nor false.
        """
        agent = await self.get(agent_id)
        agent.runtime_status = runtime_status
        if health is not None:
            agent.health = health
        agent.last_heartbeat_at = utcnow()
        agent.updated_at = utcnow()
        await self._session.flush()
        return agent

    async def heartbeat(self, agent_id: str) -> None:
        """Cheap liveness write.

        Uses a targeted UPDATE rather than loading the ORM object: this runs on
        every health tick for every agent, and an identity-map round trip at that
        frequency is a real cost for no benefit.
        """
        await self._session.execute(
            update(Agent)
            .where(and_(Agent.id == agent_id, Agent.organization_id == self._org))
            .values(last_heartbeat_at=utcnow())
        )

    async def adjust_load(
        self, agent_id: str, *, active_delta: int = 0, queue_delta: int = 0
    ) -> None:
        """Increment load counters, clamped at zero.

        Clamping matters: a double-decrement from a retried completion would
        otherwise leave a negative counter, and a negative load reads as "this
        agent is idle" when it is actually saturated.
        """
        await self._session.execute(
            text(
                """
                UPDATE agents
                   SET active_tasks = GREATEST(0, active_tasks + :active_delta),
                       queue_depth = GREATEST(0, queue_depth + :queue_delta),
                       updated_at = now()
                 WHERE id = :agent_id AND organization_id = :org
                """
            ),
            {
                "active_delta": active_delta,
                "queue_delta": queue_delta,
                "agent_id": agent_id,
                "org": self._org,
            },
        )

    async def discover(
        self,
        *,
        required_capabilities: Sequence[str] = (),
        only_available: bool = True,
        exclude: Sequence[str] = (),
        limit: int = 20,
    ) -> Sequence[Agent]:
        """Find agents that can take work.

        Capability matching is done in SQL with the `&&` overlap operator, so a
        discovery query stays a single index scan instead of loading every agent
        and filtering in Python. `exclude` carries the current delegation path,
        which is what stops discovery from proposing an agent that would close
        a cycle.
        """
        stmt = select(Agent).where(
            and_(
                Agent.organization_id == self._org,
                Agent.lifecycle_status.in_(["active", "degraded"]),
            )
        )
        if required_capabilities:
            # `&&` is array overlap: "has at least one of these capabilities".
            # The column is `text[]` rather than jsonb precisely so this operator
            # exists and can use the GIN index.
            stmt = stmt.where(
                Agent.capabilities.op("&&")(cast(list(required_capabilities), ARRAY(String)))
            )
        if only_available:
            stmt = stmt.where(Agent.runtime_status.in_(["idle", "ready"]))
        if exclude:
            stmt = stmt.where(Agent.id.not_in(list(exclude)))
        # Least loaded first, then fastest. Availability and load beat raw
        # latency when both are eligible.
        stmt = stmt.order_by(Agent.active_tasks, Agent.estimated_latency_ms).limit(limit)
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def count_by_status(self) -> dict[str, int]:
        result = await self._session.execute(
            select(Agent.lifecycle_status, func.count())
            .where(Agent.organization_id == self._org)
            .group_by(Agent.lifecycle_status)
        )
        return {status: count for status, count in result.all()}


class AgentDefinitionRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def create(
        self,
        *,
        name: str,
        role_id: str,
        system_instructions: str,
        model_profile: str = "default",
        allowed_child_roles: list[str] | None = None,
        allowed_task_types: list[str] | None = None,
        behavior_config: dict[str, Any] | None = None,
        memory_policy: dict[str, Any] | None = None,
        escalation_policy: dict[str, Any] | None = None,
        prompt_version: str = "1",
        created_by: str | None = None,
    ) -> AgentDefinition:
        definition = AgentDefinition(
            id=str(AgentDefinitionId.create()),
            organization_id=self._org,
            name=name.strip(),
            role_id=role_id,
            system_instructions=system_instructions,
            model_profile=model_profile,
            allowed_child_roles=allowed_child_roles or [],
            allowed_task_types=allowed_task_types or [],
            behavior_config=behavior_config or {},
            memory_policy=memory_policy or {},
            escalation_policy=escalation_policy or {},
            prompt_version=prompt_version,
            created_by=created_by,
        )
        self._session.add(definition)
        await self._session.flush()
        return definition

    async def get(self, definition_id: str) -> AgentDefinition:
        result = await self._session.execute(
            select(AgentDefinition).where(
                and_(
                    AgentDefinition.id == definition_id,
                    AgentDefinition.organization_id == self._org,
                )
            )
        )
        definition = result.scalar_one_or_none()
        if definition is None:
            msg = f"agent definition not found: {definition_id}"
            raise NotFoundError(msg, resource_type="agent_definition", resource_id=definition_id)
        return definition

    async def latest_version(self, name: str) -> AgentDefinition | None:
        result = await self._session.execute(
            select(AgentDefinition)
            .where(and_(AgentDefinition.organization_id == self._org, AgentDefinition.name == name))
            .order_by(AgentDefinition.version.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class AgentCapabilityRepository:
    """Skill and tool bindings.

    A binding is a grant, and like every grant it is default-deny: an agent with
    no row for a tool does not have that tool. `authorized_tools` intersects
    the role's grants with the bindings, so an operator can narrow a role
    without editing every agent.
    """

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def bind_skill(
        self,
        *,
        agent_id: str,
        skill_id: str,
        skill_version_id: str | None = None,
        constraints: dict[str, Any] | None = None,
        granted_by: str | None = None,
    ) -> AgentSkillBinding:
        existing = await self._session.execute(
            select(AgentSkillBinding).where(
                and_(
                    AgentSkillBinding.agent_id == agent_id,
                    AgentSkillBinding.skill_id == skill_id,
                )
            )
        )
        binding = existing.scalar_one_or_none()
        if binding is not None:
            # Rebinding updates the pinned version rather than inserting a
            # duplicate: two rows for one (agent, skill) would make
            # "which version is this agent using?" ambiguous.
            binding.skill_version_id = skill_version_id
            binding.constraints = constraints or {}
            binding.is_enabled = True
            await self._session.flush()
            return binding

        binding = AgentSkillBinding(
            id=str(AgentId.create()),
            organization_id=self._org,
            agent_id=agent_id,
            skill_id=skill_id,
            skill_version_id=skill_version_id,
            constraints=constraints or {},
            granted_by=granted_by,
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def bind_tool(
        self,
        *,
        agent_id: str,
        tool_id: str,
        max_risk: str = "medium",
        requires_approval: bool = False,
        rate_limit_override: int | None = None,
        granted_by: str | None = None,
    ) -> AgentToolBinding:
        existing = await self._session.execute(
            select(AgentToolBinding).where(
                and_(AgentToolBinding.agent_id == agent_id, AgentToolBinding.tool_id == tool_id)
            )
        )
        binding = existing.scalar_one_or_none()
        if binding is not None:
            binding.max_risk = max_risk
            binding.requires_approval = requires_approval or binding.requires_approval
            binding.rate_limit_override = rate_limit_override
            binding.is_enabled = True
            await self._session.flush()
            return binding

        binding = AgentToolBinding(
            id=str(ToolId.create()),
            organization_id=self._org,
            agent_id=agent_id,
            tool_id=tool_id,
            max_risk=max_risk,
            requires_approval=requires_approval,
            rate_limit_override=rate_limit_override,
            granted_by=granted_by,
        )
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def skill_ids_for(self, agent_id: str) -> Sequence[str]:
        result = await self._session.execute(
            select(AgentSkillBinding.skill_id).where(
                and_(AgentSkillBinding.agent_id == agent_id, AgentSkillBinding.is_enabled.is_(True))
            )
        )
        return list(result.scalars().all())

    async def tool_bindings_for(self, agent_id: str) -> Sequence[AgentToolBinding]:
        result = await self._session.execute(
            select(AgentToolBinding).where(
                and_(
                    AgentToolBinding.agent_id == agent_id,
                    AgentToolBinding.is_enabled.is_(True),
                )
            )
        )
        return result.scalars().all()

    async def revoke_tool(self, agent_id: str, tool_id: str) -> None:
        """Disable rather than delete.

        The grant is part of the audit trail: an operator needs to be able to see
        that an agent once had a destructive tool and when it was taken away.
        """
        await self._session.execute(
            update(AgentToolBinding)
            .where(
                and_(
                    AgentToolBinding.agent_id == agent_id,
                    AgentToolBinding.tool_id == tool_id,
                )
            )
            .values(is_enabled=False)
        )

    async def capabilities_of(self, agent_id: str) -> list[str]:
        """The agent's effective tool and skill names.

        Used by discovery: an agent that has a `web_search` binding can be found
        by "who can search the web" without joining three tables at call time.
        """
        tool_names = await self._session.execute(
            select(Tool.name)
            .join(AgentToolBinding, AgentToolBinding.tool_id == Tool.id)
            .where(
                and_(
                    AgentToolBinding.agent_id == agent_id,
                    AgentToolBinding.is_enabled.is_(True),
                    Tool.is_active.is_(True),
                )
            )
        )
        skill_names = await self._session.execute(
            select(Skill.name)
            .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
            .where(
                and_(
                    AgentSkillBinding.agent_id == agent_id,
                    AgentSkillBinding.is_enabled.is_(True),
                )
            )
        )
        return [*tool_names.scalars().all(), *skill_names.scalars().all()]


class AgentRelationshipRepository:
    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def link(
        self,
        *,
        source_agent_id: str,
        target_agent_id: str,
        relationship_type: str = "peer",
        can_delegate: bool = False,
        max_risk: str = "low",
        notes: str = "",
    ) -> AgentRelationship:
        if source_agent_id == target_agent_id:
            msg = "an agent cannot be related to itself"
            raise PreconditionError(msg, details={"agent_id": source_agent_id})
        existing = await self._session.execute(
            select(AgentRelationship).where(
                and_(
                    AgentRelationship.source_agent_id == source_agent_id,
                    AgentRelationship.target_agent_id == target_agent_id,
                    AgentRelationship.relationship_type == relationship_type,
                )
            )
        )
        if existing.scalar_one_or_none() is not None:
            msg = f"relationship already exists between {source_agent_id} and {target_agent_id}"
            raise ConflictError(msg, details={"source": source_agent_id, "target": target_agent_id})

        rel = AgentRelationship(
            id=str(AgentId.create()),
            organization_id=self._org,
            source_agent_id=source_agent_id,
            target_agent_id=target_agent_id,
            relationship_type=relationship_type,
            can_delegate=can_delegate,
            max_risk=max_risk,
            notes=notes,
        )
        self._session.add(rel)
        await self._session.flush()
        return rel

    async def delegable_targets(self, source_agent_id: str) -> Sequence[AgentRelationship]:
        """Peers this agent is explicitly allowed to delegate to.

        Default-deny: no relationship row means no peer delegation, so a
        hierarchy is not silently a mesh.
        """
        result = await self._session.execute(
            select(AgentRelationship).where(
                and_(
                    AgentRelationship.source_agent_id == source_agent_id,
                    AgentRelationship.can_delegate.is_(True),
                )
            )
        )
        return result.scalars().all()


__all__ = [
    "AgentCapabilityRepository",
    "AgentDefinitionRepository",
    "AgentRelationshipRepository",
    "AgentRepository",
    "OrgUnitRepository",
    "OrganizationRepository",
    "RoleRepository",
]
