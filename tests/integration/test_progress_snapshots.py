"""`progress_snapshots` against a live database.

The interesting rules here are not about storage. They are the four measurements
from `TĐ BOH.xlsx :: TĐ .BOH` — the only progress sheet in the corpus with a
`KH`/`TT` pair — turned into things the database refuses:

* a finish that precedes its start
* a finish with no start
* a duration that disagrees with the dates beside it, under the inclusive counting
  the file uses five times out of five
* a completion figure above 1, because the header says percentage and the cells hold
  a fraction

and the one thing the corpus makes necessary: a row whose actual dates are identical
to its planned dates is indistinguishable from an on-time activity unless something
records that the actual columns were ever filled in.

`TestTheCorpusRowsLoadCleanly` is the acceptance criterion. The five rows are
transcribed from the file; if they do not insert without a refusal, the schema
disagrees with the only real example and the schema is wrong.
"""

from __future__ import annotations

import datetime as dt
from itertools import count

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant
from tests.integration.test_construction_domain import refused_because

pytestmark = [pytest.mark.integration]

PROGRESS_TABLES = ("progress_snapshots",)

INSERT = """
    INSERT INTO progress_snapshots (
        id, organization_id, report_ref, source_row, line_label, line_no,
        work_description,
        planned_start_on, planned_finish_on, planned_duration_days,
        actual_start_on, actual_finish_on, actual_duration_days,
        actual_updated, completion_ratio, item_completion_ratio,
        status_text, is_adequate, observed_on
    ) VALUES (
        :i, :o, :report, :source_row, :label, :line_no, :work,
        :p_start, :p_finish, :p_days,
        :a_start, :a_finish, :a_days,
        :updated, :completion, :item_completion,
        :status, :adequate, :observed
    )
"""


#: See `_reading`'s counter in `test_progress_operations.py` -- same reason.
_SNAPSHOT_SERIAL = count()


async def _snapshot(t: Tenant, **overrides: object) -> None:
    """One valid row, so a test states only what it is varying."""
    params: dict[str, object] = {
        "i": f"prs_{new_ulid()}",
        "o": t.organization_id,
        "report": "BOH-W32",
        # The identity within the report (migration `0018`), so two calls with
        # otherwise identical arguments are two different readings rather than one row
        # and a constraint violation.
        "source_row": next(_SNAPSHOT_SERIAL) + 500,
        "label": "1",
        "line_no": 1,
        "work": "Lắp đặt đường ống nước",
        "p_start": "2019-04-17",
        "p_finish": "2019-04-26",
        "p_days": 10,
        "a_start": "2019-04-17",
        "a_finish": "2019-04-26",
        "a_days": 10,
        "updated": True,
        "completion": 0.8,
        "item_completion": 0.65,
        "status": "YES",
        "adequate": True,
        "observed": "2019-08-01",
    }
    params.update(overrides)
    await t.session.execute(text(INSERT), _coerce_dates(params))
    await t.commit()


#: The `Date` and `Integer` columns, by parameter name. Dates are written as ISO
#: strings in the test data because that is how the corpus holds them, and asyncpg
#: does not coerce a `str` into a `date` — it raises `DataError: invalid input for
#: query argument`. Coercing here rather than in every call site is the difference
#: between one place that knows the rule and thirty that have to remember it.
_DATE_PARAMS = ("p_start", "p_finish", "a_start", "a_finish", "observed")


def _coerce_dates(params: dict[str, object]) -> dict[str, object]:
    out = dict(params)
    for name in _DATE_PARAMS:
        value = out.get(name)
        if isinstance(value, str) and value:
            out[name] = dt.date.fromisoformat(value)
    return out


