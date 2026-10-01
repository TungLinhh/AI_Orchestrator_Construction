"""A department can call an agent that does not share this database.

The playbook's jobs are full of "ask the system of record" -- Odoo, a tender
portal, a bank. Those systems are not in this organisation and cannot be a row in
this database, which is exactly the case the `call_a2a_agent` tool exists for.

This drives the **real** thing: `examples/a2a_remote_agent.py` is spawned as a
separate process and reached over a socket, through the gateway, through the tool.
A stub would hide the handshake, the card check, the timeout and the fact that the
reply is untrusted -- which are the four things that make an external call
different from delegating to a colleague.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import pytest
import pytest_asyncio

from ai_orchestrator.a2a.gateway import A2AGateway
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType, RunMode
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "a2a_remote_agent.py"
PEER_NAME = "peer-analyst"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_port(port: int, proc: asyncio.subprocess.Process) -> None:
    for _ in range(200):
        if proc.returncode is not None:
            out = (await proc.stdout.read()).decode() if proc.stdout else ""
            pytest.fail(f"the peer agent exited before listening:\n{out[-2000:]}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.1)
    pytest.fail("the peer agent never started listening")


@pytest_asyncio.fixture
async def peer():  # type: ignore[no-untyped-def]
    """A real A2A agent in a real process, on a real socket."""
    port = _free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXAMPLE),
        "--port",
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(EXAMPLE.parents[1] / "src")},
    )
    try:
        await _wait_for_port(port, proc)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError:
            proc.kill()


@pytest_asyncio.fixture
async def seeded(tenant):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


class TestNamingAPeer:
    async def test_a_registered_peer_is_reachable_by_name(self, seeded, peer) -> None:  # type: ignore[no-untyped-def]
        """**The gap this closes.** `register` took a name and lookup took an id,
        and nothing connected the two -- so an agent could only call a peer whose
        id it already held, which is the one thing it cannot know. A model names a
        colleague; the platform resolves that, tenant-scoped."""
        gateway = A2AGateway(seeded.session, seeded.organization_id)
        result = await gateway.register(
            name=PEER_NAME, base_url=peer, require_tls=False, timeout_seconds=10
        )
        assert result.verified, result.problem

        resolved = await gateway.resolve(organization_id=seeded.organization_id, name=PEER_NAME)
        assert resolved == str(result.agent.id)

    async def test_an_unknown_name_is_a_lookup_failure_not_a_call(self, seeded, peer) -> None:  # type: ignore[no-untyped-def]
        gateway = A2AGateway(seeded.session, seeded.organization_id)
        with pytest.raises(LookupError, match="no remote agent named"):
            await gateway.resolve(organization_id=seeded.organization_id, name="nobody-here")

    async def test_an_unverified_peer_is_refused_rather_than_called(self, seeded) -> None:  # type: ignore[no-untyped-def]
        """A registration is a claim. Calling one that never proved its card would
        send this company's data to whatever answered the socket."""
        from ai_orchestrator.domain.errors import PreconditionError

        gateway = A2AGateway(seeded.session, seeded.organization_id)
        await gateway.register(
            name="unreachable-peer",
            base_url="http://127.0.0.1:1",
            require_tls=False,
            timeout_seconds=1,
        )
        with pytest.raises(PreconditionError, match="registered but not usable"):
            await gateway.resolve(organization_id=seeded.organization_id, name="unreachable-peer")


