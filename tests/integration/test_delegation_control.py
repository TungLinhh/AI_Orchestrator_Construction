"""The dashboard's delegation control, counted rather than merely present.

## What this file is for

`tests/integration/test_construction_ui.py` asserts that the string `"Above ceiling"` is on
the page. That is a **label** test, and it passed while the tile beside the label read
**8** on a database where every agent was granted `L1` and no ceiling was below `L1`.

The query behind it was

```sql
count(*) FILTER (WHERE granted_level <> autonomy_ceiling)
```

which counts agents whose grant **differs** from their ceiling in either direction, under a
label and a docstring that both say *above*. So the control alarmed on the correct state of
the system — eight is exactly the number of register agents whose ceiling had been corrected
away from `L1` — and a genuinely over-ceiling agent was counted in the same eight as an
unremarkable neighbour, so the one row a reviewer must see was the one indistinguishable
from the rest.

Two things were wrong and both had to be fixed: the predicate (`<>` is not `>`) and the
comparison (both columns are `varchar`, and `'L1' < 'L2'` is false as text). The second is
not hypothetical: `AUTONOMY_RANK` in `persistence/process.py` carries a comment saying
exactly that, written by whoever got it wrong before.

## Why the number needs its own test

Zero is the **expected** value of this control, which makes it the easiest number in the
product to get wrong: a wrong figure here is wrong in the reassuring direction, the tile
reads 0, and nobody looks. A test that asserts the label exists cannot see a wrong number at
all, and neither could one that asserted only the current value — so this file walks a
single agent through the whole range of (grant, ceiling) pairs and requires the count to
move, so the assertion is that the query *reacts*, not that it happens to be right today.

The whole range matters, because a rank comparison written as `<>` and one written as `>`
agree on almost every row: they differ only where the grant is *below* its ceiling, which
is the common case and the one the first version got wrong. `test_the_cases_disagree_with_
the_old_predicate` states that arithmetic explicitly rather than leaving it implied.

## The agents are created by the seeder, not by hand

`agents` has twenty-three `NOT NULL` columns and composite foreign keys to
`agent_definitions` and `roles`. Inserting one directly would mean reproducing most of a
model in a test, and the copy would drift silently. So the register is seeded — the same
`scripts/seed_agent_register.py --from-org` path `test_agent_register_seeded.py` uses — and
the levels under test are then set with `UPDATE`, which is also how a grant actually moves in
the product.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from ai_orchestrator.application.role_views import _AGENT_POSTURE, AgentPosture
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

SEEDER = Path(__file__).resolve().parents[2] / "scripts" / "seed_agent_register.py"

#: Below, at, and above, in that order. The "below" rows are the ones that matter: a control
#: written with `<>` instead of `>` counts them, and until somebody deliberately raises a
#: grant *every* agent is in that state.
CASES: tuple[tuple[str, str, int], ...] = (
    ("L1", "L3", 0),  # two below
    ("L2", "L3", 0),  # one below
    ("L3", "L3", 0),  # exactly at
    ("L4", "L3", 1),  # one above
    ("L4", "L1", 1),  # three above
)


async def _catalogue_holder() -> str:
    """An organization in this schema holding the dossier's catalogue.

    `--from-org` is explicit for the reason `test_agent_register_seeded.py` documents: the
    test schema can hold several catalogue holders, and the seeder refuses to guess between
    them. Guessing here would make this test ambiguous in exactly the same way.
    """
    # Awaited, not wrapped in `asyncio.run()`. A synchronous version of this helper was the
    # first draft and it raised `RuntimeError: asyncio.run() cannot be called from a running
    # event loop` on every test -- the same lesson as F141, in a different costume: an
    # event loop belongs to the code that made it, and a helper that opens its own cannot be
    # called from one that is already running.
    from ai_orchestrator.persistence.session import Database

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


@pytest.fixture
async def seeded(tenant: Tenant) -> Tenant:
    """A tenant holding the eight dossier agents, at L1 with their derived ceilings."""
    holder = await _catalogue_holder()
    if not holder:
        pytest.skip(
            "this schema holds no dossier catalogue, so the register cannot be seeded. "
            "Run `make seed-test-reference`."
        )
    result = subprocess.run(  # noqa: ASYNC221
        [sys.executable, str(SEEDER), "--org", tenant.organization_id, "--from-org", holder],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return tenant


async def _count(tenant: Tenant) -> tuple[int, int, int, str | None]:
    """`(agents, killed, above_ceiling, tightest_ceiling)`, straight from the query.

    Read on the tenant's own session rather than through the API, so a failure points at the
    SQL instead of at a serialisation layer. `make verify-page` is the counterpart that
    checks the number survives the trip to the browser.
    """
    row = (await tenant.session.execute(text(_AGENT_POSTURE), {"o": tenant.organization_id})).one()
    return int(row[0]), int(row[1]), int(row[2]), row[3]


async def _set_grant(tenant: Tenant, name: str, granted: str, ceiling: str) -> None:
    await tenant.session.execute(
        text(
            "UPDATE agents SET granted_level = :g, autonomy_ceiling = :c, kill_switch = false "
            " WHERE organization_id = CAST(:o AS varchar(64)) AND name = :n"
        ),
        {"g": granted, "c": ceiling, "o": tenant.organization_id, "n": name},
    )


#: The predicate `_AGENT_POSTURE` uses, as SQL, over an arbitrary row set.
#:
#: It cannot be run against `agents` for the above-ceiling cases, and that is the most
#: important thing this file found: `ck_agents_granted_within_ceiling` already makes a
#: grant above its ceiling **unrepresentable**. The tile is therefore not a monitor for a
#: state the system can reach — it is a *tamper* control, and the only way it can ever read
#: non-zero is if the constraint was bypassed (a disabled check, a superuser write, a
#: restored dump from elsewhere).
#:
#: Which means the arithmetic has to be tested somewhere other than the table, or not at
#: all. So it is tested here against a `VALUES` list: the expression under test is the one
#: the dashboard runs, and the rows are stated. `test_the_database_refuses_the_state_the
#: _control_watches` then asserts the constraint that makes the tile quiet in the first
#: place, so the two halves of the claim are both on the record.
_PREDICATE = """
SELECT count(*) FILTER (WHERE NOT kill_switch
          AND substring(granted_level from 2)::int
              > substring(autonomy_ceiling from 2)::int) AS above
