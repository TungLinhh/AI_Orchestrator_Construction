"""Create the local operator's `users` row, so the approver foreign key holds.

## Why this has to exist at all

`approvals.decided_by` is a **composite foreign key to `users`** -- `(organization_id, id)`.
So a decision cannot be attributed to somebody who does not exist, which is a rule worth
having: an approval signed by a name nobody holds is not an approval.

The consequence here is that with authentication off, the principal's id `dev:no-auth` must
be a real `users` row or **Approve cannot record who approved**. Measured, in order:

1. `no_auth_principal` was `ActorType.SERVICE`, so `ApprovalService.decide` refused with
   *"an agent cannot approve an action; a human approver is required"* -- for a person, at a
   browser, who was the human. Fixed in `security/auth.py`.
2. With the kind corrected, the write then failed with
   `ForeignKeyViolationError: ... violates foreign key constraint
   "fk_approvals_decided_by_users"` on `UPDATE approvals SET ... decided_by = 'dev:no-auth'`.

The alternative to this script is to weaken the foreign key or to write the actor's name
into a free-text column. **Both are worse**, and the reason is the whole point of the
constraint: a decision must be attributable to a row that could be looked up, disabled,
and audited. Provisioning the row keeps the constraint and makes the claim true.

## It is provisioning, not impersonation

The row is a **local operator account** in the development tenant, named as such, with
`is_org_admin` and `is_privileged` true so the second gate in `decide` also passes. It is
created only for the organization that holds the dossier catalogue, only when
`api_auth_disabled` is on, and the id is `dev:no-auth` -- so every row written through it
says *authentication was off*, never a person's name.

`password_hash` is a literal that is **not** a hash of any password, and the account cannot
be reached without `api_auth_disabled`, which cannot be enabled in production or in tests
(both asserted). It is there to satisfy a `NOT NULL`, not to be a credential.

Idempotent, and it re-points an existing row rather than failing, so running it twice
changes nothing and a row that drifted is repaired.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.persistence.session import Database  # noqa: E402


#: The id the principal presents, per organisation. **Not a constant.**
#:
#: This module provisioned the fixed id `dev:no-auth` while
#: `security.auth.operator_id_for` had already moved to `dev:no-auth:<suffix>`,
#: because `users` is keyed on `id` alone and one id belongs to exactly one
#: organisation. So the row it created was not the row the FK needed: provisioning
#: for a second tenant failed on `pk_users` with the first tenant's id, and the
#: first gate of the first hiring stage returned a foreign-key violation naming
#: `decided_by`.
#:
#: Derived from the same function the principal uses, so a row and an actor cannot
#: disagree.
def operator_id(organization_id: str) -> str:
    from ai_orchestrator.security.auth import operator_id_for

    return operator_id_for(organization_id)


EMAIL = "local-operator@example.invalid"
#: Not a hash of anything. Satisfies `NOT NULL` and cannot be used to authenticate,
#: because the only path to this row requires `api_auth_disabled`.
NOT_A_PASSWORD = "authentication-is-off-there-is-no-password"

_UPSERT = """
INSERT INTO users (
    id, organization_id, email, display_name, password_hash, role,
    is_org_admin, is_privileged, mfa_enabled, token_version, is_active)
VALUES (CAST(:i AS varchar(40)), CAST(:o AS varchar(40)), :e, :n, :h, 'org_admin',
        true, true, false, 1, true)
ON CONFLICT (id, organization_id) DO UPDATE
SET display_name = EXCLUDED.display_name,
    is_org_admin = true,
    is_privileged = true,
    is_active = true,
    updated_at = now()
RETURNING id
"""

_VERIFY = """
SELECT id, display_name, role, is_privileged, is_active
FROM users WHERE id = CAST(:i AS varchar(40)) AND organization_id = CAST(:o AS varchar(40))
"""


async def provision(organization_id: str | None) -> int:
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.session() as session:
            if organization_id is None:
                organization_id = (
                    await session.execute(
                        text(
                            "SELECT organization_id FROM sop_definitions "
                            " GROUP BY organization_id ORDER BY count(*) DESC LIMIT 1"
                        )
                    )
                ).scalar()
            if organization_id is None:
                print(
                    "  no organization holds the dossier catalogue; run `make ingest` first",
                    file=sys.stderr,
                )
                return 1
            written = await session.execute(
                text(_UPSERT),
                {
                    "i": operator_id(organization_id),
                    "o": str(organization_id),
                    "e": EMAIL,
                    "n": "Local operator (authentication off)",
                    "h": NOT_A_PASSWORD,
                },
            )
            await session.commit()
            row = (
                (
                    await session.execute(
                        text(_VERIFY),
                        {
                            "i": operator_id(organization_id),
                            "o": str(organization_id),
                        },
                    )
                )
                .mappings()
                .one_or_none()
            )
    finally:
        await db.dispose()

    print(f"  org: {organization_id}")
    if row is None:
        print("  FAILED: the row is not there after writing it")
        return 1
    # Read back in the same run. A provisioning step that reports a row it did not write is
    # the F178 shape -- `seed_free_model.py` printed "repaired 3 of 5" three times against a
    # table it had rolled back.
    print(
        f"  {row['id']} · {row['display_name']} · {row['role']} · "
        f"privileged={row['is_privileged']} · active={row['is_active']}"
    )
    print(f"  (was {written.scalar_one()})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    args = parser.parse_args()
    return asyncio.run(provision(args.org))


if __name__ == "__main__":
    raise SystemExit(main())
