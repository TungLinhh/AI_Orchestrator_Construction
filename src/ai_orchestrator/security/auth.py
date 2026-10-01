"""Authentication.

Three identity classes, three credentials, and no shared secret between them:

  human      a signed JWT, subject = user id. Short-lived by design.
  service    a shared internal secret presented as a bearer token. For
             service-to-service calls, never for a user.
  agent      an agent's identity, established by the workflow that runs it. An
             agent does not authenticate at the API; the control plane acts on
             its behalf using its own credentials, which is what stops an agent
             holding a long-lived key.

The distinction is the point. A single shared API key for every agent is the
design that lets one compromised agent act as the CEO.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ai_orchestrator.config.settings import Settings, get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import AuthorizationError
from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.telemetry.logging import get_logger

#: Access tokens are short. A leaked access token should stop working before the
#: operator notices the leak, not after.
ACCESS_TOKEN_TTL = timedelta(minutes=30)

#: Issuer/audience. Both are checked: a token minted for a different service must
#: not be accepted here, which is the confused-deputy case.
ISSUER = "ai-orchestrator"
AUDIENCE = "ai-orchestrator-api"

ALGORITHM = "HS256"

_hasher = PasswordHasher()


@dataclass(slots=True)
class Principal:
    """The authenticated caller."""

    actor: Actor
    token_version: int = 1
    is_service: bool = False

    @property
    def organization_id(self) -> str:
        return str(self.actor.organization_id)

    def require_human(self) -> Actor:
        if self.actor.kind is not ActorType.HUMAN:
            msg = "this endpoint requires a human principal"
            raise AuthorizationError(msg, details={"actor_kind": self.actor.kind.value})
        return self.actor

    def require_admin(self) -> Actor:
        actor = self.require_human()
        if not actor.is_privileged_human:
            msg = "this endpoint requires a privileged human"
            raise AuthorizationError(msg, details={"required": "is_privileged_human"})
        return actor


def hash_password(password: str) -> str:
    """Argon2id. Never a fast hash: a stolen table must be expensive to attack."""
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        _hasher.verify(stored_hash, password)
    except VerifyMismatchError, InvalidHashError:
        return False
    return True


def issue_access_token(
    *,
    user_id: str,
    organization_id: str,
    role: str,
    is_org_admin: bool,
    is_privileged: bool,
    token_version: int,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "org": organization_id,
        "role": role,
        "is_org_admin": is_org_admin,
        "is_privileged": is_privileged,
        # Stamped in the token. A password change bumps the stored version, which
        # invalidates every outstanding token without a per-token denylist.
        "tv": token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + ACCESS_TOKEN_TTL).timestamp()),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        msg = "access token has expired"
        raise AuthorizationError(msg, details={"reason": "expired"}) from exc
    except jwt.InvalidTokenError as exc:
        # The reason is deliberately not surfaced: distinguishing "wrong
        # signature" from "wrong audience" tells an attacker what to fix.
        msg = "access token is not valid"
        raise AuthorizationError(msg, details={"reason": "invalid"}) from exc


def principal_from_token(token: str, settings: Settings | None = None) -> Principal:
    claims = decode_access_token(token, settings)
    return Principal(
        actor=Actor(
            id=str(claims["sub"]),
            kind=ActorType.HUMAN,
            organization_id=OrganizationId(str(claims["org"])),
            display_name="",
            is_privileged_human=bool(claims.get("is_privileged", False)),
        ),
        token_version=int(claims.get("tv", 1)),
    )


def no_auth_principal(organization_id: str) -> Principal:
    """The principal used when `api_auth_disabled` is set.

    Separate from `service_principal` rather than a flag on it, because the two differ
    in a way a flag would hide: the service path *verifies a secret* and this one does
    not, so sharing the function would mean checking a credential that nobody presented
    and, in a deployment where none is configured, refusing every request in exactly
    the mode that exists to stop needing one.

    The actor id is `dev:no-auth` so every row written through this path is
    attributable to "authentication was off" rather than to a person, and the tenant is
    still required, because row-level security is not part of what is being turned off.

    **The kind is `HUMAN`, and it was `SERVICE`.** With `SERVICE`, every act that requires a
    person was refused in exactly the mode that exists so a person does not have to
    authenticate. The Approve button answered:

        Could not approve: an agent cannot approve an action; a human approver is required

    -- for a person, at a browser, who is the human. `ApprovalService.decide` checks
    `approver.kind is not ActorType.HUMAN` and that check is **right**: an agent must never
    approve, and it lives in the domain for the best possible reason, in a comment that says
    so. The bug was never the check. It was that this mode handed it an `ActorType.SERVICE`
    and so guaranteed its own refusal.

    `is_privileged_human` is set because the second gate in `decide` requires it, and a
    principal that is a human but cannot decide anything is the same dead end one level down.

    **What this is not.** It is not a bypass: `api_auth_disabled` cannot be enabled in
    production or in tests -- both are asserted -- and with authentication on, every request
    still resolves from a verified credential and this function is never reached. The
    attribution is unchanged: the id is still `dev:no-auth`, so a row written by clicking
    Approve says `dev:no-auth` decided it, and the banner on the page says authentication
    is off. Nobody is being impersonated; the platform is being told the truth about the
    only mode in which there is no credential to inspect.
    """
    if not organization_id:
        msg = "x-organization-id is required even with api_auth_disabled"
        raise AuthorizationError(msg, details={"reason": "missing_org_header"})
    return Principal(
        actor=Actor(
            id=operator_id_for(organization_id),
            kind=ActorType.HUMAN,
            display_name="local operator (authentication off)",
            organization_id=OrganizationId(organization_id),
            is_privileged_human=True,
        ),
        is_service=False,
    )


def operator_id_for(organization_id: str) -> str:
    """The `users` id this organisation's local operator is recorded under.

    **It is not `dev:no-auth`, and that is the whole point of this function.**

    `approvals.decided_by` is a composite foreign key --
    `(organization_id, decided_by) REFERENCES users(organization_id, id)` --
    while `users` is keyed on `id` alone. A single id therefore belongs to exactly
    one organisation, and a fixed id works in the first tenant created and in no
    other. Measured: with `dev:no-auth` used everywhere, Approve succeeded in the
    original tenant and returned 500 in every other one, because the operator row
    the foreign key needed could not be created -- the id was already taken.

    Two earlier attempts failed in ways worth recording, because both looked
    correct:

    * `ON CONFLICT (id, organization_id)` does not match the primary key, so the
      insert raised, and a raised conflict **aborts the transaction**, taking the
      approval decision with it.
    * `WHERE NOT EXISTS` is subtler and worse: `users` has RLS, so from a second
      tenant the check cannot see the row the first tenant owns, concludes none
      exists, and inserts into a primary key it could not see.

    So the id is derived from the organisation, which makes it unique per tenant
    and keeps the schema's two constraints satisfied without either one being
    argued with. The attribution survives: the row still says the decision was
    made with authentication off, and the page still says so, because the id
    still contains `no-auth`.

    This is a second string, not a second concept. One concept -- "who is
    operating with authentication off" -- with a stable marker and a
    tenant-scoped identity, which is exactly the two things the database
    requires of a principal.
    """
    return f"{NO_AUTH_ACTOR}:{organization_id[-12:]}"


#: Creates the operator's `users` row for one organisation.
#:
#: This exists because `approvals.decided_by` is a **composite** foreign key --
#: `(organization_id, decided_by) REFERENCES users(organization_id, id)` --
#: while the id itself is a fixed string. One id cannot satisfy a composite key
#: in two organisations, so clicking Approve in a tenant other than the first
#: one raised a foreign-key error and returned "Internal Server Error" with no
#: way to decide the request from the page.
#:
#: The failure was silent from the operator's side: the button was there, the
#: click registered, and the answer was a 500. Nothing about the UI said that
#: approval was impossible in this tenant -- the platform knew and said nothing.
#: Which is why this runs on the request path rather than in a setup script that
#: somebody has to remember to run.
#:
#: `password_hash` is a fixed string that is not a hash of anything: the only
#: path to this row is `api_auth_disabled`, which cannot start in production or
#: in tests (F182).
#:
#: The conflict target is the **primary key alone**, `(id)`, not the composite
#: `(id, organization_id)` the foreign key uses -- and it has to be, for two
#: reasons that were each found by running this rather than by reading it:
#:
#: 1. `users` is keyed on `id`. Naming the composite does not match the primary
#:    key, so the conflict is raised rather than handled, and a raised conflict
#:    **aborts the transaction** -- taking the approval decision down with a
#:    "current transaction is aborted" that names neither the duplicate nor the
#:    approval.
#: 2. `WHERE NOT EXISTS` is worse: `users` has RLS, so inside a second tenant the
#:    `SELECT` cannot see the row the first tenant owns, concludes none exists,
#:    and inserts -- colliding with the primary key precisely because it could
#:    not see what it was colliding with.
#:
#: So the guard has to be the database's own conflict handling on the constraint
#: that actually fires. If the row exists in *another* tenant the insert is
#: skipped and the decision fails on its own with a clear error; if it exists in
#: this tenant it is skipped and the decision succeeds.
_PROVISION_OPERATOR = """
INSERT INTO users (
    id, organization_id, email, display_name, password_hash, role,
    is_org_admin, is_privileged, mfa_enabled, token_version, is_active)
