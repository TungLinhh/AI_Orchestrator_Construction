"""A2A wire protocol: JSON-RPC 2.0 over HTTP, in typed form.

The shape follows the A2A specification closely enough to interoperate and no
further. Two deliberate strictnesses:

  * **Every response is validated before it is believed.** A remote agent's
    reply is untrusted input in exactly the way an MCP server's output is, and the
    failure mode is the same: a malformed or hostile reply is easier to detect at
    the boundary than three layers later.
  * **A payload is bounded.** `max_payload_bytes` is not paranoia about
    bandwidth; it is the difference between a slow remote agent and a
    memory-exhaustion vector. The MCP client has the same limit for the same
    reason.
"""

from __future__ import annotations

import json
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_orchestrator.domain.errors import ExternalServiceError, ValidationError

#: Method names. `message/send` is the synchronous request/response form;
#: `message/stream` needs a server-sent-events transport this platform does not
#: implement yet, and advertising it would be a claim the client cannot honour.
METHOD_SEND = "message/send"
METHOD_GET_CARD = "agent/getAuthenticatedExtendedCard"

JSONRPC_VERSION = "2.0"

#: Hard ceiling on one response body. Chosen to be generous for a text result
#: and small enough that a hostile peer cannot exhaust memory.
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


class TaskState(StrEnum):
    """The remote agent's own lifecycle.

    Not the same vocabulary as our task states, and deliberately not mapped onto
    them. A remote agent's `input-required` is not our `waiting_for_input`: the
    remote side owns that machine, and pretending otherwise would make a
    `TaskStatus` mean two things depending on who answered.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskState.COMPLETED,
            TaskState.CANCELED,
            TaskState.FAILED,
            TaskState.REJECTED,
        }


class MessagePart(BaseModel):
    # `populate_by_name` on every model in this module: `as_wire()` emits the
    # camelCase the specification uses, and a model that cannot read back its own
    # output cannot interoperate with a copy of itself, let alone another vendor's.
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    kind: str = "text"
    text: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    def as_wire(self) -> dict[str, Any]:
        if self.kind == "data":
            return {"kind": "data", "data": self.data}
        return {"kind": "text", "text": self.text}


class Message(BaseModel):
    """One turn sent to or received from a remote agent."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    role: str = "user"
    parts: tuple[MessagePart, ...] = ()
    message_id: str = Field(
        default_factory=lambda: f"msg_{uuid.uuid4().hex[:26]}", alias="messageId"
    )
    task_id: str | None = Field(default=None, alias="taskId")

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.kind == "text")

    def as_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "role": self.role,
            "parts": [p.as_wire() for p in self.parts],
            "messageId": self.message_id,
        }
        if self.task_id:
            wire["taskId"] = self.task_id
        return wire


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    artifact_id: str = Field(
        default_factory=lambda: f"art_{uuid.uuid4().hex[:26]}", alias="artifactId"
    )
    name: str = ""
    parts: tuple[MessagePart, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.parts if p.kind == "text")

    def as_wire(self) -> dict[str, Any]:
        return {
            "artifactId": self.artifact_id,
            "name": self.name,
            "parts": [p.as_wire() for p in self.parts],
        }


