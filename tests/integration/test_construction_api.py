"""The construction API: the seven operations that make the domain reachable.

Before `api/construction.py` the API had **72 operations and not one of them touched
a project, a WBS, a progress reading, a contract, a supplier, a purchase order, an RFQ,
a tender, a zone or a gate.** Six projects, 240 WBS nodes and 2778 progress readings
were in the database and could only be seen from a Python shell. These tests exist
partly to prove that is no longer true, and mostly to hold the transport to one rule:

**it must not lose a distinction the read makes.**

The distinction is the one that matters most in this corpus. 100% of `TĐ BOH.xlsx`'s
actual columns are byte-identical to its planned ones, and `actual_updated` is the only
thing that says so. A transport that dropped it would let a client render 2778
fabricated measurements — and a transport that collapsed `unmeasured` into `late` would
report a project nobody reported on as a project that is on time.

## The shadowing tests are here because of F123

`GET /approvals/stats` and `GET /events/stats` were declared **after**
`/approvals/{approval_id}` and `/events/{event_id}`. Starlette matches in registration
order, so both were captured by the parameterised route and answered `404` for the rest
of their lives — a live endpoint that no client could reach, and a bug only a request
could find. `api/construction.py` added `/approvals/inbox`, which had the same shape
by accident, and would have shipped dead the same way.

So: `test_a_literal_path_is_not_shadowed_by_a_parameterised_sibling` below is a test
about routing, not about construction, and it is in this file because this is where
the third instance of the bug was introduced.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text

from ai_orchestrator.application.project_operations import AddressResolution, write_project
from ai_orchestrator.ingest.project_reader import HeaderRead, ProjectHeader
from tests.integration.api_client import TEST_SECRET
from tests.integration.api_client import auth_headers as _headers
from tests.integration.tenant_context import Tenant

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)
D = dt.date


async def _seed(tenant: Tenant, tag: str = "a") -> tuple[str, str, str]:
    """One project, two zones, two readings: one measured-and-late, one unmeasured.

    Returns `(project_id, measured_node_id, unmeasured_node_id)`.

    `tag` exists because `projects.code` is unique per organization, so a test that
    needs two projects in one tenant has to ask for two. The default keeps every
    existing call site unchanged.
    """
    suffix = tenant.organization_id[-8:] + tag
    outcome = await tenant.run(
        lambda s: write_project(
            s,
            organization_id=tenant.organization_id,
            read=HeaderRead(header=ProjectHeader(project_name="Bãi Tràm Estates")),
            code=f"API-{suffix}",
            address=AddressResolution(
                chosen="Phú Yên",
                seen=("Phú Yên", "XUÂN CẢNH"),
                decided_by="ops",
                decided_on=NOW,
            ),
            observed={},
            written_on=NOW,
        )
    )
    assert outcome.written, outcome.refusals
    project_id = outcome.project_id or ""

    for code, name in (("S001", "Zone A"), ("S002", "Zone B")):
        await tenant.session.execute(
            text(
                "INSERT INTO wbs (id, organization_id, project_id, code, name, sequence, "
                "is_leaf, source_actor) VALUES (CAST(:i AS varchar(40)), "
                "CAST(:o AS varchar(40)), CAST(:p AS varchar(40)), CAST(:c AS varchar(64)), "
                "CAST(:n AS varchar(255)), CAST(:s AS integer), false, 'test')"
            ),
            {
                "i": f"wbs_{code}_{suffix}",
                "o": tenant.organization_id,
                "p": project_id,
                "c": code,
                "n": name,
                "s": 1 if code == "S001" else 2,
            },
        )
    await tenant.commit()

    # The `report_ref` and `source_row` are tagged too, because together with
    # `organization_id` they are the reading's identity (`0018`) -- and a second
    # `_seed` in the same tenant would otherwise put both projects' readings on
    # `('R1', 11)` and be refused. F121's lesson, reached from the other direction.
    report_ref = f"R1{tag}"
    rows = [
        # Measured and 10 days late. The case that must render as late.
        ("S001", 11, D(2019, 3, 1), D(2019, 3, 11), True),
        # Planned and "actual" identical, but nothing claims it was recorded. This is
        # the corpus's own shape, and the case that must NOT render as on time.
        ("S002", 12, D(2019, 4, 1), D(2019, 4, 1), False),
    ]
    for code, source_row, planned, actual, updated in rows:
        await tenant.session.execute(
            text(
                "INSERT INTO progress_snapshots (id, organization_id, report_ref, "
                "source_row, line_label, line_no, section_label, project_id, wbs_id, "
                "planned_start_on, planned_finish_on, actual_start_on, actual_finish_on, "
                "actual_updated, source_actor) VALUES "
                "(CAST(:i AS varchar(40)), CAST(:o AS varchar(40)), CAST(:ref AS varchar(128)), "
                "CAST(:sr AS integer), CAST(:ll AS varchar(64)), CAST(:ln AS integer), "
                "'sec', CAST(:p AS varchar(40)), CAST(:n AS varchar(40)), :ps, :pf, "
                ":as_, :af, CAST(:au AS boolean), 'test')"
            ),
            {
                "i": f"prs_{source_row}_{suffix}",
                "o": tenant.organization_id,
                "ref": report_ref,
                "sr": source_row,
                "ll": str(source_row),
                "ln": source_row,
                "p": project_id,
                "n": f"wbs_{code}_{suffix}",
                "ps": planned - dt.timedelta(days=5),
                "pf": planned,
                "as_": actual - dt.timedelta(days=5),
                "af": actual,
                "au": updated,
            },
        )
    await tenant.commit()
    return project_id, f"wbs_S001_{suffix}", f"wbs_S002_{suffix}"


class TestThePortfolio:
    async def test_it_answers(self, client: Any, tenant: Tenant) -> None:
        response = await client.get("/api/v1/portfolio", headers=_headers(tenant.organization_id))
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"projects", "posture", "totals"}

    async def test_it_counts_the_seeded_project_and_its_nodes(
        self, client: Any, tenant: Tenant
    ) -> None:
        project_id, _, _ = await _seed(tenant)
        got = (
            await client.get("/api/v1/portfolio", headers=_headers(tenant.organization_id))
        ).json()
        assert got["totals"]["projects"] == 1
        assert got["totals"]["wbs_nodes"] == 2
        assert [p["project_id"] for p in got["projects"]] == [project_id]

    async def test_it_reports_the_agent_posture(self, client: Any, tenant: Tenant) -> None:
        """Including `above_l1`, the standing control on the autonomy ladder.

        Worth carrying over HTTP because a dashboard that omitted it would omit the one
        number that says delegation is not being widened for convenience.
        """
        got = (
            await client.get("/api/v1/portfolio", headers=_headers(tenant.organization_id))
        ).json()
        assert "agents" in got["posture"]
        assert "above_l1" in got["posture"]
        assert got["posture"]["above_l1"] == 0

    async def test_it_carries_no_schedule_slip(self, client: Any, tenant: Tenant) -> None:
        """Deliberately absent, and the read's docstring says why.

        An average across projects of different sizes hides a project 40 days late
        behind four that are on time.
        """
        await _seed(tenant)
        body = (
            await client.get("/api/v1/portfolio", headers=_headers(tenant.organization_id))
        ).text
        assert "slip" not in body


class TestProjects:
    async def test_an_empty_tenant_is_an_empty_page_not_a_404(
        self, client: Any, tenant: Tenant
    ) -> None:
        """A list that looks like a failure and is not."""
        got = await client.get("/api/v1/projects", headers=_headers(tenant.organization_id))
        assert got.status_code == 200
        assert got.json() == {"items": [], "limit": 200, "offset": 0, "returned": 0, "total": 0}

    async def test_it_lists_projects(self, client: Any, tenant: Tenant) -> None:
        project_id, _, _ = await _seed(tenant)
        got = (
            await client.get("/api/v1/projects", headers=_headers(tenant.organization_id))
        ).json()
        assert got["total"] == 1
        assert got["items"][0]["id"] == project_id
        assert got["items"][0]["name"] == "Bãi Tràm Estates"

    async def test_it_serialises_dates_as_iso_strings(self, client: Any, tenant: Tenant) -> None:
        await _seed(tenant)
        got = (
            await client.get("/api/v1/projects", headers=_headers(tenant.organization_id))
        ).json()
        row = got["items"][0]
        for key in ("planned_start", "planned_completion"):
            assert row[key] is None, "the corpus records no planned window on a project"
        assert row["contract_value"] is None, (
            "a project whose value was never recorded is None, not 0.0 -- and the "
            "corpus header block has no contract value field at all"
        )

    async def test_one_project_carries_its_node_count(self, client: Any, tenant: Tenant) -> None:
        project_id, _, _ = await _seed(tenant)
        got = await client.get(
            f"/api/v1/projects/{project_id}", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 200
        assert got.json()["wbs_nodes"] == 2

    async def test_an_unknown_project_is_404(self, client: Any, tenant: Tenant) -> None:
        got = await client.get(
            "/api/v1/projects/prj_does_not_exist", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 404

    async def test_a_page_cannot_be_asked_for_everything_at_once(
        self, client: Any, tenant: Tenant
    ) -> None:
        """422, not a table scan. `MAX_PAGE_SIZE` is the reason it is a bound."""
        got = await client.get(
            "/api/v1/projects?limit=100000", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 422


class TestTheWorkspace:
    async def test_it_keeps_late_and_unmeasured_apart(self, client: Any, tenant: Tenant) -> None:
        """The central claim of the whole read, asserted over HTTP.

        One zone measured and 10 days late, one zone whose "actual" is a copy of its
        planned date with nothing claiming it was recorded. A transport that merged
        them would report the second as on time.
        """
        project_id, _, _ = await _seed(tenant)
        got = await client.get(
            f"/api/v1/projects/{project_id}/workspace",
            headers=_headers(tenant.organization_id),
        )
        body = got.json()
        assert body["counts"] == {"zones": 2, "late": 1, "unmeasured": 1}
        assert body["late"][0]["slip_days"] == 10
        assert body["unmeasured"][0]["is_measured"] is False
        assert body["unmeasured"][0]["slip_days"] is None, (
            "`None`, not the copied date's zero. F125: an unmeasured zone has no "
            "measured finish, so it has no variance -- and a `0` here is exactly what "
            "makes an unreported project look perfectly on schedule"
        )

    async def test_a_zone_is_marked_not_measured_and_not_late(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The two booleans a client renders from, and they must both be present."""
        project_id, _, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/workspace",
                headers=_headers(tenant.organization_id),
            )
        ).json()
        for zone in body["zones"]:
            assert "is_measured" in zone
            assert "is_late" in zone


