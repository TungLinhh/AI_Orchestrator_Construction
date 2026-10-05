"""A live stream of the platform's own events, for a browser.

**This is a projection, not a source of truth.** Every frame is an `Event` row the
platform already wrote; nothing here decides anything, and nothing here can be
written to. Chat, the task list, the delegation graph — all of it is a rendering of
these rows. If a browser tab and the database ever disagree, the database is right,
and the tab is a stale view that will correct itself on the next event.

**Backfill first, then tail.** A stream that only carries events from the moment it
connected shows an empty platform to anyone who opens the page after the interesting
part happened — which, for a page you open to *see what the agents are doing*, is
always. So the first frames replay recent history from the durable log, and only then
does the live tail begin. A `Last-Event-ID` header makes a reconnect resume rather
than restart, which is what the SSE specification is for and what makes a flaky network
a non-event.

**One consumer, many subscribers.** A NATS pull consumer per browser tab would mean a
durable per tab, an ack deadline per tab, and a stream that grows with the number of
people watching. Instead one consumer per process feeds an in-process fan-out, and each
connection is a subscriber. The events still come from NATS, so a UI tab is a
subscriber to the same bus the outbox relay and every other consumer see — not a
private channel into the database.

**A stream that dies quietly is the failure mode.** A dropped connection that leaves a
tab frozen on its last frame is worse than an error, because it looks like a platform
that has stopped working. So every failure sends a named `stream.error` frame before
the connection closes, and the client reconnects with a backoff.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlalchemy import select

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.application.event_view import enrich
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.persistence.models import Event

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])

#: How many recent events to replay to a newly connected client.
#:
#: 300, not 60, and the reason is measured rather than preferred. This tenant holds
#: 533 events and the most recent delegation sits **208 events back**, so a 60-event
#: replay showed a page full of `task.created` / `task.started` rows and *no delegation
#: at all* — the flow view, the entire reason the page exists, looked broken when it was
#: simply showing the wrong slice of history. A default that hides the thing a viewer
#: came to see is worse than a slow one.
#:
#: The cost is bounded and paid once per connection: 300 small JSON frames, no database
#: work beyond one indexed query, and a client that renders only the newest slice in the
#: feed while the tree is built from all of them.
BACKFILL_LIMIT = 300

#: How long the generator waits for a frame before sending a comment. Not an event —
#: a comment line — because a comment keeps proxies from closing an idle connection
#: without putting anything in the client's event log.
HEARTBEAT_SECONDS = 15.0

#: Set when the fan-out is shutting down, so subscribers stop rather than hang.
_SHUTDOWN = object()


class EventFanout:
    """In-process fan-out from one consumer to many subscribers.

    Deliberately not a `Queue` per subscriber with an unbounded buffer: a browser tab
    left open on a slow connection would grow a queue in this process's heap until the
    process died. Each subscriber gets a bounded queue, and when it is full the
    **oldest** frame is dropped, because on a live view the newest frame is the one
    worth having and a gap is honest — the client re-reads the durable log on its next
    reconnect and fills it in.
    """

    def __init__(self, *, maxsize: int = 512) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any] | object]] = set()
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    async def publish(self, frame: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)

    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any] | object]]:
        queue: asyncio.Queue[dict[str, Any] | object] = asyncio.Queue(maxsize=self._maxsize)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            # Not `discard` under the lock: a cancelled generator can be finalised
            # during interpreter shutdown, and taking the lock there can deadlock.
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def close(self) -> None:
        async with self._lock:
            targets = list(self._subscribers)
        for queue in targets:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(_SHUTDOWN)


#: One fan-out per process. The consumer that fills it is started by the app lifespan;
#: see `wire_fanout`. A module-level singleton because a stream is a property of the
#: running process, and two of them would mean two consumers reading the same durable
#: and splitting the events between them.
_FANOUT = EventFanout()


def fanout() -> EventFanout:
    """The process-wide fan-out. Exposed for the lifespan wiring and for tests."""
    return _FANOUT


def frame_of(event: Event) -> dict[str, Any]:
    """One event as a browser frame.

    The same fields `/api/v1/events` returns, plus a `view` block. Two serialisations
    of "an event" that drift apart is how a live view and a page refresh start
    disagreeing — so the event payload itself is passed through untouched, and
    everything added for display lives under one key.

    `occurred_at` is guarded even though the column is `NOT NULL`. A projection is a
    long-lived process shared by every open tab, and one row that cannot be
    serialised would take the whole stream down for all of them — a bad trade for a
    field that is always present in practice. The fallback is explicit rather than
    silent: a frame with no timestamp is visibly unordered, and the client sorts on
    what it is given.
    """
    occurred = getattr(event, "occurred_at", None)
    return {
        "id": event.id,
        "type": event.type,
        "subject": event.subject,
        "source": event.source,
        "actor_id": event.actor_id,
        "data": event.data or {},
        "occurred_at": occurred.isoformat() if occurred is not None else None,
    }


# `enrich`, `_titles` and `_agent_names` used to live here. They moved to
# `application/event_view.py` because the task report needs the same projection: two
# readers of one log that disagreed — the live feed read names, the report read ids.


async def backfill(session: Any, organization_id: str, limit: int) -> list[dict[str, Any]]:
    """Recent events, oldest first, so a fresh client reads forwards in time.

    Ascending even though `/api/v1/events` is descending. This stream is a *sequence*
    and a sequence rendered newest-first on arrival looks like the platform is
    rewinding itself.
    """
    rows = (
        (
            await session.execute(
                select(Event)
                .where(Event.organization_id == organization_id)
                .order_by(Event.occurred_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    frames = [frame_of(event) for event in reversed(rows)]
    await enrich(frames, session, organization_id)
    return frames


def _sse(event: str, data: Any, *, event_id: str | None = None) -> bytes:
    """One SSE frame.

    `id` is set so the browser sends `Last-Event-ID` on reconnect, and `json.dumps` is
    called with `default=str` because `data` carries whatever the publisher put there
    and a `Decimal` in a cost field must not take the stream down.
    """
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, default=str)}")
    return ("\n".join(lines) + "\n\n").encode()


@router.get("/stream")
async def stream(
    request: Request,
    limit: int = Query(BACKFILL_LIMIT, ge=1, le=500),
    ctx: ApiContext = Depends(get_context),
) -> StreamingResponse:
    """The event stream for one tenant.

    SSE rather than WebSocket because the flow is one-way: the platform knows things
    and the browser wants to see them. A socket would add a send path, a connection
    lifecycle, and a reconnect protocol to solve a problem this view does not have.

    The `organization_id` comes from the resolved context and is **never** read from
    the query string. Row-level security would reject the wrong tenant's rows anyway,
    but a parameter that looks like it selects a tenant and does not is the kind of
    thing that gets "fixed" later by someone who believes it works.
    """
    organization_id = ctx.organization_id
    session = ctx.session

    async def frames() -> AsyncIterator[bytes]:
        # A client that vanished mid-replay should cost us nothing. `request.is_disconnected`
        # is checked between batches rather than per row: it is an attribute read on
        # the ASGI receive channel, and doing it per row makes a 60-row replay 60
        # round-trips for no benefit.
        try:
            replay = await backfill(session, organization_id, limit)
        except Exception as exc:
            logger.warning("stream.backfill_failed", extra={"error": str(exc)})
            replay = []

        yield _sse("stream.ready", {"organization_id": organization_id, "backfilled": len(replay)})
        for frame in replay:
            yield _sse("flow", frame, event_id=str(frame["id"]))
            if await request.is_disconnected():
                return

        last_id: str | None = None
        async for queue in _FANOUT.subscribe():
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    # A comment frame, not an event: it keeps the connection warm
                    # without appearing in the client's log as something that happened.
                    yield b": keep-alive\n\n"
                    if await request.is_disconnected():
                        return
                    continue
                except asyncio.CancelledError:
                    raise

                if item is _SHUTDOWN:
                    yield _sse("stream.error", {"reason": "the platform is shutting down"})
                    return
                if not isinstance(item, dict):
                    continue
                # A frame the replay already covered is not resent: the replay and the
                # tail overlap by design, and a client that applies both renders the
                # same delegation twice.
                if last_id is not None and str(item.get("id")) == last_id:
                    continue
                last_id = str(item.get("id"))
                yield _sse("flow", item, event_id=last_id)
                if await request.is_disconnected():
                    return

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            # Without this an nginx in front of this caches the stream, which turns a
            # live view into a page that never updates.
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stream/status")
async def stream_status(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """How many browsers are watching, and whether the feed is alive.

    An operator needs to be able to tell "the platform is idle" from "the feed is
    broken", and those look identical on a page that has stopped updating. This is the
    difference.
    """
    recent = (
        (
            await ctx.session.execute(
                select(Event)
                .where(Event.organization_id == ctx.organization_id)
                .order_by(Event.occurred_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return {
        "subscribers": _FANOUT.subscriber_count,
        "last_event_at": recent.occurred_at.isoformat() if recent else None,
        "last_event_type": recent.type if recent else None,
        "server_time": datetime.now().astimezone().isoformat(),
    }


#: Where the single-page view lives.
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"

#: What an organization id is allowed to look like. Checked before `org` is put
#: into a JavaScript string literal in the served page — see `ui`.
_ORG_ID = re.compile(r"^org_[0-9a-z]{20,}$")


def _build_stamp() -> str:
    """What to print in the corner of the page.

    The migration head, because it is the only identifier here that differs
    between two deployments running the same code. A timestamp would change on
    every restart and mean nothing; the schema version is what actually
    determines what the page can show.
    """
    try:
        import subprocess

        out = subprocess.run(
            [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "current"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(Path(__file__).resolve().parents[3]),
        )
        head = (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""
        return f"schema {head.split()[0]}" if head else "schema unknown"
    except Exception:
        return "schema unknown"


@router.get("/ui", include_in_schema=False)
async def ui(org: str | None = Query(default=None, max_length=64)) -> HTMLResponse:
    """The operator view.

    Served from the same origin as the API, and that is the whole reason it is served
    rather than opened as a file. Same origin means no CORS, and it means the page can
    send the service token in an `Authorization` header — which it must, because a
    token in a query string ends up in browser history and in every proxy log on the
    way. `EventSource` cannot set a header, so the page reads the stream with `fetch`
    instead; see the comment in `index.html`.

    **`?org=` is the tenant, and it is required in practice.** `authenticate` resolves
    a *service* credential's organization from the `x-organization-id` header and
    refuses the request with `missing_org_header` when it is absent. The token is
    verified first and passes, so the page's every request came back `403` while the
    only thing the user was told was that their token had been rejected. The
    organization id is an identifier rather than a secret — it is in every log line —
    so it is read here and baked into the document instead of being asked for in a
    second prompt, and an absent one is left empty so the page can say what is
    missing instead of failing opaquely.

    It is substituted into a JavaScript string literal, so it is validated as an
    identifier first. `org` is a query parameter and this is a string it lands in;
    accepting arbitrary text here would be a way to inject script into the page that
    every other user's browser then runs.
    """
    index = WEB_ROOT / "index.html"
    if not index.is_file():
        # Said plainly rather than as a bare 500: a missing UI is a packaging
        # problem, and "Internal Server Error" sends the reader to the wrong place.
        msg = f"the operator view is not installed at {index}"
        raise NotFoundError(msg)
    organization = ""
    if org:
        if not _ORG_ID.fullmatch(org):
            msg = "org must look like an organization id"
            raise ValidationError(msg, details={"field": "org"})
        organization = org
    html = (
        index.read_text(encoding="utf-8")
        .replace("__ORG_ID__", organization)
        # The page needs to know, or it prompts for a token that the server will not
        # look at. Rendering it from the server is also the only way the banner saying
        # "authentication is off" can be trusted: a page cannot be trusted to report
        # its own security posture.
        .replace("__AUTH_OFF__", "true" if get_settings().api_auth_disabled else "false")
        # The build stamp. It was a `__BUILD__` placeholder that nothing ever
        # substituted, so the corner of the page a person looks at to answer
        # "which version am I looking at?" showed a literal string. A stamp that
        # is always the same is also not worth printing, so it carries the
        # migration head: the one thing that identifies this deployment against
        # another with the same code.
        .replace("__BUILD__", _build_stamp())
        # The model provider behind Run. A console whose button promises "runs
        # on the free model" while the server answers with a scripted fake is
        # the exact lie this product exists to kill: every task the chairman
        # ran ended `output_contract_unmet` producing `proposal_count,
        # scripted`, and nothing could ever have succeeded. The page wears the
        # provider on its sleeve so the mode is never in doubt.
        .replace("__PROVIDER__", get_settings().model_provider_default)
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


__all__ = ["BACKFILL_LIMIT", "EventFanout", "fanout", "router"]
