"""The console is told a three-tier organisation. It must be able to say so.

**There was no test on this payload at all.** `fleet_view.fleet_tree` and
`GET /departments` — the endpoint that supplies every box, every count and every
ancestry the Departments view draws — had no Python test. The only instrument was
`scripts/verify_page.mjs`, in JavaScript, and it asserted:

    check("the chief is the root of the tree", /Chief/.test(tree) && /Executive/.test(tree));

which is a string match. A flat list of eleven boxes passes it. So the tree could be
drawn as three unrelated bands for as long as the code was green, and it was: 100
console checks passing while a department sat in no office.

Everything here fails on the current code. That is the point of writing them first.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.application.fleet_view import fleet_tree
from ai_orchestrator.persistence.models import Organization
from ai_orchestrator.seed import DEPARTMENTS, OFFICES, seed

pytestmark = pytest.mark.integration

WANTED_OFFICES = len(OFFICES)
WANTED_DEPARTMENTS = len(DEPARTMENTS) - 1  # [0] is the executive


@pytest_asyncio.fixture
async def tenant_tree(tenant):  # type: ignore[no-untyped-def]
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    await tenant.commit()
    payload = await fleet_tree(tenant.session, organization_id=str(tenant.organization_id))
    return str(tenant.organization_id), payload


class TestThePayloadIsAThreeTierTree:
    async def test_there_is_a_chief_an_office_tier_and_a_department_tier(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        _org, d = tenant_tree
        assert d["chief"], "no chief in the payload"
        assert len(d["second_tier"]) == WANTED_OFFICES
        assert len(d["offices"]) == WANTED_DEPARTMENTS

    async def test_every_department_names_the_office_it_belongs_to(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """The link the whole view is missing.

        `organizational_units.parent_id` is correct in the database -- company, then
        three offices, then seven departments -- and `_TREE` never selects it, so
        `parent_unit_slug` is `None` for every department. Measured: 7 of 7.
        """
        _org, d = tenant_tree
        office_slugs = {b.get("key") for b in d["second_tier"]}
        missing = [b["name"] for b in d["offices"] if not b.get("parent_unit_slug")]
        assert not missing, (
            f"{len(missing)} department(s) do not name an office, so the console "
            f"cannot nest them: {missing}"
        )
        assert all(b["parent_unit_slug"] in office_slugs for b in d["offices"]), (
            f"a department names an office that is not in the payload: "
            f"{sorted({b['parent_unit_slug'] for b in d['offices']} - office_slugs)}"
        )

    async def test_no_department_reports_directly_to_the_company(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        _org, d = tenant_tree
        for box in d["offices"]:
            assert box.get("parent_unit_slug") != box.get("key"), f"{box['name']} is its own parent"

    async def test_every_office_has_at_least_one_department(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """An office with nothing under it is a tier that exists only on a diagram."""
        _org, d = tenant_tree
        used = {b["parent_unit_slug"] for b in d["offices"]}
        empty = [b["name"] for b in d["second_tier"] if b.get("key") not in used]
        assert not empty, f"these offices have no department under them: {empty}"

    async def test_the_server_sends_a_nested_tree_not_just_flat_lists(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """So the page does not reassemble a hierarchy and get it wrong."""
        _org, d = tenant_tree
        assert "tree" in d, "no nested tree in the payload"
        assert len(d["tree"]["offices"]) == WANTED_OFFICES
        nested = [dept for office in d["tree"]["offices"] for dept in office["departments"]]
        assert len(nested) == WANTED_DEPARTMENTS
        assert {x["name"] for x in nested} == {x["name"] for x in d["offices"]}

    async def test_the_chief_rolls_up_every_office(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """The chief is a unit to be watched, not a box that happens to be on top.

        `rollup.agents` counts **the unit and everything under it**, so the chief
        reports the whole organisation: 1 + 3 offices + 7 departments = 11. The
        alternative -- counting only what is below -- makes the chief's number differ
        from the payload's own total by exactly one, and a discrepancy of one is
        precisely what gets "fixed" by editing the seed instead of the arithmetic.
        """
        _org, d = tenant_tree
        rollup = d["chief"].get("rollup") or {}
        assert rollup, "the chief carries no rollup, so nothing above it is watchable"
        assert rollup["agents"] == 1 + WANTED_OFFICES + WANTED_DEPARTMENTS, (
            f"the chief reports {rollup['agents']} agent(s), expected "
            f"{1 + WANTED_OFFICES + WANTED_DEPARTMENTS}"
        )
        assert rollup["agents"] == d["tree"]["total_agents"], (
            "the roll-up and the payload disagree about how large this organisation is"
        )

    async def test_an_office_rolls_up_exactly_its_own_departments(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        _org, d = tenant_tree
        by_office: dict[str, int] = {}
        for box in d["offices"]:
            slug = box["parent_unit_slug"]
            counts = box.get("counts") or {}
            by_office[slug] = by_office.get(slug, 0) + (counts.get("open", 0) or 0)
        for office in d["second_tier"]:
            rollup = office.get("rollup") or {}
            assert rollup, f"{office['name']} carries no rollup"
            assert rollup["agents"] == 1 + sum(
                1 for b in d["offices"] if b["parent_unit_slug"] == office["key"]
            ), f"{office['name']} reports {rollup['agents']} agent(s), not itself plus its own"
            assert rollup["open"] == by_office.get(office["key"], 0), (
                f"{office['name']} claims {rollup['open']} open but its departments "
                f"hold {by_office.get(office['key'], 0)}"
            )


class TestEveryBoxCanBeOpened:
    """Each tier is a unit a person tracks, so each tier needs an identity.

    The detail panel is already generic — it reads one agent box, and offices and the
    chief already carry `runs`, `counts` and the task lists. What they lack is the
    `key` the router uses and the `label` the heading shows. Measured: office missing
    `key` and `label`, chief missing `key`, `label` and `unit`.
    """

    async def test_every_box_in_the_payload_has_a_key_a_label_and_a_unit(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        _org, d = tenant_tree
        boxes = [d["chief"], *d["second_tier"], *d["offices"]]
        for field in ("key", "label", "unit"):
            blank = [b.get("name") for b in boxes if not b.get(field)]
            assert not blank, f"{len(blank)} box(es) have no `{field}`: {blank}"

    async def test_the_keys_are_unique_across_the_whole_organisation(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """They are route segments. A collision means one box opens the other."""
        _org, d = tenant_tree
        keys = [b["key"] for b in [d["chief"], *d["second_tier"], *d["offices"]]]
        assert len(keys) == len(set(keys)), f"duplicate route key(s): {keys}"

    async def test_the_chief_is_reachable_by_the_key_the_page_uses(self, tenant_tree) -> None:  # type: ignore[no-untyped-def]
        """`renderOneDepartment` routes the literal `"chief"`. A key change that does
        not carry that string breaks a route that already works."""
        _org, d = tenant_tree
        assert d["chief"]["key"] == "chief"
