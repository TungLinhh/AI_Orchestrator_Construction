"""A department may not read another department's work, through the database.

**There was no unit boundary before migration 0030.** `app.current_tenant` was the only
predicate, the whole company is one organisation, and `internal_database_query` refuses
writes, refuses multi-statement SQL and refuses catalog reads — three careful controls,
none of them about separation. A department with that tool could read the entire ledger.

These tests go through the database rather than through the Python policy, because the
Python policy is a decision function and the database is the boundary. A decision
function with no enforcement is a comment. Each one sets `app.agent_unit_ids` the way
`TaskExecutionService._bind_unit_scope` does and then counts rows.

**The rows come from real work.** A department reading a peer's rows is only interesting
if the peer has rows, so every fixture creates them through the repository rather than
inserting fixtures by hand.
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.unit_scope import permitted_unit_ids_for, tenant_units

pytestmark = pytest.mark.integration


async def _units(session: Any, org_id: str) -> dict[str, Any]:
    return {u.slug: u for u in await tenant_units(session, org_id)}


async def _set_scope(session: Any, unit_ids: list[str]) -> None:
    """Exactly what the executor does, so the test and the product share one path."""
    await session.execute(
        text("SELECT set_config('app.agent_unit_ids', :ids, true)"),
        {"ids": ",".join(unit_ids)},
    )


@pytest_asyncio.fixture
async def units(tenant):  # type: ignore[no-untyped-def]
    """The seeded tree, keyed by slug. Most of these tests are about units, and a
    tenant with no units would make every assertion vacuously true."""
    from sqlalchemy import select as _select

    from ai_orchestrator.persistence.models import Organization as _Org
    from ai_orchestrator.seed import seed as _seed

    org = (
        await tenant.session.execute(_select(_Org).where(_Org.id == tenant.organization_id))
    ).scalar_one()
    await _seed(tenant.session, into=org)
    await tenant.commit()
    return {u.slug: u for u in await tenant_units(tenant.session, str(tenant.organization_id))}


class TestTheScopeComputation:
    async def test_a_department_is_scoped_to_its_branch(self, tenant, units) -> None:
        org = str(tenant.organization_id)
        permitted = await permitted_unit_ids_for(tenant.session, org, units["hr"].id)
        slugs = {u.slug for u in units.values() if u.id in set(permitted)}
        assert slugs == {"root", "back-office", "hr"}, (
            f"HR reads {sorted(slugs)}, so it can see work it does not own"
        )

    async def test_an_office_is_scoped_to_its_own_departments(self, tenant, units) -> None:
        org = str(tenant.organization_id)
        permitted = set(await permitted_unit_ids_for(tenant.session, org, units["back-office"].id))
        slugs = {u.slug for u in units.values() if u.id in permitted}
        assert slugs == {"root", "back-office", "finance", "hr", "it"}, slugs

    async def test_the_chief_is_scoped_to_everything(self, tenant, units) -> None:
        org = str(tenant.organization_id)
        permitted = set(await permitted_unit_ids_for(tenant.session, org, units["root"].id))
        assert {u.slug for u in units.values() if u.id in permitted} == set(units)

    async def test_an_agent_with_no_unit_reads_nothing(self, tenant) -> None:
        """The safe direction. A mis-bound agent is the least privileged, not the most."""
        org = str(tenant.organization_id)
        assert await permitted_unit_ids_for(tenant.session, org, None) == []
        assert await permitted_unit_ids_for(tenant.session, org, "org_unit_that_is_not") == []

    async def test_an_office_may_not_delegate_across_branches(self, tenant, units) -> None:
        """The scope is a read scope and the delegation rule is separate, but they must
        agree: an office that cannot read another office's work has no business handing
        it any."""
        from ai_orchestrator.domain.access import may_delegate_to

        org = str(tenant.organization_id)
        units = await _units(tenant.session, org)
        permitted = set(await permitted_unit_ids_for(tenant.session, org, units["back-office"].id))
        assert units["finance"].id in permitted
        assert units["procurement"].id not in permitted
        assert not may_delegate_to(units["back-office"], units["procurement"])


class TestTheDatabaseEnforcesIt:
    """The boundary, exercised through a session with the scope set."""

    @pytest.fixture
    async def with_work(self, tenant):  # type: ignore[no-untyped-def]
        """One task per department, owned by that department's agent."""
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Agent, Organization
        from ai_orchestrator.seed import seed

        org = (
            await tenant.session.execute(
                select(Organization).where(Organization.id == tenant.organization_id)
            )
        ).scalar_one()
        await seed(tenant.session, into=org)
        org_id = str(tenant.organization_id)
        units = await _units(tenant.session, org_id)
        agents = {
            str(a.org_unit_id): str(a.id)
            for a in (
                await tenant.session.execute(select(Agent).where(Agent.organization_id == org_id))
            ).scalars()
        }
        repo = TaskRepository(tenant.session, org_id)
        made = {}
        for slug in ("finance", "hr", "procurement"):
            unit = units[slug]
            agent_id = agents[unit.id]
            made[slug] = str(
                (
                    await repo.create(
                        title=f"{slug} work",
                        goal=f"do the {slug} work",
                        owner_agent_id=agent_id,
                        # The unit is what the policy filters on, so a fixture that
                        # leaves it NULL is testing the "no unit, everyone reads it"
                        # branch and would pass for the wrong reason. This is exactly
                        # the omission the delegation executor had.
                        org_unit_id=unit.id,
                        allow_parallel=True,
                    )
                ).id
            )
        await tenant.commit()
        return {"org": org_id, "units": units, "tasks": made}

    async def _titles_readable(self, tenant, org_id: str, unit_ids: list[str]) -> set[str]:
        """Set the scope the way the executor does, then read the titles it permits."""
        await _set_scope(tenant.session, unit_ids)
        return await self._titles_readable_unscoped(tenant, org_id)

    async def _titles_readable_unscoped(self, tenant, org_id: str) -> set[str]:
        rows = (
            await tenant.session.execute(
                text("SELECT title FROM tasks WHERE organization_id = :o"),
                {"o": org_id},
            )
        ).scalars()
        return {str(r) for r in rows}

    async def test_a_department_cannot_read_its_peers_tasks(self, tenant, with_work) -> None:
        """The test the whole migration exists for."""
        org, units = with_work["org"], with_work["units"]
        scope = await permitted_unit_ids_for(tenant.session, org, units["finance"].id)
        titles = await self._titles_readable(tenant, org, scope)

        assert "finance work" in titles, "a department cannot read its own work"
        # **A sibling is not readable either, and that is the split working.**
        #
        # `domain/access.py` gives a peer `STATUS` -- enough to know HR exists and is
        # busy -- and denies it `CONTENT`. Status is not a row: it is the roster the
        # agent is handed. So at the database layer a department sees *only its own*
        # rows, and "HR exists" is answered by `agents`, which stays readable by
        # everyone because an agent that cannot read the roster cannot route.
        assert "hr work" not in titles, (
            "a sibling in the same office is readable, so the peer rule is not applied "
            "at the row level"
        )
        assert "procurement work" not in titles, (
            "Finance is reading Procurement's rows: the tenant predicate is the only "
            "one in force, so there is no unit boundary"
        )

    async def test_an_office_reads_its_own_departments_only(self, tenant, with_work) -> None:
        org, units = with_work["org"], with_work["units"]
        scope = await permitted_unit_ids_for(tenant.session, org, units["back-office"].id)
        titles = await self._titles_readable(tenant, org, scope)
        assert {"finance work", "hr work"} <= titles
        assert "procurement work" not in titles

    async def test_the_chief_reads_everything(self, tenant, with_work) -> None:
        org, units = with_work["org"], with_work["units"]
        scope = await permitted_unit_ids_for(tenant.session, org, units["root"].id)
        titles = await self._titles_readable(tenant, org, scope)
        assert {"finance work", "hr work", "procurement work"} <= titles

    async def test_an_unset_scope_sees_nothing(self, tenant, with_work) -> None:
        """Fail closed. `current_setting(..., true)` reads NULL as "unrestricted" by the
        usual convention, and here that convention would make an unbound agent the most
        privileged reader on the tenant."""
        org = with_work["org"]
        await _set_scope(tenant.session, [])
        titles = await self._titles_readable(tenant, org, [])
        assert titles == set(), f"an empty scope read {sorted(titles)}"

    async def test_the_operator_path_is_not_sandboxed(self, tenant, with_work) -> None:
        """`'*'` is what every non-agent reader gets, and it must mean unrestricted.

        If this fails the console and the API see an empty product, which is worse than
        the leak it was written to prevent.
        """
        org = with_work["org"]
        # Deliberately **not** through `_titles_readable`, which sets the scope itself
        # and would overwrite the wildcard with the empty list this test is about.
        await _set_scope(tenant.session, ["*"])
        titles = await self._titles_readable_unscoped(tenant, org)
        assert {"finance work", "hr work", "procurement work"} <= titles

    async def _titles_readable_unscoped(self, tenant, org_id: str) -> set[str]:
        rows = (
            await tenant.session.execute(
                text("SELECT title FROM tasks WHERE organization_id = :o"),
                {"o": org_id},
            )
        ).scalars()
        return {str(r) for r in rows}

    async def test_the_scope_does_not_survive_its_transaction(self, tenant, with_work) -> None:
        """`SET LOCAL`, or the connection goes back to the pool still scoped.

        An agent's narrow scope surviving into the next borrower would be the leak in
        the other direction — and the one nobody would notice, because it hides rather
        than exposes.
        """
        org = with_work["org"]
        await _set_scope(tenant.session, [with_work["units"]["hr"].id])
        narrow = await self._titles_readable(tenant, org, [with_work["units"]["hr"].id])
        assert "finance work" not in narrow

        # **`SET LOCAL` dies with the transaction.** `tenant.commit()` ends the current
        # one and re-establishes the tenant binding, which is the shape the fixture
        # itself uses for the same reason. A narrow scope that outlived its transaction
        # would travel back to the pool with the connection -- the leak in the other
        # direction, and the one nobody notices because it hides rather than exposes.
        await tenant.commit()
        await tenant.session.execute(
            text("SELECT set_config('app.current_tenant', :o, true)"),
            {"o": org},
        )
        await tenant.session.execute(text("SELECT set_config('app.agent_unit_ids', '*', true)"))
        wide = await self._titles_readable_unscoped(tenant, org)
        assert "finance work" in wide, "a stale narrow scope survived its transaction"
