"""Agent-to-agent: calling an agent that lives in another process.

The point of this package is one sentence, and everything in it exists to
support it: **a remote agent is a capability, and calling one is an outbound
side effect to a system this platform does not control.**

That has three consequences, each of which is a design decision rather than a
detail:

  * `card.py` validates rather than trusts. The agent card is a peer's
    description of itself, fetched from the peer.
  * `gateway.py` is the only way to make a call. An `A2AClient` handed to a model
    is a way around the `EXTERNAL_SEND` gate.
  * `protocol.py` bounds and validates every reply, because a reply is untrusted
    input in exactly the way an MCP server's output is.

The remote task vocabulary is deliberately *not* merged with `TaskStatus`. The
remote side owns its own state machine, and one enum that means two things
depending on who answered is worse than two enums.
"""

from ai_orchestrator.a2a.card import (
    CARD_PATH,
    PROTOCOL_VERSION,
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    SecurityScheme,
    parse_card,
)
from ai_orchestrator.a2a.client import A2AClient
from ai_orchestrator.a2a.gateway import ACTION_CALL, A2AGateway, A2AOutcome, RegistrationResult
from ai_orchestrator.a2a.protocol import (
    MAX_PAYLOAD_BYTES,
    METHOD_SEND,
    Artifact,
    JsonRpcRequest,
    JsonRpcResponse,
    Message,
    MessagePart,
    RemoteTask,
    TaskState,
    parse_task,
)

__all__ = [
    "ACTION_CALL",
    "CARD_PATH",
    "MAX_PAYLOAD_BYTES",
    "METHOD_SEND",
    "PROTOCOL_VERSION",
    "A2AClient",
    "A2AGateway",
    "A2AOutcome",
    "AgentCapabilities",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "Message",
    "MessagePart",
    "RegistrationResult",
    "RemoteTask",
    "SecurityScheme",
    "TaskState",
    "parse_card",
    "parse_task",
]