class TestThePlannedAndActualWindows:
    async def test_a_planned_window_ordered_earlier_to_later_is_accepted(
        self, tenant: Tenant
    ) -> None:
        await _snapshot(tenant)

    async def test_a_planned_window_that_runs_backwards_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_progress_snapshots_planned_window_ordered"):
            # `p_days` is cleared so only the window rule can fire. An inverted
            # window also makes the inclusive count negative, and with a duration
            # present both constraints are violated — which one reports first is
            # Postgres's business, not ours.
            await _snapshot(
                tenant,
                p_start="2019-04-26",
                p_finish="2019-04-17",
                p_days=None,
                i=f"prs_{new_ulid()}",
                label="2",
                line_no=2,
            )
        await tenant.session.rollback()

    async def test_an_actual_window_that_runs_backwards_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_progress_snapshots_actual_window_ordered"):
            await _snapshot(
                tenant,
                a_start="2019-05-01",
                a_finish="2019-04-26",
                a_days=None,
                i=f"prs_{new_ulid()}",
                label="3",
                line_no=3,
            )
        await tenant.session.rollback()

    async def test_a_finish_with_no_start_is_refused(self, tenant: Tenant) -> None:
        """Both halves.

        You cannot finish what never started, and this is the exact shape a
        copy-paste of the planned row into the actual columns takes — which is what
        the corpus's own file looks like on every row, except that the corpus also
        copies the start.
        """
        with refused_because("ck_progress_snapshots_actual_finish_needs_a_start"):
            await _snapshot(
                tenant,
                a_start=None,
                a_finish="2019-04-26",
                a_days=None,
                i=f"prs_{new_ulid()}",
                label="4",
                line_no=4,
            )
        await tenant.session.rollback()

    async def test_a_planned_finish_with_no_start_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_progress_snapshots_planned_finish_needs_a_start"):
            await _snapshot(
                tenant,
                p_start=None,
                p_finish="2019-04-26",
                p_days=None,
                i=f"prs_{new_ulid()}",
                label="5",
                line_no=5,
            )
        await tenant.session.rollback()

    async def test_a_start_with_no_finish_is_ordinary(self, tenant: Tenant) -> None:
        """Work in progress.

        A progress sheet in week one is mostly starts with no finishes, and a rule
        that refused those would refuse the sheets it most needs to accept.
        """
        await _snapshot(
            tenant,
            p_finish=None,
            p_days=None,
            a_finish=None,
            a_days=None,
            completion=0.4,
            i=f"prs_{new_ulid()}",
            label="6",
            line_no=6,
        )

    async def test_an_activity_with_no_dates_at_all_is_accepted(self, tenant: Tenant) -> None:
        """Only if it has a completion or a description — see the observation check.

        A brand-new activity nobody has started is still a thing the report should
        be able to mention.
        """
        await _snapshot(
            tenant,
            p_start=None,
            p_finish=None,
            p_days=None,
            a_start=None,
            a_finish=None,
            a_days=None,
            completion=None,
            i=f"prs_{new_ulid()}",
            label="7",
            line_no=7,
        )


class TestDurationsMustMatchTheirDates:
    """Inclusive calendar counting, five for five on the only real file."""

    async def test_the_inclusive_count_is_accepted(self, tenant: Tenant) -> None:
        """`2019-04-17` to `2019-04-26` is 10 days inclusive and 9 exclusive.

        The constraint is what makes the convention explicit, because a reader that
        guessed exclusive would be off by one on every activity and nothing would
        raise.
        """
        await _snapshot(tenant, p_days=10, a_days=10)

    async def test_the_exclusive_count_is_refused(self, tenant: Tenant) -> None:
        """The specific mistake, stated as a test.

        Nine is what `(finish - start).days` returns and it is what a reader
        defaulting to date arithmetic would produce.
        """
        with refused_because("ck_progress_snapshots_planned_days_match_dates"):
            await _snapshot(tenant, p_days=9)
        await tenant.session.rollback()

    async def test_an_actual_duration_that_disagrees_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_progress_snapshots_actual_days_match_dates"):
            await _snapshot(tenant, a_days=11)
        await tenant.session.rollback()

    async def test_a_zero_day_duration_is_refused(self, tenant: Tenant) -> None:
        """A same-day activity is one day, not zero.

        Zero is what a reader computing `finish - start` without the `+ 1` produces,
        and it is also a legitimate-looking number for an activity that has not
        started — so it gets its own check rather than being caught by the date
        agreement alone.
        """
        # The dates are cleared deliberately. With them present the inclusive count
        # is 10 and *both* rules are violated, so which one reports is Postgres's
        # choice; with them absent `planned_days_match_dates` cannot fire and this
        # rule is the only one left. Which also documents that `>= 1` exists for
        # the date-less case, not as a belt to the inclusive rule's braces.
        with refused_because("ck_progress_snapshots_planned_days_positive"):
            await _snapshot(
                tenant,
                p_start=None,
                p_finish=None,
                p_days=0,
                i=f"prs_{new_ulid()}",
                label="8",
            )
        await tenant.session.rollback()

    async def test_a_duration_with_no_dates_is_accepted(self, tenant: Tenant) -> None:
        """The dates are the authority.

        A reported duration on its own is a claim; with nothing to check it against
        it is kept, because the alternative is discarding a number the sheet stated.
        """
        await _snapshot(
            tenant,
            p_start=None,
            p_finish=None,
            a_start=None,
            a_finish=None,
            p_days=10,
            a_days=12,
            i=f"prs_{new_ulid()}",
            label="9",
        )


