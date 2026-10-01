"""Print the organization that holds this schema's dossier catalogue. For `make`.

## Why a script and not a shell one-liner in the Makefile

`ORGS ?= $(shell python -c "...")` with a multi-line Python program inside a make variable
does not work reliably: make expands `$(shell)` before the recipe runs, the backslash
continuations get eaten in a different order than expected, and the result is a *silent
empty string* rather than an error. That is the worst shape for a default — the recipe then
becomes `seed_agent_register.py --org` and argparse says something about argument parsing,
which points at the script rather than at the Makefile.

A twenty-line script has one obvious failure mode, and this one prints nothing and exits
non-zero rather than printing something wrong.

## The refusal

If **no** organization holds the catalogue, or if **several** do, this prints nothing and
exits 1. Several is a real state — `seed_reference_catalogue.py` was run more than once
before it was made idempotent — and picking the first would seed a tenant that happens to
share a copy rather than the one the caller meant. The seeder makes the same choice, and
for the same reason.
"""

from __future__ import annotations

import asyncio
import sys
import warnings

warnings.filterwarnings("ignore")

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.persistence.session import Database  # noqa: E402

#: A holder is an organization with at least this many of the dossier's twenty-eight SOPs.
#: Chosen to be "the whole catalogue" rather than "some SOPs", so a tenant with two
#: procedure rows of its own is not mistaken for a copy of the dossier.
NEEDED = 16


async def _holders() -> list[str]:
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT organization_id FROM sop_definitions "
                            " GROUP BY organization_id HAVING count(*) >= :needed "
                            " ORDER BY 1"
                        ),
                        {"needed": NEEDED},
                    )
                )
                .scalars()
                .all()
            )
        return [str(r) for r in rows]
    finally:
        await db.dispose()


def main() -> int:
    found = asyncio.run(_holders())
    if len(found) != 1:
        print(
            f"expected exactly one organization holding the dossier catalogue, "
            f"found {len(found)}: {', '.join(found) or 'none'}. "
            "Run `make seed-test-reference` (test schema) or the process-spine seed "
            "(development schema), and if several hold it, pass ORGS=<org> explicitly.",
            file=sys.stderr,
        )
        return 1
    print(found[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
