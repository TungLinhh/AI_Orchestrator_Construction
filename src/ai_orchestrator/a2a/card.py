"""The A2A agent card: what a remote agent claims about itself.

A card is **untrusted input from the internet**, so the parse is where the
platform decides what it is willing to believe. Three rules, each one a failure
mode somebody has already had:

  * **The version is checked, not assumed.** A card that says `0.1.0` when the
    platform speaks `0.3.0` is either a different protocol or a lie; either way
    the fields below do not mean what they would mean here.
  * **Capabilities are a closed set.** `capabilities.streaming: true` is a claim
    about behaviour this platform will depend on. An unknown capability is not
    "probably fine", it is a field nobody has read.
  * **`securitySchemes` is parsed, not stored verbatim.** A card that declares
    `apiKey` in a header named `Authorization` with an empty scheme is trying to
    get the platform to send a credential somewhere it should not go.

The normalised form is what gets persisted. Re-reading the raw card on every
call would mean re-deciding what to trust at call time, which is exactly when
nobody is looking.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_orchestrator.domain.errors import ValidationError

#: The protocol revision this platform speaks.
PROTOCOL_VERSION = "0.3.0"

#: Capabilities the platform knows how to use. Anything else is rejected rather
#: than ignored: an unknown capability is a promise nobody has read, and this
#: platform will make decisions based on the promises it accepts.
KNOWN_CAPABILITIES = frozenset({"streaming", "pushNotifications", "stateTransitionHistory"})

#: Security schemes this platform can actually honour. `oauth2` is absent on
#: purpose until there is code to do the token dance; accepting the card without
#: it would mean recording a capability the platform cannot satisfy.
KNOWN_SECURITY_SCHEMES = frozenset({"none", "apiKey", "bearer"})

#: Where a card is fetched from, per the specification.
CARD_PATH = "/.well-known/agent-card.json"

#: Wire capability name -> the field on `AgentCapabilities`. Closed by
#: construction, so `supports()` cannot be handed a name nobody checks.
_CAPABILITY_FIELDS: dict[str, str] = {
    "streaming": "streaming",
    "pushNotifications": "push_notifications",
    "stateTransitionHistory": "state_transition_history",
}


class AgentSkill(BaseModel):
    """One thing the remote agent says it can do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
            msg = f"skill id {value!r} must be lower-case kebab-case"
            raise ValueError(msg)
        return value


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False

    def as_wire(self) -> dict[str, bool]:
        return {
            "streaming": self.streaming,
            "pushNotifications": self.push_notifications,
            "stateTransitionHistory": self.state_transition_history,
        }


class SecurityScheme(BaseModel):
    # `populate_by_name` because the wire key is `in`, which is a Python keyword
    # and cannot be a field name. Without it the field can only be set through
    # `model_validate`, and a call that names it directly is a type error.
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    type: str
    scheme: str | None = None
    #: The *name* of the environment variable holding the credential. Never the
    #: credential itself — see `security/secrets.py`.
    secret_ref: str | None = None
    in_: str | None = Field(default=None, alias="in")

    @field_validator("type")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in KNOWN_SECURITY_SCHEMES:
            msg = (
                f"security scheme {value!r} is not one this platform can honour; "
                f"known: {sorted(KNOWN_SECURITY_SCHEMES)}"
            )
            raise ValueError(msg)
        return value

    def as_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {"type": self.type}
        if self.scheme:
            wire["scheme"] = self.scheme
        if self.in_:
            wire["in"] = self.in_
        return wire


