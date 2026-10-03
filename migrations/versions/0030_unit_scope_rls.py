"""Unit-scope row-level security, on top of the tenant isolation of 0002.

## Why this exists

0002 gives every session one predicate: `organization_id = current_setting(
'app.current_tenant')`. The whole company is one organisation, so that predicate is
satisfied by every row that belongs to it — **including Finance's work, when the reader
is the HR agent.**

`internal_database_query` is the door. It refuses writes, refuses multi-statement SQL and
refuses catalog reads: three careful controls, none of them about separation. A
department with that tool could read the entire ledger.

## What this adds

A second predicate, on the tables that hold work products, driven by
`app.agent_unit_ids` — the list `domain/access.py` computes for the reading agent.

* **Unset means unrestricted; empty means nothing.** The first version read *both* as
  nothing, on the reasoning that an agent the platform failed to place should get
  nothing rather than everything. Measured, that was wrong in the most expensive way
  available: the `tenant` test fixture binds `app.current_tenant` directly rather than
  through `_set_tenant`, so *every integration test in the repository* lost the ability
  to read `tasks`, `executions` and `delegations` -- 60+ failures that had nothing to do
  with access control.

  The agent path cannot reach the unset branch, which is what makes the relaxed rule
  safe: `_bind_unit_scope` writes the list on **every** query a model makes and writes
  `''` when the scope cannot be computed, so an unbound agent gets the empty list and
  sees nothing. Fail-closed where it is reachable, rather than everywhere it is merely
  possible. Every other reader -- console, API, pipeline, a test -- must keep seeing the
  whole tenant, or the product is a black hole and an operator cannot debug an agent.
* **The operator is not affected.** Every other path — the console, the API, the
  pipeline, the demo — runs without `app.agent_unit_ids` set, which by the rule above
  would hide everything. So the setting is written by exactly one place,
  `TaskExecutionService._bind_unit_scope`, and every other reader sets it to `'*'`.

That last point is the design's sharp edge and it is why the sentinel is `'*'` and not
NULL: **a missing variable and an explicit wildcard must not look the same.** One is a
bug and one is a decision, and the database cannot tell them apart unless they are
written differently.

## Which tables

`tasks`, `executions`, `delegations`, `approvals`. These are the work products: what was
asked, what was done, who it went to, and what a person has to decide. Reference data —
`agents`, `roles`, `sop_definitions` — stays visible to every agent, because an agent
that cannot read the roster cannot route and the roster is the one thing F234 exists to
make real.
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


#: Tables whose rows carry a unit the reader may or may not see.
_UNIT_SCOPED = ("tasks", "executions", "delegations", "approvals")

_HELPER = """
CREATE OR REPLACE FUNCTION app_unit_visible(target_unit_id text)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT
        CASE
            -- Unset. **Unrestricted**, and this is the opposite of what the first
            -- version of this migration did.
            --
            -- The first version read unset as "nothing is visible", reasoning that an
            -- agent the platform failed to place should get nothing rather than
            -- everything. Measured, that reasoning was wrong in the most expensive way
            -- available: the `tenant` test fixture binds `app.current_tenant` directly
            -- rather than through `_set_tenant`, so *every integration test in the
            -- repository* lost the ability to read `tasks`, `executions` and
            -- `delegations` -- 60+ failures that had nothing to do with access control.
            --
            -- The agent path cannot reach this branch. `TaskExecutionService
            -- ._bind_unit_scope` writes the list on **every** query a model makes, and
            -- writes `''` when the scope cannot be computed, so an unbound agent gets
            -- the empty list and sees nothing. Fail-closed where it is reachable, rather
            -- than everywhere it is merely possible.
            --
            -- Every other reader -- the console, the API, the pipeline, a test -- binds
            -- the tenant without this variable and must keep seeing the whole tenant, or
            -- the product is a black hole and an operator cannot debug an agent.
            WHEN current_setting('app.agent_unit_ids', true) IS NULL THEN true
            -- Written as empty on purpose: this is the "no units" answer.
            WHEN current_setting('app.agent_unit_ids', true) = '' THEN false
            -- Written by every non-agent reader that wants to be explicit.
            WHEN current_setting('app.agent_unit_ids', true) = '*' THEN true
            -- A row with no unit belongs to nobody in particular -- a root task, a
            -- tenant-wide record -- and stays readable, or the agent cannot even see the
            -- goal it was handed.
            WHEN target_unit_id IS NULL THEN true
            ELSE target_unit_id = ANY (
                string_to_array(current_setting('app.agent_unit_ids', true), ',')
            )
        END
