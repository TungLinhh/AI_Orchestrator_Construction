"""Add `model_profiles.max_classification`.

The API reported a profile's privacy ceiling by reading a column that did not
exist, so every request to that endpoint raised `AttributeError` and returned
500. The per-provider ceilings in `privacy_rules` are a different thing: they say
which provider may handle which classification, while this column says how
sensitive a class of work the profile is for at all.

Reversible. The seeded default is `restricted`, the most conservative value,
because a ceiling that is too low only ever refuses a call that could have been
made with a wider profile, while one that is too high silently sends restricted
data somewhere it should not go.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "model_profiles"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "max_classification",
            sa.String(length=64),
            nullable=False,
            server_default="restricted",
        ),
    )


def downgrade() -> None:
    op.drop_column(TABLE, "max_classification")