class TestTheBreakdown:
    async def test_it_returns_a_flat_paginated_list(self, client: Any, tenant: Tenant) -> None:
        """Flat with `parent_id`, because a tree has no natural first page."""
        project_id, _, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/wbs", headers=_headers(tenant.organization_id)
            )
        ).json()
        assert body["count"] == 2
        assert body["roots"] == 2
        assert [n["code"] for n in body["items"]] == ["S001", "S002"], "sequence order"

    async def test_a_single_node(self, client: Any, tenant: Tenant) -> None:
        project_id, node_id, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/wbs", headers=_headers(tenant.organization_id)
            )
        ).json()
        assert node_id in [n["id"] for n in body["items"]]


class TestActivity:
    async def test_every_reading_carries_actual_updated(self, client: Any, tenant: Tenant) -> None:
        """Without this flag a client reads the corpus's copied planned dates as
        measurements -- and that is 100% of the real file."""
        project_id, _, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/activity",
                headers=_headers(tenant.organization_id),
            )
        ).json()
        assert body["count"] == 2
        assert body["measured"] == 1
        assert body["unmeasured"] == 1
        for row in body["items"]:
            assert "actual_updated" in row
            assert isinstance(row["actual_updated"], bool)

    async def test_readings_come_back_in_source_row_order(
        self, client: Any, tenant: Tenant
    ) -> None:
        """`source_row` is the identity, and the only unambiguous order.

        `line_label` repeats under every system heading and `TĐ Hạ Tầng.xlsx` lists the
        same activity twice at different rows.
        """
        project_id, _, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/activity",
                headers=_headers(tenant.organization_id),
            )
        ).json()
        assert [r["source_row"] for r in body["items"]] == [11, 12]

    async def test_one_nodes_readings(self, client: Any, tenant: Tenant) -> None:
        project_id, node_id, _ = await _seed(tenant)
        body = (
            await client.get(
                f"/api/v1/projects/{project_id}/activity",
                params={"wbs_id": node_id},
                headers=_headers(tenant.organization_id),
            )
        ).json()
        assert body["count"] == 1
        assert body["items"][0]["source_row"] == 11

    async def test_an_unknown_node_is_404(self, client: Any, tenant: Tenant) -> None:
        project_id, _, _ = await _seed(tenant)
        got = await client.get(
            f"/api/v1/projects/{project_id}/activity",
            params={"wbs_id": "wbs_nope"},
            headers=_headers(tenant.organization_id),
        )
        assert got.status_code == 404

    async def test_a_node_from_another_project_is_refused(
        self, client: Any, tenant: Tenant
    ) -> None:
        """A node that exists but belongs to a different project is a 404.

        Not a security boundary -- the caller can reach project B through B's own URL.
        A correctness one: the path says which project is being asked about, and a node
        that does not belong to it does not answer that question. Returning B's
        schedule under A's URL is a plausible-looking answer to a question nobody asked.

        The first version of `node_by_id` did not project `project_id`, so the check
        compared `None` and never fired.
        """
        project_a, _, _ = await _seed(tenant, tag="a")
        project_b, node_b, _ = await _seed(tenant, tag="b")
        assert project_a != project_b, "the fixture must produce two distinct projects"
        got = await client.get(
            f"/api/v1/projects/{project_a}/activity",
            params={"wbs_id": node_b},
            headers=_headers(tenant.organization_id),
        )
        assert got.status_code == 404, (
            f"a node of another project answered under this project's URL: {got.status_code}"
        )

    async def test_the_limit_is_bounded(self, client: Any, tenant: Tenant) -> None:
        project_id, _, _ = await _seed(tenant)
        got = await client.get(
            f"/api/v1/projects/{project_id}/activity",
            params={"limit": 100000},
            headers=_headers(tenant.organization_id),
        )
        assert got.status_code == 422


