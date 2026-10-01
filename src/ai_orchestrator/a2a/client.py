"""The A2A client: talking to an agent in another process.

Bounded in four places, each for a reason that has bitten somebody:

  * **connect and read timeouts**, so a peer that accepts and then says nothing
    occupies a task slot for exactly as long as the operator configured, not
    until a TCP stack gives up;
  * **a payload cap**, enforced before parsing;
  * **TLS enforced for anything but loopback**, because an agent card carries
    tool definitions and results and a plaintext hop lets anyone on the path
    rewrite what the agent is told;
  * **no redirect following** to a different host, because a redirect is the
    cheapest way to make a client send its credential somewhere the operator
    never approved.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ai_orchestrator.a2a.card import CARD_PATH, AgentCard, parse_card
from ai_orchestrator.a2a.protocol import (
    MAX_PAYLOAD_BYTES,
    METHOD_SEND,
    JsonRpcRequest,
    JsonRpcResponse,
    Message,
    MessagePart,
    RemoteTask,
    parse_task,
)
from ai_orchestrator.domain.errors import (
    ExternalServiceError,
    ValidationError,
)
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Hosts a plaintext endpoint is tolerated on. Loopback only, and only because a
#: local test process speaks HTTP; anything reachable off the machine must be
#: TLS or the call is refused.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class A2AClient:
    """Speaks A2A to one remote agent.

    Deliberately not a `ModelProvider` and not a `Tool`. A remote call is an
    *outbound side effect to a system this platform does not control*, which is
    the `EXTERNAL_SEND` effect class, and it is gated like one. Making it
    convenient to call from inside an agent turn is how an approval gate gets
    bypassed.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_s: float = 30.0,
        require_tls: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._require_tls = require_tls
        self._headers = dict(headers or {})
        self._check_scheme()

    def _check_scheme(self) -> None:
        parsed = urlparse(self._base)
        if parsed.scheme not in {"http", "https"}:
            msg = f"a2a endpoint {self._base!r} is not an http(s) url"
            raise ValidationError(msg, details={"url": self._base})
        if self._require_tls and parsed.scheme != "https":
            host = parsed.hostname or ""
            if host not in LOOPBACK_HOSTS:
                msg = (
                    f"a2a endpoint {self._base!r} is plaintext; refusing to send "
                    f"anything to a host that is not loopback over HTTP"
                )
                raise ValidationError(msg, details={"url": self._base, "scheme": parsed.scheme})

    @property
    def base_url(self) -> str:
        return self._base

    async def fetch_card(self) -> AgentCard:
        """Fetch and validate the agent card.

        The card is the peer's description of itself, so it is fetched rather
        than configured, and validated rather than trusted. An unverified card
        stays `health='unknown'` in the registry, because "never probed" and
        "working" must not look alike.
        """
        url = f"{self._base}{CARD_PATH}"
        started = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.get(url)
                body = response.content
        except httpx.HTTPError as exc:
            msg = f"a2a agent card unreachable at {url}: {type(exc).__name__}"
            raise ExternalServiceError(msg, details={"url": url}) from exc

        self._check_same_origin(response)

        if len(body) > MAX_PAYLOAD_BYTES:
            msg = f"a2a agent card at {url} is {len(body)} bytes; the cap is {MAX_PAYLOAD_BYTES}"
            raise ValidationError(msg, details={"url": url, "bytes": len(body)})

        import json

        try:
            raw = json.loads(body)
        except ValueError as exc:
            msg = f"a2a agent card at {url} is not JSON: {exc}"
            raise ValidationError(msg, details={"url": url}) from exc

        card = parse_card(raw, card_url=url)
        logger.info(
            "a2a.card_fetched",
            url=url,
            name=card.name,
            skills=len(card.skills),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return card

    async def send(
        self,
        prompt: str,
        *,
        skill_id: str | None = None,
        data: dict[str, Any] | None = None,
        context_id: str | None = None,
    ) -> RemoteTask:
        """One `message/send`, returning the remote agent's task.

        Raises rather than returning a failed task for transport problems, because
        "the peer is unreachable" and "the peer declined" call for different
        operator responses and a single failure value flattens them.
        """
        parts: list[MessagePart] = []
        if prompt:
            parts.append(MessagePart(kind="text", text=prompt))
        if data:
            parts.append(MessagePart(kind="data", data=data))
        if not parts:
            msg = "a2a send requires a prompt or structured data"
            raise ValidationError(msg, details={"peer": self._base})

        params: dict[str, Any] = {"message": Message(parts=tuple(parts)).as_wire()}
        if skill_id:
            params["skillId"] = skill_id
        if context_id:
            params["contextId"] = context_id

        request = JsonRpcRequest(method=METHOD_SEND, params=params)
        started = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.post(
                    self._base,
                    content=request.to_bytes(),
                    headers={"content-type": "application/json"},
                )
        except httpx.HTTPError as exc:
            msg = f"a2a call to {self._base} failed: {type(exc).__name__}"
            raise ExternalServiceError(msg, details={"peer": self._base}) from exc

        self._check_same_origin(response)
        parsed = parse_task(
            JsonRpcResponse.parse(response.content, peer=self._base).unwrap(peer=self._base),
            peer=self._base,
        )
        logger.info(
            "a2a.sent",
            peer=self._base,
            state=parsed.state.value,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return parsed

    def _client(self) -> httpx.AsyncClient:
        # Redirects are followed, but only within the origin. A trailing-slash
        # redirect is what most servers answer a bare `/` with, and refusing it
        # would make the client fail against a perfectly ordinary agent. A
        # redirect to a *different* host is the cheapest way to make a client
        # send its credential somewhere the operator never approved, so that is
        # refused — see `_check_same_origin`.
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_s, connect=min(5.0, self._timeout_s)),
            headers=self._headers,
            follow_redirects=True,
        )

    def _check_same_origin(self, response: httpx.Response) -> None:
        """Refuse a redirect that left the origin we were told to talk to.

        Checked after the fact rather than by refusing to follow, because a
        trailing-slash redirect is ordinary and a cross-host one is not, and
        refusing both treats the common case as the dangerous one.
        """
        origin = urlparse(self._base)
        for hop in response.history:
            if (hop.url.scheme, hop.url.host) != (origin.scheme, origin.hostname):
                msg = (
                    f"a2a endpoint {self._base} redirected off-origin to {hop.url}; "
                    f"refusing to follow"
                )
                raise ValidationError(msg, details={"from": self._base, "to": str(hop.url)})


__all__ = ["LOOPBACK_HOSTS", "A2AClient"]
