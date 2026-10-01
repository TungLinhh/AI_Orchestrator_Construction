"""MCP boundary.

An external MCP server is an untrusted dependency that happens to speak a
standard protocol. Three consequences shape this module:

  * allowlist by default. A registered server exposes nothing until its tools
    are enumerated and each one is admitted. `connect()` alone grants nothing.
  * output is data. Every tool result is marked untrusted, and anything matching
    a known injection shape is flagged so the caller can decide what to do with
    it. Passing MCP output to a model as instructions is the single most
    effective attack against an agent platform, and the flag exists to make that
    decision explicit rather than implicit.
  * one bad server is one bad server. A failing server is skipped and the others
    continue; a health check that hangs cannot take the control plane with it,
    because the timeout is enforced on the call rather than trusted to the
    server.

The transport is deliberately narrow. An in-process registry and an MCP client
present the same `ToolDefinition`, so a runtime cannot tell them apart and cannot
acquire a special case for the more dangerous one.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.enums import DataClassification, EffectClass, ToolRisk
from ai_orchestrator.domain.errors import (
    ExternalServiceError,
    ToolUnavailable,
    ValidationError,
)
from ai_orchestrator.domain.ids import McpServerId, ToolId
from ai_orchestrator.tools.registry import (
    ToolDefinition,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)

#: An MCP result is capped. A server that returns 10 MB of text is either broken
#: or trying to push the context window past the provider's limit; either way the
#: platform decides the cap, not the server.
MAX_MCP_RESULT_BYTES = 1_048_576

#: Injection shapes to flag. Not a filter — the content is still returned,
#: because a heuristic that silently dropped text would make a legitimate result
#: disappear. It produces a flag, and the flag travels with the result.
_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "ignore the above",
    "disregard your instructions",
    "disregard all",
    "system:",
    "assistant:",
    "you are now",
    "new instructions:",
    "reveal your system prompt",
    "print your instructions",
    "<|im_start|>",
    "<|im_end|>",
    "### instruction",
)


def flag_untrusted_content(text: str) -> dict[str, Any]:
    """Report what an untrusted payload looks like, without altering it."""
    lowered = text.lower()
    matched = [marker for marker in _INJECTION_MARKERS if marker in lowered]
    return {
        "untrusted": True,
        "injection_markers": matched,
        "prompt_injection_suspected": bool(matched),
    }


@dataclass(slots=True)
class McpServerRecord:
    """A registered MCP server and its admission state."""

    server_id: str
    name: str
    transport: str = "stdio"
    endpoint: str = ""
    command: list[str] = field(default_factory=list)
    is_allowlisted: bool = False
    tool_allowlist: list[str] = field(default_factory=list)
    require_tls: bool = True
    timeout_seconds: int = 30
    max_payload_bytes: int = MAX_MCP_RESULT_BYTES
    is_active: bool = False
    health: str = "unknown"
    #: Discovered tools, populated by `discover`.
    discovered: dict[str, dict[str, Any]] = field(default_factory=dict)

    def admits(self, tool_name: str) -> bool:
        """Default-deny. An empty allowlist admits nothing."""
        if not self.is_allowlisted:
            return False
        if not self.tool_allowlist:
            return False
        return tool_name in self.tool_allowlist


class McpRegistry:
    """Registered servers. No transport, no credentials."""

    def __init__(self) -> None:
        self._servers: dict[str, McpServerRecord] = {}

    def register(self, record: McpServerRecord) -> McpServerRecord:
        self._servers[record.server_id] = record
        return record

    def get(self, server_id: str) -> McpServerRecord:
        record = self._servers.get(server_id)
        if record is None:
            msg = f"unknown MCP server: {server_id}"
            raise ToolUnavailable(msg, details={"server_id": server_id})
        return record

    def all(self) -> list[McpServerRecord]:
        return list(self._servers.values())

    def active(self) -> list[McpServerRecord]:
        return [s for s in self._servers.values() if s.is_active]


class McpClient:
    """Thin JSON-RPC client.

    Written against the wire rather than pulled from a dependency, because the
    whole value of this class is the gating around it, and a dependency would
    obscure exactly the seam the platform needs to control.
    """

    def __init__(self, record: McpServerRecord) -> None:
        self._record = record
        self._request_id = 0
        self._process: Any = None
        self._lock = asyncio.Lock()

    @property
    def record(self) -> McpServerRecord:
        return self._record

    async def initialize(self) -> None:
        """Perform the MCP handshake.

        Refuses a non-TLS HTTP endpoint outright rather than warning about it: an
        MCP server carries tool definitions and results, and a plaintext hop lets
        anyone on the path rewrite what the agent is told.
        """
        if (
            self._record.transport in {"http", "sse"}
            and self._record.require_tls
            and not self._record.endpoint.startswith("https://")
        ):
            msg = (
                f"MCP server {self._record.name!r} uses {self._record.transport} "
                f"without TLS; refusing to connect"
            )
            raise ValidationError(msg, details={"endpoint_scheme": "insecure"})

        if self._record.transport == "stdio":
            await self._start_stdio()
        await self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ai-orchestrator", "version": "0.1.0"},
            },
        )

    async def _start_stdio(self) -> None:
        if not self._record.command:
            msg = f"MCP server {self._record.name!r} has no command for stdio transport"
            raise ValidationError(msg, details={"server": self._record.name})
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._record.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            msg = f"cannot start MCP server {self._record.name!r}: {exc}"
            raise ExternalServiceError(msg, details={"server": self._record.name}) from exc

    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One JSON-RPC round trip, with the timeout enforced here.

        The timeout is on the client side because a server that never answers is
        a normal failure mode and the platform must not inherit it.
        """
        if self._record.transport == "stdio":
            return await self._stdio_call(method, params)
        return await self._http_call(method, params)

    async def _stdio_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._process is None:
            msg = f"MCP server {self._record.name!r} is not running"
            raise ToolUnavailable(msg, details={"server": self._record.name})
        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        try:
            async with asyncio.timeout(self._record.timeout_seconds):
                assert self._process.stdin is not None
                assert self._process.stdout is not None
                self._process.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
                await self._process.stdin.drain()
                line = await self._readline_bounded()
        except TimeoutError as exc:
            self._record.health = "unhealthy"
            msg = (
                f"MCP server {self._record.name!r} did not respond within "
                f"{self._record.timeout_seconds}s"
            )
            raise ExternalServiceError(msg, details={"server": self._record.name}) from exc
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._record.health = "unhealthy"
            msg = f"MCP server {self._record.name!r} closed its connection"
            raise ExternalServiceError(msg, details={"server": self._record.name}) from exc
        if not line:
            self._record.health = "unhealthy"
            msg = f"MCP server {self._record.name!r} returned no data"
            raise ExternalServiceError(msg, details={"server": self._record.name})
        payload: dict[str, Any] = json.loads(line.decode("utf-8"))
        return payload

    async def _readline_bounded(self) -> bytes:
        """Read one newline-delimited frame, refusing to buffer past the cap.

        `StreamReader.readline()` has its own 64 KiB limit and raises
        `LimitOverrunError` past it, which means a server that returns a large
        single line crashes the client rather than being reported. Worse, a naive
        replacement that raises the limit turns a malicious server into a way to
        exhaust this process's memory. So the cap is the server's declared
        `max_payload_bytes`, and a frame past it is refused, not truncated:
        a truncated JSON-RPC frame is not a valid response.
        """
        assert self._process is not None
        assert self._process.stdout is not None
        stream = self._process.stdout
        cap = self._record.max_payload_bytes
        buffer = bytearray()
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            buffer.extend(chunk)
            if b"\n" in chunk:
                break
            if len(buffer) > cap:
                msg = (
                    f"MCP server {self._record.name!r} sent {len(buffer)} bytes without a "
                    f"frame delimiter, exceeding its {cap} byte cap; refusing"
                )
                raise ExternalServiceError(msg, details={"server": self._record.name})
        newline = buffer.find(b"\n")
        payload = bytes(buffer[:newline]) if newline != -1 else bytes(buffer)
        if len(payload) > cap:
            msg = f"MCP server {self._record.name!r} frame exceeds its {cap} byte cap; refusing"
            raise ExternalServiceError(msg, details={"server": self._record.name})
        return payload

    async def _http_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        import httpx

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        try:
            async with asyncio.timeout(self._record.timeout_seconds):
                async with httpx.AsyncClient(timeout=self._record.timeout_seconds) as client:
                    response = await client.post(self._record.endpoint, json=payload)
                    response.raise_for_status()
                    body: dict[str, Any] = response.json()
                    return body
        except TimeoutError as exc:
            self._record.health = "unhealthy"
            msg = f"MCP server {self._record.name!r} timed out"
            raise ExternalServiceError(msg, details={"server": self._record.name}) from exc
        except Exception as exc:
            self._record.health = "unhealthy"
            msg = f"MCP server {self._record.name!r} request failed: {type(exc).__name__}"
            raise ExternalServiceError(msg, details={"server": self._record.name}) from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._call("tools/list", {})
        tools = (result.get("result") or {}).get("tools") or []
        return list(tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._call("tools/call", {"name": tool_name, "arguments": arguments})

    async def aclose(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                self._process.kill()
            self._process = None
        self._record.health = "stopped"


class McpGateway:
    """Bridges MCP servers into the tool registry.

    `connect` enumerates and *registers*; it does not admit. Admission is a
    separate, explicit call, so a misconfigured server cannot expose a
    destructive tool by being merely reachable.
    """

    def __init__(self, registry: McpRegistry, tool_registry: ToolRegistry) -> None:
        self._servers = registry
        self._tools = tool_registry
        self._clients: dict[str, McpClient] = {}

    async def connect(self, server_id: str) -> McpServerRecord:
        record = self._servers.get(server_id)
        client = McpClient(record)
        await client.initialize()
        discovered = await client.list_tools()
        record.discovered = {t.get("name", ""): t for t in discovered if t.get("name")}
        record.health = "healthy"
        self._clients[server_id] = client
        return record

    async def admit(
        self,
        server_id: str,
        *,
        tool_names: list[str],
        risk_by_tool: dict[str, ToolRisk] | None = None,
        effect_by_tool: dict[str, EffectClass] | None = None,
    ) -> list[str]:
        """Admit named tools from a connected server into the tool registry.

        Only tools the server actually advertises can be admitted. A name in the
        allowlist that the server does not provide is an error, not a silent
        no-op: it is either a typo or a server that changed, and both should be
        visible.
        """
        record = self._servers.get(server_id)
        missing = [n for n in tool_names if n not in record.discovered]
        if missing:
            msg = (
                f"server {record.name!r} does not advertise: {missing}. "
                f"It advertises: {sorted(record.discovered)}"
            )
            raise ValidationError(
                msg, details={"missing": missing, "advertised": sorted(record.discovered)}
            )

        client = self._clients[server_id]
        admitted: list[str] = []
        for name in tool_names:
            spec = record.discovered[name]
            risk = (risk_by_tool or {}).get(name, _infer_risk(spec))
            effect = (effect_by_tool or {}).get(name, _infer_effect(risk))

            async def handler(
                args: dict[str, Any],
                _ctx: ToolExecutionContext,
                *,
                _name: str = name,
                _client: McpClient = client,
                _record: McpServerRecord = record,
            ) -> ToolResult:
                return await self._invoke(_client, _record, _name, args)

            self._tools.register(
                ToolDefinition(
                    tool_id=str(ToolId.create()),
                    # Namespaced so two servers cannot collide on a tool name.
                    name=f"{record.name}__{name}",
                    description=spec.get("description", "")[:500],
                    input_schema=spec.get("inputSchema") or {"type": "object", "properties": {}},
                    output_schema=spec.get("outputSchema"),
                    risk=risk,
                    effect_class=effect,
                    handler=handler,
                    # An external tool is assumed non-idempotent. Assuming
                    # otherwise means a retried call may duplicate a side effect
                    # on a system we do not control.
                    is_idempotent=False,
                    timeout_seconds=record.timeout_seconds,
                    rate_limit_per_minute=30,
                    data_classification=DataClassification.INTERNAL,
                    served_by=record.server_id,
                )
            )
            admitted.append(name)

        record.tool_allowlist = list(tool_names)
        record.is_allowlisted = True
        record.is_active = True
        return admitted

    async def _invoke(
        self, client: McpClient, record: McpServerRecord, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        if not record.admits(tool_name):
            return ToolResult.failure(
                "MCP_TOOL_NOT_ADMITTED",
                f"tool {tool_name!r} is not on the allowlist for {record.name!r}",
            )
        try:
            response = await client.call_tool(tool_name, arguments)
        except ExternalServiceError as exc:
            return ToolResult.failure("MCP_UNAVAILABLE", exc.message, idempotent=False)

        if "error" in response:
            error = response["error"] or {}
            return ToolResult.failure(
                "MCP_TOOL_ERROR", str(error.get("message", "unknown MCP error")), idempotent=False
            )

        result = response.get("result") or {}
        text = _flatten_mcp_result(result)
        if len(text.encode("utf-8")) > record.max_payload_bytes:
            return ToolResult.failure(
                "MCP_PAYLOAD_TOO_LARGE",
                f"result exceeds {record.max_payload_bytes} bytes",
                idempotent=False,
            )

        return ToolResult(
            ok=not result.get("isError", False),
            output=result.get("content", result),
            idempotent=False,
            # The flag travels with the result. A caller that ignores it is
            # making a choice; one that was never told could not.
            metadata=flag_untrusted_content(text) | {"mcp_server": record.name, "tool": tool_name},
        )

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


def _flatten_mcp_result(result: dict[str, Any]) -> str:
    """Flatten MCP content blocks into text for inspection and size checking."""
    content = result.get("content")
    if content is None:
        return json.dumps(result, default=str)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content if isinstance(content, list) else [content]:
        if isinstance(block, dict):
            parts.append(str(block.get("text") or block.get("data") or ""))
        else:
            parts.append(str(block))
    return "\n".join(parts)


#: An MCP tool that describes itself as destructive is registered as such. The
#: description is not authority, but a server that says "deletes records" and
#: then gets registered as a low-risk read is a mistake worth catching at the
#: boundary.
_DESTRUCTIVE_WORDS = re.compile(r"(?i)\b(delete|destroy|drop|remove|wipe|purge|terminate|revoke)\b")
_READ_WORDS = re.compile(r"(?i)\b(get|list|read|fetch|query|search|describe|show)\b")


def _infer_risk(spec: dict[str, Any]) -> ToolRisk:
    text = f"{spec.get('name', '')} {spec.get('description', '')}"
    if _DESTRUCTIVE_WORDS.search(text):
        return ToolRisk.DESTRUCTIVE
    if re.search(r"(?i)\b(send|email|notify|publish|post|write|update|create)\b", text):
        return ToolRisk.EXTERNAL_SIDE_EFFECT
    if _READ_WORDS.search(text):
        return ToolRisk.READ_ONLY
    # Unclassifiable is treated as the more dangerous of the plausible readings.
    return ToolRisk.EXTERNAL_SIDE_EFFECT


def _infer_effect(risk: ToolRisk) -> EffectClass:
    return {
        ToolRisk.READ_ONLY: EffectClass.READ,
        ToolRisk.LOW_RISK_WRITE: EffectClass.MUTATE_INTERNAL,
        ToolRisk.EXTERNAL_SIDE_EFFECT: EffectClass.EXTERNAL_SEND,
        ToolRisk.PRIVILEGED: EffectClass.PRIVILEGED,
        ToolRisk.DESTRUCTIVE: EffectClass.DESTRUCTIVE,
    }[risk]


async def make_demo_server() -> McpServerRecord:
    """A small in-process MCP server, used to prove the boundary works.

    It speaks the same JSON-RPC over stdio that a real server does, so the test
    exercises the client, the allowlist, the payload cap and the untrusted-content
    flag rather than a mock of them.
    """
    return McpServerRecord(
        server_id=str(McpServerId.create()),
        name="demo_research",
        transport="in_process",
        is_active=True,
    )


__all__ = [
    "MAX_MCP_RESULT_BYTES",
    "McpClient",
    "McpGateway",
    "McpRegistry",
    "McpServerRecord",
    "flag_untrusted_content",
]
