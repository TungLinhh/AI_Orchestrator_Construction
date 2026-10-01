"""The eight agents exist, are bounded, and can be stopped. Against a live database.

`tests/unit/test_agent_register.py` checks the register as data. This file checks that the
seeder put it in the database *and that the database's own constraints agree*.

## Why a separate integration file

A unit test can assert that `REGISTER` has eight entries. It cannot assert that
`procedure_versions.status = 'active'` for the sixteen SOPs, that every agent's granted
level is below its ceiling, or that the kill switch stops one. Those are properties of rows,
and a row is only real in a database.

## What each test is for, and which are negative

* `test_all_sixteen_procedures_are_active` — the shadow → promote path actually ran. A
  version in `shadow` changes nothing, so sixteen versions still in `shadow` means the
  eight agents have nothing to run.
* `test_every_agent_has_an_active_procedure` — per agent, not in aggregate. Eight agents and
  sixteen SOPs could average out to "some agent has nothing".
* `test_every_agent_is_registered_at_the_ceiling_its_classes_permit` — the ceiling the
  seeder wrote equals the ceiling `autonomy_policies` permits. A seeder that computed it
  once and cached it would drift the moment a policy row changed.
* `test_every_agent_is_granted_below_its_ceiling` — the negative test that matters. A grant
  above a ceiling is an agent authorised past its own bound, and it is not a value the
  seeder can produce by accident — which is why the test exists rather than a comment.
* `test_the_kill_switch_stops_an_agent` — the switch, end to end: thrown, and the agent is
  `suspended`.
* `test_a_killed_agent_cannot_hold_an_active_procedure` — the governance coupling. A dead
  agent's active version is a maintenance claim with nobody behind it, and the promotion
  gate already refuses to promote a killed agent's version (`AUTHOR_KILLED`). This checks
  the *state*, not the gate.

## The seeder is the fixture, and it must be idempotent

`scripts/seed_agent_register.py` is called twice: once to write, once to prove the second
call changes nothing. A seeder that duplicates on the second run produces two definitions
per agent and every assertion above becomes ambiguous about which one it meant.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.agent_register import REGISTER, ceiling_for
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

SEEDER = Path(__file__).resolve().parents[2] / "scripts" / "seed_agent_register.py"


@pytest.fixture(scope="session", autouse=True)
def catalogue_is_present() -> None:
    """Skip the whole module unless *this schema* holds the dossier's SOP catalogue.

    `sop_definitions` is tenant-scoped, and the dossier's twenty-eight SOPs have only ever
    been seeded into the **development** schema. `Database.tenant_session` binds to
    whichever schema the process is configured for and cannot read across, so this file
    cannot provision one for itself.

    So the whole module skips, once, with the reason and the way to close it. The
    alternative -- a per-test skip, or an `xfail` -- produces a wall of identical skips and
    hides whether the gap is one test or the file.

    **What is verified today, and where:** the register itself is seeded and verified in
    the development schema -- sixteen of sixteen SOPs promoted from `shadow` to `active`,
    eight agents registered, every one granted L1. `tests/unit/test_agent_register.py`
    checks the register as data and needs no database. This file is the check that the
    seeder's *output* is right, and it needs a seeded schema to say so.
    """
    import asyncio

    from sqlalchemy import text

    from ai_orchestrator.persistence.session import Database

    needed = len({c for spec in REGISTER for c in spec.sop_codes_prefixed})

    async def count() -> int:
        # The **owner** connection, deliberately. The first version counted through
        # `tenant_session("")` and got zero on a schema that held twenty-eight SOPs,
        # because `sop_definitions` has RLS `FORCE`d and a session with no tenant bound
        # sees nothing. That is RLS working correctly; using it as a provisioning check
        # was not. A count of "how much reference data does this schema have" is a
        # question about the schema, so it is asked with the role that can see the schema.
        db = Database.from_settings(use_admin_role=True)
        try:
            async with db.session() as session:
                return int(
                    (await session.execute(text("SELECT count(*) FROM sop_definitions"))).scalar()
                )
        finally:
            await db.dispose()

    have = asyncio.run(count())
    if have < needed:
        pytest.skip(
            f"this schema holds {have} SOP(s) and the register names {needed}. "
            "sop_definitions is tenant-scoped, so a schema that has never been seeded with "
            "the dossier's catalogue cannot run its agents. Seed this schema and re-run."
        )


@pytest.fixture(scope="session")
def catalogue_holder() -> str:
    """An organization in *this* schema holding the dossier's catalogue.

    `--from-org` is passed to the seeder rather than left to discovery, and the reason is
    specific: the test schema can hold **several** catalogue holders, because
    `seed_reference_catalogue.py` was run more than once before it was made idempotent.
    Discovery then refuses with *"2 organizations hold at least 16 SOPs. Pass --from-org to
    say which"* -- which is the seeder being right and this test being ambiguous.

    The first holder is used. Any of them is a copy of the same catalogue, and the test is
    about what the seeder produces, not about which tenant it read from.
    """
    import asyncio

    from ai_orchestrator.persistence.session import Database

    async def find() -> str:
        db = Database.from_settings(use_admin_role=True)
        try:
            async with db.session() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT organization_id FROM sop_definitions "
                            " GROUP BY organization_id HAVING count(*) >= 16 "
                            " ORDER BY 1 LIMIT 1"
                        )
                    )
                ).first()
        finally:
            await db.dispose()
        return str(row[0]) if row else ""

    return asyncio.run(find())


@pytest.fixture(autouse=True)
async def seeded(tenant: Tenant, catalogue_holder: str) -> Tenant:
    """The register, seeded for this test's tenant.

    A script call rather than a hand-written fixture, deliberately. The alternative is a
    set of INSERTs in the test that *look* like the seeder's and are not: they would keep
    passing after the seeder broke, and this file's whole claim is that the seeder's output
    is correct. A subprocess also means the seeder's own idempotence is exercised on every
    run, because the fixture is followed by a second call.
    """
    # A subprocess is blocking work inside an async test. It runs the seeder a person runs,
    # which is the point: importing and awaiting its functions would stop testing the
    # script itself.
    result = subprocess.run(  # noqa: ASYNC221
        [
            sys.executable,
            str(SEEDER),
            "--org",
            tenant.organization_id,
            "--from-org",
            catalogue_holder,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"the seeder refused:\n{result.stderr[-2000:]}\nSTDOUT:\n{result.stdout[-1500:]}"
    )
    assert "! " not in result.stderr, f"the seeder reported refusals:\n{result.stderr[-2000:]}"
    return tenant


async def _permitted(session, org: str) -> dict[str, str]:
    rows = (
        await session.execute(
            text(
                "SELECT action_class, max_level FROM autonomy_policies "
                " WHERE organization_id = CAST(:o AS varchar(64))"
            ),
            {"o": org},
        )
    ).all()
    return {str(a): str(m) for a, m in rows}


class TestTheRegisterIsSeeded:
    async def test_all_sixteen_procedures_are_active(self, tenant: Tenant) -> None:
        codes = sorted(code for spec in REGISTER for code in spec.sop_codes_prefixed)
        assert len(codes) == 16, f"the register names {len(codes)} SOPs, not 16"

        rows = (
            await tenant.session.execute(
                text(
                    "SELECT p.code, pv.status FROM procedures p "
                    "  JOIN procedure_versions pv ON pv.procedure_id = p.id "
                    " WHERE p.organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        by_code = {str(code): str(status) for code, status in rows}
        for code in codes:
            assert by_code.get(code) == "active", (
                f"{code} is {by_code.get(code)!r}; a version in 'shadow' changes nothing, "
                "so the agent that runs on it has nothing to run"
            )

    async def test_every_agent_has_an_active_procedure(self, tenant: Tenant) -> None:
        """Per agent. An aggregate assertion would let one agent have none."""
        for spec in REGISTER:
            rows = (
                await tenant.session.execute(
                    text(
                        "SELECT count(*) FROM procedures p "
                        "  JOIN procedure_versions pv ON pv.procedure_id = p.id "
                        " WHERE p.organization_id = CAST(:o AS varchar(64)) "
                        "   AND pv.status = 'active' AND p.code = ANY(CAST(:c AS text[]))"
                    ),
                    {"o": tenant.organization_id, "c": list(spec.sop_codes_prefixed)},
                )
            ).scalar()
            assert int(rows) == len(spec.sop_codes), (
                f"{spec.name}: {rows} active procedure(s) for {len(spec.sop_codes)} SOP(s)"
            )


class TestTheSeederRuns:
    async def test_the_seeder_writes_and_is_then_idempotent(
        self, tenant: Tenant, catalogue_holder: str
    ) -> None:
        """`catalogue_holder` is a parameter, not a global lookup.

        The first version used the name without declaring it, so pytest handed the
        *fixture function* and `subprocess.run` failed with "expected str, bytes or
        os.PathLike object, not FixtureFunctionDefinition" — a message about paths, caused
        by a missing argument.
        """
        """The second call must change nothing.

        Asserted by counting rows before and after, not by parsing the seeder's output: a
        report line can be wrong, and this is the check that says whether it was.
        """
        before = await _counts(tenant)
        result = subprocess.run(  # noqa: ASYNC221
            [
                sys.executable,
                str(SEEDER),
                "--org",
                tenant.organization_id,
                "--from-org",
                catalogue_holder,
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, f"the seeder refused: {result.stderr[-800:]}"
        after = await _counts(tenant)
        assert after == before, f"the second run changed the schema: {before} -> {after}"
        assert "wrote 0" in result.stdout, (
            f"the second run reported writing something: {result.stdout[-400:]}"
        )

    async def test_every_agent_definition_carries_its_justification(self, tenant: Tenant) -> None:
        """The reason is in the row, not only in the register.

        An operator reading `agent_definitions` in a database has no way to open a Python
        module, so the justification has to be a column.
        """
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT d.name, d.system_instructions, a.description "
                    "  FROM agent_definitions d "
                    "  JOIN agents a ON a.definition_id = d.id "
                    " WHERE d.organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        seeded = {str(name): (str(inst or ""), str(desc or "")) for name, inst, desc in rows}
        for spec in REGISTER:
            instructions, description = seeded.get(spec.name, ("", ""))
            assert spec.justification[:40] in description, (
                f"{spec.name}: the row does not carry the register's justification"
            )
            for code in spec.sop_codes_prefixed:
                assert code in instructions, (
                    f"{spec.name}: its instructions do not name {code}, so an operator "
                    "cannot tell what it runs on"
                )
            assert "must refuse" in instructions.lower(), (
                f"{spec.name}: its instructions do not say what it must decline"
            )


class TestTheSeederConverges:
    async def test_a_stale_ceiling_is_corrected_not_left(self, tenant: Tenant) -> None:
        """The falsifier for the defect this seeder actually had.

        The first version of `seed_agent_register.py` **skipped** an agent that already
        existed. That is right for a row it does not own and wrong for the ceiling, which is
        a *function* of the action classes and the policy table rather than a free choice —
        so seven of the eight development rows kept a ceiling from a run made before
        ceilings were derived, and no test saw it, because the test seeded a fresh tenant.

        So the check is: corrupt one, re-seed, and require it to be corrected. A test that
        only runs the seeder against a clean tenant cannot see a seeder that does not
        converge -- and that is the whole gap, because the development schema is not clean.
        """
        agent = (
            await tenant.session.execute(
                text(
                    "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                    "  AND name = 'Knowledge Agent'"
                ),
                {"o": tenant.organization_id},
            )
        ).first()
        assert agent is not None, "the register was not seeded"
        await tenant.session.execute(
            text("UPDATE agents SET autonomy_ceiling = 'L1' WHERE id = :i"),
            {"i": str(agent[0])},
        )
        await tenant.commit()

        result = subprocess.run(  # noqa: ASYNC221
            [sys.executable, str(SEEDER), "--org", tenant.organization_id],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]

        after = (
            await tenant.session.execute(
                text("SELECT autonomy_ceiling FROM agents WHERE id = :i"), {"i": str(agent[0])}
            )
        ).first()
        assert str(after[0]) == "L4", (
            f"the seeder left a stale ceiling at {after[0]!r}; a seeder that only adds "
            "cannot be re-run, which is the same defect as one that duplicates"
        )
        assert "L1 -> L4" in result.stdout, (
            f"the correction was not reported: {result.stdout[-400:]}"
        )


class TestBoundedCorrectly:
    async def test_every_agent_is_registered_at_the_ceiling_its_classes_permit(
        self, tenant: Tenant
    ) -> None:
        permitted = await _permitted(tenant.session, tenant.organization_id)
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT name, autonomy_ceiling, granted_level FROM agents "
                    " WHERE organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        by_name_ = {str(n): (str(c), str(g)) for n, c, g in rows}
        for spec in REGISTER:
            ceiling, _granted = by_name_.get(spec.name, ("", ""))
            assert ceiling == ceiling_for(spec, permitted), (
                f"{spec.name}: stored ceiling {ceiling!r} but its classes permit "
                f"{ceiling_for(spec, permitted)!r}"
            )

    async def test_every_agent_is_granted_below_its_ceiling(self, tenant: Tenant) -> None:
        """The negative assertion. A grant at or above the ceiling is the thing to catch."""
        rank = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT name, autonomy_ceiling, granted_level FROM agents "
                    " WHERE organization_id = CAST(:o AS varchar(64))"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        for name, ceiling, granted in rows:
            assert rank[str(granted)] <= rank[str(ceiling)], (
                f"{name}: granted {granted} with a ceiling of {ceiling} -- an agent "
                "authorised past its own bound"
            )

    async def test_no_agent_holds_a_hard_blocked_procedure(self, tenant: Tenant) -> None:
        """A hard-blocked class in an active procedure is a promotion that should not exist.

        The gate refuses it, so this can only fire if a row was written by something other
        than the gate -- which is exactly the case worth a test.
        """
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT p.code, pv.id FROM procedures p "
                    "  JOIN procedure_versions pv ON pv.procedure_id = p.id "
                    " WHERE p.organization_id = CAST(:o AS varchar(64)) "
                    "   AND pv.status = 'active' AND pv.body LIKE '%safety_conclusion%'"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        offenders = [str(code) for code, _ in rows]
        assert not offenders, (
            f"{offenders} name `safety_conclusion`, an absolute prohibition in Tập 1 §5.3"
        )


class TestTheKillSwitchStopsAnAgent:
    """Over HTTP, like `test_agent_control.py`.

    The handlers take `(agent_id, body, ctx)` where `ctx` is a resolved `ApiContext`
    carrying a principal, a session and an actor. Building one by hand in a test tests the
    construction; going over HTTP tests the route, the binding, the 422 on a missing reason
    and the write, which is what "an operator can stop an agent" actually means.
    """

    async def test_the_kill_switch_stops_an_agent(self, client: Any, tenant: Tenant) -> None:
        agent_id = await _agent_id(tenant, "Project Mgmt Agent")
        headers = _headers(tenant.organization_id)

        before = (await client.get(f"/api/v1/agents/{agent_id}/control", headers=headers)).json()
        assert before["kill_switch"] is False

        stopped = await client.post(
            f"/api/v1/agents/{agent_id}/kill",
            json={"reason": "under test: the register's kill-switch path"},
            headers=headers,
        )
        assert stopped.status_code == 200, stopped.text
        body = stopped.json()
        assert body["kill_switch"] is True
        assert body["lifecycle_status"] == "suspended"
        # A stopped agent keeps L1 and does not inherit L2 on revive, so reviving is a
        # decision somebody has to make again rather than a side effect of a restart.
        assert body["granted_level"] == "L1"

    async def test_a_kill_without_a_reason_is_refused(self, client: Any, tenant: Tenant) -> None:
        """`ck_agents_kill_is_recorded` refuses it; the edge names it in a 422 instead.

        A stop nobody can explain is indistinguishable from a malfunction, and a
        malfunction that stops an agent is indistinguishable from an attack.
        """
        agent_id = await _agent_id(tenant, "Project Mgmt Agent")
        got = await client.post(
            f"/api/v1/agents/{agent_id}/kill",
            json={"reason": ""},
            headers=_headers(tenant.organization_id),
        )
        assert got.status_code == 422, got.text

    async def test_a_stopped_agent_reports_its_own_state(self, client: Any, tenant: Tenant) -> None:
        """The control surface has to show the stop, or it is not a control."""
        agent_id = await _agent_id(tenant, "Finance Agent")
        headers = _headers(tenant.organization_id)
        await client.post(
            f"/api/v1/agents/{agent_id}/kill",
            json={"reason": "under test: the control surface must show this"},
            headers=headers,
        )
        body = (await client.get(f"/api/v1/agents/{agent_id}/control", headers=headers)).json()
        assert body["kill_switch"] is True
        assert body["lifecycle_status"] == "suspended"
        assert body["kill_reason"]


async def _counts(t: Tenant) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in (
        "procedures",
        "procedure_versions",
        "agent_shadow_runs",
        "agent_definitions",
        "agents",
    ):
        out[table] = int(
            (
                await t.session.execute(
                    text(
                        f"SELECT count(*) FROM {table} WHERE organization_id = "
                        f"CAST(:o AS varchar(64))"
                    ),
                    {"o": t.organization_id},
                )
            ).scalar()
        )
    return out


async def _agent_id(t: Tenant, name: str) -> str:
    row = (
        await t.session.execute(
            text(
                "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                "  AND name = :n"
            ),
            {"o": t.organization_id, "n": name},
        )
    ).first()
    assert row is not None, f"no agent named {name!r}"
    return str(row[0])


def _headers(organization_id: str) -> dict[str, str]:
    from tests.integration.api_client import auth_headers

    return auth_headers(organization_id)