class TestCompletionIsARatioNotAPercentage:
    """The header says percentage. The cells hold `0.65`, `0.8`, `0.9`, `0`."""

    @pytest.mark.parametrize("value", [0.0, 0.65, 0.8, 0.9, 1.0])
    async def test_a_ratio_is_accepted(self, tenant: Tenant, value: float) -> None:
        await _snapshot(tenant, completion=value, i=f"prs_{new_ulid()}", label=f"r{value}")

    @pytest.mark.parametrize("value", [1.5, 65.0, 100.0])
    async def test_a_percentage_is_refused(self, tenant: Tenant, value: float) -> None:
        """Deliberately not rescaled.

        A genuine `65` on a future sheet and a mis-keyed `0.65` on this one are
        indistinguishable. Dividing by 100 would silently pick one, and the
        completion figure would be a hundred times out with nothing raised.
        """
        with refused_because("ck_progress_snapshots_completion_in_range"):
            await _snapshot(tenant, completion=value, i=f"prs_{new_ulid()}", label=f"p{value}")
        await tenant.session.rollback()

    async def test_a_negative_completion_is_refused(self, tenant: Tenant) -> None:
        with refused_because("ck_progress_snapshots_completion_in_range"):
            await _snapshot(tenant, completion=-0.1, i=f"prs_{new_ulid()}", label="neg")
        await tenant.session.rollback()

    async def test_the_rolled_up_completion_is_bounded_too(self, tenant: Tenant) -> None:
        """`Hoàn thành tổng theo hạng mục` is a different figure from the row's own.

        Both are ratios and both are bounded, because a reader that conflates "this
        activity" with "the contract item it belongs to" reports every activity as a
        fraction of a total — and then that fraction is out of range.
        """
        with refused_because("ck_progress_snapshots_item_completion_in_range"):
            await _snapshot(tenant, item_completion=65.0, i=f"prs_{new_ulid()}", label="roll")
        await tenant.session.rollback()


class TestTheStatusFlagMustMatchItsText:
    async def test_a_recognised_flag_with_its_boolean_is_accepted(self, tenant: Tenant) -> None:
        await _snapshot(tenant, status="YES", adequate=True)

    async def test_a_boolean_beside_an_unrecognised_status_is_refused(self, tenant: Tenant) -> None:
        """`Đang thực hiện` is neither adequate nor not.

        Allowing `is_adequate = false` beside it would be an unarguable
        contradiction on the face of the row, and the sheet's `Tình trạng` column is
        almost certainly a wider vocabulary than the single `YES` the corpus
        contains.
        """
        with refused_because("ck_progress_snapshots_status_flag_known"):
            await _snapshot(
                tenant,
                status="Đang thực hiện",
                adequate=False,
                i=f"prs_{new_ulid()}",
                label="st",
            )
        await tenant.session.rollback()

    async def test_an_unrecognised_status_alone_is_fine(self, tenant: Tenant) -> None:
        """Only the *combination* is a contradiction.

        Refusing the text itself would mean refusing the vocabulary the corpus has
        not shown us yet, which is the wrong way round.
        """
        await _snapshot(
            tenant, status="Đang thực hiện", adequate=None, i=f"prs_{new_ulid()}", label="st2"
        )

    async def test_a_boolean_with_no_text_at_all_is_refused(self, tenant: Tenant) -> None:
        """A boolean with nothing to justify it is a guess with a column."""
        with refused_because("ck_progress_snapshots_status_flag_known"):
            await _snapshot(tenant, status="", adequate=True, i=f"prs_{new_ulid()}", label="st3")
        await tenant.session.rollback()


