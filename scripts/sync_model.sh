#!/usr/bin/env bash
# Bring the ORM models into line with the database, in one order, and prove convergence.
#
#   scripts/sync_model.sh          run the pipeline
#   scripts/sync_model.sh --verify run it twice and require the second to change nothing
#
# ## Why the order is fixed
#
# The four steps are not independent, and running them out of order is what produced the
# state this script exists to clean up (F133, and everything after it):
#
#   0. `rebuild_model_table_args.py` — replace every `__table_args__` with what the schema
#      says. Destructive, and first, because it is the only step that can undo a
#      duplicated block; appending cannot.
#   1. `apply_composite_fks_to_model.py` — reduce each class to one `__table_args__`, then
#      add the composite foreign keys and the unique tenant pair. It *creates* a
#      `__table_args__` where a parent had none.
#   2. `sync_model_constraints.py` — add the indexes, unique constraints and check
#      constraints the database has and the model lacks. It reads `pg_indexes` and
#      `pg_constraint`, so it must run *after* step 1 has created the tuples, or it has
#      nowhere to put what it finds.
#   3. `dedupe_index_against_unique.py` — a `UNIQUE` constraint appears in both
#      `pg_indexes` and `pg_constraint`, so step 2 can add an `Index` and a
#      `UniqueConstraint` under one name. This drops the shadowed one.
#   4. `sync_sqlalchemy_imports.py` — a step-1 or step-2 insertion can be the first use of
#      `ForeignKeyConstraint` in a module. Last, because it reads the finished file.
#
# ## Why `--verify`
#
# Every one of these scripts is supposed to be idempotent, and none of them was on first
# write. An idempotent pipeline is checkable: run it twice, and the second run must change
# nothing. A non-idempotent step shows up as a hash difference, which is a much cheaper
# signal than a red test three files away.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
PERSISTENCE=src/ai_orchestrator/persistence

run_pipeline() {
    # The rebuild is the whole pipeline. It replaces every `__table_args__` with what the
    # schema says, creates one where a class has none, strips the column-level
    # `ForeignKey("parent.id")` that a composite key replaces, and emits the primary key.
    #
    # Three additive steps used to follow it -- `apply_composite_fks_to_model`,
    # `sync_model_constraints`, `dedupe_index_against_unique` -- and they were removed
    # because they appended to a block the rebuild had just replaced. `Procedure` ended up
    # declaring `uq_procedures_org_code` three times, one of them as an `Index` on a
    # column named `org_code` that does not exist, which fails at import with
    # `ConstraintColumnNotFoundError` and a message pointing at the wrong table.
    #
    # A rewrite is a subtraction. Three scripts whose job was "add what is missing" were
    # the wrong shape once the correct operation turned out to be "replace what is there".
    $PY scripts/rebuild_model_table_args.py
    $PY scripts/dedupe_column_derived_index.py
    $PY scripts/sync_sqlalchemy_imports.py
}

fingerprint() {
    cat "$PERSISTENCE"/*.py | sha256sum | cut -d' ' -f1
}

run_pipeline

if [ "${1:-}" = "--verify" ]; then
    before=$(fingerprint)
    run_pipeline
    after=$(fingerprint)
    if [ "$before" != "$after" ]; then
        echo "  NOT IDEMPOTENT: $before -> $after" >&2
        echo "  a step in the pipeline adds something on every run." >&2
        exit 1
    fi
    echo "  idempotent: $after"
fi
