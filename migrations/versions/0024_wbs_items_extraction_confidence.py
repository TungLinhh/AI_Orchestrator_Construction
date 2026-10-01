"""A model-extracted work-package line must carry its extraction confidence.

Revision ID: 0024
Revises: 0023

## What is missing and why it matters

`wbs_items` holds values an extraction model wrote, from a spreadsheet, about a work
package. `bid_items` and `tender_requirements` hold values an extraction model wrote too,
and both carry this constraint:

    ck_bid_items_agent_row_needs_a_confidence
    CHECK (source <> 'agent_proposal' OR extraction_confidence IS NOT NULL)

`wbs_items` does not, and nothing noticed — because a `CHECK` that is absent cannot fail
its own test, and because the ingestion path writes `source = 'import'`, not
`'agent_proposal'`, so no row in the corpus trips it either way.

The gap is that **the value is unconstrained, not that a row is wrong.** A row written by
a human may legitimately have no confidence. A row written by a model may not: nobody can
triage it without knowing how sure the model was, and the review queue is ordered by
confidence, so a null confidence puts the row nowhere in it. That is the same class of
defect as a missing `proposal_id` — the row looks recorded and is effectively invisible to
the process meant to review it.

## How it was found

`tests/unit/test_construction_schema.py::TestAiAuthoredValuesAreTriageable` asserts the
constraint exists on all three tables. It failed on `wbs_items` after the ORM was rebuilt
from the schema — which is the point of rebuilding from the schema: the rebuild copied
what the database had, faithfully, including this absence, and the test then said so.

The test was written before the constraint was missing. It had been passing because the
*model* declared it, and the model and the schema had drifted apart without the drift test
noticing the shape of it.

## Why the expression is written out and not copied

`pg_get_constraintdef` on the sibling tables returns Postgres's normalised form, casts
included. This writes the plain form, because the plain form is what a reader can check
against the sibling's by eye, and because a normalised copy would be
`(("source")::text <> 'agent_proposal'::text)` — the same rule spelled in a way that hides
which column it is about.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: **The suffix, not the full name.** The naming convention composes
#: `ck_%(table_name)s_%(constraint_name)s`, so passing
#: `ck_wbs_items_agent_row_needs_a_confidence` produced
#: `ck_wbs_items_ck_wbs_items_agent_row_needs_a_confidence` in the database — a name in
#: neither the migration nor the model, and the same mistake as F138 one layer down, in a
#: migration rather than a model. The siblings read
#: `ck_bid_items_agent_row_needs_a_confidence` because they were declared before this
#: convention was understood; matching them means passing `agent_row_needs_a_confidence`
#: and letting the convention supply the prefix.
CONSTRAINT = "agent_row_needs_a_confidence"
EXPRESSION = "source <> 'agent_proposal' OR extraction_confidence IS NOT NULL"


def upgrade() -> None:
    op.create_check_constraint(CONSTRAINT, "wbs_items", EXPRESSION)


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, "wbs_items", type_="check")
