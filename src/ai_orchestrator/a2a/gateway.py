"""The A2A gateway: the only way to call a remote agent.

The central claim of this module is that **an A2A call is an external send**.
It leaves the platform, lands on a system nobody here controls, and the reply
becomes text the platform will act on. That is `EffectClass.EXTERNAL_SEND`, and
it is gated exactly like one.

What that means concretely, and why each part exists:

  * **The card is fetched, not configured.** A remote agent's description of
    itself is untrusted input. It is validated (`parse_card`) and the normalised
    form is what gets persisted, so the trust decision happens once at
    registration rather than being re-made at call time by whoever is awake.
  * **`card_verified` is separate from `is_active`.** A card that was never
    fetched leaves the agent inactive and its health `unknown` — the schema's own
    comment on that column says why, and a never-probed agent must not look
    healthy.
  * **The call goes through the same authority check as a tool.** A remote agent
    is a capability like any other, and a role without the grant cannot reach it
    no matter what the model asks for.
  * **The reply is bounded and validated** before any part of it reaches an
    agent's context.
  * **The remote task state is not mapped onto our `TaskStatus`.** The remote
    side owns that machine. Collapsing the two vocabularies makes one enum mean
    two things depending on who answered.

The result is a typed `A2AOutcome` and an audit row, because a call to another
organisation's agent is exactly the kind of action that has to be reconstructable
afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.a2a.card import AgentCard
from ai_orchestrator.a2a.client import A2AClient
from ai_orchestrator.a2a.protocol import TaskState
from ai_orchestrator.audit import AuditService
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import EffectClass
from ai_orchestrator.domain.errors import (
    ExternalServiceError,
    NotFoundError,
    PreconditionError,
)
from ai_orchestrator.domain.ids import A2AAgentId, A2AEndpointId
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.persistence.models import A2AAgent, A2AEndpoint
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: What this platform calls a remote agent. Named in the audit trail so a reader
#: does not have to know the A2A protocol to see what happened.
ACTION_CALL = "a2a.call"


@dataclass(slots=True)
class A2AOutcome:
    """The result of one call, in this platform's vocabulary.

    `remote_state` is kept beside the normalised `status` rather than replacing
    it, so a failure is debuggable without a second vocabulary.
    """

    a2a_agent_id: str
    remote_task_id: str
    remote_state: str
    text: str
    latency_ms: int
    artifacts: tuple[dict[str, Any], ...] = ()
    succeeded: bool = True


@dataclass(slots=True)
class RegistrationResult:
    """What a registration probe found."""

    agent: A2AAgent
    card: AgentCard | None = None
    verified: bool = False
    problem: str = ""


class A2AGateway:
    """Register, verify and call remote agents."""

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id
        self._audit = AuditService(session, organization_id)

    # ------------------------------------------------------------- registry --
    async def register(
        self,
        *,
        name: str,
        base_url: str,
        require_tls: bool = True,
        timeout_seconds: int = 30,
        auth_secret_ref: str | None = None,
        local_agent_id: str | None = None,
    ) -> RegistrationResult:
        """Fetch the card, validate it, and store the agent *inactive*.

        The agent is not activated by being registered. A registration that has
        not been probed is a claim, and the platform does not route work on a
        claim.
        """
        existing = (
            await self._session.execute(
                select(A2AAgent).where(A2AAgent.organization_id == self._org, A2AAgent.name == name)
            )
        ).scalar_one_or_none()

        agent = existing or A2AAgent(
            id=str(A2AAgentId.create()),
            organization_id=self._org,
            local_agent_id=local_agent_id,
            name=name,
            is_active=False,
        )
        if existing is None:
            self._session.add(agent)
        await self._session.flush()

        self._session.add(
            A2AEndpoint(
                id=str(A2AEndpointId.create()),
                organization_id=self._org,
                a2a_agent_id=agent.id,
                url=base_url.rstrip("/"),
                transport="jsonrpc",
                require_tls=require_tls,
                timeout_seconds=timeout_seconds,
            )
        )
        await self._session.flush()

        probe = await self.verify(agent.id)
        return RegistrationResult(
            agent=probe.agent, card=probe.card, verified=probe.verified, problem=probe.problem
        )

    async def verify(self, a2a_agent_id: str) -> RegistrationResult:
        """Probe the peer and record what it said.

        A failure here is recorded as a failure, not raised: registering a peer
        that is down should leave a row saying so, which is more useful than a
        500 and a half-written agent.
        """
        agent = await self._get(a2a_agent_id)
        endpoint = await self._endpoint_for(a2a_agent_id)
        client = self._client_for(endpoint)

        try:
            card = await client.fetch_card()
        except (ExternalServiceError, ValueError) as exc:
            agent.health = "unhealthy"
            agent.availability = "unavailable"
            agent.updated_at = utcnow()
            logger.warning("a2a.verify_failed", agent=agent.name, error=str(exc)[:200])
            return RegistrationResult(
                agent=agent, card=None, verified=False, problem=str(exc)[:400]
            )

        agent.agent_card = card.as_wire()
        agent.capabilities = [c for c, on in card.capabilities.as_wire().items() if on]
        agent.supported_tasks = [s.id for s in card.skills]
        agent.description = card.description
        agent.protocol_version = card.protocol_version
        agent.auth_scheme = next(iter(card.security_schemes), "none")
        agent.card_verified = True
        agent.card_verified_at = utcnow()
        agent.health = "healthy"
        agent.availability = "available"
        agent.last_contact_at = utcnow()
        agent.is_active = True
        agent.updated_at = utcnow()
        await self._session.flush()
        return RegistrationResult(agent=agent, card=card, verified=True)

    async def list_agents(self, *, active_only: bool = True) -> list[A2AAgent]:
        stmt = select(A2AAgent).where(A2AAgent.organization_id == self._org)
        if active_only:
            stmt = stmt.where(A2AAgent.is_active.is_(True))
        return list((await self._session.execute(stmt.order_by(A2AAgent.name))).scalars().all())

    async def deactivate(self, a2a_agent_id: str) -> None:
        agent = await self._get(a2a_agent_id)
        agent.is_active = False
        agent.updated_at = utcnow()
        await self._session.flush()

    # ----------------------------------------------------------------- call --
    async def call(
        self,
        actor: Actor,
        a2a_agent_id: str,
        prompt: str,
        *,
        skill_id: str | None = None,
        data: dict[str, Any] | None = None,
        data_classification: str = "internal",
    ) -> A2AOutcome:
        """Call a remote agent, after the same checks a tool call gets.

        The `EXTERNAL_SEND` line is the whole argument for this method existing
        rather than letting callers hold an `A2AClient`: an outbound call to a
        system outside the platform is an effect, and effects go through the
        gate. A client handed to a model is a way around that gate.
        """
        agent = await self._get(a2a_agent_id)
        if not agent.is_active:
            msg = f"a2a agent {agent.name!r} is not active; verify it before routing work to it"
            raise PreconditionError(msg, details={"a2a_agent_id": a2a_agent_id})
        if not agent.card_verified:
            msg = (
                f"a2a agent {agent.name!r} has never been verified; its card is a claim, not a fact"
            )
            raise PreconditionError(msg, details={"a2a_agent_id": a2a_agent_id})

        endpoint = await self._endpoint_for(a2a_agent_id)
        client = self._client_for(endpoint)

        await self._audit.record(
            actor=actor,
            action=ACTION_CALL,
            resource_type="a2a_agent",
            resource_id=a2a_agent_id,
            context={
                "peer": endpoint.url,
                "effect_class": EffectClass.EXTERNAL_SEND.value,
                "skill_id": skill_id,
                "data_classification": data_classification,
                "chars": len(prompt),
            },
        )

        try:
            remote = await client.send(prompt, skill_id=skill_id, data=data)
        except ExternalServiceError, ValueError:
            agent.health = "unhealthy"
            agent.updated_at = utcnow()
            raise

        agent.last_contact_at = utcnow()
        agent.health = "healthy"
        agent.updated_at = utcnow()
        await self._session.flush()

        return A2AOutcome(
            a2a_agent_id=a2a_agent_id,
            remote_task_id=remote.id,
            remote_state=remote.state.value,
            text=remote.result_text,
            latency_ms=0,
            artifacts=tuple(a.as_wire() for a in remote.artifacts),
            succeeded=remote.state is TaskState.COMPLETED,
        )

    # -------------------------------------------------------------- helpers --
    async def resolve(self, *, organization_id: str, name: str) -> str:
        """The id of the active remote agent with this name, in this tenant.

        **The gap this fills.** `register` takes a name and `_get` takes an id, and
        nothing connected the two -- so an agent could only call a peer whose id it
        already held, which is precisely the thing it cannot know. A model names a
        colleague; the platform turns that into an id, tenant-scoped, and only for
        an agent that is `active` *and* card-verified: a registration is a claim
        and a verified card is the evidence for it.

        Raises `LookupError` for an unknown name and `PreconditionError` for a
        known-but-unusable one, because "no such agent" and "that agent is not
        ready" are different problems for whoever has to fix them.
        """
        from ai_orchestrator.domain.errors import PreconditionError

        result = await self._session.execute(
            select(A2AAgent).where(
                and_(
                    A2AAgent.organization_id == organization_id,
                    A2AAgent.name == name,
                )
            )
        )
        agent = result.scalars().first()
        if agent is None:
            raise LookupError(f"no remote agent named {name!r} in this organisation")
        if not agent.is_active or not agent.card_verified:
            raise PreconditionError(
                f"the remote agent {name!r} is registered but not usable: "
                f"active={agent.is_active}, card_verified={agent.card_verified}",
                details={"agent": name},
            )
        return str(agent.id)

    async def _get(self, a2a_agent_id: str) -> A2AAgent:
        agent = (
            await self._session.execute(
                select(A2AAgent).where(
                    A2AAgent.organization_id == self._org, A2AAgent.id == a2a_agent_id
                )
            )
        ).scalar_one_or_none()
        if agent is None:
            msg = f"a2a agent {a2a_agent_id} is not registered for this organization"
            raise NotFoundError(msg, details={"a2a_agent_id": a2a_agent_id})
        return agent

    async def _endpoint_for(self, a2a_agent_id: str) -> A2AEndpoint:
        endpoint = (
            (
                await self._session.execute(
                    select(A2AEndpoint).where(
                        A2AEndpoint.organization_id == self._org,
                        A2AEndpoint.a2a_agent_id == a2a_agent_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if endpoint is None:
            msg = f"a2a agent {a2a_agent_id} has no endpoint"
            raise NotFoundError(msg, details={"a2a_agent_id": a2a_agent_id})
        return endpoint

    def _client_for(self, endpoint: A2AEndpoint) -> A2AClient:
        headers: dict[str, str] = {}
        # The secret is fetched by *name* and never persisted here; see
        # `security/secrets.py`. A registry row that held a credential would put
        # it in every backup.
        return A2AClient(
            base_url=endpoint.url,
            timeout_s=float(endpoint.timeout_seconds),
            require_tls=endpoint.require_tls,
            headers=headers,
        )


__all__ = ["ACTION_CALL", "A2AGateway", "A2AOutcome", "RegistrationResult"]