class RemoteTask(BaseModel):
    """The remote agent's task, as it reports it."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    id: str
    state: TaskState
    context_id: str | None = Field(default=None, alias="contextId")
    artifacts: tuple[Artifact, ...] = ()
    history: tuple[Message, ...] = ()

    @property
    def result_text(self) -> str:
        """The answer, from the artifacts if there are any, else the last turn.

        The order matters. A remote agent may echo the request in its history
        and answer in an artifact, so reading the history first returns the
        question back.
        """
        if self.artifacts:
            return "\n".join(a.text for a in self.artifacts if a.text)
        for message in reversed(self.history):
            if message.role != "user" and message.text:
                return message.text
        return ""

    def as_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"id": self.id, "state": self.state.value}
        if self.context_id:
            wire["contextId"] = self.context_id
        if self.artifacts:
            wire["artifacts"] = [a.as_wire() for a in self.artifacts]
        if self.history:
            wire["history"] = [m.as_wire() for m in self.history]
        return wire


class JsonRpcRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    jsonrpc: str = JSONRPC_VERSION
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    method: str
    params: dict[str, Any] = Field(default_factory=dict)

    def as_wire(self) -> dict[str, Any]:
        return {
            "jsonrpc": self.jsonrpc,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.as_wire()).encode("utf-8")


class JsonRpcError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    code: int
    message: str
    data: Any = None


class JsonRpcResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    jsonrpc: str = JSONRPC_VERSION
    id: str | None = None
    result: dict[str, Any] | None = None
    error: JsonRpcError | None = None

    @classmethod
    def parse(cls, body: bytes, *, peer: str) -> JsonRpcResponse:
        """Parse a reply, refusing anything oversized or malformed.

        The cap is enforced *before* parsing. A peer that streams a gigabyte of
        JSON gets refused at the socket rather than after the allocation.
        """
        if len(body) > MAX_PAYLOAD_BYTES:
            msg = f"a2a response from {peer} is {len(body)} bytes; the cap is {MAX_PAYLOAD_BYTES}"
            raise ValidationError(msg, details={"peer": peer, "bytes": len(body)})
        try:
            raw = json.loads(body)
        except (TypeError, ValueError) as exc:
            msg = f"a2a response from {peer} is not JSON: {exc}"
            raise ValidationError(msg, details={"peer": peer}) from exc
        if not isinstance(raw, dict):
            msg = f"a2a response from {peer} is not a JSON object"
            raise ValidationError(msg, details={"peer": peer})
        try:
            return cls.model_validate(raw)
        except Exception as exc:
            msg = f"a2a response from {peer} is malformed: {exc}"
            raise ValidationError(msg, details={"peer": peer}) from exc

    def unwrap(self, *, peer: str) -> dict[str, Any]:
        """The `result`, or an error raised as ours.

        A JSON-RPC error is not an exception in the transport, so without this
        the caller sees a missing `result` and reports a protocol failure instead
        of what the remote agent actually said.
        """
        if self.error is not None:
            msg = f"a2a peer {peer} returned error {self.error.code}: {self.error.message}"
            raise ExternalServiceError(msg, details={"peer": peer, "code": self.error.code})
        if self.result is None:
            msg = f"a2a peer {peer} returned neither a result nor an error"
            raise ExternalServiceError(msg, details={"peer": peer})
        return self.result


def parse_task(result: dict[str, Any], *, peer: str) -> RemoteTask:
    """The `result` of `message/send` -> a `RemoteTask`.

    A2A allows either a task or a bare message back. Both are accepted; neither
    is assumed.
    """
    if "task" in result and isinstance(result["task"], dict):
        raw = result["task"]
    elif "kind" in result and result.get("kind") == "message":
        message = Message.model_validate(result)
        return RemoteTask(
            id=message.task_id or message.message_id,
            state=TaskState.COMPLETED,
            artifacts=(Artifact(name="message", parts=message.parts),),
        )
    else:
        msg = f"a2a peer {peer} returned neither a task nor a message"
        raise ValidationError(msg, details={"peer": peer, "keys": sorted(result)})

    try:
        return RemoteTask.model_validate(raw)
    except Exception as exc:
        msg = f"a2a task from {peer} is malformed: {exc}"
        raise ValidationError(msg, details={"peer": peer}) from exc


__all__ = [
    "JSONRPC_VERSION",
    "MAX_PAYLOAD_BYTES",
    "METHOD_GET_CARD",
    "METHOD_SEND",
    "Artifact",
    "JsonRpcError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "Message",
    "MessagePart",
    "RemoteTask",
    "TaskState",
    "parse_task",
]
