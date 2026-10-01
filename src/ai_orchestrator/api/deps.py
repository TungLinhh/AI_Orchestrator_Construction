"""Shared API plumbing: tenant binding, idempotency and pagination.

Three things every write endpoint needs, in one place so they cannot be
forgotten on the seventh router:

  * tenant binding. The organization comes from the authenticated principal, never
    from the request body. A body that names a different organization is rejected
    rather than ignored, because silently ignoring it produces a confusing
    success that wrote nothing.
  * idempotency. A retried POST must not create a second task. The key is
    required on the endpoints where duplication costs something, and the stored
    response is replayed rather than recomputed.
  * pagination. `limit`/`offset` with a hard ceiling, because an unbounded list
    endpoint is a denial of service against the database.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.errors import ConflictError, ValidationError
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.security.auth import Principal, authenticate

#: Hard ceiling on a page. A client asking for 100 000 rows gets 500 and a
#: message, rather than a table scan.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


@dataclass(slots=True)
class ApiContext:
    """Everything a handler needs, resolved once."""

    principal: Principal
    session: AsyncSession
    organization_id: str
    actor: Actor

    @property
    def is_admin(self) -> bool:
        return self.actor.is_privileged_human

    def require_human(self) -> Actor:
        """Refuse a non-human caller.

        Delegated rather than reimplemented so there is one definition of what
        "human" means. The handlers call `ctx.require_human()`; without this
        method they would raise `AttributeError` and return 500 on exactly the
        endpoints whose job is to refuse — an authorisation failure that looks
        like a server fault.
        """
        return self.principal.require_human()

    def require_admin(self) -> Actor:
        """Refuse a caller that is not a privileged human.

        A non-human, or an unprivileged human, gets 403. Raising here rather
        than checking `is_admin` at each call site is what stops one of eleven
        endpoints from being written to forget.
        """
        return self.principal.require_admin()


@asynccontextmanager
async def tenant_session(request: Request) -> AsyncIterator[ApiContext]:
    """Bind a session to the caller's tenant for the life of the request.

    The transaction is committed on a clean exit and rolled back on an error, so a
    handler cannot accidentally commit half its work by returning early.
    """
    principal = await authenticate(request)
    database: Database = request.app.state.db
    org_id = principal.organization_id

    async with database.tenant_session(org_id) as session:
        yield ApiContext(
            principal=principal,
            session=session,
            organization_id=org_id,
            actor=principal.actor,
        )


async def get_context(request: Request) -> AsyncIterator[ApiContext]:
    """FastAPI dependency wrapping `tenant_session`."""
    async with tenant_session(request) as ctx:
        yield ctx


def require_body_tenant(body: dict[str, Any], ctx: ApiContext) -> None:
    """Reject a body that names a different organization.

    Ignored silently would mean a 200 response for a write that went nowhere,
    which is the worst of both: the client believes it worked and the data is
    missing.
    """
    named = body.get("organization_id")
    if named is not None and str(named) != ctx.organization_id:
        msg = "organization_id in the request body does not match the authenticated organization"
        raise AuthorizationDenied(msg)


class AuthorizationDenied(ValidationError):
    """A tenant mismatch. Distinct from a policy denial so the client can tell
    a bug from a refusal."""


def page_params(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> tuple[int, int]:
    """Pagination, validated by FastAPI before the handler runs."""
    return limit, offset


def paginate(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items[offset : offset + limit],
        "limit": limit,
        "offset": offset,
        "returned": min(limit, max(0, len(items) - offset)),
        "total": len(items),
    }


def request_hash(payload: dict[str, Any]) -> str:
    """Stable hash of a request body, for idempotency mismatch detection."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def check_idempotency(
    ctx: ApiContext, *, key: str | None, operation: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    """Return a previously stored response for this key, or None.

    A replay with a *different* body is a client bug and is rejected rather than
    answered with the first response: answering would make two different
    operations look like one.
    """
    if not key:
        return None
    stored = await ctx.session.execute(
        text(
            "SELECT request_hash, response_body, resource_id FROM idempotency_records "
            "WHERE idempotency_key = :key AND operation = :op"
        ),
        {"key": key, "op": operation},
    )
    row = stored.first()
    if row is None:
        return None
    if row.request_hash != request_hash(payload):
        msg = f"idempotency key {key!r} was already used for a different request body"
        raise ConflictError(msg, details={"idempotency_key": key, "operation": operation})
    return {"resource_id": row.resource_id, "response": row.response_body, "replayed": True}


async def store_idempotency(
    ctx: ApiContext,
    *,
    key: str | None,
    operation: str,
    payload: dict[str, Any],
    resource_id: str | None,
    response: dict[str, Any] | None = None,
) -> None:
    """Record that an operation has been performed.

    `ON CONFLICT DO NOTHING`: a concurrent duplicate is an expected race, and the
    winner's record is the authoritative one.
    """
    if not key:
        return
    await ctx.session.execute(
        text(
            """
            INSERT INTO idempotency_records
                (id, organization_id, idempotency_key, operation, request_hash,
                 resource_id, response_body)
            VALUES (gen_random_uuid()::text, :org, :key, :op, :hash, :rid, :body)
            ON CONFLICT (idempotency_key, operation) DO NOTHING
            """
        ),
        {
            "org": ctx.organization_id,
            "key": key,
            "op": operation,
            "hash": request_hash(payload),
            "rid": resource_id,
            "body": json.dumps(response or {}, default=str),
        },
    )


def idem_key(request: Request) -> str | None:
    """The `Idempotency-Key` header, if present."""
    value = request.headers.get("idempotency-key")
    return value.strip() if value and value.strip() else None


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "ApiContext",
    "AuthorizationDenied",
    "check_idempotency",
    "get_context",
    "idem_key",
    "page_params",
    "paginate",
    "request_hash",
    "require_body_tenant",
    "store_idempotency",
    "tenant_session",
]
