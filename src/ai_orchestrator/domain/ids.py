"""Typed identifiers.

Every id in the system is a distinct type so that an `AgentId` can never be
passed where a `TaskId` is expected. This is the cheapest available defence
against the single most damaging class of bug in an agent platform: one agent
acting on another agent's task because two strings happened to match.

The wire format is a prefixed lowercase string, e.g. `agt_01J9...`. Prefixes
make IDs self-describing in logs, events and audit rows.
"""

from __future__ import annotations

import re
import secrets
import threading
import time
from typing import Any, Final, Self, TypeVar

# Crockford-style base32 without I, L, O, U: no ambiguity when a human reads
# an id out of a log line.
_ALPHABET: Final = "0123456789abcdefghjkmnpqrstvwxyz"
_ULID_LEN: Final = 26
_ULID_BITS: Final = 128
_RANDOM_BITS: Final = 80
_MAX_RANDOM: Final = (1 << _RANDOM_BITS) - 1

_PREFIX_RE: Final = re.compile(r"^[a-z]{2,5}_[0-9a-z]{26}$")

# Monotonic state. Two ids minted in the same millisecond must still sort, or
# keyset pagination silently skips rows and event replay loses ordering — the
# kind of bug that shows up as "occasionally one task is missing".
_lock: Final = threading.Lock()
# Not `Final`: these two are the entire point of the monotonic ULID and are
# reassigned on every mint.
_last_ms: int = 0
_last_random: int = 0


def new_ulid() -> str:
    """A strictly monotonic, lexicographically sortable 128-bit id.

    Lexicographic order equals creation order. Not a UUID: we do not need 122
    random bits, we need ordering, and Postgres already handles identity.
    """
    global _last_ms, _last_random
    with _lock:
        now_ms = int(_now_ms())
        if now_ms == _last_ms:
            # Same millisecond: increment rather than re-randomise, so order is
            # preserved. Overflow just moves into the next millisecond, which is
            # still monotonic.
            _last_random = (_last_random + 1) & _MAX_RANDOM
            if _last_random == 0:
                now_ms = _last_ms + 1
        else:
            if now_ms < _last_ms:
                # Clock went backwards (NTP step, VM resume). Keep the last
                # observed millisecond rather than issuing out-of-order ids.
                now_ms = _last_ms
                _last_random = (_last_random + 1) & _MAX_RANDOM
            else:
                _last_random = secrets.randbits(_RANDOM_BITS)
        _last_ms = now_ms
        value = (now_ms << _RANDOM_BITS) | _last_random
    return _encode(value)


def _encode(value: int) -> str:
    chars = [_ALPHABET[value & 0x1F]]
    value >>= 5
    while value:
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars)).rjust(_ULID_LEN, "0")


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_valid_ulid(value: str) -> bool:
    if len(value) != _ULID_LEN:
        return False
    return all(c in _ALPHABET for c in value)


def make_id(prefix: str) -> str:
    """`make_id("agt")` -> `agt_01j9...`. Validates the prefix.

    Digits are allowed. `a2a` is the natural prefix for the agent-to-agent
    tables and a letters-only rule made `A2AAgentId.create()` raise — a type that
    ships unusable because nothing had yet tried to mint one. The prefix is
    delimited by `_` and the ULID that follows is Crockford base32, so a digit
    there is unambiguous.
    """
    if not re.fullmatch(r"[a-z0-9]{2,5}", prefix):
        msg = f"invalid id prefix: {prefix!r} (expected 2-5 lowercase letters or digits)"
        raise ValueError(msg)
    return f"{prefix}_{new_ulid()}"


