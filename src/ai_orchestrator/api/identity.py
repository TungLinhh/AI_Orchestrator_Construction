"""Tenant-bound human login and membership administration.

No self-registration or external identity provider is implied. An existing tenant
administrator provisions members; bootstrap remains an operator responsibility.
"""

from __future__ import annotations

from functools import partial
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, select, text

from ai_orchestrator.api.deps import ApiContext, get_context
from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    PreconditionError,
)
from ai_orchestrator.domain.ids import OrganizationId, make_id
from ai_orchestrator.persistence.models import User
from ai_orchestrator.security.auth import (
    ACCESS_TOKEN_TTL,
    current_human_principal,
    hash_password,
    issue_access_token,
    verify_password,
)

router = APIRouter(prefix="/identity", tags=["identity"])
# Work is bounded across requests, and cancellation waits for the hash operation
# before returning its worker permit. Missing users perform the same hash work.
_DUMMY_HASH = hash_password("synthetic-dummy-password-never-an-account")


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str = Field(min_length=1, max_length=40)
    email: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1, max_length=1024)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class CreateMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^\s@]+@[^\s@]+$")
    display_name: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: Literal["member", "org_admin"] = "member"


class UpdateMemberRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["member", "org_admin"] | None = None
    is_active: bool | None = None
    revoke_sessions: bool = False


def _public_member(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "organization_id": user.organization_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active and user.disabled_at is None,
        "is_privileged": user.is_privileged,
        "mfa_enabled": user.mfa_enabled,
    }


def _require_real_human(ctx: ApiContext) -> None:
    ctx.require_human()
    if get_settings().api_auth_disabled:
        raise PreconditionError("Enable authentication before managing human credentials")


async def _hash_work(request: Request, fn: Any, *args: Any) -> Any:
    return await anyio.to_thread.run_sync(
        partial(fn, *args), limiter=request.app.state.identity_hash_limiter
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    if get_settings().api_auth_disabled:
        raise PreconditionError("Authentication is disabled on this local instance")
    # A supplied tenant locates credentials, never grants membership. This session
    # uses ao_app with RLS; there is no global user lookup or owner-role connection.
    async with request.app.state.db.tenant_session(body.organization_id) as session:
        user = (
            await session.execute(
                select(User).where(
                    User.organization_id == body.organization_id,
                    User.email == body.email.strip().lower(),
                )
            )
        ).scalar_one_or_none()
        valid = await _hash_work(
            request,
            verify_password,
            body.password.get_secret_value(),
            (user.password_hash if user else None) or _DUMMY_HASH,
        )
        if (
            not valid
            or user is None
            or not user.is_active
            or user.disabled_at is not None
            or user.mfa_enabled
        ):
            # MFA-enrolled accounts cannot bypass the unavailable second factor.
            raise AuthorizationError("Credentials are not valid")
        actor = Actor(
            id=user.id,
            kind=ActorType.HUMAN,
            organization_id=OrganizationId(user.organization_id),
            display_name=user.display_name,
            is_privileged_human=user.is_privileged,
        )
        token = issue_access_token(
            user_id=user.id,
            organization_id=user.organization_id,
            role=user.role,
            is_org_admin=user.is_org_admin,
            is_privileged=user.is_privileged,
            token_version=user.token_version,
        )
        user.last_login_at = func.now()
        await AuditService(session, user.organization_id).record(
            actor=actor, action="identity.login", resource_type="user", resource_id=user.id
        )
        result = {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
            "member": _public_member(user),
        }
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/me")
async def me(response: Response, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    _require_real_human(ctx)
    user = await ctx.session.get(User, str(ctx.actor.id))
    if user is None:
        raise AuthorizationError("Credentials are not valid")
    response.headers["Cache-Control"] = "no-store"
    return _public_member(user)


@router.post("/logout")
async def logout(ctx: ApiContext = Depends(get_context)) -> dict[str, bool]:
    _require_real_human(ctx)
    # Atomic increment serializes concurrent revocations without lost updates.
    from sqlalchemy import update

    await ctx.session.execute(
        update(User)
        .where(User.id == str(ctx.actor.id), User.organization_id == ctx.organization_id)
        .values(token_version=User.token_version + 1)
    )
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="identity.logout_all_sessions",
        resource_type="user",
        resource_id=str(ctx.actor.id),
    )
    return {"revoked_all_sessions": True}


@router.post("/password")
async def change_password(
    body: PasswordRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, bool]:
    _require_real_human(ctx)
    user = (
        await ctx.session.execute(
            select(User)
            .where(User.id == str(ctx.actor.id), User.organization_id == ctx.organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if user.token_version != ctx.principal.token_version or not await _hash_work(
        request, verify_password, body.current_password.get_secret_value(), user.password_hash
    ):
        raise AuthorizationError("Credentials are not valid")
    user.password_hash = await _hash_work(
        request, hash_password, body.new_password.get_secret_value()
    )
    user.token_version += 1
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor,
        action="identity.password_changed",
        resource_type="user",
        resource_id=user.id,
    )
    return {"revoked_all_sessions": True}


@router.post("/members", status_code=201)
async def create_member(
    body: CreateMemberRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    _require_real_human(ctx)
    ctx.require_admin()
    await _membership_lock(ctx)
    email = body.email.strip().lower()
    existing = await ctx.session.scalar(
        select(User.id).where(User.organization_id == ctx.organization_id, User.email == email)
    )
    if existing:
        raise ConflictError("A member with this email already exists")
    admin = body.role == "org_admin"
    user = User(
        id=make_id("usr"),
        organization_id=ctx.organization_id,
        email=email,
        display_name=body.display_name.strip(),
        password_hash=await _hash_work(request, hash_password, body.password.get_secret_value()),
        role=body.role,
        is_org_admin=admin,
        is_privileged=admin,
    )
    ctx.session.add(user)
    await ctx.session.flush()
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor, action="identity.member_created", resource_type="user", resource_id=user.id
    )
    return _public_member(user)


async def _membership_lock(ctx: ApiContext) -> None:
    await ctx.session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:org), hashtext('identity.members'))"),
        {"org": ctx.organization_id},
    )
    # A caller may have waited while another administrator revoked its token.
    # Re-check after the lock before making an administrative mutation.
    (await current_human_principal(ctx.session, ctx.principal)).require_admin()


