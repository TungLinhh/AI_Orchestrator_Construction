"""A closed decision vocabulary, and the two decisions Phase 4c had to add to it.

Migration `0019`.

## Why the vocabulary is closed at all

`ck_ai_decision_log_decision_known` allows exactly five values:

    acted · proposed · refused · escalated · shadowed

A closed set is a *good* constraint. It means an unknown decision is refused by the
database rather than written and never noticed, and the five values are the whole of what
Tập 1 asks a decision log to distinguish. An open `decision` column with a `varchar` and
no list would have recorded `agent.kill` happily and left a reader with nothing to group
by.

## What I got wrong, and it is worth writing down

Phase 4c's first attempt wrote `decision = 'agent.kill'` and the constraint refused it,
which is the constraint working. I had treated `decision` as a field for *any* decision
about an agent. It is not: it is a field for **the agent's own decisions** — what it did,
what it proposed, what it refused, what it escalated, what its shadow run said.

A kill is not the agent deciding anything. The agent did nothing; it was stopped by a
person. Writing it as the agent's decision would have been the same lie in a different
column: the log claiming an agent chose its own suspension.

## What this migration does

Adds two values, and **requires a rationale for both**:

    acted · proposed · refused · escalated · shadowed · killed · revived

`killed` and `revived` are system decisions *about* an agent, which is why they live
alongside the agent's own and not in a separate table: an auditor asking "what happened
to this agent" should not have to query two places, and the `actor_type = 'system'` plus
nullable `agent_id` shape (`ck_ai_decision_log_is_attributable`) already exists for it.

The rationale requirement is the important half. `ck_ai_decision_log_refusal_is_explained`
exists because a refusal with no reason is indistinguishable from a failure to decide,
and those two need different investigations. The same argument applies harder to a kill:
"stopped, no reason given" and "stopped, reason lost" are the same row, and one of them
is a governance failure. The endpoint already requires a non-empty reason; this puts it
under the database as well, so **no code path** can write an unexplained kill.

## Also in this migration

The five check constraints `0016` created on `agents` existed in the database and not in
the ORM model, so `alembic revision --autogenerate` would have proposed dropping them.
They are declared on the model now. **No database change is needed for that**, and this
migration deliberately touches no `agents` DDL — a migration that "also" tidies the model
is a migration nobody can review in one sitting.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The five the log already allowed, plus the two Phase 4c needs.
KNOWN = (
    "acted",
    "proposed",
    "refused",
    "escalated",
    "shadowed",
    "killed",
    "revived",
)

#: Decisions that are meaningless without a reason. `refused` was here first; the two
#: system decisions join it because an unexplained kill and a kill whose reason was lost
#: are the same row, and only one of them is acceptable.
MUST_EXPLAIN = ("refused", "killed", "revived")


def _vocabulary() -> str:
    return "decision IN (" + ",".join(f"'{v}'" for v in KNOWN) + ")"


def _explained() -> str:
    return "decision NOT IN (" + ",".join(f"'{v}'" for v in MUST_EXPLAIN) + ") OR rationale <> ''"


def upgrade() -> None:
    # `ALTER TABLE` path, so the `ck_%(table_name)s_%(constraint_name)s` convention
    # applies and the names come out as `ck_ai_decision_log_...` from the bare suffixes
    # given here. `create_table` in `0016` used `op.f()` for the same reason (F109).
    op.drop_constraint("ck_ai_decision_log_decision_known", "ai_decision_log")
    op.create_check_constraint("decision_known", "ai_decision_log", _vocabulary())

    op.drop_constraint("ck_ai_decision_log_refusal_is_explained", "ai_decision_log")
    op.create_check_constraint(
        "explained_is_explained", "ai_decision_log", _explained()
    )


def downgrade() -> None:
    # Back to the five, and back to explaining only refusals. The rows written by
    # Phase 4c are **not** deleted on the way down -- an audit trail that loses entries
    # because a migration went backwards is worse than one that cannot be reverted at
    # all, so the constraint is narrowed and the rows are left, which means the downgrade
    # fails loudly if any exist. That is the correct trade for this table.
    op.drop_constraint("ck_ai_decision_log_explained_is_explained", "ai_decision_log")
    op.drop_constraint("ck_ai_decision_log_decision_known", "ai_decision_log")
    op.create_check_constraint(
        "decision_known",
        "ai_decision_log",
        "decision IN ('acted','proposed','refused','escalated','shadowed')",
    )
    op.create_check_constraint(
        "refusal_is_explained", "ai_decision_log", "decision <> 'refused' OR rationale <> ''"
    )
