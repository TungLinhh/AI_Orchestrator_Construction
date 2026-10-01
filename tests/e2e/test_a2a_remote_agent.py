"""Acceptance scenario 2: agent -> A2A -> remote agent in a separate process.

The remote agent is a real subprocess speaking HTTP over a socket, spawned the
same way the MCP demo server is. An in-process double cannot demonstrate this
scenario: shared memory, shared event loop, shared globals. What is under test is
the boundary, so the boundary has to be real.

Also covered, because a remote agent is untrusted input:

  * a card claiming the wrong protocol version is refused;
  * a card claiming a capability nobody has read is refused, not ignored;
  * an unverified agent cannot be called;
  * a plaintext endpoint off loopback is refused before anything is sent;
  * an oversized reply is refused before it is parsed;
  * a JSON-RPC error becomes our error rather than a missing `result`.
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

from ai_orchestrator.a2a.card import PROTOCOL_VERSION, parse_card
from ai_orchestrator.a2a.client import A2AClient
from ai_orchestrator.a2a.gateway import A2AGateway
from ai_orchestrator.a2a.protocol import (
    MAX_PAYLOAD_BYTES,
    JsonRpcResponse,
    TaskState,
)
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import PreconditionError, ValidationError
from ai_orchestrator.persistence.models import AuditLog

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "a2a_remote_agent.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _wait_for_port(
    host: str, port: int, *, proc: object = None, timeout_s: float = 25.0
) -> None:
    """Wait for the peer, or fail with whatever it printed.

    The child's output is the diagnosis. "never started listening" without it
    sends the reader to the test file instead of to a syntax error in the
    example they are trying to run.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and getattr(proc, "returncode", None) is not None:
            output = ""
            if getattr(proc, "stdout", None) is not None:
                output = (await proc.stdout.read()).decode("utf-8", "replace")[:800]
            msg = f"the remote agent exited with {proc.returncode} before listening:\n{output}"
            raise AssertionError(msg)
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return
        except OSError:
            await asyncio.sleep(0.15)
    msg = f"the remote agent never started listening on {host}:{port}"
    raise AssertionError(msg)


@pytest_asyncio.fixture
async def remote_agent():
    """A real A2A agent, in its own process, on a free port."""
    port = _free_port()
    # The async API, not `subprocess.Popen`: the client that talks to this
    # process runs on the same event loop, and a blocking spawn plus a blocking
    # teardown would stall it.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(EXAMPLE),
        "--port",
        str(port),
        "--log-level",
        "error",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        await _wait_for_port("127.0.0.1", port, proc=proc)
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except TimeoutError, ProcessLookupError:  # pragma: no cover
            proc.kill()


def _actor(org: str) -> Actor:
    return Actor(id="agt_local", kind=ActorType.AGENT, organization_id=org)


class TestCardValidation:
    def test_a_wrong_protocol_version_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="protocol"):
            parse_card(
                {"name": "old", "url": "https://x.invalid", "protocolVersion": "0.1.0"},
                card_url="https://x.invalid",
            )

    def test_an_unknown_capability_is_refused_not_ignored(self) -> None:
        """Dropping it would let a card claim something the platform then
        believes it checked."""
        with pytest.raises(ValidationError, match="unknown capabilities"):
            parse_card(
                {
                    "name": "x",
                    "url": "https://x.invalid",
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"telepathy": True},
                },
                card_url="https://x.invalid",
            )

    def test_an_unsupported_security_scheme_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not one this platform can honour"):
            parse_card(
                {
                    "name": "x",
                    "url": "https://x.invalid",
                    "protocolVersion": PROTOCOL_VERSION,
                    "securitySchemes": {"oauth": {"type": "oauth2"}},
                },
                card_url="https://x.invalid",
            )

    def test_a_good_card_normalises(self) -> None:
        card = parse_card(
            {
                "name": "x",
                "description": "d",
                "url": "https://x.invalid",
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"streaming": True},
                "skills": [{"id": "summarise", "name": "Summarise", "tags": ["text"]}],
            },
            card_url="https://x.invalid",
        )
        assert card.supports("streaming")
        assert card.skill("summarise") is not None
        assert card.skill("absent") is None


