"""The demo organisation must be able to run a task, not merely exist.

`scripts/demo_hierarchy_run.py` answers "have the departments done any work yet?"
by running the platform and printing what happened. These tests assert the things
that answer must contain, so it cannot quietly change to "nothing works" — or to
"it works" — without the suite noticing.

The second test is the one that matters. It is the test that would have caught
the defect this file was written for: the seeded company was carrying tool ids
from an older version of the seed, so building a context for any agent raised

    ToolId ... String should have at least 31 characters

and **no task could run at all**. Every acceptance test still passed, because
those tests build their own agents and tools inside a test tenant rather than
using the company an operator would actually get.

The default suite runs the real seed/runtime in an isolated test company. The
operator's existing company is reserved for explicit live smoke tests; an
ordinary pytest invocation must not add demo tasks to it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from structlog.testing import capture_logs

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.domain.ids import AgentId, ToolId, ToolVersionId
from ai_orchestrator.persistence.models import (
    Agent,
    Organization,
    OrgUnit,
    Task,
    Tool,
    ToolVersion,
)
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def seeded(tenant):
    """The demo company, seeded into the test's own tenant."""
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


async def test_every_seeded_id_is_accepted_by_its_own_type(seeded) -> None:
    """A seeded id its own branded type rejects makes the row unusable.

    Nothing catches this by construction: the row inserts fine, the column is
    `varchar`, and the failure only appears three layers later when somebody
    builds a context. The check belongs here because this is the last place it is
    cheap to notice.
    """
    for model, id_type in ((Tool, ToolId), (ToolVersion, ToolVersionId), (Agent, AgentId)):
        rows = (await seeded.session.execute(select(model))).scalars().all()
        assert rows, f"the seed created no {model.__tablename__} rows"
        for row in rows:
            (
                id_type(str(row.id)),
                (f"a seeded {model.__tablename__} row has an id its own type rejects: {row.id!r}"),
            )


async def test_the_seed_reports_what_it_actually_created(tenant) -> None:
    """The seed's own log line must match the tenant it just wrote.

    It did not. `units=len(DEPARTMENTS)` and `agents=len(department_agents) + 1`
    counted the spec lists, which exclude the three offices and their three
    agents -- so the log said `units 7, agents 7` over a tenant holding 10 and 10,
    and every downstream reading of that number was wrong by a third. Nothing
    failed: the seed worked, and the report about the seed was false.

    Asserting the log is the only instrument that catches it, because a count
    that is merely *correct* cannot tell you a count is being taken at all.
    """
    org_id = tenant.organization_id
    org = (
        await tenant.session.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one()
    # structlog does not route through stdlib logging, so `caplog` cannot see
    # this line at all -- an assertion against caplog would pass vacuously.
    with capture_logs() as captured:
        await seed(tenant.session, into=org)

    record = next(r for r in captured if r.get("event") == "seed.created")

    units = (
        (await tenant.session.execute(select(OrgUnit).where(OrgUnit.organization_id == org_id)))
        .scalars()
        .all()
    )
    agents = (
        (await tenant.session.execute(select(Agent).where(Agent.organization_id == org_id)))
        .scalars()
        .all()
    )

    assert record["units"] == len(units), (
        f"the seed reported {record['units']} units and wrote {len(units)}"
    )
    assert record["agents"] == len(agents), (
        f"the seed reported {record['agents']} agents and wrote {len(agents)}"
    )
    # The three tiers are what the count is for, so assert the shape too: a count of
    # 11 is only correct for 1 company + 3 offices + 7 departments.
    #
    # The 7 is IT, added as the seventh department so `ONX-BO-IT-SOP-007` and
    # `ONX-PMO-KNW-SOP-006` stopped having no owner. This is the assertion that failed
    # when it was seeded and not updated — which is the point of writing the shape
    # rather than only the total: a total alone would have gone from 10 to 11 without
    # saying anything about the shape behind it.
    assert {u.unit_type for u in units} == {"company", "office", "department"}
    assert sum(1 for u in units if u.unit_type == "office") == 3
    assert sum(1 for u in units if u.unit_type == "department") == 7
    assert len(agents) == 11
    it = [a for a in agents if a.name == "IT Agent"]
    assert len(it) == 1, "the seventh department did not seed its agent"
    assert it[0].org_unit_id is not None, "the IT agent has no unit to report from"


async def test_a_seeded_agent_can_actually_execute_a_task(seeded) -> None:
    """The whole pipeline, against a *seeded* agent.

    A real task, assigned to a seeded agent, executed by the real service with
    the context the real context builder produces. This is the test that would
    have failed on the stale-id defect.
    """
    org_id = seeded.organization_id
    agent = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Sales Agent")
        )
    ).scalar_one()

    tasks = TaskRepository(seeded.session, org_id)
    service = TaskExecutionService(seeded.session, org_id, runtime=ScriptedRuntime())

    task = await tasks.create(
        title="Prove the seeded agent runs",
        goal="Summarise the quarter",
        task_type="analysis",
        requester_type="human",
    )
    await tasks.assign(task.id, agent.id)
    outcome = await service.execute_task(task.id, agent_id=agent.id)

    assert outcome.succeeded, f"a seeded agent could not run: {outcome.summary}"
    assert (await tasks.get(task.id)).status == "completed"