class BrandedId(str):
    """A `str` subclass that gives an identifier its own type.

    Subclassing `str` keeps the value JSON-serialisable and directly comparable
    with the raw column value, while the distinct subclass keeps a type checker
    from confusing an `AgentId` with a `TaskId`. The type is the only guard
    against one agent acting on another agent's work because two strings
    happened to match.

    Construction does not validate: ids arrive from the database and off the
    wire, and rejecting them at construction costs more than it buys. Shape is
    validated once, at the API boundary, by `__get_pydantic_core_schema__`.
    """

    __slots__ = ()
    _PREFIX: str = "id"

    def __new__(cls, value: object = "") -> Self:
        if not isinstance(value, str):
            msg = f"{cls.__name__} must wrap a str, got {type(value).__name__}"
            raise TypeError(msg)
        return super().__new__(cls, value)

    @classmethod
    def create(cls) -> Self:
        return cls(make_id(cls._PREFIX))

    @classmethod
    def parse(cls, value: str) -> Self:
        return cls(value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: type, handler: Any) -> Any:
        """Pydantic hook: a branded id is a prefixed 26-char ULID string.

        Validating the shape at the API boundary is what stops a caller from
        passing an `AgentId` field a `TaskId` value, which no amount of static
        typing protects against once the data comes off the wire.
        """
        from pydantic_core import core_schema

        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(
                min_length=len(cls._PREFIX) + 27,
                max_length=len(cls._PREFIX) + 27,
                pattern=rf"^{cls._PREFIX}_[0-9a-hjkmnp-tv-z]{{26}}$",
            ),
            serialization=core_schema.to_string_ser_schema(),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: Any, handler: Any) -> Any:
        return {"type": "string", "pattern": rf"^{cls._PREFIX}_[0-9a-hjkmnp-tv-z]{{26}}$"}


T = TypeVar("T", bound=BrandedId)


class OrganizationId(BrandedId):
    _PREFIX = "org"


class OrgUnitId(BrandedId):
    _PREFIX = "unit"


class RoleId(BrandedId):
    _PREFIX = "role"


class AgentDefinitionId(BrandedId):
    _PREFIX = "adef"


class AgentId(BrandedId):
    _PREFIX = "agt"


class SubagentRunId(BrandedId):
    _PREFIX = "sub"


class SkillId(BrandedId):
    _PREFIX = "skl"


class SkillVersionId(BrandedId):
    _PREFIX = "sklv"


class ToolId(BrandedId):
    _PREFIX = "tool"


class ToolVersionId(BrandedId):
    _PREFIX = "tolv"


class McpServerId(BrandedId):
    _PREFIX = "mcp"


class TaskId(BrandedId):
    _PREFIX = "tsk"


class DelegationId(BrandedId):
    _PREFIX = "del"


class ExecutionId(BrandedId):
    _PREFIX = "exe"


class ApprovalId(BrandedId):
    _PREFIX = "apr"


class EventId(BrandedId):
    _PREFIX = "evt"


class MessageId(BrandedId):
    _PREFIX = "msg"


class MemoryItemId(BrandedId):
    _PREFIX = "mem"


class DocumentId(BrandedId):
    _PREFIX = "doc"


class PolicyId(BrandedId):
    _PREFIX = "pol"


class PolicyVersionId(BrandedId):
    _PREFIX = "polv"


class BudgetId(BrandedId):
    _PREFIX = "bud"


class BudgetLedgerEntryId(BrandedId):
    _PREFIX = "ble"


class ModelProfileId(BrandedId):
    _PREFIX = "mpf"


class ModelUsageId(BrandedId):
    _PREFIX = "mus"


class A2AAgentId(BrandedId):
    _PREFIX = "a2a"


class A2AEndpointId(BrandedId):
    _PREFIX = "a2ep"


class UserId(BrandedId):
    _PREFIX = "usr"


class AuditLogId(BrandedId):
    _PREFIX = "aud"


class ConnectorId(BrandedId):
    _PREFIX = "cnn"


class EvaluationRunId(BrandedId):
    _PREFIX = "evr"


class OutboxEventId(BrandedId):
    _PREFIX = "obx"


class IdempotencyRecordId(BrandedId):
    _PREFIX = "idm"


def is_well_formed_id(value: str) -> bool:
    return bool(_PREFIX_RE.match(value))


__all__ = [
    "A2AAgentId",
    "A2AEndpointId",
    "AgentDefinitionId",
    "AgentId",
    "ApprovalId",
    "AuditLogId",
    "BrandedId",
    "BudgetId",
    "BudgetLedgerEntryId",
    "ConnectorId",
    "DelegationId",
    "DocumentId",
    "EvaluationRunId",
    "EventId",
    "ExecutionId",
    "IdempotencyRecordId",
    "McpServerId",
    "MemoryItemId",
    "MessageId",
    "ModelProfileId",
    "ModelUsageId",
    "OrgUnitId",
    "OrganizationId",
    "OutboxEventId",
    "PolicyId",
    "PolicyVersionId",
    "RoleId",
    "SkillId",
    "SkillVersionId",
    "SubagentRunId",
    "TaskId",
    "ToolId",
    "ToolVersionId",
    "UserId",
    "is_valid_ulid",
    "is_well_formed_id",
    "make_id",
    "new_ulid",
]