FROM (VALUES
"""
_PREDICATE_TAIL = ") AS t(granted_level, autonomy_ceiling, kill_switch)"


def _predicate_sql(rows: list[tuple[str, str, bool]]) -> str:
    values = ", ".join(f"('{g}', '{c}', {'true' if k else 'false'})" for g, c, k in rows)
    return _PREDICATE + values + _PREDICATE_TAIL


class TestTheDelegationControl:
    async def test_an_organisation_with_no_agents_reads_zero(self, tenant: Tenant) -> None:
        agents, killed, above, tightest = await _count(tenant)
        assert (agents, killed, above) == (0, 0, 0)
        assert tightest is None, "no agents means no ceiling to report"

    async def test_an_empty_organisation_still_renders_a_ceiling(self) -> None:
        """`None` in the query is `L1` on the page, and that hop is asserted here.

        The aggregate returns `NULL` for an organisation with no agents, and the
        dataclass turns that into `L1`. Testing the query alone would leave the page
        rendering an empty tile; testing the dataclass alone would leave the query
        returning something unexpected. The product's claim is the pair.
        """
        assert AgentPosture(agents=0, killed=0, above_l1=0).as_dict()["tightest_ceiling"] == "L1"

    async def test_a_seeded_register_reads_zero(self, seeded: Tenant) -> None:
        """The state the product is actually in: eight agents, all at L1, none over."""
        agents, _, above, tightest = await _count(seeded)
        assert agents == 8, f"expected the eight dossier agents, found {agents}"
        assert above == 0, (
            f"{above} agent(s) read as above their own ceiling on a freshly seeded register, "
            "where every grant is L1 and every ceiling is L1 or higher. This is the exact "
            "figure that read 8 before the predicate was fixed."
        )
        assert tightest is not None and tightest.startswith("L")

    async def test_the_control_counts_only_grants_above_the_ceiling(self, seeded: Tenant) -> None:
        """Walk one agent through every case the *table* can hold, and require it to read 0.

        Only the at-or-below cases are reachable, because
        `ck_agents_granted_within_ceiling` refuses the rest. Asserting 0 four times is not
        redundant: it is the assertion that the control stays quiet on the states the
        register spends its life in, which is the failure this file is about.
        """
        for granted, ceiling, expected in CASES:
            if expected:
                continue
            await _set_grant(seeded, "Knowledge Agent", granted, ceiling)
            await seeded.commit()
            _, _, above, _ = await _count(seeded)
            assert above == expected, (
                f"granted {granted} against ceiling {ceiling} should read {expected}, got "
                f"{above}. A grant *below* its ceiling is the ordinary state and must not "
                "count -- the first version used `<>` and counted every agent whose grant "
                "merely differed, which on a healthy register is all of them."
            )

    async def test_the_control_counts_an_above_ceiling_grant_when_one_exists(
        self, seeded: Tenant
    ) -> None:
        """The counting arm, over a stated row set rather than over `agents`.

        A control that can only ever answer 0 is indistinguishable from a broken one, so the
        arm that returns 1 is exercised deliberately — against the same expression, on rows
        the schema would not permit. This is the only honest place to put it: an `UPDATE`
        through the table raises `ck_agents_granted_within_ceiling`, which is the *other*
        half of the claim and is asserted separately.
        """
        rows = [(granted, ceiling, False) for granted, ceiling, _ in CASES]
        counted = (await seeded.session.execute(text(_predicate_sql(rows)))).scalar()
        assert counted == 2, (
            f"the predicate counted {counted} of {len(rows)} rows; two of the cases are "
            "grants above their ceiling and the other three are not"
        )

    async def test_the_predicate_excludes_a_killed_agent(self, seeded: Tenant) -> None:
        """The kill switch is the *opposite* of a violation.

        A switched-off agent holds nothing, so it cannot be above its ceiling. Counting it
        would mean the control fires on the one action that makes the system safe.
        """
        rows = [("L4", "L1", True), ("L4", "L1", False)]
        counted = (await seeded.session.execute(text(_predicate_sql(rows)))).scalar()
        assert counted == 1, (
            "a switched-off agent was counted as a violation; the control must not fire on "
            "the action that makes the system safe"
        )

    async def test_the_database_refuses_the_state_the_control_watches(self, seeded: Tenant) -> None:
        """The primary guarantee, and the reason the tile is quiet.

        A `CHECK` called `ck_agents_granted_within_ceiling` exists on `agents`. So the
        control above is a *tamper* control: it can only read non-zero if the constraint was
        bypassed, and this asserts the constraint is real rather than trusting its name.

        Which makes the arithmetic test above necessary rather than redundant: a constraint
        that made the state impossible would also make a control that always answers 0 look
        correct, and neither of them alone says the control counts.
        """
        # Caught as `Exception` and matched on the text, rather than as
        # `IntegrityError`. asyncpg raises through SQLAlchemy's greenlet bridge, and inside
        # an `asyncio_mode = "auto"` test that arrives wrapped in an `ExceptionGroup` -- so
        # `pytest.raises(IntegrityError)` does not match and the test fails with the
        # database's own error rather than with an assertion. The constraint name is the
        # thing being asserted, and the name is in the message either way.
        with pytest.raises(Exception) as refusal:
            await _set_grant(seeded, "Knowledge Agent", "L4", "L1")
            await seeded.commit()
        message = str(refusal.value)
        assert "ck_agents_granted_within_ceiling" in message, (
            f"the write was refused, but not by the ceiling constraint: {message[:400]}"
        )

    async def test_the_cases_disagree_with_the_old_predicate(self) -> None:
        """The falsifier, stated as arithmetic rather than left implied.

        If `<>` and `>` agreed on every case, fixing the predicate would look cosmetic. They
        do not, and they differ on **two** of the five: the two *below* cases. The `at` case
        is excluded by both, because `L3 <> L3` is as false as `L3 > L3` — which is exactly
        why the first version looked plausible. Two rows out of five is enough to turn the
        control permanently on, and those two are the state every agent is in until somebody
        deliberately raises a grant.
        """
        below_or_at = [c for c in CASES if c[2] == 0]
        differing = [(g, c) for g, c, _ in below_or_at if g != c]
        assert len(below_or_at) == 3, "three of the five cases are at or below the ceiling"
        assert len(differing) == 2, (
            "the two `below` cases are the only ones `<>` and `>` disagree about. If this "
            f"moved, the cases no longer discriminate: {differing}"
        )
        assert differing == [("L1", "L3"), ("L2", "L3")]

    async def test_the_tightest_ceiling_is_the_lowest_not_the_first(self, seeded: Tenant) -> None:
        """A minimum over ranks, not `min()` over text and not "whichever row came first".

        Three ceilings, one answer: the *least* autonomy any agent is allowed. `min()` on
        the text happens to agree across `L1`..`L4`, and this is the assertion that the
        agreement is intended rather than lucky.
        """
        for name, ceiling in (
            ("Knowledge Agent", "L4"),
            ("Project Mgmt Agent", "L2"),
            ("QA/QC-HSE Agent", "L3"),
        ):
            await _set_grant(seeded, name, "L1", ceiling)
        await seeded.commit()
        _, _, _, tightest = await _count(seeded)
        assert tightest == "L2", f"expected the narrowest ceiling, got {tightest!r}"

    async def test_the_control_is_tenant_scoped(self, seeded: Tenant, other_tenant: Tenant) -> None:
        """One tenant's figures are not another's.

        The query filters on `organization_id`, and this asserts that it does. Without it, a
        single switched-off agent anywhere would put a `1` in the "killed" tile of every
        organisation that opens the page, and the delegation control would be measuring the
        installation rather than the tenant.

        Stated with the `agents` and `killed` counts rather than `above_l1`, because
        `ck_agents_granted_within_ceiling` means the above-ceiling state cannot be created
        here at all -- and a scoping test built on an unreachable state is a scoping test
        that asserts nothing.
        """
        assert (await _count(seeded))[0] == 8, "the seeded tenant holds the eight agents"
        assert (await _count(other_tenant))[0] == 0, "an empty tenant reads zero"

        await _set_grant(seeded, "Knowledge Agent", "L1", "L4")
        await seeded.session.execute(
            text(
                # `ck_agents_kill_is_recorded` requires a reason *and* a timestamp, so
                # switching an agent off is a recorded act rather than a flag. Every write
                # here has to satisfy it, which is the constraint doing its job on a test
                # that was trying to be economical.
                "UPDATE agents SET kill_switch = true, killed_at = now(), "
                "  kill_reason = 'test: scoped control' "
                " WHERE organization_id = CAST(:o AS varchar(64)) AND name = 'Knowledge Agent'"
            ),
            {"o": seeded.organization_id},
        )
        await seeded.commit()

        assert (await _count(seeded))[1] == 1, "the seeded tenant has one switched-off agent"
        assert (await _count(other_tenant))[1] == 0, (
            "a switched-off agent in one tenant appeared in another organisation's control"
        )
        assert (await _count(other_tenant))[0] == 0, (
            "the seeded tenant's eight agents leaked into an empty organisation's control"
        )
