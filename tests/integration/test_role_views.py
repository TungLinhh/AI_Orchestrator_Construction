"""The three role-shaped reads, and the distinctions they exist to keep.

`role_views.py` shipped with no tests, which is the one thing this repository has spent
a dozen tranches refusing to do. These are organised around the three claims its
docstring makes, because each is a claim that is easy to state and easy to leak:

* **A PM workspace does not report an unmeasured project as on schedule.** The corpus's
  progress sheets record planned dates and leave the actual columns empty — 100% of
  `TĐ BOH.xlsx` -- so a naive `actual - planned` reports the whole project as
  on time. `unmeasured` and `late` are separate lists for exactly this.
* **A CEO cockpit does not average away a late project.**
* **A HITL inbox does not silently omit rows it cannot explain.**

Every fixture here builds a project with a WBS and readings, because the interesting
assertions are about how the two combine — and a read tested only against an empty table
proves nothing that an empty table does not already prove.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text

from ai_orchestrator.application.project_operations import (
    AddressResolution,
    write_project,
)
from ai_orchestrator.application.role_views import (
    ceo_cockpit,
    hitl_inbox,
    pm_workspace,
)
from ai_orchestrator.ingest.project_reader import HeaderRead, ProjectHeader
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)
D = dt.date


async def _project(tenant: Tenant, code: str, name: str) -> str:
    outcome = await tenant.run(
        lambda s: write_project(
            s,
            organization_id=tenant.organization_id,
            read=HeaderRead(header=ProjectHeader(project_name=name)),
            code=code,
            address=AddressResolution(
                chosen="Phú Yên", seen=("Phú Yên",), decided_by="ops", decided_on=NOW
            ),
            observed={},
            written_on=NOW,
            contract_value=1_000_000.0,
        )
    )
    assert outcome.written, outcome.refusals
    return outcome.project_id or ""


async def _node(tenant: Tenant, project_id: str, code: str, name: str) -> str:
    node_id = f"wbs_{code.lower()}_{tenant.organization_id[-6:]}"
    await tenant.session.execute(
        text(
            "INSERT INTO wbs (id, organization_id, project_id, code, name, sequence, "
            "is_leaf, source_actor) VALUES (:i, :o, :p, :c, :n, 1, false, 'test')"
        ),
        {"i": node_id, "o": tenant.organization_id, "p": project_id, "c": code, "n": name},
    )
    return node_id


async def _reading(
    tenant: Tenant,
    *,
    project_id: str,
    node_id: str | None,
    report_ref: str,
    source_row: int,
    planned_finish: D,
    actual_finish: D | None,
    actual_updated: bool,
) -> None:
    """One progress reading, with the two facts the workspace sorts on.

    `actual_finish` is populated even when `actual_updated` is false, because that is
    what the corpus does: it copies the planned date into the actual column and leaves
    the "was this actually recorded" question unanswered. A reader that only looks at
    the date would read a copy as a measurement.

    The start dates come along with the finishes because the table refuses a finish
    without one -- `ck_progress_snapshots_planned_finish_needs_a_start` and its actual
    twin. The first version of this fixture set only the finishes and the constraint
    caught it, which is the constraint working.
    """
    await tenant.session.execute(
        text(
            "INSERT INTO progress_snapshots (id, organization_id, report_ref, "
            "source_row, line_label, line_no, section_label, project_id, wbs_id, "
            "planned_start_on, planned_finish_on, actual_start_on, actual_finish_on, "
            "actual_updated, source_actor) "
            "VALUES (:i, :o, :r, :sr, :ll, :sr, 'sec', :p, :n, "
            ":ps, :pf, :as_, :af, :au, 'test')"
        ),
        {
            "i": f"prs_{source_row}_{tenant.organization_id[-6:]}",
            "o": tenant.organization_id,
            "r": report_ref,
            "sr": source_row,
            "ll": str(source_row),
            "p": project_id,
            "n": node_id,
            "ps": planned_finish - dt.timedelta(days=5),
            "pf": planned_finish,
            "as_": (actual_finish - dt.timedelta(days=5)) if actual_finish else None,
            "af": actual_finish,
            "au": actual_updated,
        },
    )


class TestTheCockpit:
    async def test_an_empty_tenant_is_an_empty_cockpit_not_an_error(self, tenant: Tenant) -> None:
        """A list that looks like a failure and is not.

        Worth a test because it is indistinguishable from a broken endpoint without one.
        """
        got = (
            await tenant.run(lambda s: ceo_cockpit(s, organization_id=tenant.organization_id))
        ).as_dict()
        assert got["projects"] == []
        assert got["totals"] == {
            "projects": 0,
            "wbs_nodes": 0,
            "gates_open": 0,
            "contract_value": 0,
        }

    async def test_it_lists_projects_with_their_node_counts(self, tenant: Tenant) -> None:
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        await _project(tenant, "P-2", "Melia Cam Ranh")
        await _node(tenant, p1, "S001", "Zone A")
        await _node(tenant, p1, "S002", "Zone B")

        got = (
            await tenant.run(lambda s: ceo_cockpit(s, organization_id=tenant.organization_id))
        ).as_dict()
        assert [p["name"] for p in got["projects"]] == ["Bãi Tràm", "Melia Cam Ranh"]
        assert got["totals"]["projects"] == 2
        assert got["totals"]["wbs_nodes"] == 2, "only P-1 has nodes"

    async def test_it_sums_contract_value(self, tenant: Tenant) -> None:
        await _project(tenant, "P-1", "One")
        await _project(tenant, "P-2", "Two")
        got = (
            await tenant.run(lambda s: ceo_cockpit(s, organization_id=tenant.organization_id))
        ).as_dict()
        assert got["totals"]["contract_value"] == 2_000_000.0

    async def test_it_reports_the_agent_posture(self, tenant: Tenant) -> None:
        """Including the number that is expected to stay at zero for a while.

        `0016` defaults every agent to L1 and nothing raises it, so `above_l1` is a
        standing check that nothing has quietly granted more autonomy than a ceiling
        allows.
        """
        await _node_free_agent(tenant)
        got = (
            await tenant.run(lambda s: ceo_cockpit(s, organization_id=tenant.organization_id))
        ).as_dict()
        assert got["posture"]["agents"] == 1
        assert got["posture"]["above_l1"] == 0, (
            "an agent at its ceiling is not above it; the first version counted "
            "granted_level = 'L1' and reported every agent as over-authorised"
        )
        assert got["posture"]["killed"] == 0
        assert got["posture"]["tightest_ceiling"] == "L1"

    async def test_it_does_not_report_schedule_slip(self, tenant: Tenant) -> None:
        """Deliberately absent, and the docstring says why.

        An average across projects of different sizes hides a project 40 days late
        behind four that are on time, which is worse than reporting nothing.
        """
        p1 = await _project(tenant, "P-1", "One")
        node = await _node(tenant, p1, "S001", "Zone A")
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 4, 10),
            actual_updated=True,
        )
        got = (
            await tenant.run(lambda s: ceo_cockpit(s, organization_id=tenant.organization_id))
        ).as_dict()
        assert "slip" not in str(got).lower(), (
            "the cockpit must not carry a slip figure; pm_workspace is where it lives"
        )


class TestTheWorkspace:
    async def test_a_zone_with_no_actual_is_unmeasured_not_on_time(self, tenant: Tenant) -> None:
        """The central claim, and the corpus's actual shape.

        `TĐ BOH.xlsx` has 110 activities and not one of them records an actual date,
        so a reader that filtered on "has dates" would report the whole project as
        on schedule with a variance of zero.
        """
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        node = await _node(tenant, p1, "S001", "Zone A")
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=None,
            actual_updated=False,
        )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert got["counts"] == {"zones": 1, "late": 0, "unmeasured": 1}

    async def test_a_copied_actual_date_is_still_unmeasured(self, tenant: Tenant) -> None:
        """The corpus's own trick: actual == planned, and nothing marks it as recorded.

        This is the case a date-only filter gets wrong, and it is why `actual_updated`
        exists at all.
        """
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        node = await _node(tenant, p1, "S001", "Zone A")
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 1),
            actual_updated=False,
        )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert got["counts"]["late"] == 0
        assert got["counts"]["unmeasured"] == 1, (
            "an actual date the sheet never claimed to have recorded is not a measurement"
        )

    async def test_a_measured_late_zone_appears_in_late_with_its_slip(self, tenant: Tenant) -> None:
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        node = await _node(tenant, p1, "S001", "Zone A")
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 11),
            actual_updated=True,
        )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert got["counts"]["late"] == 1
        assert got["late"][0]["slip_days"] == 10
        assert got["late"][0]["is_measured"] is True

    async def test_on_time_and_unmeasured_are_different_lists(self, tenant: Tenant) -> None:
        """Four zones, one late, one on time, two unmeasured.

        A caller that got only `late` could not tell "on time" from "not measured", and
        the two call for completely different responses.
        """
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        await _reading(
            tenant,
            project_id=p1,
            node_id=await _node(tenant, p1, "S001", "Late"),
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 11),
            actual_updated=True,
        )
        await _reading(
            tenant,
            project_id=p1,
            node_id=await _node(tenant, p1, "S002", "On time"),
            report_ref="r1",
            source_row=2,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 1),
            actual_updated=True,
        )
        for i, code in ((3, "S003"), (4, "S004")):
            await _reading(
                tenant,
                project_id=p1,
                node_id=await _node(tenant, p1, code, f"Unmeasured {code}"),
                report_ref="r1",
                source_row=i,
                planned_finish=D(2019, 3, 1),
                actual_finish=None,
                actual_updated=False,
            )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert got["counts"] == {"zones": 4, "late": 1, "unmeasured": 2}
        assert [z["name"] for z in got["late"]] == ["Late"]

    async def test_the_worst_zone_comes_first(self, tenant: Tenant) -> None:
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        for i, (code, slip) in enumerate(((("S001"), 2), ("S002", 9), ("S003", 5)), start=1):
            await _reading(
                tenant,
                project_id=p1,
                node_id=await _node(tenant, p1, code, code),
                report_ref="r1",
                source_row=i,
                planned_finish=D(2019, 3, 1),
                actual_finish=D(2019, 3, 1 + slip),
                actual_updated=True,
            )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert [z["slip_days"] for z in got["late"]] == [9, 5, 2], (
            "worst first, because a list sorted by a PM's patience rather than by "
            "days lost is a list nobody trusts"
        )

    async def test_a_zone_with_no_node_row_at_all_is_still_listed(self, tenant: Tenant) -> None:
        """A WBS node nobody has reported against is a fact about the plan."""
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        await _node(tenant, p1, "S001", "Unreported")
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()
        assert got["counts"]["zones"] == 1
        assert got["zones"][0]["readings"] == 0
        assert got["zones"][0]["is_measured"] is False

    async def test_the_position_list_and_the_late_list_agree(self, tenant: Tenant) -> None:
        """F125. Two queries in one module must not disagree about one row.

        The first version of `_ZONE_POSITION` aggregated over **every** reading and
        reported `measured` as a count beside it, so a node with one measured reading
        ten days late and one unmeasured one -- whose "actual" is a copy of its planned
        date -- reported `measured = 1` and `slip_days = 0`. The unmeasured row won the
        `max()`. The `late` list, which filters properly, called the same node ten days
        late.

        Found by `scripts/verify_page.mjs` executing the page against the real corpus:
        the late zone drew a green "on plan" bar while the page's own legend said one
        zone was late. Both halves were individually correct, which is exactly why
        neither half looked wrong.
        """
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        node = await _node(tenant, p1, "S001", "Zone A")
        # Measured, and ten days late.
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 11),
            actual_updated=True,
        )
        # Not measured, and its actual equals its planned -- the corpus's own shape,
        # and the row that used to overwrite the real variance.
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=2,
            planned_finish=D(2019, 6, 1),
            actual_finish=D(2019, 6, 1),
            actual_updated=False,
        )
        body = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()

        zone = body["zones"][0]
        assert zone["measured"] == 1
        assert zone["readings"] == 2
        assert zone["slip_days"] == 10, (
            "the slip must be computed over the measured readings only; the "
            "unmeasured row's copied planned date was winning the max(). Got "
            f"{zone['slip_days']}"
        )
        assert zone["is_late"] is True
        assert body["late"][0]["slip_days"] == zone["slip_days"], (
            "the two lists are the same measurement computed twice, and they must agree"
        )

    async def test_an_unmeasured_node_reports_no_slip_at_all(self, tenant: Tenant) -> None:
        """`None`, not `0`, and not the unmeasured row's copied dates.

        A node with only unmeasured readings has no measured finish, so there is no
        variance to report. Returning the copied `0` is the F99 shape in a module that
        already believed it had been fixed.
        """
        p1 = await _project(tenant, "P-1", "Bãi Tràm")
        node = await _node(tenant, p1, "S001", "Zone A")
        await _reading(
            tenant,
            project_id=p1,
            node_id=node,
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 1),
            actual_updated=False,
        )
        zone = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p1)
            )
        ).as_dict()["zones"][0]
        assert zone["measured"] == 0
        assert zone["slip_days"] is None, (
            f"an unmeasured zone has no variance; got {zone['slip_days']!r}"
        )
        assert zone["last_actual_finish"] is None, (
            "and no measured actual finish, even though unmeasured rows carry one"
        )

    async def test_one_projects_readings_do_not_leak_into_another(self, tenant: Tenant) -> None:
        p1 = await _project(tenant, "P-1", "One")
        p2 = await _project(tenant, "P-2", "Two")
        await _reading(
            tenant,
            project_id=p1,
            node_id=await _node(tenant, p1, "S001", "A"),
            report_ref="r1",
            source_row=1,
            planned_finish=D(2019, 3, 1),
            actual_finish=D(2019, 3, 9),
            actual_updated=True,
        )
        got = (
            await tenant.run(
                lambda s: pm_workspace(s, organization_id=tenant.organization_id, project_id=p2)
            )
        ).as_dict()
        assert got["counts"] == {"zones": 0, "late": 0, "unmeasured": 0}


class TestTheInbox:
    async def test_an_empty_inbox_is_an_empty_list(self, tenant: Tenant) -> None:
        got = await tenant.run(
            lambda s: hitl_inbox(s, organization_id=tenant.organization_id, now=NOW)
        )
        assert got == []

    async def test_it_lists_pending_approvals_oldest_first(self, tenant: Tenant) -> None:
        tail = tenant.organization_id[-6:]
        await _approval(tenant, f"apr_a_{tail}", created_at=NOW - dt.timedelta(days=5))
        await _approval(tenant, f"apr_b_{tail}", created_at=NOW - dt.timedelta(hours=1))
        got = await tenant.run(
            lambda s: hitl_inbox(s, organization_id=tenant.organization_id, now=NOW)
        )
        assert [a.approval_id for a in got] == sorted(a.approval_id for a in got), (
            "oldest first: an approval queue sorted by insertion is one people work by "
            "habit rather than by policy"
        )
        assert got[0].waiting_for(NOW) == dt.timedelta(days=5)

    async def test_a_decided_approval_is_not_in_the_inbox(self, tenant: Tenant) -> None:
        await _approval(
            tenant,
            f"apr_a_{tenant.organization_id[-6:]}",
            created_at=NOW,
            status="approved",
        )
        got = await tenant.run(
            lambda s: hitl_inbox(s, organization_id=tenant.organization_id, now=NOW)
        )
        assert got == []

    async def test_a_free_text_requester_is_listed_even_with_no_user_row(
        self, tenant: Tenant
    ) -> None:
        """`requested_by` is free text, not a `users` reference.

        That is deliberate — the actor has to survive their departure — and it means a
        requester can name somebody who no longer exists. A join to `users` would drop
        the row, and a queue that silently omits rows cannot be reconciled against
        `approvals`; a person deciding on a queue they cannot count is deciding on a
        queue they do not trust.
        """
        await _approval(
            tenant,
            f"apr_a_{tenant.organization_id[-6:]}",
            created_at=NOW,
            requested_by="agent:hr (deleted)",
        )
        got = await tenant.run(
            lambda s: hitl_inbox(s, organization_id=tenant.organization_id, now=NOW)
        )
        assert len(got) == 1
        assert got[0].requested_by == "agent:hr (deleted)"
        assert got[0].as_dict(now=NOW)["requested_by"] == "agent:hr (deleted)"

    async def test_it_does_not_invent_a_wait_for_an_unrecorded_creation(
        self, tenant: Tenant
    ) -> None:
        """Clamped at zero.

        A clock skewed into the future would otherwise render "-3 days waiting", which
        is a number nobody can act on and which sorts to the top of the wrong list.
        """
        await _approval(
            tenant,
            f"apr_a_{tenant.organization_id[-6:]}",
            created_at=NOW + dt.timedelta(hours=3),
        )
        got = await tenant.run(
            lambda s: hitl_inbox(s, organization_id=tenant.organization_id, now=NOW)
        )
        assert got[0].waiting_for(NOW) == dt.timedelta(0)
        assert got[0].as_dict(now=NOW)["waiting_days"] == 0

    async def test_a_naive_now_is_refused(self, tenant: Tenant) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            await tenant.run(
                lambda s: hitl_inbox(
                    s,
                    organization_id=tenant.organization_id,
                    now=dt.datetime(2026, 9, 29, 12, 0),
                )
            )


async def _approval(
    tenant: Tenant,
    approval_id: str,
    *,
    created_at: dt.datetime,
    status: str = "pending",
    requested_by: str = "agent:hr",
) -> None:
    # `decided_at` alongside an approved status: the table refuses an approval that was
    # approved without a moment, which is the same "a decision without a record" rule
    # `agents.kill_switch` carries.
    await tenant.session.execute(
        text(
            "INSERT INTO approvals (id, organization_id, action_type, action_payload, "
            "payload_hash, requested_by, status, created_at, decided_at) "
            "VALUES (:i, :o, 'procedure.change', CAST(:p AS jsonb), :h, :by, "
            "CAST(:s AS varchar(32)), CAST(:c AS timestamptz), "
            "CASE WHEN CAST(:s AS varchar(32)) = 'approved' "
            "THEN CAST(:c AS timestamptz) ELSE NULL END)"
        ),
        {
            "i": approval_id,
            "o": tenant.organization_id,
            "p": '{"c":1}',
            "h": "0" * 64,
            "by": requested_by,
            "s": status,
            "c": created_at,
        },
    )


async def _node_free_agent(tenant: Tenant) -> None:
    """A minimal `agents` row, which needs a definition, a role and an org unit."""
    tail = tenant.organization_id[-10:]
    for statement, params in (
        (
            "INSERT INTO roles (id, organization_id, name) VALUES (:i, :o, :n) "
            "ON CONFLICT DO NOTHING",
            {"i": f"rol_{tail}", "o": tenant.organization_id, "n": f"role {tail}"},
        ),
        (
            "INSERT INTO organizational_units (id, organization_id, name, slug) "
            "VALUES (:i, :o, :n, :s) ON CONFLICT DO NOTHING",
            {
                "i": f"oru_{tail}",
                "o": tenant.organization_id,
                "n": f"unit {tail}",
                "s": f"u-{tail}",
            },
        ),
        (
            "INSERT INTO agent_definitions (id, organization_id, name, role_id) "
            "VALUES (:i, :o, :n, :r) ON CONFLICT DO NOTHING",
            {
                "i": f"def_{tail}",
                "o": tenant.organization_id,
                "n": f"def {tail}",
                "r": f"rol_{tail}",
            },
        ),
        (
            "INSERT INTO agents (id, organization_id, name, definition_id, role_id, org_unit_id) "
            "VALUES (:i, :o, :n, :d, :r, :u) ON CONFLICT DO NOTHING",
            {
                "i": f"agt_{tail}",
                "o": tenant.organization_id,
                "n": "probe",
                "d": f"def_{tail}",
                "r": f"rol_{tail}",
                "u": f"oru_{tail}",
            },
        ),
    ):
        await tenant.session.execute(text(statement), params)