class AgentCard(BaseModel):
    """A validated, normalised agent card.

    Constructed by `parse_card`, never by `AgentCard.model_validate` on raw
    input: the constructor is where the trust decision is made and recorded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str = ""
    url: str
    version: str = "0.1.0"
    protocol_version: str = PROTOCOL_VERSION
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: tuple[AgentSkill, ...] = ()
    security_schemes: dict[str, SecurityScheme] = Field(default_factory=dict)
    preferred_transport: str = "jsonrpc"

    def supports(self, capability: str) -> bool:
        """Is a wire-named capability enabled?

        A closed mapping rather than a set intersection. The wire uses camelCase
        and the model uses snake_case, and building the answer by filtering a set
        of enabled names is how a comprehension ends up referencing a variable
        that was never bound.
        """
        if capability not in KNOWN_CAPABILITIES:
            return False
        return bool(getattr(self.capabilities, _CAPABILITY_FIELDS[capability], False))

    def skill(self, skill_id: str) -> AgentSkill | None:
        return next((s for s in self.skills if s.id == skill_id), None)

    def as_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": self.version,
            "protocolVersion": self.protocol_version,
            "preferredTransport": self.preferred_transport,
            "capabilities": self.capabilities.as_wire(),
            "skills": [
                {
                    "id": s.id,
                    "name": s.name,
                    "description": s.description,
                    "tags": list(s.tags),
                }
                for s in self.skills
            ],
            "securitySchemes": {k: v.as_wire() for k, v in self.security_schemes.items()},
        }


def parse_card(raw: dict[str, Any], *, card_url: str) -> AgentCard:
    """Parse and validate a card fetched from `card_url`.

    Raises `ValidationError` with a reason naming the field, because a card that
    fails validation is an operator's problem to fix and "invalid agent card" is
    not a diagnosis.
    """
    if not isinstance(raw, dict):
        msg = f"agent card from {card_url} is not a JSON object"
        raise ValidationError(msg, details={"url": card_url})

    version = str(raw.get("protocolVersion") or raw.get("protocol_version") or "")
    if version != PROTOCOL_VERSION:
        msg = (
            f"agent card from {card_url} declares protocol {version!r}; this "
            f"platform speaks {PROTOCOL_VERSION!r}"
        )
        raise ValidationError(msg, details={"url": card_url, "found": version})

    # Unknown capabilities are refused, not dropped. Dropping them would let a
    # card claim something the platform would then believe it had checked.
    claimed = dict(raw.get("capabilities") or {})
    unknown = set(claimed) - {
        "streaming",
        "pushNotifications",
        "stateTransitionHistory",
    }
    if unknown:
        msg = f"agent card from {card_url} claims unknown capabilities: {sorted(unknown)}"
        raise ValidationError(msg, details={"url": card_url, "unknown": sorted(unknown)})

    url = str(raw.get("url") or "")
    if not url.startswith(("http://", "https://")):
        msg = f"agent card from {card_url} has no usable url: {url!r}"
        raise ValidationError(msg, details={"url": card_url})

    try:
        return AgentCard(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            url=url,
            version=str(raw.get("version") or "0.1.0"),
            protocol_version=version,
            capabilities=AgentCapabilities(
                streaming=bool(claimed.get("streaming")),
                push_notifications=bool(claimed.get("pushNotifications")),
                state_transition_history=bool(claimed.get("stateTransitionHistory")),
            ),
            skills=tuple(
                AgentSkill(
                    id=str(s.get("id") or ""),
                    name=str(s.get("name") or ""),
                    description=str(s.get("description") or ""),
                    tags=tuple(str(t) for t in (s.get("tags") or ())),
                )
                for s in (raw.get("skills") or ())
            ),
            security_schemes={
                str(name): SecurityScheme(
                    type=str(spec.get("type") or "none"),
                    scheme=spec.get("scheme"),
                    in_=spec.get("in"),
                )
                for name, spec in (raw.get("securitySchemes") or {}).items()
            },
        )
    except ValidationError:
        raise
    except Exception as exc:  # pydantic and ValueError alike
        msg = f"agent card from {card_url} is malformed: {exc}"
        raise ValidationError(msg, details={"url": card_url}) from exc


__all__ = [
    "CARD_PATH",
    "KNOWN_CAPABILITIES",
    "KNOWN_SECURITY_SCHEMES",
    "PROTOCOL_VERSION",
    "AgentCapabilities",
    "AgentCard",
    "AgentSkill",
    "SecurityScheme",
    "parse_card",
]
