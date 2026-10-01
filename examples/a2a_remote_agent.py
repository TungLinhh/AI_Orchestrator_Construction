"""A remote agent in its own process, for the A2A acceptance scenario.

Acceptance scenario 2 is "agent → A2A → remote agent as a **separate process**".
A mock in the same interpreter cannot demonstrate that: shared memory, shared
event loop, shared globals. This is a real HTTP server the platform reaches over
a socket, and it is spawned the same way the MCP demo server is.

It is a real agent, not a stub. It reads the request, does the small amount of
work the prompt describes, and answers with an artifact. A server that echoed
the request would prove the transport and nothing else — which is exactly what
the acceptance scenario must not be.

Run it directly to poke at it by hand:

    uv run python examples/a2a_remote_agent.py --port 8931
    curl -s localhost:8931/.well-known/agent-card.json | jq
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from ai_orchestrator.a2a.card import PROTOCOL_VERSION
from ai_orchestrator.a2a.protocol import (
    Artifact,
    JsonRpcError,
    JsonRpcResponse,
    Message,
    MessagePart,
    RemoteTask,
    TaskState,
)

AGENT_NAME = "remote-echo-analyst"
SKILL_ID = "summarise"

app = FastAPI(title="A2A remote agent", docs_url=None, redoc_url=None)


def _card() -> dict:
    return {
        "name": AGENT_NAME,
        "description": "A separate-process agent that summarises a prompt.",
        "url": _base_url(),
        "version": "0.1.0",
        "protocolVersion": PROTOCOL_VERSION,
        "preferredTransport": "jsonrpc",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {
                "id": SKILL_ID,
                "name": "Summarise",
                "description": "Return a short, structured summary of the input.",
                "tags": ["text", "analysis"],
            }
        ],
        "securitySchemes": {"none": {"type": "none"}},
    }


def _base_url() -> str:
    host = getattr(app.state, "host", "127.0.0.1")
    port = getattr(app.state, "port", 8931)
    return f"http://{host}:{port}"


@app.get("/.well-known/agent-card.json")
async def agent_card() -> JSONResponse:
    return JSONResponse(_card())


def _answer(prompt: str) -> str:
    """Do the small amount of work the skill describes.

    Word and character counts, plus a content digest. Real output, derived from
    the input, so a test can assert on it: a server that echoed the request
    would prove the transport and nothing else.
    """
    words = [w for w in re.split(r"\s+", prompt.strip()) if w]
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    if not words:
        return "empty input"
    longest = max(words, key=len)
    return f"words={len(words)} chars={len(prompt)} longest={longest} digest={digest}"


@app.post("/")
async def rpc(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=400,
            content=JsonRpcResponse(
                error=JsonRpcError(code=-32700, message="parse error")
            ).model_dump(),
        )

    rpc_id = body.get("id") if isinstance(body, dict) else None
    method = body.get("method") if isinstance(body, dict) else None

    if method != "message/send":
        return JSONResponse(
            status_code=400,
            content=JsonRpcResponse(
                id=rpc_id,
                error=JsonRpcError(code=-32601, message=f"method not found: {method}"),
            ).model_dump(),
        )

    try:
        message = Message.model_validate(body.get("params", {}).get("message", {}))
    except Exception as exc:
        # A malformed request is the peer's problem, reported in the protocol it
        # speaks. Raising instead returns a bare 500 with a stack trace, which
        # tells the caller nothing about what to fix and leaks internals.
        return JSONResponse(
            status_code=400,
            content=JsonRpcResponse(
                id=rpc_id,
                error=JsonRpcError(code=-32602, message=f"invalid params: {exc}"),
            ).model_dump(),
        )

    remote = RemoteTask(
        id=f"task_{hashlib.sha256(message.message_id.encode()).hexdigest()[:20]}",
        state=TaskState.COMPLETED,
        artifacts=(
            Artifact(
                name="summary",
                parts=(
                    MessagePart(
                        kind="text",
                        text=_answer(message.text),
                    ),
                ),
            ),
        ),
    )
    return JSONResponse(
        content=JsonRpcResponse(
            id=rpc_id, result={"kind": "task", "task": remote.as_wire()}
        ).model_dump()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="A remote A2A agent, in its own process.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8931)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()

    import uvicorn

    app.state.host = args.host
    app.state.port = args.port
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
