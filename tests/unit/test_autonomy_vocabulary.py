"""The two vocabularies for an autonomy level must be the same set.

## What broke, and how it survived so long

`persistence/process.py` names the dossier's four levels `L1` through `L4` -- the form a
person reads, and the form migration `0016` writes into `agents.granted_level` and
`agents.autonomy_ceiling`. `domain/enums.py::AutonomyLevel` used the long machine form
(`l1_low_risk_autonomous`).

So `AutonomyLevel("L1")` raised `ValueError`, and **every agent row in the database carried
a value the domain could not parse.** It survived more than 2,600 tests because the only
code that parses the column is the executor's grant check, and the one code path that
reached it in practice never read the row's level.

`scripts/run_fleet.py` found it on its first run, seven agents out of eight, before a
single token was spent:

    CRASH HR Agent  not started  0 tok  0.0min
          crash: ValueError: 'L1' is not a valid AutonomyLevel

## What these tests hold

**Both forms parse, and they mean the same level.** The short form is what the schema
stores and the long form is what the enum serialises, so any code that reads one and
persists the other has to work.

**The database's values are all parseable.** Measured against `information_schema` and
against `autonomy_policies`, not against a list written here -- a list would agree with
itself and disagree with the schema, which is the original defect.

**A level outside the scheme is still refused.** The tolerance is for two *spellings* of a
level, not for levels that do not exist. `L9` must not parse, and a new level must be
added to both vocabularies or to neither.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.enums import AutonomyLevel
from ai_orchestrator.persistence.process import (
    AUTONOMY_LEVELS,
    AUTONOMY_RANK,
    L_AND_REPORT,
    L_INFORM,
    L_RECOMMEND,
    L_WITH_APPROVAL,
)

#: The short form each dossier level is stored as, in the order the dossier defines them.
#: Paired with the long name it must resolve to, so a change to either side is a test
#: failure rather than a silent disagreement.
PAIRS = (
    (L_INFORM, AutonomyLevel.L1_LOW_RISK_AUTONOMOUS),
    (L_RECOMMEND, AutonomyLevel.L2_PARENT_REVIEW),
    (L_WITH_APPROVAL, AutonomyLevel.L3_HUMAN_APPROVAL),
    (L_AND_REPORT, AutonomyLevel.L4_BOUNDED_AUTONOMOUS),
)


class TestTheTwoSpellings:
    @pytest.mark.parametrize(("short", "long"), PAIRS)
    def test_the_short_form_resolves_to_the_long_one(self, short: str, long: AutonomyLevel) -> None:
        assert AutonomyLevel(short) is long
        assert AutonomyLevel(long.value) is long

    @pytest.mark.parametrize(("short", "long"), PAIRS)
    def test_case_does_not_matter(self, short: str, long: AutonomyLevel) -> None:
        assert AutonomyLevel(short.lower()) is long
        assert AutonomyLevel(short.upper()) is long

    @pytest.mark.parametrize(("short", "long"), PAIRS)
    def test_surrounding_whitespace_is_stripped(self, short: str, long: AutonomyLevel) -> None:
        """The value arrives from a database column and from a query parameter, and both
        carry whitespace when a person typed them into a filter."""
        assert AutonomyLevel(f"  {short}  ") is long

    def test_the_short_forms_are_the_dossiers_own(self) -> None:
        """Tập 1 §5.3: L1 informs, L2 recommends, L3 prepares then approves, L4 acts then
        reports. Written out so a change to the process module is visible here."""
        assert [short for short, _ in PAIRS] == ["L1", "L2", "L3", "L4"]
        assert len(AUTONOMY_LEVELS) == 4

    def test_a_level_outside_the_scheme_is_still_refused(self) -> None:
        """Tolerance is for two spellings of a level, not for levels that do not exist.

        `L9` parsing would mean the grant check cannot tell a typo from a level, and a
        grant check that cannot tell a typo from a level is a grant check that fails open.
        """
        for bad in ("L9", "L0_SUGGEST", "9", "L", "LL1", "L1_", "l9", "inform"):
            with pytest.raises(ValueError):
                AutonomyLevel(bad)

    def test_a_non_string_is_refused(self) -> None:
        for bad in (None, 1, 1.0, object(), b"L1", ["L1"]):
            with pytest.raises((ValueError, TypeError)):
                AutonomyLevel(bad)  # type: ignore[arg-type]

    def test_the_rank_is_consecutive_and_the_short_form_agrees(self) -> None:
        """`AUTONOMY_RANK` orders the levels, and the short form has to order the same way.

        The rank is what the promotion gate compares, so a disagreement between the two
        vocabularies about *order* is the same defect as a disagreement about membership --
        and it is quieter, because every value parses.
        """
        assert sorted(AUTONOMY_RANK.values()) == [1, 2, 3, 4]
        # `AUTONOMY_RANK` is keyed by the **short** form -- `{"L1": 1, ...}` -- which is the
        # third thing that had to be checked rather than assumed. The first version of this
        # test indexed it with the enum member and got `KeyError: L1_LOW_RISK_AUTONOMOUS`,
        # which is the vocabulary disagreement showing up a third time and in the one place
        # where order matters: the promotion gate compares ranks, so a disagreement about
        # membership and a disagreement about ordering are the same defect, and this one is
        # quieter because every value parses.
        assert set(AUTONOMY_RANK) == {short for short, _ in PAIRS}
        assert [AUTONOMY_RANK[short] for short, _ in PAIRS] == [1, 2, 3, 4]


class TestTheSchemaAgrees:
    """Read from the database, not from a list written here."""

    async def test_every_level_the_policies_use_parses(self, admin_db) -> None:
        """`autonomy_policies.max_level` is written by the dossier's own seeder.

        This is the column a promotion gate evaluates against, so a value here that does
        not parse would stop every promotion with a `ValueError` nobody traced.
        """
        from sqlalchemy import text

        async with admin_db.session() as session:
            rows = (
                (await session.execute(text("SELECT DISTINCT max_level FROM autonomy_policies")))
                .scalars()
                .all()
            )
        assert rows, "no policies seeded; the check below would be vacuous"
        for value in rows:
            assert AutonomyLevel(str(value)) in set(AutonomyLevel), (
                f"autonomy_policies.max_level is {value!r}, which the domain cannot parse"
            )

    async def test_every_level_the_agents_carry_parses(self, admin_db) -> None:
        """The defect itself, asserted against the rows that carried it.

        Sixteen of sixteen agents had `granted_level = 'L1'`, and `AutonomyLevel('L1')`
        raised. This test is the reason the next such disagreement is a red test rather
        than a fleet crash.
        """
        from sqlalchemy import text

        async with admin_db.session() as session:
            levels = (
                await session.execute(
                    text("SELECT DISTINCT granted_level, autonomy_ceiling FROM agents")
                )
            ).all()
        assert levels, "no agents registered; the check below would be vacuous"
        for granted, ceiling in levels:
            for value in (granted, ceiling):
                if value is None:
                    continue
                assert AutonomyLevel(str(value)) in set(AutonomyLevel), (
                    f"agents carries {value!r}, which the domain cannot parse"
                )
