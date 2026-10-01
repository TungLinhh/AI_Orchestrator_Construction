"""Give a learned skill somewhere to keep the log it was learned from.

A skill version written from an approved run has to carry two different things,
and they are not the same thing:

* **test results** -- "tests were run against this and they passed". This is what
  the publication gate checks, and it is the right thing for it to check.
* **evidence** -- "this text was composed from this run's tool sequence and these
  refusals, on this date, by this agent".

The first version of the learning loop put the evidence in `test_results`, which
is convenient and wrong in a way that only shows up later: `POST /skills/{id}/publish`
refuses a version whose `test_results` does not say `passed: true`, so **a lesson
could never be published at all**, and the only way to publish one would have been
to fabricate a passing test result.

That is the worst possible pressure. It asks for the one artefact a reviewer
trusts most, and rewards inventing it. So the evidence gets its own column, and
the publication gate keeps requiring what it always required.

The column is nullable with no default: existing rows have no evidence and saying
so is truer than storing `{}`, which would read as "this was learned from nothing
and that is fine".

Downgrade drops the column. It is additive and holds nothing that is not also in
`audit_logs`, so nothing is lost that cannot be rebuilt.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028"
down_revision = "0027"

#: Shared with `models.py`. Two copies of a comment is one too many.
SKILL_DERIVED_FROM_COMMENT = (
    "The run log a learned skill was composed from: task title, tool sequence, platform "
    "refusals, attempt count. Null for a skill a person wrote, which is most of them."
)

TABLE = "skill_versions"
COLUMN = "derived_from"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            COLUMN,
            # **JSONB, not `sa.JSON()`.** The first version of this migration
            # wrote `sa.JSON()`, which creates a `json` column in a schema where
            # every other JSON column is `jsonb` -- and nothing notices until
            # `test_there_is_no_drift` fails on `modify_type`, comparing
            # `JSON` against `JSONB` for a column whose comment, nullability and
            # meaning are all correct. Migration 0006 already made this repair
            # once, for `quarantined_proposals`, and wrote down why: JSONB is
            # binary and indexable, JSON is text. The lesson survived the fix and
            # not the file.
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            # The same constant the model uses. A comment written out twice
            # drifts the first time one of them is edited, and the drift shows up
            # as `test_there_is_no_drift` failing on `modify_comment` with no
            # other symptom -- which is exactly what happened here.
            comment=SKILL_DERIVED_FROM_COMMENT,
        ),
    )


def downgrade() -> None:
    op.drop_column(TABLE, COLUMN)
