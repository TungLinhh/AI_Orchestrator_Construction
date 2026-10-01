"""API rate limiting.

An in-process sliding window, per key. Two honest limitations, documented rather
than hidden:

  * it is per instance. Behind N instances the effective limit is N times what is
    configured. The fix is a shared store, which is not worth adding until the
    limit is actually being hit across instances.
  * it is best-effort under concurrency. The check and the record are not atomic.

What it does buy: an agent loop cannot call task creation in a tight cycle and
run up a bill before anyone notices. That is the failure worth guarding against,
and it is guarded against.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from ai_orchestrator.config.settings import Settings

#: Paths that are never limited. A liveness probe that is rate-limited is a
#: false alarm, and an operator refreshing a dashboard is not the threat model.
_UNLIMITED_PREFIXES = ("/health", "/ready", "/metrics", "/docs", "/openapi.json")

#: (prefix, requests per minute) applied to the first matching rule.
_LIMITS: list[tuple[str, int]] = [
    ("/api/v1/tasks", 60),
    ("/api/v1/agents", 30),
    ("/api/v1/organizations", 20),
    ("/api/v1/approvals", 120),
]


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._rules = [(prefix, self._limit_for(prefix)) for prefix, _ in _LIMITS]

    def _limit_for(self, prefix: str) -> int:
        mapping = dict(_LIMITS)
        return mapping.get(prefix, 60)

    def _key(self, request: Request) -> str:
        # Prefer the authenticated subject: two operators behind one NAT do not
        # share a budget, and one operator behind many IPs does not get extra.
        auth = request.headers.get("authorization", "")
        if auth:
            return f"tok:{hash(auth) & 0xFFFFFF:06x}"
        forwarded = request.headers.get("x-forwarded-for")
        client = (
            forwarded.split(",")[0].strip()
            if forwarded
            else (request.client.host if request.client else "unknown")
        )
        return f"ip:{client}"

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in _UNLIMITED_PREFIXES):
            return await call_next(request)
        if request.method == "OPTIONS":
            return await call_next(request)

        limit = next(
            (n for prefix, n in self._rules if path.startswith(prefix)),
            self._settings.rate_limit_tool_invoke_per_min,
        )

        key = f"{self._key(request)}|{path}"
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60.0:
            window.popleft()

        if len(window) >= limit:
            retry_after = int(60.0 - (now - window[0])) + 1
            return JSONResponse(
                status_code=429,
                headers={"retry-after": str(retry_after)},
                content={
                    "error": {
                        "kind": "RATE_LIMITED",
                        "category": "validation_error",
                        "message": f"rate limit exceeded: {limit} requests per minute",
                        "details": {"path": path, "limit": limit, "retry_after_s": retry_after},
                        "retryable": True,
                    }
                },
            )

        window.append(now)
        # Bounded: a long-running process would otherwise accumulate one deque per
        # distinct path, which is a slow memory leak driven by client behaviour.
        if len(self._hits) > 10_000:
            cutoff = now - 120.0
            self._hits = {k: v for k, v in self._hits.items() if v and v[-1] > cutoff}

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - len(window)))
        return response


__all__ = ["RateLimitMiddleware"]