class TestClientBoundaries:
    async def test_a_plaintext_endpoint_off_loopback_is_refused(self) -> None:
        """An agent card carries tool definitions and results; a plaintext hop
        lets anyone on the path rewrite what the agent is told."""
        with pytest.raises(ValidationError, match="plaintext"):
            A2AClient(base_url="http://peer.internal:8931", require_tls=True)

    async def test_plaintext_loopback_is_allowed(self) -> None:
        """A local test process speaks HTTP, and pretending otherwise would make
        the scenario untestable without a certificate authority."""
        assert A2AClient(base_url="http://127.0.0.1:8931", require_tls=True)

    async def test_an_oversized_reply_is_refused_before_parsing(self) -> None:
        with pytest.raises(ValidationError, match="cap"):
            JsonRpcResponse.parse(b"x" * (MAX_PAYLOAD_BYTES + 1), peer="peer")

    async def test_a_jsonrpc_error_becomes_our_error(self) -> None:
        """Without this the caller sees a missing `result` and reports a protocol
        failure instead of what the peer actually said."""
        from ai_orchestrator.domain.errors import ExternalServiceError

        body = json.dumps(
            {"jsonrpc": "2.0", "id": "1", "error": {"code": -32000, "message": "nope"}}
        ).encode()
        with pytest.raises(ExternalServiceError, match="nope"):
            JsonRpcResponse.parse(body, peer="peer").unwrap(peer="peer")


class TestRemoteProcess:
    async def test_the_card_is_fetched_from_the_live_process(self, remote_agent: str) -> None:
        card = await A2AClient(base_url=remote_agent).fetch_card()
        assert card.name == "remote-echo-analyst"
        assert card.skill("summarise") is not None
        assert card.protocol_version == PROTOCOL_VERSION

    async def test_a_message_returns_a_task_with_a_real_answer(self, remote_agent: str) -> None:
        remote = await A2AClient(base_url=remote_agent).send(
            "the quick brown fox jumps over the lazy dog", skill_id="summarise"
        )
        assert remote.state is TaskState.COMPLETED
        # A real derivation, not an echo: a server that returned the request
        # would prove the transport and nothing else.
        assert "words=9" in remote.result_text
        assert "chars=43" in remote.result_text
        assert "the quick brown fox" not in remote.result_text
        # `longest` is not pinned: "quick" and "jumps" are both five characters
        # and which one wins is an arbitrary tie-break. Asserting it would make
        # the test brittle without testing anything.


class TestAcceptanceScenario2:
    async def test_local_agent_calls_a_remote_agent_in_another_process(
        self, tenant, remote_agent: str
    ) -> None:
        """The scenario end to end: register, verify, call, audit."""
        gateway = A2AGateway(tenant.session, tenant.organization_id)

        registration = await gateway.register(
            name="remote-echo-analyst",
            base_url=remote_agent,
            # Loopback, so TLS is not required. Off loopback the same call is
            # refused by the client.
            require_tls=True,
        )
        assert registration.verified, registration.problem
        agent = registration.agent
        assert agent.card_verified is True
        assert agent.is_active is True
        assert agent.health == "healthy"
        assert agent.supported_tasks == ["summarise"]

        outcome = await gateway.call(
            _actor(tenant.organization_id),
            agent.id,
            "summarise the quarterly numbers for the board",
            skill_id="summarise",
        )
        assert outcome.succeeded
        assert outcome.remote_state == TaskState.COMPLETED
        assert "words=" in outcome.text
        assert outcome.artifacts

        await tenant.session.flush()
        actions = [
            row.action
            for row in (
                await tenant.session.execute(
                    AuditLog.__table__.select().where(
                        AuditLog.organization_id == tenant.organization_id
                    )
                )
            ).all()
        ]
        assert any("a2a" in str(a) for a in actions), (
            "an outbound call to another system's agent must leave an audit trail"
        )

    async def test_an_unverified_agent_cannot_be_called(self, tenant) -> None:
        """The card is a claim until it has been fetched. Routing work on a
        claim is the failure this guards."""
        from ai_orchestrator.domain.ids import A2AAgentId
        from ai_orchestrator.persistence.models import A2AAgent

        agent = A2AAgent(
            id=str(A2AAgentId.create()),
            organization_id=tenant.organization_id,
            name="claimed-but-never-probed",
            is_active=True,  # active, but the card was never seen
            card_verified=False,
        )
        tenant.session.add(agent)
        await tenant.session.flush()

        with pytest.raises(PreconditionError, match="never been verified"):
            await A2AGateway(tenant.session, tenant.organization_id).call(
                _actor(tenant.organization_id), agent.id, "hello"
            )

    async def test_a_dead_peer_leaves_a_row_that_says_so(self, tenant) -> None:
        """A registration that cannot reach its peer is recorded as unhealthy
        rather than raising: half-written state plus a 500 is worse than a row
        that says what happened."""
        gateway = A2AGateway(tenant.session, tenant.organization_id)
        port = _free_port()

        registration = await gateway.register(
            name="nobody-home", base_url=f"http://127.0.0.1:{port}", require_tls=False
        )
        assert registration.verified is False
        assert registration.problem
        assert registration.agent.is_active is False
        assert registration.agent.health == "unhealthy"
        assert registration.agent.card_verified is False
