"""Tell the agent what the database looks like, because it cannot guess.

Measured, on a real free-tier model against a real tenant: **46 tool calls, 13
succeeded, 25 failed with a database error, and every single call was the same
tool** — `internal_database_query`. The model wrote SQL against a schema it had
never seen, got a `ProgrammingError`, guessed again, and ran out the tool-call
ceiling with nothing produced.

The refusal it hit first made it worse. `every query must reference
organization_id` says what is wrong and not what is right, and a model told only
that has one option: try again. It did, eight times, with the same query.

So the fix is not a higher ceiling. It is the schema, in the place the model is
already guaranteed to read it — the tool's own description, which the runtime
renders as the function docstring. A tool that says what it queries without
saying what the tables are is a tool that cannot be used correctly.

Built from `Base.metadata`, which means it cannot drift from the models: a
column renamed in a migration and in the ORM appears here without anyone editing
a list. That is the property worth having. A hand-written table list is correct
on the day it is written and silently wrong on the day a column is added.
"""

from __future__ import annotations

from functools import lru_cache

from ai_orchestrator.persistence import models as _models  # noqa: F401 - registers the tables
from ai_orchestrator.persistence.base import Base

#: The tables an agent is pointed at. Not "every table" — there are a hundred,
#: and a hundred tables of columns is a document nobody reads. These are the
#: ones that answer a question about work: what is open, who did it, what was
#: delegated, what was decided, what it cost.
#:
#: The order is the order a person would look in them, which is the order that
#: makes the list read as a path rather than an index.
QUERYABLE_TABLES = (
    "tasks",
    "agents",
    "organizational_units",
    "delegations",
    "approvals",
    "executions",
    "model_usage",
    "audit_logs",
)

#: Columns that are noise in a query. `created_at` on every table is the obvious
#: one; the audit ledger's `context` and `user_agent` are the other, because a
#: model that reads a blob of JSON to answer "what happened" gets noise back.
_HIDDEN_COLUMNS = frozenset({"context", "user_agent", "ip_address", "password_hash"})


@lru_cache(maxsize=1)
def schema_brief(tables: tuple[str, ...] = QUERYABLE_TABLES) -> str:
    """One line per table, and its columns.

    **Names, not types.** The first version printed `id VARCHAR(40)` for every
    column and came to 5,213 characters -- mostly the repeated `VARCHAR(40)`.
    What went wrong was a model not knowing which columns *exist*, and a name
    answers that. Types still matter where they change what a comparison means,
    so those are kept; `VARCHAR` is dropped because every id in this schema is
    one and saying so forty times teaches nothing.

    Cached because it is built from ORM metadata and the answer does not change
    within a process -- but `lru_cache` is keyed on the tuple, so a caller asking
    for a different set gets a different answer rather than the first one.
    """
    lines: list[str] = []
    for name in tables:
        table = Base.metadata.tables.get(name)
        if table is None:
            # A table in the list that the ORM does not have is a mistake in the
            # list, and showing nothing would hide it. Named, so it is visible in
            # a prompt review rather than discovered by a model that tries it.
            lines.append(f"{name} -- NOT IN THE SCHEMA (this list is wrong)")
            continue
        columns: list[str] = []
        for c in table.columns:
            if c.name in _HIDDEN_COLUMNS:
                continue
            type_name = str(c.type)
            # `VARCHAR(40)` on an id column is noise; `NUMERIC(18, 6)` on a money
            # column is the difference between a query that works and one that
            # does not. Keep the type only where it changes the comparison.
            columns.append(c.name if "VARCHAR" in type_name else f"{c.name} {type_name}")
        lines.append(f"{name}: " + ", ".join(columns))
    return "\n".join(lines)


#: A complete, runnable query. It is documentation, never executed, so nothing
#: here reaches a cursor -- which is the only thing that would make it an
#: injection vector. Kept out of the concatenation above so it reads as one
#: literal example rather than as string-built SQL.
_EXAMPLE_QUERY = (
    "  SELECT title, status FROM tasks WHERE organization_id = 'org_...' ORDER BY created_at DESC"
)


def schema_note() -> str:
    """The text appended to the query tool's description.

    Includes a worked example that is **actually runnable**, because the failure
    this addresses was a model not knowing the shape of a valid query rather than
    not knowing a table name. An example with the tenant predicate in it teaches
    both at once; a description that states a rule teaches one.
    """
    return (
        "\n\nThese are the tables you may query, with their columns:\n"
        f"{schema_brief()}\n\n"
        "Every query must filter on the tenant. Write the column and the platform "
        "binds the value, so this is a complete, runnable query:\n"
        f"{_EXAMPLE_QUERY}\n\n"
        "If a query fails, read the message: it names the table and the column "
        "that were wrong. Guessing the same query again is the one thing that "
        "cannot work."
    )
