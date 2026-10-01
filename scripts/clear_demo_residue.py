"""Remove the residue `scripts/demo_real_run.py` leaves in a development tenant.

## Why this exists, and why it is a script and not a `make` target with the deletes inline

`demo_real_run.py` creates a task titled `Executive goal` with the goal

    Draft a one-line status update

It was run **52 times** on the development tenant, which left 52 tasks behind — 51 of them
`failed` — and those 51 are **88% of every failure the CEO queue shows**. They are not business
records: they are one demo script's output, identifiable by a title, a goal prefix and a
`requester_id IS NULL`.

## What it would delete, measured before anything is written

| table | column | rows |
|---|---|---|
| `audit_logs` | `task_id` | 630 |
| `model_usage` | `task_id` | 607 |
| `executions` | `task_id` | 52 |
| `task_dependencies` | `depends_on_task_id` | 2 |
| `tasks` | `root_task_id`, `parent_task_id` | 2 |
| `delegations` | `parent_task_id` | 1 |

**1,294 rows, 630 of them audit history.** `audit_logs` is deliberately not writable by the
application role — the platform refuses to let application code rewrite its own trail. This
script uses the admin role, which is the only way to do it, and that is exactly why it should
be a deliberate, counted, dry-run-first action rather than something a `make` target does
quietly on every call.

## `--dry-run` is the default, and it is not a safety net bolted on afterwards

Deleting the platform's audit history is irreversible and the rows are somebody's evidence.
So the default is to **count and print**, and `--apply` is required to write. A script whose
default is destructive is a script that will eventually destroy something by accident, and
"but I passed `--apply`" is a poor defence.

## The signature is narrow on purpose

Goal prefix, title **prefix**, `requester_id IS NULL` and `requester_type = 'human'`. All four,
in the same tenant. Any single one of them would match real work; the conjunction is what makes
this safe to run against a database that also holds business rows.

**The title is matched by prefix, not exactly.** A first version used `title = 'Executive
goal'`, which left behind every **retry** of the demo tasks — and the retry button makes those
easy to create, because retrying a failed demo task is a reasonable thing to do while testing.
`retry_failed` copies the goal, so the goal prefix still matches; the title has become
`Executive goal (lần chạy lại) 1`. Measured: three such rows survived the first cleanup. **A
retry of demo residue is still demo residue**, and a signature that misses it means running the
cleanup twice is not enough.

The count is printed before and after, and the script **verifies the remainder** rather than
trusting its own report — F178, where a seeder claimed repairs it had rolled back.
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

#: All four clauses, together. See the module docstring.
SIGNATURE = """
    requester_id IS NULL
    AND requester_type = 'human'
    AND title LIKE 'Executive goal%'
    AND goal LIKE 'Draft a one-line status update%'
"""

#: Which column each table links to `tasks` **on**, asked of Postgres rather than assumed.
#:
#: Every foreign key here is **composite** on `(organization_id, id)`, so the *first* column of
#: each key is `organization_id` — counting on it returns 0 everywhere, which looks like "no
#: references" rather than "wrong query". A measurement that returns a suspiciously round zero
#: is the one to re-check first. So: the **last** column of the key.
_CHILD_COLUMNS = """
SELECT c.conrelid::regclass::text AS table_name, a.attname AS column_name
FROM pg_constraint c
JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
WHERE c.contype = 'f' AND c.confrelid = 'tasks'::regclass
  AND k.ord = array_length(c.conkey, 1)
