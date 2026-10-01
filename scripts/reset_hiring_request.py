"""Delete the hiring chain this script created, so a demo can be run from the start.

**`--reset` and not a flag on the run**, because the two operations are different in kind: the
run converges (adopts what is there, adds what is missing), and a reset destroys. A seeder
whose reset is a flag on its normal path is a seeder that can be destructive by accident, and
`audit_logs` is deliberately not writable by the application role — so where the deletion stops
has to be a decision somebody made on purpose.

What it deletes: the tasks carrying `request_key`, and the approvals whose `action_type` is
`hr.open_headcount`. **The executions, delegations and audit rows of those tasks are left
alone**, and the delete of the tasks will be **refused by the foreign keys** if anything else
points at them. That refusal is the platform doing the right thing: a task that has been run
has a history, and the history is not the demo's to throw away.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seed_hiring_request import REQUEST_KEY  # noqa: E402
from sqlalchemy import text  # noqa: E402

from ai_orchestrator.persistence.session import Database  # noqa: E402

TASKS = """
SELECT id FROM tasks
WHERE organization_id=CAST(:o AS varchar(64)) AND input->>'request_key' = :k
ORDER BY (input->>'stage_n')::int DESC NULLS LAST
"""


async def reset(organization_id: str) -> int:
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.tenant_session(organization_id) as session:
            ids = list(
                (await session.execute(text(TASKS), {"o": organization_id, "k": REQUEST_KEY}))
                .scalars()
                .all()
            )
            if not ids:
                print("  nothing to reset")
                return 0

            # **Read, validate, then write -- in that order.**
            #
            # The first version deleted and let the foreign keys object afterwards, having
            # already removed the `task_dependencies` rows. So a reset the platform refused
            # left the chain **half deleted**: the links gone, the tasks still there. That is
            # the F160 shape -- a guard that runs after the effect cannot undo it.
            #
            # Measured: `ForeignKeyViolationError ... fk_audit_logs_task_id_tasks`, because
            # stage 1 had really run and its audit rows are a record of that. So the check comes
            # first, it names the tasks that have a history, and nothing is written unless every
            # one of them is clean.
            with_history = list(
                (
                    await session.execute(
                        text(
                            "SELECT t.id FROM tasks t WHERE t.id = ANY(CAST(:i AS text[])) "
                            "  AND (EXISTS (SELECT 1 FROM executions x "
                            "             WHERE x.task_id = t.id) "
                            "    OR EXISTS (SELECT 1 FROM audit_logs a "
                            "             WHERE a.task_id = t.id) "
                            "    OR EXISTS (SELECT 1 FROM delegations g "
                            "             WHERE g.parent_task_id = t.id "
                            "               OR g.child_task_id = t.id))"
                        ),
                        {"i": ids},
                    )
                )
                .scalars()
                .all()
            )
            if with_history:
                print(
                    f"  {len(with_history)} of {len(ids)} task(s) have been run and keep a "
                    "history that is not a demo's to delete:"
                )
                for tid in with_history[:6]:
                    print(f"    {tid}")
                print("  Nothing was written. Reset the tenant, or leave the chain where it is.")
                return 1
            # Dependencies first: they are the only child rows this script wrote.
            await session.execute(
                text(
                    "DELETE FROM task_dependencies WHERE organization_id=CAST(:o AS varchar(64)) "
                    "AND task_id = ANY(CAST(:i AS text[]))"
                ),
                {"o": organization_id, "i": ids},
            )
            await session.execute(
                text(
                    "DELETE FROM approvals WHERE organization_id=CAST(:o AS varchar(64)) "
                    "AND action_type='hr.open_headcount' AND task_id = ANY(CAST(:i AS text[]))"
                ),
                {"o": organization_id, "i": ids},
            )
            try:
                deleted = (
                    (
                        await session.execute(
                            text(
                                "DELETE FROM tasks WHERE organization_id=CAST(:o AS varchar(64)) "
                                "AND id = ANY(CAST(:i AS text[])) RETURNING id"
                            ),
                            {"o": organization_id, "i": ids},
                        )
                    )
                    .scalars()
                    .all()
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                print(f"  the database refused: {type(exc).__name__}: {str(exc)[:160]}")
                print(
                    "  That is the platform refusing to orphan a history. A task that has been "
                    "run is not a demo's to delete -- reset the tenant instead."
                )
                return 1
    finally:
        await db.dispose()
    print(f"  deleted {len(deleted)} task(s) and their approvals")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True)
    args = parser.parse_args()
    return asyncio.run(reset(args.org))


if __name__ == "__main__":
    raise SystemExit(main())