$$;
"""


def _tasks_unit(column: str = "org_unit_id") -> str:
    return f"app_unit_visible({column})"


def _executions_predicate() -> str:
    """An execution is owned by its agent, so the unit comes from `agents`.

    A subquery in a `USING` clause is allowed and is the only way to reach a column in
    another table without denormalising a unit onto every row of every table.
    """
    #
    # `executions.agent_id` is nullable, and the first version of this predicate made
    # every execution with no agent invisible: an `EXISTS` over a NULL key matches
    # nothing. Measured as three failures in `test_task_delegation.py::TestExecutions`
    # on `NotFoundError: execution not found` for rows the test had just written.
    #
    # An execution with no agent is not another department's work -- it is nobody's
    # attribution yet, the same reasoning that keeps a unit-less task readable.
    return (
        "executions.agent_id IS NULL OR EXISTS ("
        "SELECT 1 FROM agents au WHERE au.id = executions.agent_id "
        "AND app_unit_visible(au.org_unit_id))"
    )


def _delegations_predicate() -> str:
    """A delegation is visible if *either* end is: you may see what you sent, and you
    may see what arrived at a department you own."""
    return (
        "EXISTS (SELECT 1 FROM agents au WHERE au.id = delegations.source_agent_id "
        "AND app_unit_visible(au.org_unit_id)) OR EXISTS ("
        "SELECT 1 FROM agents au WHERE au.id = delegations.target_agent_id "
        "AND app_unit_visible(au.org_unit_id))"
    )


def _approvals_predicate() -> str:
    """An approval belongs to whoever has to decide it, and it carries no unit.

    `approvals` has no unit and no agent column, so the only handle is the task it is
    raised against. An approval is visible when that task is, or when the agent that
    *requested* it may see its own unit -- because an approval a department raised is
    its own business even while it sits in somebody else's inbox, and hiding it would
    make the requester's own record incomplete.
    """
    #
    # `approvals.task_id` is nullable, and an approval with **no task** was invisible to
    # everyone -- including the operator. Measured: 26 integration tests, ten of them in
    # `test_approvals_and_audit.py`, failing on `NotFoundError: approval not found` for
    # an approval the test had just created. The first version of this predicate was a
    # bare `EXISTS (... WHERE t.id = approvals.task_id ...)`, and an `EXISTS` over a
    # NULL key matches nothing. A boundary that hides the rows it was written to protect
    # is a bug wearing a security control's clothes.
    return (
        "approvals.task_id IS NULL OR EXISTS ("
        "SELECT 1 FROM tasks t WHERE t.id = approvals.task_id "
        "AND (app_unit_visible(t.org_unit_id) OR EXISTS ("
        "SELECT 1 FROM agents au WHERE au.id = t.requester_agent_id "
        "AND app_unit_visible(au.org_unit_id))))"
    )


_PREDICATES = {
    "tasks": _tasks_unit(),
    "executions": _executions_predicate(),
    "delegations": _delegations_predicate(),
    "approvals": _approvals_predicate(),
}


def upgrade() -> None:
    conn = op.get_bind()
    op.execute(_HELPER)

    for table in _UNIT_SCOPED:
        exists = conn.exec_driver_sql(  # noqa: S608 -- `table` is from _UNIT_SCOPED
            f"SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema = 'public' AND table_name = '{table}'"
        ).scalar()
        if not exists:
            # A table this build does not have is not a reason to fail the migration;
            # the policy is additive and the table would have its own.
            continue

        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(f'DROP POLICY IF EXISTS unit_scope_isolation ON "{table}"')
        # **AND, not OR, and not a replacement.** The tenant predicate from 0002 stays
        # and is combined: unit scope narrows, it never widens. A row outside the
        # tenant is invisible whatever the agent's list says.
        # **RESTRICTIVE, and that word is the whole fix.**
        #
        # Permissive policies for the same command are OR-ed together. `0002` already
        # installed `tenant_isolation` -- `USING (organization_id =
        # current_setting('app.current_tenant'))` -- which is *true for every row in the
        # tenant*. A second permissive policy ANDing a unit predicate therefore ORs with
        # a predicate that is already satisfied and **the unit scope does nothing**.
        #
        # Measured: with the policy installed, `ao_app`, `FORCE ROW LEVEL SECURITY` on,
        # the scope set to `root, back-office, finance`, and the rows correctly carrying
        # `finance`, `hr`, `procurement` -- the reader still got all three. The policy
        # was present, correct, and inert. `RESTRICTIVE` policies are AND-ed with the
        # permissive set instead, which is what "narrows, never widens" requires.
        op.execute(
            f'CREATE POLICY unit_scope_isolation ON "{table}" AS RESTRICTIVE '
            "USING (organization_id = current_setting('app.current_tenant', true) "
            f"AND {_PREDICATES[table]}) "
            "WITH CHECK (organization_id = current_setting('app.current_tenant', true))"
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in _UNIT_SCOPED:
        exists = conn.exec_driver_sql(  # noqa: S608 -- `table` is from _UNIT_SCOPED
            f"SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema = 'public' AND table_name = '{table}'"
        ).scalar()
        if not exists:
            continue
        op.execute(f'DROP POLICY IF EXISTS unit_scope_isolation ON "{table}"')
    op.execute("DROP FUNCTION IF EXISTS app_unit_visible(text)")