ORDER BY 1
"""

#: Every table that reaches `tasks`, with the **column that holds the task id** and how many
#: hops away it is. Asked of Postgres rather than assumed, and walked **transitively** --
#: because a first version listed tables by hand and a second version asked only for direct
#: foreign keys, and both were wrong in ways that looked like something else:
#:
#: * a hand-written list cannot know that `subagent_runs` has no `task_id` at all;
#: * **every** foreign key here is composite on `(organization_id, id)`, so the *first* column
#:   of each key is `organization_id`; counting on it returns 0 everywhere, which reads as
#:   "nothing references these tasks" rather than "the query was wrong". So: the **last**
#:   column of the key;
#: * a table can reach `tasks` **through another table** -- `model_usage` points at
#:   `executions`, which points at `tasks` -- so a one-hop query misses it entirely.
#:
#: And the *order* matters as much as the membership. The first version deleted in
#: alphabetical order, which put `executions` before `model_usage`, and the delete died on
#: `fk_model_usage_execution_id_executions`:
#:
#:     IntegrityError: update or delete on table "executions" violates foreign key
#:     constraint "fk_model_usage_execution_id_executions" on table "model_usage"
#:
#: Which is the database being exactly right: it refused to orphan a child. **Deleting a
#: parent means deleting every descendant, deepest first** -- which is what `descendants`
#: returns, ordered by depth descending.
#: `tasks` referencing `tasks` is not in the plan -- it is the thing being deleted. Its own
#: `root_task_id`/`parent_task_id` are **nulled rather than cascaded**: a chain that pointed
#: at the residue becomes a chain with a hole, and `ON DELETE CASCADE` would erase the fact
#: that the link ever existed. The platform refuses to let application code rewrite its own
#: audit trail, so where a hole is left has to be visible.
_SELF_REFERENCES = ("root_task_id", "parent_task_id")

_FK_EDGES = """
SELECT c.conrelid::regclass::text AS child,
       c.confrelid::regclass::text AS parent,
       a.attname AS column_name
FROM pg_constraint c
JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
WHERE c.contype = 'f' AND c.connamespace = 'public'::regnamespace
  AND k.ord = array_length(c.conkey, 1)
  AND a.attname <> 'organization_id'
