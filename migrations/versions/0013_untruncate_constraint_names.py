"""Rename two constraints that Postgres had silently truncated.

Postgres truncates identifiers over 63 *bytes* by keeping a prefix and appending
`_` plus four hex digits. Two constraint names reached the database at 64 bytes
and arrived as:

    ck_gate_criterion_evaluations_proposal_required_for_age_dd1c
    ck_goods_receipts_acceptance_requires_quality_and_qa_hse_signoff
      -> ck_goods_receipts_acceptance_requires_quality_and_qa_hs_5164

Nothing failed. The constraints still fired, the rules were still enforced, and
every migration and every test reported success — but the names no longer matched
the models, so a rule could not be found by grepping for its own name, and
`refused_because("...acceptance_requires_quality_and_qa_hse_signoff")` could not
be written at all. It surfaced only because a test named the constraint it
expected and the database had a different one.

Renamed to names that fit:

    ck_gate_criterion_evaluations_agent_source_needs_proposal   (57 bytes)
    ck_goods_receipts_acceptance_needs_docs_qa_and_hse         (49 bytes)

The first is the provenance check, which exists on every domain table, so the
upgrade discovers its targets from `pg_constraint` rather than listing fifty near
identical statements. Fifty hand-written renames means one typo that only appears
on the table nobody tested.

## A shape is not an identity

Getting to the two truncated names took three attempts and two of them failed in
ways worth recording, because both reported progress while being wrong.

The obvious query is `WHERE conname LIKE '%proposal_required_for_agent_source'`,
and it cannot work. Truncation *removes* the suffix being searched for —
`..._proposal_required_for_age_dd1c` does not contain the string
`proposal_required_for_agent_source`. That version renamed forty-eight tables,
skipped the two that were broken, and printed no error.

Widening it with `OR conname ~ '_[0-9a-f]{4}$'` to catch names "by shape" is
worse, and it fails in a way that is much harder to read. The marker identifies
*any* name Postgres rewrote — and the truncated **acceptance** constraint ends in
`_5164` too. So the loop renamed the acceptance rule to
`ck_goods_receipts_agent_source_needs_proposal`, the name the provenance rename on
that same table had just taken, and the migration died on `DuplicateObjectError`
with half the schema renamed. The marker answers "did Postgres rewrite this?" and
the question being asked is "is this the rule I am looking for?".

So the two truncated names are named explicitly, from measurement. A shape is not
an identity, and a rename list of two entries is both shorter and correct where a
clever query is neither.

## The downgrade names are measured, not computed

Reversing a truncation needs the original 64-byte name *and* the four hex digits
Postgres appended, and those digits are not reproducible: they are neither the
md5 nor the sha1 of the original name, and Postgres kept 55 characters where the
limit is 63, so the cut point is not derivable either. An implementation that
guessed would restore a *different* name and leave the database in a third state
matching neither this migration nor the models.

So `_PRE_0013_*` holds the names as they were **read out of `pg_constraint`**
before this migration ran. Restoring a measured value is verifiable; a
reconstructed one is a guess wearing a docstring that claims otherwise.

`tests/integration/test_schema_matches_models.py::test_migration_0013_round_trips`
is what makes that claim checkable: it downgrades, asserts the names come back
byte for byte, and upgrades again.

## What stops this recurring

`tests/unit/test_construction_schema.py::TestIdentifiersFitPostgres` refuses any
name over 63 bytes at the model, before a migration can carry one.
`test_no_constraint_looks_postgres_truncated` in
`tests/integration/test_procurement_chain.py` refuses a truncated name in a live
database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_OLD_PROVENANCE = "proposal_required_for_agent_source"
_NEW_PROVENANCE = "agent_source_needs_proposal"


#: `(table, name before 0013, name after 0013)`, read from `pg_constraint`.
#:
#: Only the two truncated names need recording. The other forty-eight provenance
#: checks were never affected, so their `0012` name is the plain long one and is
#: reconstructed from the table name in `downgrade`.
_PRE_0013_ACCEPTANCE = (
    "goods_receipts",
    "ck_goods_receipts_acceptance_requires_quality_and_qa_hs_5164",
    "ck_goods_receipts_acceptance_needs_docs_qa_and_hse",
)
_PRE_0013_PROVENANCE = (
    "gate_criterion_evaluations",
    "ck_gate_criterion_evaluations_proposal_required_for_age_dd1c",
    f"ck_gate_criterion_evaluations_{_NEW_PROVENANCE}",
)


def _rename(table: str, current: str, target: str) -> None:
    """`ALTER TABLE ... RENAME CONSTRAINT`, with both names quoted.

    Quoted because these are identifiers read from a catalogue, and quoting is what
    makes the statement correct for any name rather than only for the lowercase
    ASCII ones the schema happens to use today.
    """
    op.execute(sa.text(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{current}" TO "{target}"'))


def _provenance_targets() -> list[tuple[str, str]]:
    """`(table, current_constraint_name)` for the provenance checks that fit.

    Matched on the old suffix, which finds the forty-eight tables whose names were
    never truncated. The two that *were* truncated are named explicitly in
    `_PRE_0013_PROVENANCE` and `_PRE_0013_ACCEPTANCE` and renamed by `upgrade`.

    This function deliberately does **not** try to find them by shape. Two earlier
    attempts did, and both were wrong:

    * `LIKE '%' || :old` alone missed them, because truncation removes the suffix
      being searched for. The migration reported success having renamed forty-eight
      tables and skipped the two that were broken.

    * `OR conname ~ '_[0-9a-f]{4}$'` alone over-matched. The marker identifies
      *any* name Postgres rewrote, and the truncated **acceptance** constraint ends
      in `_5164` too — so the loop renamed the acceptance rule to
      `ck_goods_receipts_agent_source_needs_proposal`, which is the name the
      provenance rename on that same table had just taken. The migration died on
      `DuplicateObjectError` with 49 of 50 tables renamed.

    A shape is not an identity. The truncated names are two known strings, they
    were measured rather than derived, and naming them is both shorter and correct.
    """
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT conrelid::regclass::text, conname
            FROM pg_constraint
            WHERE contype = 'c' AND conname LIKE '%' || :old
            """
        ),
        {"old": _OLD_PROVENANCE},
    )
    return [(r[0], r[1]) for r in rows]


def upgrade() -> None:
    for table, current in _provenance_targets():
        _rename(table, current, f"ck_{table}_{_NEW_PROVENANCE}")
    # The two names Postgres rewrote, in the order measured.
    _rename(*_PRE_0013_PROVENANCE[:2], _PRE_0013_PROVENANCE[2])
    _rename(*_PRE_0013_ACCEPTANCE)


def downgrade() -> None:
    # Reverse order, so the acceptance rule is back in place before the
    # provenance sweep runs over the same table.
    _rename(_PRE_0013_ACCEPTANCE[0], _PRE_0013_ACCEPTANCE[2], _PRE_0013_ACCEPTANCE[1])
    _rename(_PRE_0013_PROVENANCE[0], _PRE_0013_PROVENANCE[2], _PRE_0013_PROVENANCE[1])

    # The forty-eight that were never truncated, reconstructed from the table name
    # rather than measured — their `0012` name is the plain long one.
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT conrelid::regclass::text FROM pg_constraint
            WHERE contype = 'c' AND conname LIKE '%' || :new
            """
        ),
        {"new": _NEW_PROVENANCE},
    )
    for (table,) in rows:
        if table == _PRE_0013_PROVENANCE[0]:
            continue
        _rename(table, f"ck_{table}_{_NEW_PROVENANCE}", f"ck_{table}_{_OLD_PROVENANCE}")