class TestTheCallItself:
    async def test_a_department_can_call_the_peer_and_get_its_answer(self, seeded, peer) -> None:  # type: ignore[no-untyped-def]
        gateway = A2AGateway(seeded.session, seeded.organization_id)
        await gateway.register(name=PEER_NAME, base_url=peer, require_tls=False, timeout_seconds=10)

        outcome = await gateway.call(
            actor=Actor(id="agt_test", kind=ActorType.AGENT),
            a2a_agent_id=await gateway.resolve(
                organization_id=seeded.organization_id, name=PEER_NAME
            ),
            prompt="Hãy tóm tắt báo cáo tiến độ dự án Bãi Trầm cho tôi",
            skill_id="summarise",
        )

        assert outcome.succeeded
        # The peer derives an answer rather than echoing, so this is evidence it
        # actually ran. An echo would return the question back.
        assert "words=" in outcome.text
        assert "Hãy tóm tắt" not in outcome.text
        assert outcome.remote_task_id

    async def test_the_call_is_recorded_as_an_outbound_side_effect(self, seeded, peer) -> None:  # type: ignore[no-untyped-def]
        """A send to a system this platform does not control must leave a record.

        Not for the audit's own sake: the next question anyone will ask is "what
        did we tell the outside, and when", and the answer has to be a query.
        """
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import AuditLog

        gateway = A2AGateway(seeded.session, seeded.organization_id)
        await gateway.register(name=PEER_NAME, base_url=peer, require_tls=False, timeout_seconds=10)
        await gateway.call(
            actor=Actor(id="agt_test", kind=ActorType.AGENT),
            a2a_agent_id=await gateway.resolve(
                organization_id=seeded.organization_id, name=PEER_NAME
            ),
            prompt="tóm tắt",
            skill_id="summarise",
        )
        actions = (
            (
                await seeded.session.execute(
                    select(AuditLog.action).where(
                        AuditLog.organization_id == seeded.organization_id,
                        AuditLog.action == "a2a.call",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert "a2a.call" in actions


class TestTheBoundary:
    def test_a_peer_is_not_a_delegate_target(self) -> None:
        """**The answer to "why is delegation a database and not A2A".**

        A remote agent lives in `a2a_agents`, has no `agents` row, no org unit,
        no autonomy level and no role. It therefore cannot be written into
        `tasks.owner_agent_id` or `delegations.target_agent_id` -- and that is not
        an omission. It cannot be granted a budget, held to an output contract,
        reviewed by an office, or given a task that survives a restart, because it
        is not in the organisation. Making it look like one would produce a
        department that could never be governed.
        """
        from ai_orchestrator.persistence.models import A2AAgent, Agent

        peer_columns = set(A2AAgent.__table__.c.keys())
        agent_columns = set(Agent.__table__.c.keys())
        # Nothing that makes a row a *member of the organisation*.
        for column in (
            "org_unit_id",
            "role_id",
            "autonomy_ceiling",
            "granted_level",
            "parent_agent_id",
            "lifecycle_status",
        ):
            assert column not in peer_columns, (
                f"a2a_agents has {column}, which would make a remote peer look "
                f"like a member of the organisation"
            )
            assert column in agent_columns

    def test_the_tool_is_registered_as_an_external_side_effect(self) -> None:
        """The class of effect decides the run-mode gate, and it is asserted here
        because it is the difference between "simulation sent an email" and it
        did not."""
        from ai_orchestrator.domain.enums import EffectClass
        from ai_orchestrator.tools.builtin import build_default_tools

        tools = {t.name: t for t in build_default_tools().all()}
        assert "call_a2a_agent" in tools, "the tool is not offered to any agent"
        tool = tools["call_a2a_agent"]
        assert tool.effect_class is EffectClass.EXTERNAL_SEND
        assert tool.is_idempotent is False, (
            "the peer is outside our transaction, so a retry may be a second "
            "request to a system that already acted on the first"
        )

    async def test_a_simulation_run_cannot_send_anything(self, seeded, peer) -> None:  # type: ignore[no-untyped-def]
        """`EXTERNAL_SIDE_EFFECT` + `SIMULATION` is a refusal.

        Asserted through the gateway rather than by reading the enum, because the
        gate is the thing: an exploratory curl, a test, or a demo must not be able
        to send a message to a system this platform does not control, and the only
        evidence is that the call was actually refused.
        """

        from ai_orchestrator.domain.policy import RuleBasedPolicyEngine, default_policy_set
        from ai_orchestrator.tools.builtin import build_default_tools
        from ai_orchestrator.tools.gateway import ToolGateway

        called: list[str] = []

        async def _never(**_: object) -> object:
            called.append("called")
            raise AssertionError("the peer was called during a simulation run")

        gateway = ToolGateway(
            build_default_tools(),
            policy_engine=RuleBasedPolicyEngine(default_policy_set().rules),
        )
        result = await gateway.invoke(
            organization_id=seeded.organization_id,
            actor=Actor(id="agt_test", kind=ActorType.AGENT),
            tool_name="call_a2a_agent",
            arguments={"agent_name": PEER_NAME, "prompt": "anything"},
            context_attributes={"a2a_call": _never},
            run_mode=RunMode.SIMULATION,
        )
        # A refusal, whichever way the gateway chose to report it: `result` is a
        # failure, or there is no result at all. Asserting the *effect* (the peer
        # was never reached) rather than the field name, which is a detail.
        assert result.result is None or not result.result.ok
        assert not called, "a simulation run reached the peer"