ORDER BY 1
"""


async def descendants(session, org: str) -> list[tuple[str, list[str], int]]:
    """(table, [columns that point at tasks], depth) for everything that reaches `tasks`.

    Deepest first, which is the order a delete has to run in.

    ## Two things this got wrong first, and both looked like something else

    1. **A table can point at `tasks` through more than one column.**
       `task_dependencies` has both `task_id` and `depends_on_task_id`; `subagent_runs` has
       `parent_task_id` and `child_task_id`. A version that kept one column per table
       counted only the first it saw, so `task_dependencies` was measured on `task_id` and the
       two rows that point the *other* way were invisible -- reported as "2" when deleting them
       would have orphaned 2 more. **One column per table is a guess, and this schema has
       tables where the guess is wrong.**

    2. **A self-referencing foreign key makes a naive depth walk never terminate.**
       `tasks.root_task_id -> tasks.id` is a real constraint, and with it in the graph the
       relaxation loop raised `depth['tasks']` on every pass: the plan came back reporting
       `model_usage` at **depth 156**. The number was not a warning, it was the loop counting.
       Self-references are dropped from the walk -- `tasks` is the root, so a cycle through
       the root is not a path.
    """
    edges = (await session.execute(text(_FK_EDGES))).mappings().all()

    # Every (table, column) that points at `tasks`, kept in full.
    to_tasks: dict[str, list[str]] = {}
    # child -> parents, for the depth walk.
    parents: dict[str, set[str]] = {}
    for e in edges:
        if e["child"] == e["parent"]:
            continue  # a self-reference is a cycle, not a path
        parents.setdefault(e["child"], set()).add(e["parent"])
        if e["parent"] == "tasks":
            to_tasks.setdefault(e["child"], []).append(e["column_name"])

    # Longest path from `tasks`. Small graph; a fixed number of passes is simpler to trust
    # than a worklist that has to detect cycles.
    depth = {"tasks": 0}
    for _ in range(len(edges) + 1):
        changed = False
        for child, ps in parents.items():
            for parent in ps:
                if parent in depth and depth.get(child, -1) < depth[parent] + 1:
                    depth[child] = depth[parent] + 1
                    changed = True
        if not changed:
            break

    plan = [(t, cols, depth.get(t, 1)) for t, cols in to_tasks.items() if t != "tasks"]
    return sorted(plan, key=lambda x: -x[2])


def _where_any(column: str, columns: list[str], org: str, ids: list[str]) -> tuple[str, dict]:
    """`WHERE` matching **any** of a table's columns that point at `tasks`.

    Built rather than hand-written per table, because a table with two such columns is normal
    here and a delete that only covered the first would orphan the rest.
    """
    clause = " OR ".join(
        f"{c} = ANY(CAST(:i AS text[]))" for c in columns if c != "organization_id"
    )
    return (
        f"organization_id=CAST(:o AS varchar(64)) AND ({clause})",
        {"o": org, "i": ids},
    )


async def _counts(session, org: str, ids: list[str], plan) -> list[tuple[str, list[str], int, int]]:
    out: list[tuple[str, list[str], int, int]] = []
    for table, columns, depth in plan:
        where, params = _where_any(table, columns, org, ids)
        n = (
            await session.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)
        ).scalar() or 0
        if n:
            out.append((table, columns, depth, n))
    return out


async def run(organization_id: str | None, *, apply: bool) -> int:
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
                print("  no organization holds the dossier catalogue; run `make ingest` first")
                return 1
        org = str(organization_id)

        async with db.tenant_session(org) as session:
            ids = list(
                (
                    await session.execute(
                        text(
                            "SELECT id FROM tasks "
                            f"WHERE organization_id=CAST(:o AS varchar(64)) AND {SIGNATURE}"
                        ),
                        {"o": org},
                    )
                )
                .scalars()
                .all()
            )
            if not ids:
                print("  nothing matches the demo signature; nothing to do")
                return 0
            plan = await descendants(session, org)
            children = await _counts(session, org, ids, plan)
            total_tasks = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM tasks "
                        f"WHERE organization_id=CAST(:o AS varchar(64)) AND {SIGNATURE}"
                    ),
                    {"o": org},
                )
            ).scalar_one()

        print(f"  org            : {org}")
        print(f"  demo-residue   : {total_tasks} task(s)")
        print("  descendants that would go with them, deepest first:")
        total = 0
        for table, columns, depth, n in children:
            total += n
            print(f"    {table:22} {','.join(columns):22} depth {depth:<2} {n:>5}")
        print(f"    {'TOTAL':22} {'':22} {'':8} {total:>5}")

        if not apply:
            print("\n  DRY RUN. Nothing was written. Re-run with --apply to delete.")
            return 0

        async with db.tenant_session(org) as session:
            for table, columns, _depth, _n in children:
                where, params = _where_any(table, columns, org, ids)
                await session.execute(text(f"DELETE FROM {table} WHERE {where}"), params)
            for column in _SELF_REFERENCES:
                await session.execute(
                    text(
                        f"UPDATE tasks SET {column} = NULL "
                        "WHERE organization_id=CAST(:o AS varchar(64)) "
                        f"AND {column} = ANY(CAST(:i AS text[]))"
                    ),
                    {"o": org, "i": ids},
                )
            deleted = (
                (
                    await session.execute(
                        text(
                            "DELETE FROM tasks "
                            f"WHERE organization_id=CAST(:o AS varchar(64)) AND {SIGNATURE} "
                            "RETURNING id"
                        ),
                        {"o": org},
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        # Read back. A maintenance step that reports a change it then rolled back is F178, and
        # this one rewrites audit history, so believing the return value is not good enough.
        async with db.tenant_session(org) as session:
            left = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM tasks "
                        f"WHERE organization_id=CAST(:o AS varchar(64)) AND {SIGNATURE}"
                    ),
                    {"o": org},
                )
            ).scalar_one()
            rest = (
                (
                    await session.execute(
                        text(
                            "SELECT status, count(*) AS n FROM tasks "
                            "WHERE organization_id=CAST(:o AS varchar(64)) "
                            "AND status = 'failed' GROUP BY 1"
                        ),
                        {"o": org},
                    )
                )
                .mappings()
                .all()
            )
        print(f"\n  deleted {len(deleted)} task(s); {left} still match the signature")
        print("  the tenant's remaining failures:")
        for r in rest:
            print(f"    {r['status']:10} {r['n']}")
        if left:
            print("  FAILED: the signature still matches rows after writing")
            return 1
        return 0
    finally:
        await db.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete. Without it, this counts and prints.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args.org, apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
