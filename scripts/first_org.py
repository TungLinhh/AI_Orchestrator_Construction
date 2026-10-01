#!/usr/bin/env python
"""Print the organization id to browse, or fail with an explanation.

`make page` needs to put a real tenant in the URL, and asking a human to copy an id out
of a table is a step where everything goes wrong silently: a wrong id gives a page that
renders, fetches nothing, and shows an empty portfolio — which looks like a broken
product rather than a wrong argument.

So the id is looked up, checked, and the count of construction rows under it is printed
with it. An empty answer is an error here rather than a blank page there.

**And it has to be the *right* tenant, which is harder than the first one.** It used to
be `ORDER BY created_at, id LIMIT 1` — the oldest organisation on the machine. That is
the tenant created before the three-tier seed, so `make page` verified the product
against an organisation that predates it and reported:

    FAIL  all six departments are present  — 8

which is true, and about a tenant no longer describes. The page worked; the *check* was
pointed at history. Verifying a system against a snapshot of how it used to be is the
same mistake as testing a migration against the schema it replaced.

So the choice is **structural**: the newest organisation whose unit tree is the one the
current seed builds — offices and departments, in the current counts. A tenant that
matches the product is found by asking the tenant, not by guessing its age.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

from ai_orchestrator.persistence.session import Database

#: What the current seed builds. A tenant that does not have this shape is not the
#: product, and the checks written against the product will not apply to it.
#:
#: **Read from the seed rather than written here, because it was written here and it
#: went stale.** Adding the seventh department left `WANTED_DEPARTMENTS = 6`, and the
#: picker then selected a six-department tenant while the one the console had just
#: been pointed at was the seven-department one. The console opened a stale tenant and
#: said "no projects -- run `make seed-construction`" about a tenant that had never
#: been short of documents.
#:
#: That is this project's own shape, one layer down: a literal that duplicates a fact
#: which lives somewhere else. The office and department counts are not this script's
#: to decide, so it asks the module that builds them. The duplicate definitions below
#: were the tell -- the same two names, written twice, in one file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ai_orchestrator.seed import DEPARTMENTS, OFFICES

WANTED_OFFICES = len(OFFICES)
#: `DEPARTMENTS[0]` is the executive, which is a department-shaped entry and not a
#: department unit -- the same distinction the seed itself makes.
WANTED_DEPARTMENTS = len(DEPARTMENTS) - 1

#: How many candidates to try, newest first. More than one because the newest
#: tenant on a working machine is often a throwaway from a test run.
CANDIDATES = 12

#: `organizations` is a **directory** and is readable by the application role;
#: `organizational_units` is not -- row-level security hides every row until a
#: tenant is bound, which is F102's shape and the reason this cannot be one join.
#: So: enumerate from the directory, bind one candidate, look at its units, move
#: on if the shape is wrong. Least privilege is kept for the data read; only the
#: *enumeration* relies on the directory being readable, and that is asserted by
#: the query below rather than assumed.
_CANDIDATES = """
SELECT id FROM organizations ORDER BY created_at DESC, id DESC LIMIT :n
"""

_SHAPE_OF_ONE = """
SELECT count(*) FILTER (WHERE unit_type = 'office')      AS offices,
       count(*) FILTER (WHERE unit_type = 'department') AS departments
  FROM organizational_units
 WHERE organization_id = CAST(:org AS varchar(40))
"""


async def main() -> int:
    db = Database.from_settings()
    try:
        async with db.engine.connect() as conn:
            # Newest first: of the tenants shaped like the product, the most recent
            # is the one the current seed wrote and the one a person is looking at.
            candidates = [
                str(row[0])
                for row in (await conn.execute(text(_CANDIDATES), {"n": CANDIDATES})).all()
            ]
            if not candidates:
                print(
                    "no organisation exists. Run `make seed-process` first.",
                    file=sys.stderr,
                )
                return 1
            org = None
            for candidate in candidates:
                await conn.execute(
                    text("SELECT set_config('app.current_tenant', :org, true)"),
                    {"org": candidate},
                )
                row = (await conn.execute(text(_SHAPE_OF_ONE), {"org": candidate})).one()
                if (
                    int(row.offices) == WANTED_OFFICES
                    and int(row.departments) == WANTED_DEPARTMENTS
                ):
                    org = candidate
                    break
            if org is None:
                print(
                    f"none of the {len(candidates)} most recent organisations has the "
                    f"current three-tier shape ({WANTED_OFFICES} offices, "
                    f"{WANTED_DEPARTMENTS} departments). Seed a fresh tenant with "
                    "`make seed-process`, or pass ORG=<org_...> explicitly.",
                    file=sys.stderr,
                )
                return 1
            # Still bound from the loop above, and the count below is the one
            # worth showing an operator: a tenant with the right shape and no
            # projects renders an empty portfolio, which reads as a broken page.
            #
            # This is F102's shape and it is worth naming, because the failure it causes
            # here is a *lie in the helpful direction*: the first version of this script
            # counted unbound, RLS hid all six projects, and it printed "this tenant has
            # no projects -- run make seed-construction". The data was already there. A
            await conn.execute(
                text("SELECT set_config('app.organization_id', :org, true)"),
                {"org": org},
            )
            projects = (await conn.execute(text("SELECT count(*) FROM projects"))).scalar()
            print(org)
            if not projects:
                print(
                    "note: this tenant has no projects -- run `make seed-construction`",
                    file=sys.stderr,
                )
    finally:
        await db.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
