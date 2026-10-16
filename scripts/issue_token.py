#!/usr/bin/env python
"""Issue an access token for a seeded user.

The demo CEO has no usable password hash on purpose: this repository must not
contain a credential that works. The token is minted from the server's own JWT
secret, which is the only way to authenticate against a local instance without
putting a password in source control.

    uv run python scripts/issue_token.py --email ceo@demo.invalid
    uv run python scripts/issue_token.py --email ceo@demo.invalid --org <slug>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.audit.service import AuditService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.persistence.models import Organization, User
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.security.auth import issue_access_token


async def issue(email: str, org_slug: str | None) -> int:
    settings = get_settings()
    if not settings.jwt_secret.get_secret_value():
        sys.stderr.write("JWT_SECRET must be configured before issuing a human token\n")
        return 1
    database = Database.from_settings(settings)
    try:
        async with database.session() as session:
            query = select(Organization)
            if org_slug:
                query = query.where(Organization.slug == org_slug)
            organizations = (await session.scalars(query.limit(2))).all()
            if not organizations:
                sys.stderr.write("selected organization does not exist\n")
                return 1
            if len(organizations) != 1:
                sys.stderr.write("--org is required when more than one organization exists\n")
                return 1
            org = organizations[0]
            if org.status != "active":
                sys.stderr.write("selected organization is not active\n")
                return 1
            org_id, slug = str(org.id), org.slug
        async with database.tenant_session(org_id) as session:
            user = await session.scalar(
                select(User).where(
                    User.organization_id == org_id, User.email == email.strip().lower()
                )
            )
            if (
                user is None
                or not user.is_active
                or user.disabled_at is not None
                or user.mfa_enabled
            ):
                sys.stderr.write("selected user cannot receive an operator token\n")
                return 1
            token = issue_access_token(
                user_id=user.id,
                organization_id=user.organization_id,
                role=user.role,
                is_org_admin=user.is_org_admin,
                is_privileged=user.is_privileged,
                token_version=user.token_version,
                settings=settings,
            )
            await AuditService(session, org_id).record(
                actor=Actor(
                    id="svc:operator-cli",
                    kind=ActorType.SERVICE,
                    organization_id=OrganizationId(org_id),
                    display_name="Local operator token issuance",
                ),
                action="identity.operator_token_issued",
                resource_type="user",
                resource_id=user.id,
                context={"method": "operator-cli", "expires_in": 1800},
            )
            description = f"{user.display_name} <{user.email}>  role={user.role}"
        # The token is shown only after the audit transaction commits.
        print(f"organization: {slug} ({org_id})")
        print(f"user:         {description}")
        print("expires in:   30 minutes")
        print()
        print(token)
        return 0
    finally:
        await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="ceo@demo.invalid")
    parser.add_argument("--org", default=None, help="expected organization slug")
    args = parser.parse_args()
    return asyncio.run(issue(args.email, args.org))


if __name__ == "__main__":
    raise SystemExit(main())