async def test_the_executive_is_told_which_agents_it_may_delegate_to(seeded) -> None:
    """The roster a model needs to route, built from the real organisation tree.

    A model asked to pick a colleague with no list invents a plausible
    department, and the delegation is then refused for a reason that has nothing
    to do with the goal. This asserts the list is both present and *real* — every
    name it offers is an agent that actually exists in the seeded company.
    """
    org_id = seeded.organization_id
    service = TaskExecutionService(seeded.session, org_id, runtime=ScriptedRuntime())
    executive = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Executive Agent")
        )
    ).scalar_one()
    resolved = await service._resolve_agent(executive.id)
    roster = await service._delegate_roster(resolved)

    assert roster, "the executive was given nobody to delegate to"
    real_names = set(
        (
            await seeded.session.execute(select(Agent.name).where(Agent.organization_id == org_id))
        ).scalars()
    )
    for name, purpose in roster:
        assert name in real_names, f"the roster offers a name that does not exist: {name!r}"
        assert purpose, f"the roster describes {name!r} with nothing"
    # The executive delegates to the **offices**, not straight to departments.
    # It used to reach Marketing, which was correct while every unit was a
    # direct child of the root; with three tiers in between, Marketing is two hops
    # down and offering it here would mean the hierarchy is not enforced.
    #
    # Asserted by tier rather than by one name: three offices is the shape, and a
    # name is a detail that changes when an office is renamed.
    offered = {n for n, _ in roster}
    for office in ("Front Office Agent", "Middle Office Agent", "Back Office Agent"):
        assert office in offered, f"the executive cannot reach {office}: {sorted(offered)}"
    assert "Marketing Agent" not in offered, (
        "a department is two tiers down; offering it directly would let the "
        "executive bypass the office that owns it"
    )


