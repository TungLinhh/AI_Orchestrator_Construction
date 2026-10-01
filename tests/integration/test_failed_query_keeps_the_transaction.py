"""A refused query must not kill the transaction it was refused in.

`internal_database_query` is the one tool a model is *expected* to get wrong. It
exists so an agent can read the tenant's own data, and the whole design assumes
the model will try queries that fail — a table that does not exist, a column it
invented, a predicate it got wrong — and learn from the refusal.

That assumption was not implemented. In Postgres a statement that errors aborts
the entire transaction, and every subsequent command on that session fails with
`current transaction is aborted` until something issues a `ROLLBACK`. The tool
caught the error and returned `QUERY_FAILED` correctly, so from the tool's point
of view nothing was wrong, and the run's transaction was dead all the same.

It was found by watching a demo run rather than by reading anything. The model
wrote:

    SELECT organization_id, * FROM tasks WHERE id = '08017d96' OR run_id = '08017d96'

`tasks` has `workflow_run_id` and no `run_id`, so the statement raised. The tool
reported the failure to the model, correctly. The connection then sat in
`idle in transaction (aborted)` for six minutes and the process never exited. The
tool's error handling was right and the reader underneath it was not, which is the
shape of bug this file exists for: correct behaviour at one layer hiding a missing
behaviour at the one below it.

**Why the existing tests could not have found it.** Every test of this tool
injects a fake `read_tenant_sql` that either returns a list or raises
`ValueError`. A fake has no transaction, so it cannot be poisoned, so
`test_a_bad_query_is_reported_as_a_bad_query` passes with or without the fix. The
savepoint lives in the real reader, so these tests use the real reader and a real
database. That is the difference between testing a contract and testing a stub of
one.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.application.task_execution import TaskExecutionService
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]


def _service(session: AsyncSession, organization_id: str) -> TaskExecutionService:
    """A service with no runtime.

    `_make_tenant_reader` closes over the session and the organization id and
    nothing else, so the agent runtime is never touched. Passing `None` is
    deliberate: it makes any accidental dependency on the runtime an
    `AttributeError` here rather than a subtle difference in production.
    """
    return TaskExecutionService(session, organization_id, runtime=None)  # type: ignore[arg-type]


class _Ctx:
    """The one attribute the reader reads off its context."""

    def __init__(self, organization_id: str) -> None:
        self.organization_id = organization_id


class TestARefusedQueryLeavesTheTransactionUsable:
    async def test_a_good_query_after_a_bad_one_still_works(self, tenant: Tenant) -> None:
        """The regression, stated as the smallest case that catches it.

        Bad query, then good query, on the same session. Without the savepoint the
        second raises `InFailedSqlTransaction` — or, on a driver that retries,
        returns nothing and hangs.
        """
        await tenant.session.execute(
            text(
                "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
                "VALUES (:o, 'cai', 'Cái', 'count')"
            ),
            {"o": tenant.organization_id},
        )
        await tenant.commit()

        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )

        with pytest.raises(DBAPIError):
            await read("SELECT id FROM tasks WHERE organization_id = :__tenant__ AND run_id = 'x'")

        rows = await read(
            "SELECT code FROM units_dictionary WHERE organization_id = :__tenant__ AND code = 'cai'"
        )
        assert rows == [{"code": "cai"}], (
            "the transaction was poisoned by the failed query; a model that gets "
            "one query wrong can no longer read anything at all"
        )

    async def test_a_syntax_error_is_also_recoverable(self, tenant: Tenant) -> None:
        """Not just a missing column.

        A parse error aborts the transaction identically, and it is the more common
        mistake — models drop a comma far more often than they invent a column.

        The query has to *pass* the read-only guard and then fail in Postgres, or
        it tests the guard instead. `SELEC ...` is refused before it reaches the
        database now, which is correct and is asserted separately below.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        with pytest.raises(DBAPIError):
            await read("SELECT , FROM organizations WHERE id = :__tenant__")
        rows = await read("SELECT slug FROM organizations WHERE id = :__tenant__")
        assert rows and rows[0]["slug"], "the tenant row is not readable"

    async def test_a_misspelled_keyword_never_reaches_the_database(self, tenant: Tenant) -> None:
        """The guard catches it first, and that is the better outcome.

        `SELEC` is a typo, not a write, so refusing it is a false positive by the
        letter of the rule — and it is still the right answer, because the
        alternative is a database error that poisons the transaction. Pinning it
        documents the trade: the guard is deliberately biased towards refusing, and
        the cost of a false refusal is a message the model can act on.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        with pytest.raises(ValueError, match="not a read"):
            await read("SELEC slug FROM organizations WHERE id = :__tenant__")
        rows = await read("SELECT slug FROM organizations WHERE id = :__tenant__")
        assert rows and rows[0]["slug"], "the transaction must be untouched"

    async def test_a_type_error_is_also_recoverable(self, tenant: Tenant) -> None:
        """The third shape, because there are three ways to be wrong here."""
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        with pytest.raises(DBAPIError):
            await read("SELECT 1 / 0 AS boom WHERE organization_id = :__tenant__")
        rows = await read("SELECT slug FROM organizations WHERE id = :__tenant__")
        assert rows and rows[0]["slug"], "the tenant row is not readable"

    async def test_work_written_before_the_bad_query_is_still_committable(
        self, tenant: Tenant
    ) -> None:
        """The point of `ROLLBACK TO SAVEPOINT` rather than a full `ROLLBACK`.

        A full rollback would recover the session and destroy the run's progress —
        every audit row, every tool-call record written so far. The savepoint
        undoes one statement and leaves the rest of the transaction intact, and
        this is the assertion that distinguishes the two.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        await tenant.session.execute(
            text(
                "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
                "VALUES (:o, 'm', 'Mét', 'length')"
            ),
            {"o": tenant.organization_id},
        )
        with pytest.raises(DBAPIError):
            await read("SELECT nope FROM tasks WHERE organization_id = :__tenant__")
        await tenant.session.execute(
            text(
                "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
                "VALUES (:o, 'm2', 'Mét vuông', 'area')"
            ),
            {"o": tenant.organization_id},
        )
        await tenant.commit()

        codes = (
            (
                await tenant.session.execute(
                    text(
                        "SELECT code FROM units_dictionary WHERE organization_id = :o "
                        "AND code IN ('m', 'm2') ORDER BY code"
                    ),
                    {"o": tenant.organization_id},
                )
            )
            .scalars()
            .all()
        )
        assert codes == ["m", "m2"], (
            "the unit written before the failed query and the one written after it "
            "must both survive; a full rollback would have lost the first"
        )

    async def test_the_savepoint_does_not_leak(self, tenant: Tenant) -> None:
        """A savepoint held open across calls would be a slow resource leak.

        Named savepoints and `RELEASE` mean one statement's savepoint cannot
        outlive it. A second query that fails for a different reason must still
        roll back cleanly, which it cannot if the first savepoint is still open.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        for bad in (
            "SELECT missing_column FROM tasks WHERE organization_id = :__tenant__",
            "SELECT * FROM no_such_table WHERE organization_id = :__tenant__",
            "SELECT 1/0 WHERE organization_id = :__tenant__",
        ):
            with pytest.raises(DBAPIError):
                await read(bad)
        rows = await read("SELECT slug FROM organizations WHERE id = :__tenant__")
        assert rows and rows[0]["slug"], "the tenant row is not readable"

    async def test_the_reader_refuses_to_write(self, tenant: Tenant) -> None:
        """A guarantee the reader's own docstring made and did not keep.

        `_make_tenant_reader` called itself "a read-only SQL callable". It was not
        read-only: it executed `DELETE`, and the only reason the first version of
        this test noticed is that a write returns no rows, so `.mappings()` raised
        `ResourceClosedError` *after* the rows were gone. The tool
        (`_internal_database_query`) does refuse writes by inspecting the SQL, so
        the hole was only reachable by calling the reader directly — which is
        exactly what a second tool would do.
        """
        await tenant.session.execute(
            text(
                "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
                "VALUES (:o, 'keepsake', 'Cái', 'count')"
            ),
            {"o": tenant.organization_id},
        )
        await tenant.commit()

        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        with pytest.raises(ValueError, match="not a read"):
            await read("DELETE FROM units_dictionary WHERE organization_id = :__tenant__")
        await tenant.commit()

        still_there = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM units_dictionary "
                    "WHERE organization_id = :o AND code = 'keepsake'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert still_there == 1, "the reader deleted a row it was supposed to refuse"

    async def test_a_write_shaped_cte_is_refused_too(self, tenant: Tenant) -> None:
        """The case a leading-keyword check alone would miss.

        `WITH ... DELETE` starts with `WITH`, which is a read leader, and ends with
        a write. Checking only the first word is how a read-only guard becomes
        decorative.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        with pytest.raises(ValueError, match="DELETE"):
            await read(
                "WITH doomed AS (SELECT code FROM units_dictionary) "
                "DELETE FROM units_dictionary USING doomed WHERE true"
            )

    async def test_a_legitimate_read_is_not_refused(self, tenant: Tenant) -> None:
        """The other direction, and the one that matters more.

        A read tool that refuses valid SQL is a tool whose refusals get routed
        around. A `SELECT` whose literals contain a write keyword must still run.
        """
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        rows = await read(
            "SELECT 'delete' AS keyword, 1 AS one FROM organizations WHERE id = :__tenant__"
        )
        assert rows and rows[0]["keyword"] == "delete", (
            "a string literal naming a write keyword must not make a SELECT look like a write"
        )

    async def test_a_read_only_cte_still_runs(self, tenant: Tenant) -> None:
        """`WITH` is allowed, so the CTE people actually write must work."""
        read = _service(tenant.session, tenant.organization_id)._make_tenant_reader(
            _Ctx(tenant.organization_id)
        )
        rows = await read(
            "WITH mine AS (SELECT slug FROM organizations WHERE id = :__tenant__) "
            "SELECT slug FROM mine"
        )
        assert rows and rows[0]["slug"]