VALUES (
    CAST(:id AS varchar(40)), CAST(:org AS varchar(40)), :email,
    'Local operator', 'authentication-is-off-there-is-no-password', 'org_admin',
    true, true, false, 1, true)
ON CONFLICT (id) DO NOTHING
"""


async def ensure_local_operator(session: Any, organization_id: str) -> None:
    """Make sure the no-auth principal is a real user in this organisation.

    A failure is logged, not raised: this runs inside request handling, and
    turning a missing setup row into a 500 on every request would be worse than
    the specific request that needs it failing. The write that actually needs
    the row will fail on its own, with its own error.
    """
    from sqlalchemy import text

    try:
        await session.execute(
            text(_PROVISION_OPERATOR),
            {
                "id": operator_id_for(organization_id),
                "org": organization_id,
                "email": f"local-operator@{organization_id[-12:]}.example.invalid",
            },
        )
    except Exception as exc:
        get_logger(__name__).info(
            "auth.operator_row_unavailable", organization_id=organization_id, error=str(exc)
        )


def service_principal(organization_id: str) -> Principal:
    """Identity for trusted internal callers that present the internal secret."""
    settings = get_settings()
    expected = settings.internal_service_secret.get_secret_value()
    if not expected:
        msg = "INTERNAL_SERVICE_SECRET is not configured; service auth is disabled"
        raise AuthorizationError(msg, details={"reason": "service_auth_disabled"})
    return Principal(
        actor=Actor(
            id="svc:control-plane",
            kind=ActorType.SERVICE,
            organization_id=OrganizationId(organization_id),
        ),
        is_service=True,
    )


def check_internal_secret(presented: str | None, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    expected = settings.internal_service_secret.get_secret_value()
    if not expected or not presented:
        return False
    # Constant-time. A timing-variable comparison on a shared secret is a
    # character-by-character oracle.
    return secrets.compare_digest(presented, expected)


#: The actor every unauthenticated request is attributed to. Deliberately not a
#: person: with authentication off, an approval can be granted, and the row must not be
#: able to read as though a human granted it.
NO_AUTH_ACTOR = "dev:no-auth"

_bearer = HTTPBearer(auto_error=False)


async def authenticate(request: Request) -> Principal:
    """FastAPI dependency. Resolves a bearer token to a principal."""
    settings = get_settings()
    if settings.api_auth_disabled:
        # A named, obviously non-human actor. Not `None`, and not a human: with
        # authentication off, `POST /approvals/{id}/approve` still succeeds, and an
        # approval row that says a person approved something when nobody did is the one
        # outcome this platform exists to prevent. Every row written through this path
        # says `dev:no-auth`, so it is greppable afterwards and cannot be confused with
        # a real decision.
        #
        # The tenant is still required, and still comes from `x-organization-id`. Not
        # an oversight: it is what keeps row-level security in force. Without it every
        # query runs unbound, RLS returns nothing, and the page shows an empty platform
        # — which is indistinguishable from a platform that has stopped working.
        # Turning off authentication must not also turn off tenancy.
        return no_auth_principal(request.headers.get("x-organization-id") or "")

    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    if credentials is None:
        msg = "authentication required"
        raise AuthorizationError(msg, details={"reason": "missing_credentials"})

    presented = credentials.credentials
    settings = get_settings()

    if presented.startswith("svc."):
        if not check_internal_secret(presented[4:], settings):
            msg = "service credential is not valid"
            raise AuthorizationError(msg, details={"reason": "invalid_service_credential"})
        org = request.headers.get("x-organization-id")
        if not org:
            msg = "x-organization-id header is required for service calls"
            raise AuthorizationError(msg, details={"reason": "missing_org_header"})
        return service_principal(org)

    return principal_from_token(presented, settings)


__all__ = [
    "ACCESS_TOKEN_TTL",
    "AUDIENCE",
    "ISSUER",
    "NO_AUTH_ACTOR",
    "Principal",
    "authenticate",
    "check_internal_secret",
    "decode_access_token",
    "hash_password",
    "issue_access_token",
    "no_auth_principal",
    "principal_from_token",
    "service_principal",
    "verify_password",
]