@pytest.mark.live_model
def test_the_real_demo_script_runs_end_to_end() -> None:
    """`demo_real_run.py` must not crash on the path it exists to demonstrate.

    It did, twice, and both times *after* the interesting part: the first crash
    was an un-awaited coroutine while collecting the child tasks, so the run had
    already delegated, printed `delegations recorded: 1`, and then died on the way
    to showing the children. A demo that proves delegation and then exits non-zero
    teaches the reader that the tool is broken.
    """
    import os
    import subprocess
    import sys

    # This opt-in live_model test targets the developer company. It is excluded
    # from make test, unlike the deterministic subprocess test below.
    # `AO_TEST_DB_NAME` is set alongside `AO_POSTGRES_DB` because
    # `Settings` exposes a separate test DSN, and leaving the suite's override in
    # place would point the child at the test schema while the environment claimed
    # otherwise.
    env = {
        **os.environ,
        "AO_POSTGRES_DB": "ai_orchestrator",
        "AO_TEST_DB_NAME": "ai_orchestrator",
        "AO_ENVIRONMENT": "local",
    }
    result = subprocess.run(
        [
            sys.executable,
            "scripts/demo_real_run.py",
            "--goal",
            "Draft a one-line status update",
            # One level, and a small token budget for the same reason the
            # deterministic demo is not used here: this test is about the *script*
            # running to completion and reporting the two facts that make its
            # output readable. Whether a live model decomposes a goal is a
            # question about a provider — it belongs in a demo, not in a gate that
            # has to pass on a machine with no network.
            "--depth",
            # **Zero, not one — and that is the fix for a gate that timed out.**
            #
            # This test runs the real script against a real company, and the agent's
            # `model_profile` comes from the seed, so every model call is a live free
            # model. At `--depth 1` the script also executes every child the model
            # chose to delegate, and *how many* it chose is not ours to bound:
            #
            #     322.76s   one delegation, measured standalone
            #     >900s     a suite run, timed out
            #
            # Nothing in the platform differed between those two. A gate whose
            # pass/fail is a third party's response time is not a gate, and the test's
            # own comment already says the provider question "belongs in a demo, not in
            # a gate".
            #
            # At depth 0 the script still does everything this test asserts: it creates
            # the goal, runs the Executive through the real runtime, and prints the two
            # facts that make the output readable. Only the children are skipped, and
            # the children are what cost 900 seconds. Running them is the demo's job —
            # `make demo` — and `scripts/demo_real_run.py --depth 2` still does.
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
    )
    if "no organisation with slug" in result.stdout:
        pytest.skip("the development database has no demo company; run `make seed --reset`")
    assert result.returncode == 0, (
        f"the real-run demo exited {result.returncode}\n"
        f"{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    )
    # Whichever way the model chooses to go, the two facts that make the output
    # readable have to be in it.
    assert "model used :" in result.stdout, result.stdout[-1500:]
    assert "delegations from this goal:" in result.stdout, result.stdout[-1500:]
    # The count must be scoped to the run. A tenant-wide count grows with every
    # run ever executed, so the third run reported "3" for a single delegation
    # and looked like a duplication bug that had already been fixed.
    assert "children still open:" in result.stdout, result.stdout[-1500:]


async def test_the_demo_script_reports_the_truth_about_delegation(seeded) -> None:
    """`demo_hierarchy_run.py` is the answer to "have the departments worked yet?".

    Asserted rather than trusted: the script must actually run against the real
    seeded company, and it must report the delegation count. A script that
    prints a cheerful summary while the organisation does nothing is the exact
    failure this project has already been bitten by once — the legacy system it
    learned from had a five-department orchestrator that was seven empty
    directories, and a passing test suite beside it.
    """
    import os

    from ai_orchestrator.config.settings import get_settings

    # The subprocess runs the real seeder/runtime/database path, in the same
    # isolated test company as this test. Ordinary pytest must never create demo
    # tasks in an operator's organization merely to prove a script can execute.
    settings = get_settings()
    assert settings.test_db_name == "ai_orchestrator_test"
    slug = await seeded.session.scalar(
        select(Organization.slug).where(Organization.id == seeded.organization_id)
    )
    assert slug
    await seeded.commit()  # A second process needs committed, tenant-bound seed data.
    env = {
        **os.environ,
        "AO_POSTGRES_DB": settings.test_db_name,
        "AO_TEST_DB_NAME": settings.test_db_name,
        "AO_ENVIRONMENT": "test",
        "AO_SEED_ORGANIZATION_SLUG": slug,
        "AO_MODEL_PROVIDER_DEFAULT": "fake",
        "AO_EMBEDDING_PROVIDER_DEFAULT": "hash",
    }
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "scripts/demo_hierarchy_run.py"],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
    )
    assert result.returncode == 0, (result.stdout + result.stderr)[-2000:]
    # Query the actual shared test database, not only the child's self-report.
    written = (
        (
            await seeded.session.execute(
                select(Task.id).where(
                    Task.organization_id == seeded.organization_id, Task.title == "Board pack"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(written) == 1, "the demo did not persist its root task in the isolated test company"
    bound = next(
        (
            int(line.split(":", 1)[1].split()[0])
            for line in result.stdout.splitlines()
            if line.strip().startswith("agents bound")
        ),
        None,
    )
    assert bound is not None, result.stdout[-2000:]
    assert bound >= 9, f"the block agents went missing: {result.stdout[-2000:]}"
    assert "delegations" in result.stdout
    # The honest line, whichever way the answer goes. If the organisation ever
    # starts decomposing goals by itself, this is the assertion that should fail,
    # and its failure should be read as "the world changed", not "the test broke".
    assert (
        "The organisation did NOT decompose the goal." in result.stdout
        or "The organisation decomposed the goal by itself." in result.stdout
    ), result.stdout[-2000:]