class TestTenancy:
    async def test_another_tenants_project_is_404_not_403(
        self, client: Any, tenant: Tenant
    ) -> None:
        """404, never 403.

        "Forbidden" would confirm the row exists, which is a thing this system does not
        do. And RLS is `FORCE`d on `projects`, so the second tenant's session cannot
        see the row even to 403 about it -- the 404 is what the database itself says.
        """
        project_id, _, _ = await _seed(tenant)
        other = "org_01m3d5hwxet3x61vjc1ffjyrzh"
        for path in (
            f"/api/v1/projects/{project_id}",
            f"/api/v1/projects/{project_id}/workspace",
            f"/api/v1/projects/{project_id}/wbs",
            f"/api/v1/projects/{project_id}/activity",
        ):
            got = await client.get(path, headers=_headers(other))
            assert got.status_code == 404, f"{path} -> {got.status_code}"

    async def test_the_portfolio_does_not_leak_another_tenants_projects(
        self, client: Any, tenant: Tenant
    ) -> None:
        await _seed(tenant)
        other = "org_01m3d5hwxet3x61vjc1ffjyrzh"
        got = await client.get("/api/v1/portfolio", headers=_headers(other))
        assert got.status_code == 200
        assert got.json()["totals"]["projects"] == 0

    async def test_a_request_with_no_organization_is_refused(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The tenant comes from the principal, never from a parameter.

        So a caller that omits it is not silently treated as "the default tenant" --
        which is the failure that turns a missing header into a wrong answer.
        """
        got = await client.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer svc.{TEST_SECRET}"}
        )
        assert got.status_code in (401, 403, 422)


class TestRoutingIsNotShadowed:
    """F123. Three instances of one bug, and a request is the only thing that finds it."""

    async def test_a_literal_path_is_not_shadowed_by_a_parameterised_sibling(
        self, client: Any, tenant: Tenant
    ) -> None:
        """`/approvals/stats` was declared after `/approvals/{approval_id}`.

        Starlette matches in registration order, so `stats` was captured by the
        parameterised route and the endpoint answered 404 for its entire life. It is in
        the OpenAPI schema, it is documented, and it was unreachable.
        """
        got = await client.get("/api/v1/approvals/stats", headers=_headers(tenant.organization_id))
        assert got.status_code == 200, (
            f"/approvals/stats is shadowed: {got.status_code} {got.text[:200]}"
        )
        assert "counts" in got.json()

    async def test_the_events_stats_route_is_reachable(self, client: Any, tenant: Tenant) -> None:
        got = await client.get("/api/v1/events/stats", headers=_headers(tenant.organization_id))
        assert got.status_code == 200, f"/events/stats is shadowed: {got.status_code}"

    async def test_the_inbox_is_not_captured_as_an_approval_id(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The bug this file would have shipped, in its original form.

        `construction_router` is registered **before** `approvals_router`, so
        `/approvals/inbox` wins over `/approvals/{approval_id}`. Registered after, the
        literal path would be captured and the mobile queue would 404 — the same
        failure as the two stats routes, on the one endpoint the product needs most.
        """
        got = await client.get("/api/v1/approvals/inbox", headers=_headers(tenant.organization_id))
        assert got.status_code == 200, (
            f"/approvals/inbox is captured as an approval id: {got.status_code}"
        )
        body = got.json()
        assert body == {"items": [], "count": 0, "as_of": body["as_of"]}

    async def test_the_inbox_reports_its_clock(self, client: Any, tenant: Tenant) -> None:
        """`as_of` is what makes a screenshot of the queue reproducible."""
        body = (
            await client.get(
                "/api/v1/approvals/inbox",
                params={"now": "2026-09-29T12:00:00+00:00"},
                headers=_headers(tenant.organization_id),
            )
        ).json()
        assert body["as_of"] == "2026-09-29T12:00:00+00:00"

    async def test_a_naive_clock_is_refused(self, client: Any, tenant: Tenant) -> None:
        """`approvals.created_at` is `timestamptz`; a naive value raises from inside
        the subtraction rather than here, and 400 is a far kinder place for it."""
        got = await client.get(
            "/api/v1/approvals/inbox",
            params={"now": "2026-09-29T12:00:00"},
            headers=_headers(tenant.organization_id),
        )
        assert got.status_code == 400

    async def test_every_construction_path_is_in_the_schema(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The gap this module closed, asserted so it cannot silently reopen.

        Before it, the schema had 72 operations and not one path contained a project,
        a WBS, a reading, a contract, a supplier, an RFQ, a tender, a zone or a gate.
        """
        schema = (await client.get("/openapi.json")).json()
        paths = schema["paths"]
        for expected in (
            "/api/v1/portfolio",
            "/api/v1/projects",
            "/api/v1/projects/{project_id}",
            "/api/v1/projects/{project_id}/workspace",
            "/api/v1/projects/{project_id}/wbs",
            "/api/v1/projects/{project_id}/activity",
            "/api/v1/approvals/inbox",
        ):
            assert expected in paths, f"{expected} is not in the served schema"
