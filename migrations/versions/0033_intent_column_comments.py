"""Correct two column comments, so the model and the schema say the same thing.

## Why a migration for a comment

`test_schema_matches_models` compares `information_schema.columns.column_comment` against
the ORM's `comment=`, and a `modify_comment` drift is a failure. That check exists because a
comment that has drifted from the code it documents is worse than no comment: it reads as
current.

The comments were **wrong**, not merely different. Both still described the pre-F272 world:

```
tasks.intent_fingerprint
  "Hash of the first 16 normalised tokens of the goal, in order, ..."
```

The key is the *whole* goal now. Sixteen tokens blocked a second run of the same tender from
starting, because the run marker the platform appends sits past the window — and a person
reported, accurately, that one task was stopping another (F272).

And `tasks.intent_scope` had no comment matching its model at all, because the model column
was added by hand after 0032 ran (the ORM's columns are hand-written; `make model-sync` only
rebuilds `__table_args__`).

**Done as a migration rather than by hand-patching the database**, because a comment applied
by a person to one database is not a schema, and the second database would drift the moment
it was created.

## Why the text is a constant in both files

The model and the migration must carry the *same string*. `sync_model.sh` reads the comment
out of the live database to write `comment=`, so once this migration is applied the model is
regenerated from it and the two cannot disagree — provided nobody edits one side.
"""

from __future__ import annotations

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

#: Kept byte-identical to `DEL_INTENT_COMMENT` and `DEL_SCOPE_COMMENT` in
#: `persistence/models.py`. A `\n` in a comment is two characters in the Python string and
#: one in the SQL literal, so they are written on one line each and the check is the proof.
INTENT_COMMENT = (
    "Hash of the whole normalised goal, in order, plus the owner agent. "
    "What the delegating executor writes; what the partial unique index keys on. "
    "NULL means the row was not created by a delegation. It was the first 16 tokens until "
    "F272: that blocked a second run of the same tender from starting, because the run "
    "marker the platform appends sits past the window."
)

SCOPE_COMMENT = (
    "The request this task belongs to: parent_task_id, or '' for a root. Stored rather "
    "than computed at index time, because an expression index cannot be declared on the "
    "ORM and a rule the drift test cannot see is a rule nobody verifies."
)

#: What 0026 and 0032 wrote, so `downgrade` restores rather than invents.
OLD_INTENT_COMMENT = (
    "Hash of the first 16 normalised tokens of the goal, in order, plus the owner agent. "
    "What the delegating executor writes; what the partial unique index keys on. "
    "NULL means the row was not created by a delegation."
)


def _quote(value: str) -> str:
    """A SQL string literal. Comments are not bind parameters, so this cannot be one."""
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    op.execute(f"COMMENT ON COLUMN tasks.intent_fingerprint IS {_quote(INTENT_COMMENT)}")
    op.execute(f"COMMENT ON COLUMN tasks.intent_scope IS {_quote(SCOPE_COMMENT)}")


def downgrade() -> None:
    op.execute(f"COMMENT ON COLUMN tasks.intent_fingerprint IS {_quote(OLD_INTENT_COMMENT)}")
    # 0032 wrote no comment on `intent_scope` beyond the server default, so there is
    # nothing to restore to exactly; `NULL` is the honest "there was none" and is what a
    # freshly-created column before this migration had on a database that skipped it.
    op.execute("COMMENT ON COLUMN tasks.intent_scope IS NULL")