@router.get("/members")
async def list_members(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    _require_real_human(ctx)
    ctx.require_admin()
    rows = (
        await ctx.session.scalars(
            select(User)
            .where(User.organization_id == ctx.organization_id)
            .order_by(User.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {"items": [_public_member(user) for user in rows], "limit": limit, "offset": offset}


@router.patch("/members/{user_id}")
async def update_member(
    user_id: str, body: UpdateMemberRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    _require_real_human(ctx)
    ctx.require_admin()
    await _membership_lock(ctx)
    user = (
        await ctx.session.execute(
            select(User)
            .where(User.id == user_id, User.organization_id == ctx.organization_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Member not found", resource_type="user", resource_id=user_id)
    removes_admin = body.is_active is False or body.role == "member"
    if user.is_privileged and user.is_active and user.disabled_at is None and removes_admin:
        other_admin = await ctx.session.scalar(
            select(User.id)
            .where(
                User.organization_id == ctx.organization_id,
                User.id != user.id,
                User.is_privileged.is_(True),
                User.is_active.is_(True),
                User.disabled_at.is_(None),
            )
            .limit(1)
        )
        if not other_admin:
            raise PreconditionError("The tenant must retain an active administrator")
    if body.role is not None:
        user.role = body.role
        user.is_org_admin = body.role == "org_admin"
        user.is_privileged = body.role == "org_admin"
    if body.is_active is not None:
        user.is_active = body.is_active
        user.disabled_at = None if body.is_active else func.now()
    # Role/status changes revoke existing tokens, including later reactivation.
    if body.role is not None or body.is_active is not None or body.revoke_sessions:
        user.token_version += 1
    await ctx.session.flush()
    await AuditService(ctx.session, ctx.organization_id).record(
        actor=ctx.actor, action="identity.member_updated", resource_type="user", resource_id=user.id
    )
    return _public_member(user)