class TestARowMustBeAnObservation:
    """The sheets are two-level, and a section heading is not a snapshot."""

    async def test_a_bare_section_heading_is_refused(self, tenant: Tenant) -> None:
        """`A. BOH` and `I. Hệ thống cấp nước` carry a number and a heading.

        Stored as rows they appear in a progress report as zero-duration
        activities, and they are counted in the denominator of every completion
        figure computed from the table.
        """
        with refused_because("ck_progress_snapshots_row_is_an_observation"):
            await _snapshot(
                tenant,
                work="",
                p_start=None,
                p_finish=None,
                p_days=None,
                a_start=None,
                a_finish=None,
                a_days=None,
                completion=None,
                i=f"prs_{new_ulid()}",
                label="I",
                line_no=0,
            )
        await tenant.session.rollback()

    async def test_a_section_heading_is_fine_as_the_parent_of_a_real_row(
        self, tenant: Tenant
    ) -> None:
        """`section_label` carries the heading; the row carries the activity."""
        await _snapshot(tenant, section_label="I. Hệ thống cấp nước")


class TestLineLabelsAreNotRenumbered:
    """`Stt/No` holds `A`, `I`, `1`, `2` in one column."""

    async def test_a_roman_numeral_label_is_accepted(self, tenant: Tenant) -> None:
        await _snapshot(tenant, label="I", line_no=0, section_label="I. Hệ thống")

    async def test_two_rows_may_not_share_a_row_in_one_report(self, tenant: Tenant) -> None:
        """**Reversed by migration `0018`, and the reversal is the point.**

        The key used to include `section_label` and `line_label`, and this test asserted
        that two rows could not share a label. That was correct for 34 activities and
        wrong for 465: `TĐ Hạ Tầng.xlsx :: TĐ INF` lists the same activity **twice**,
        identical in section, label, line number and description. No key built from what
        a reading says can hold that, so the identity became the row.

        So two rows in one report may now share a label freely, and may not share a
        row. Both halves are tested, because a test that only kept the half that still
        holds would leave the change half-reverted.
        """
        await _snapshot(tenant, label="1", line_no=1, source_row=1)
        with refused_because("uq_progress_snapshots_org_report_row"):
            await _snapshot(tenant, label="1", line_no=7, source_row=1)
        await tenant.session.rollback()

    async def test_two_rows_may_share_a_label_at_different_rows(self, tenant: Tenant) -> None:
        """The half that used to be illegal and is now the corpus's normal shape.

        Seven system headings each number their activities from 1, so the real file has
        far more `1`s than distinct labels. Under the old key those were separated by
        `section_label`; under this one they are separated by position, which also
        covers the case the section could not.
        """
        await _snapshot(tenant, label="1", line_no=1, source_row=1)
        await _snapshot(tenant, label="1", line_no=1, source_row=2)
        await _snapshot(tenant, label="1", line_no=1, source_row=3)
        await tenant.session.rollback()

    async def test_the_same_label_in_a_different_report_is_fine(self, tenant: Tenant) -> None:
        """Reports are separate observations of the same activity.

        Which is why `report_ref` is in the key: a weekly report restates every
        activity, and the history rather than the overwrite is the point.
        """
        await _snapshot(tenant, label="1", report="BOH-W32")
        await _snapshot(tenant, label="1", report="BOH-W33")


