"""Who may read what, across the three tiers.

These are boundary tests, so they are written as the *tree* rather than as pairs: a
policy that passes every hand-picked pair and gets a third branch wrong is a policy
that will get the fourth branch wrong too.

The tree is the real one — company, Front/Middle/Back, and seven departments:

    /                      company          depth 0   Executive Agent
    /front/                front-office     depth 1   Front Office Agent
    /front/procurement/    procurement      depth 2   Procurement Agent
    /front/sales/          sales            depth 2   Sales Agent
    /middle/               middle-office    depth 1   Middle Office Agent
    /middle/design/        design           depth 2   Design Agent
    /middle/qa/            qa               depth 2   QA/QC-HSE Agent
    /back/                 back-office      depth 1   Back Office Agent
    /back/finance/         finance          depth 2   Finance Agent
    /back/hr/              hr               depth 2   HR Agent
    /back/it/              it               depth 2   IT Agent
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.access import (
    UnitRef,
    Visibility,
    is_ancestor_or_self,
    may_delegate_to,
    may_read_content,
    may_read_status,
    scope_of,
    visibility_of,
)
from ai_orchestrator.domain.errors import ValidationError

COMPANY = UnitRef(id="c", slug="root", depth=0, path="/root")
FRONT = UnitRef(id="f", slug="front-office", depth=1, path="/root/front-office")
BACK = UnitRef(id="b", slug="back-office", depth=1, path="/root/back-office")
PROCUREMENT = UnitRef(id="fp", slug="procurement", depth=2, path="/root/front-office/procurement")
SALES = UnitRef(id="fs", slug="sales", depth=2, path="/root/front-office/sales")
FINANCE = UnitRef(id="bf", slug="finance", depth=2, path="/root/back-office/finance")
HR = UnitRef(id="bh", slug="hr", depth=2, path="/root/back-office/hr")
IT = UnitRef(id="bi", slug="it", depth=2, path="/root/back-office/it")

EVERYONE = [COMPANY, FRONT, BACK, PROCUREMENT, SALES, FINANCE, HR, IT]


def _by_slug(slug: str) -> UnitRef:
    return next(u for u in EVERYONE if u.slug == slug)


class TestReadingYourOwnWork:
    @pytest.mark.parametrize("slug", [u.slug for u in EVERYONE])
    def test_every_unit_reads_itself_in_full(self, slug: str) -> None:
        unit = _by_slug(slug)
        assert may_read_content(unit, unit)
        assert may_read_status(unit, unit)
        assert visibility_of(unit, unit) is Visibility.CONTENT


class TestReadingDownward:
    """Higher tier reads lower tier. This is the whole of management."""

    def test_the_chief_reads_every_office_and_department(self) -> None:
        for unit in EVERYONE:
            assert may_read_content(COMPANY, unit), f"the chief cannot read {unit.slug}"

    def test_an_office_reads_its_own_departments(self) -> None:
        assert may_read_content(FRONT, PROCUREMENT)
        assert may_read_content(FRONT, SALES)

    def test_an_office_does_not_read_another_offices_departments(self) -> None:
        assert not may_read_content(FRONT, FINANCE)
        assert not may_read_content(FRONT, HR)
        assert not may_read_content(FRONT, IT)


class TestReadingUpward:
    """A department must be able to see who it reports to.

    Without this the escalation path is decorative: a department that cannot see its
    office does not know whether an escalation arrived anywhere.
    """

    def test_a_department_reads_its_own_office_and_the_chief(self) -> None:
        assert may_read_content(PROCUREMENT, FRONT)
        assert may_read_content(PROCUREMENT, COMPANY)

    def test_a_department_does_not_read_a_sibling_office(self) -> None:
        assert not may_read_content(PROCUREMENT, BACK)


class TestReadingSideways:
    """Peers get enough to route to and not enough to audit."""

    def test_siblings_in_the_same_office_are_status_only(self) -> None:
        assert may_read_status(PROCUREMENT, SALES)
        assert not may_read_content(PROCUREMENT, SALES)
        assert visibility_of(PROCUREMENT, SALES) is Visibility.STATUS

    def test_departments_in_different_offices_are_status_only(self) -> None:
        assert may_read_status(PROCUREMENT, FINANCE)
        assert not may_read_content(PROCUREMENT, FINANCE)

    def test_it_is_symmetric(self) -> None:
        """Asymmetric peer access is a bug, not a policy: it would make the answer
        depend on which department happened to ask."""
        for a, b in [(PROCUREMENT, FINANCE), (FINANCE, PROCUREMENT), (HR, IT), (IT, HR)]:
            assert visibility_of(a, b) == visibility_of(b, a), f"{a.slug}/{b.slug}"

    def test_an_office_sees_its_peer_office_by_status_only(self) -> None:
        assert may_read_status(FRONT, BACK)
        assert not may_read_content(FRONT, BACK)


class TestWhatIsNeverReadable:
    def test_nothing_is_ever_invisible(self) -> None:
        """Every unit in the organisation can see that every other unit exists.

        A `NONE` here would be a roster with a hole in it, and an agent that cannot see
        a vacancy cannot tell a colleague from nobody.
        """
        for reader in EVERYONE:
            for target in EVERYONE:
                assert may_read_status(reader, target), (
                    f"{reader.slug} cannot even see that {target.slug} exists"
                )

    def test_no_department_reads_another_departments_work(self) -> None:
        departments = [PROCUREMENT, SALES, FINANCE, HR, IT]
        for reader in departments:
            for target in departments:
                if reader.id == target.id:
                    continue
                assert not may_read_content(reader, target), (
                    f"{reader.slug} can read {target.slug}'s work, so the review that "
                    "judges one result is judging something it was handed"
                )


class TestDelegation:
    def test_the_chief_may_delegate_to_everything(self) -> None:
        for unit in EVERYONE:
            if unit.id == COMPANY.id:
                continue
            assert may_delegate_to(COMPANY, unit), f"the chief cannot reach {unit.slug}"

    def test_an_office_may_delegate_to_its_departments_only(self) -> None:
        assert may_delegate_to(BACK, FINANCE)
        assert may_delegate_to(BACK, HR)
        assert may_delegate_to(BACK, IT)
        assert not may_delegate_to(BACK, PROCUREMENT), "cross-office delegation"
        assert not may_delegate_to(BACK, FRONT), "upward delegation"

    def test_a_department_may_not_delegate_at_all(self) -> None:
        """There is nothing below a department in this organisation.

        If a fourth tier is added later this assertion is the thing to revisit, and it
        is written here so that adding one is a deliberate act rather than a silent
        consequence.
        """
        for target in EVERYONE:
            assert not may_delegate_to(HR, target), f"HR may delegate to {target.slug}"

    def test_nobody_delegates_to_themselves(self) -> None:
        for unit in EVERYONE:
            assert not may_delegate_to(unit, unit)

    def test_upward_delegation_is_refused(self) -> None:
        assert not may_delegate_to(PROCUREMENT, FRONT)
        assert not may_delegate_to(PROCUREMENT, COMPANY)
        assert not may_delegate_to(HR, BACK)


class TestThePathTest:
    def test_a_sibling_prefix_is_not_an_ancestor(self) -> None:
        """The reason `path` is materialised and not rebuilt from `parent_id`.

        The seed writes the root's path as `/root` with **no trailing slash** and every
        other unit's with one, so `"/root-x".startswith("/root")` is true and a bare
        prefix test makes a sibling of the root its child -- handing it the chief's
        content. Measured against the live seed, which is where this was found.
        """
        sibling = UnitRef(id="c2", slug="root-x", depth=0, path="/root-x")
        assert "/root-x".startswith("/root"), "the prefix that fools a naive test"
        assert not is_ancestor_or_self(COMPANY, sibling), (
            "a unit whose path merely starts with the root's is treated as its child"
        )

    def test_a_department_really_is_under_its_office(self) -> None:
        """The other half, because the fix above must not have broken the true case.

        Written because the first version of the previous test asserted the opposite
        and was wrong: `/b/` *is* a prefix of `/b/bh/`, because `hr` really is inside
        `back-office`. A boundary test that also rejects the true ancestor is a test
        that will be "fixed" by deleting the boundary.
        """
        assert is_ancestor_or_self(BACK, HR)
        assert is_ancestor_or_self(BACK, FINANCE)
        assert is_ancestor_or_self(FRONT, PROCUREMENT)

    def test_a_unit_is_its_own_ancestor(self) -> None:
        assert is_ancestor_or_self(HR, HR)

    def test_nothing_is_above_the_root(self) -> None:
        """Excluding the root itself, which is its own ancestor by definition."""
        for unit in EVERYONE:
            if unit.id == COMPANY.id:
                continue
            assert not is_ancestor_or_self(unit, COMPANY), (
                f"{unit.slug} is treated as above the company"
            )


class TestScope:
    def test_a_departments_scope_is_its_own_branch_plus_the_chain_up(self) -> None:
        got = scope_of(HR, EVERYONE)
        assert got["hr"] is Visibility.CONTENT
        assert got["back-office"] is Visibility.CONTENT, "its own office"
        assert got["root"] is Visibility.CONTENT, "the chief"
        assert got["finance"] is Visibility.STATUS, "a colleague in its own office"
        assert got["it"] is Visibility.STATUS
        assert got["front-office"] is Visibility.STATUS
        assert got["procurement"] is Visibility.STATUS

    def test_the_chiefs_scope_is_everything_in_full(self) -> None:
        got = scope_of(COMPANY, EVERYONE)
        assert set(got.values()) == {Visibility.CONTENT}
        assert len(got) == len(EVERYONE)

    def test_an_offices_scope_excludes_other_offices_content(self) -> None:
        got = scope_of(BACK, EVERYONE)
        assert got["finance"] is Visibility.CONTENT
        assert got["hr"] is Visibility.CONTENT
        assert got["front-office"] is Visibility.STATUS
        assert got["sales"] is Visibility.STATUS

    def test_every_scope_has_an_entry_for_every_unit(self) -> None:
        """A scope with a missing key reads as "nobody", which is the safest wrong
        answer to notice and the easiest to produce by accident."""
        for reader in EVERYONE:
            assert set(scope_of(reader, EVERYONE)) == {u.slug for u in EVERYONE}


class TestConstruction:
    def test_a_unit_cannot_sit_above_the_root(self) -> None:
        with pytest.raises(ValidationError):
            UnitRef(id="x", slug="x", depth=-1, path="/x/")

    def test_a_path_must_start_at_the_root(self) -> None:
        with pytest.raises(ValidationError):
            UnitRef(id="x", slug="x", depth=1, path="x/")

    def test_the_root_is_recognised_as_the_root(self) -> None:
        assert COMPANY.is_root
        assert not FRONT.is_root
