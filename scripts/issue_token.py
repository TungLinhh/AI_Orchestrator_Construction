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

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.models import Organization, User
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.security.auth import issue_access_token


async def issue(email: str, org_slug: str | None) -> int:
    settings = get_settings()
    admin = Database.from_settings(use_admin_role=True)
    try:
        async with admin.session() as session:
            stmt = select(User).where(User.email == email)
            user = (await session.execute(stmt)).scalar_one_or_none()
            if user is None:
                sys.stderr.write(f"no user with email {email!r}\n")
                return 1

            org_stmt = select(Organization).where(Organization.id == user.organization_id)
            org = (await session.execute(org_stmt)).scalar_one()
            if org_slug and org.slug != org_slug:
                sys.stderr.write(f"user {email!r} belongs to {org.slug!r}, not {org_slug!r}\n")
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
            print(f"organization: {org.slug} ({org.id})")
            print(f"user:         {user.display_name} <{user.email}>  role={user.role}")
            print("expires in:   30 minutes")
            print()
            print(token)
            return 0
    finally:
        await admin.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default="ceo@demo.invalid")
    parser.add_argument("--org", default=None, help="expected organization slug")
    args = parser.parse_args()
    return asyncio.run(issue(args.email, args.org))


if __name__ == "__main__":
    raise SystemExit(main())