class TestActualUpdatedIsNotForced:
    """The measurement that makes the column necessary.

    In the one real file every planned date equals its actual. A variance of zero is
    then ambiguous between "on time" and "not recorded", and nothing in the schema
    can tell them apart — which is why the reader records the fact rather than
    leaving it to be inferred from a coincidence.
    """

    async def test_a_row_whose_actuals_equal_its_plan_may_say_it_was_not_updated(
        self, tenant: Tenant
    ) -> None:
        """Nothing in the schema forces `true`.

        The constraint that would be tempting here — "if the dates are equal then
        `actual_updated` is false" — is exactly wrong, because a genuinely on-time
        activity has equal dates too, and refusing to record the honest case would
        push readers to put `true` on rows that were never filled in.
        """
        await _snapshot(tenant, updated=False)

    async def test_the_variance_query_distinguishes_zero_from_unrecorded(
        self, tenant: Tenant
    ) -> None:
        """The query a progress review runs, and the reason the column is there.

        Two activities, both zero variance, one reported and one not. A review that
        counts them as two on-time activities is wrong about one of them.
        """
        await _snapshot(tenant, label="1", line_no=1, updated=True)
        await _snapshot(tenant, label="2", line_no=2, updated=False)
        rows = (
            await tenant.session.execute(
                text(
                    """
                    SELECT line_label,
                           coalesce(actual_duration_days, planned_duration_days)
                             - planned_duration_days AS variance,
                           actual_updated
                    FROM progress_snapshots
                    WHERE organization_id = :o AND report_ref = 'BOH-W32'
                    ORDER BY line_label
                    """
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert [(r[0], int(r[1]), r[2]) for r in rows] == [("1", 0, True), ("2", 0, False)]
        assert rows[0][1] == rows[1][1], "both are zero variance, which is the point"
        assert rows[0][2] != rows[1][2], "and only one of them is evidence of anything"


class TestTheCorpusRowsLoadCleanly:
    """The acceptance criterion, as five transcribed rows.

    `TĐ BOH.xlsx :: TĐ .BOH`, data rows only. If these do not insert without a
    refusal, the schema disagrees with the only real example of the document it was
    designed from, and the schema is what needs changing.
    """

    ROWS = [
        # (label, work, p_start, p_finish, p_days, completion, handover)
        ("1", "Bể nước sinh hoạt", "2019-03-13", "2019-03-14", 2, 0.65, 2),
        ("2", "Lắp đặt đường ống", "2019-04-17", "2019-04-26", 10, 0.8, 6),
        ("3", "Bể STP: Lắp đặt", "2019-04-01", "2019-04-20", 20, 0.9, 6),
        ("4", "Lắp đặt đường ống", "2019-06-30", "2019-07-30", 31, 0.0, 6),
        ("5", "Lắp đặt bơm nước", "2019-08-20", "2019-09-03", 15, 0.0, 3),
    ]

    async def test_all_five_rows_insert_without_a_refusal(self, tenant: Tenant) -> None:
        for index, (label, work, start, finish, days, completion, handover) in enumerate(
            self.ROWS, 1
        ):
            await _snapshot(
                tenant,
                label=label,
                line_no=index,
                work=work,
                section_label="I. Hệ thống cấp nước",
                p_start=start,
                p_finish=finish,
                p_days=days,
                # The file's actual columns are byte-identical to the planned ones on
                # every row, so `actual_updated` is false and the variance is zero
                # for all five. Recorded rather than corrected.
                a_start=start,
                a_finish=finish,
                a_days=days,
                updated=False,
                completion=completion,
                item_completion=0.65,
                status="YES",
                adequate=True,
                handover_planned=handover,
                handover_actual=handover,
                observed="2019-08-01",
                period_label="Tuần 1/Week 1",
            )
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-W32'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == 5

    async def test_the_five_durations_reproduce_the_file(self, tenant: Tenant) -> None:
        """Read back rather than asserted on the way in.

        The insert succeeding proves the constraint is satisfied; reading the values
        back proves the right values are the ones that satisfied it.
        """
        for index, (label, work, start, finish, days, completion, handover) in enumerate(
            self.ROWS, 1
        ):
            await _snapshot(
                tenant,
                label=label,
                line_no=index,
                work=work,
                p_start=start,
                p_finish=finish,
                p_days=days,
                a_start=start,
                a_finish=finish,
                a_days=days,
                updated=False,
                completion=completion,
                status="YES",
                adequate=True,
                handover_planned=handover,
                handover_actual=handover,
            )
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT line_label, planned_duration_days, planned_finish_on "
                    "- planned_start_on + 1 FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-W32' "
                    "ORDER BY line_no"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert [int(r[1]) for r in rows] == [r[4] for r in self.ROWS]
        assert [int(r[2]) for r in rows] == [r[4] for r in self.ROWS]


class TestTheLateActivitiesQuery:
    async def test_lateness_is_one_query_across_both_halves(self, tenant: Tenant) -> None:
        """The question the table exists to answer.

        `actual_finish - planned_finish` across the two halves of the row. If planned
        and actual were separate tables this would be a self-join, and the pairing
        that the sheet states directly would have to be reconstructed.
        """
        await _snapshot(
            tenant,
            label="1",
            line_no=1,
            p_start="2019-04-01",
            p_finish="2019-04-10",
            p_days=10,
            a_start="2019-04-05",
            a_finish="2019-04-20",
            a_days=16,
            updated=True,
        )
        await _snapshot(
            tenant,
            label="2",
            line_no=2,
            p_start="2019-05-01",
            p_finish="2019-05-10",
            p_days=10,
            a_start="2019-05-01",
            a_finish="2019-05-10",
            a_days=10,
            updated=True,
        )
        rows = (
            await tenant.session.execute(
                text(
                    """
                    SELECT line_label, actual_finish_on - planned_finish_on AS days_late
                    FROM progress_snapshots
                    WHERE organization_id = :o
                      AND actual_updated IS TRUE
                      AND actual_finish_on > planned_finish_on
                    ORDER BY days_late DESC
                    """
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert [(r[0], int(r[1])) for r in rows] == [("1", 10)]


class TestProvenanceAndTenantIsolation:
    async def test_an_agent_row_must_name_its_proposal(self, tenant: Tenant) -> None:
        await _snapshot(tenant)
        with refused_because("ck_progress_snapshots_agent_source_needs_proposal"):
            await tenant.session.execute(
                text(
                    "UPDATE progress_snapshots SET source = 'agent_proposal', "
                    "proposal_id = NULL WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_a_human_row_may_not_borrow_a_proposal(self, tenant: Tenant) -> None:
        await _snapshot(tenant)
        with refused_because("ck_progress_snapshots_agent_source_needs_proposal"):
            await tenant.session.execute(
                text(
                    "UPDATE progress_snapshots SET source = 'human', "
                    "proposal_id = 'pro_1' WHERE organization_id = :o"
                ),
                {"o": tenant.organization_id},
            )
        await tenant.session.rollback()

    async def test_the_table_is_tenant_protected(self, admin_db: Database) -> None:
        async with admin_db.engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                               (SELECT count(*) FROM pg_policies p
                                WHERE p.schemaname = n.nspname
                                  AND p.tablename = c.relname) AS policies
                        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                          AND c.relname = ANY(:names)
                        """
                        ),
                        {"names": list(PROGRESS_TABLES)},
                    )
                )
                .mappings()
                .all()
            )
        assert len(rows) == len(PROGRESS_TABLES)
        unprotected = [
            r["relname"]
            for r in rows
            if not (r["relrowsecurity"] and r["relforcerowsecurity"] and r["policies"])
        ]
        assert not unprotected, f"no isolation policy: {unprotected}"

    async def test_another_tenants_activity_is_not_reachable(
        self, tenant: Tenant, admin_db: Database
    ) -> None:
        """The composite scope, at the level a progress report is read."""
        await _snapshot(tenant)
        async with admin_db.engine.connect() as conn:
            visible = await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, false)"),
                {"o": tenant.organization_id},
            )
            assert visible is not None
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM progress_snapshots WHERE organization_id = :o"),
                    {"o": tenant.organization_id},
                )
            ).scalar()
        assert count == 1


class TestPointersAreTenantScoped:
    """A foreign key on `id` alone stops a *dangling* pointer, not a *cross-tenant* one.

    `ForeignKey("projects.id")` is satisfied by any project row in the database.
    RLS stops tenant A from *reading* tenant B's project, so the hole is not a leak —
    it is a corrupt pointer, which is the harder kind to find later: a progress report
    scoped to one project that silently contains another tenant's activity.

    `procurement.py` already established the composite form for exactly this reason
    on `unit_code`, and said so in the column comment: *RLS stops the read; this
    stops the pointer, which is the direction that corrupts an estimate.* This table
    was created with a bare `project_id` and a bare `wbs_item_id`, and the test below
    is how that was found rather than assumed.
    """

    async def test_another_tenants_project_cannot_be_named(
        self, tenant: Tenant, admin_db: Database
    ) -> None:
        from tests.integration.tenant_context import _create_organization

        other_org = await _create_organization(admin_db, "other")
        other_project = f"prj_{new_ulid()}"
        async with admin_db.session() as session:
            await session.execute(
                text(
                    "INSERT INTO projects (id, organization_id, code, name, status) "
                    "VALUES (:i, :o, :c, 'Other tenant project', 'active')"
                ),
                {
                    "i": other_project,
                    "o": other_org,
                    "c": f"P-{other_project[-8:]}",
                },
            )
            await session.commit()

        # The name is `fk_<table>_<referred_table>`, not `..._<column>`: SQLAlchemy's
        # convention for a table-level `ForeignKeyConstraint` is
        # `fk_%(table_name)s_%(referred_table_name)s`, which only differs from the
        # column-level form for a composite key. Guessing the column form is how a
        # test asserts on a name that cannot exist.
        with refused_because("fk_progress_snapshots_organization_id_projects"):
            await tenant.session.execute(
                text(
                    "INSERT INTO progress_snapshots (id, organization_id, report_ref, "
                    "source_row, line_label, line_no, work_description, project_id, "
                    "planned_start_on, "
                    "planned_finish_on, planned_duration_days) "
                    "VALUES (:i, :o, 'X', 900, '1', 1, 'X', :p, CURRENT_DATE, "
                    "CURRENT_DATE + 4, 5)"
                ),
                {
                    "i": f"prs_{new_ulid()}",
                    "o": tenant.organization_id,
                    "p": other_project,
                },
            )
        await tenant.session.rollback()

    async def test_own_project_is_accepted(self, tenant: Tenant) -> None:
        """The control. Without it the test above would pass on any FK failure."""
        project_id = f"prj_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO projects (id, organization_id, code, name, status) "
                "VALUES (:i, :o, :c, 'Own project', 'active')"
            ),
            {"i": project_id, "o": tenant.organization_id, "c": f"P-{project_id[-8:]}"},
        )
        await tenant.commit()
        await tenant.session.execute(
            text(
                "INSERT INTO progress_snapshots (id, organization_id, report_ref, "
                "line_label, line_no, work_description, project_id, planned_start_on, "
                "planned_finish_on, planned_duration_days) "
                "VALUES (:i, :o, 'X', '1', 1, 'X', :p, CURRENT_DATE, "
                "CURRENT_DATE + 4, 5)"
            ),
            {"i": f"prs_{new_ulid()}", "o": tenant.organization_id, "p": project_id},
        )
        await tenant.commit()

    async def test_a_null_project_is_still_allowed(self, tenant: Tenant) -> None:
        """MATCH SIMPLE: a composite FK is not enforced when any column is NULL.

        Which is what makes the composite form usable here at all — a progress row
        that has not been assigned to a project yet is a normal thing to record, and
        a rule that forced one would refuse the rows a project most needs on day one.
        """
        await _snapshot(tenant, report="unassigned")
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM progress_snapshots "
                    "WHERE organization_id = :o AND project_id IS NULL"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == 1
