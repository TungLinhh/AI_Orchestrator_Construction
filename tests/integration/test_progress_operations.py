"""The progress write path, against a live database.

This is the first domain area with a write path, and the tests here are organised
around the two things that make it trustworthy:

* **A refused reading does not become a row.** The whole point of the service is that
  a figure the system could not read stays out of a progress report, and a test that
  only checked the happy path would not know the difference.
* **The domain is re-checked on the way in, not trusted from the reader.** The reader
  is one caller; the service owns the table. A reading whose durations disagree with
  its dates is refused here even if it arrived with no refusals attached.

The corpus file is the acceptance criterion. `TestTheRealCorpusFileWrites` runs
`detect_sheet` → `read_progress` → `write_report` over
`TĐ BOH.xlsx :: TĐ .BOH` and asserts the counts the file implies.
"""

from __future__ import annotations

import datetime as dt
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import text

from ai_orchestrator.application.progress_operations import (
    item_progress,
    late_activities,
    report_exists,
    write_reading,
    write_report,
)
from ai_orchestrator.ingest.progress_reader import ProgressRead, ProgressReading
from ai_orchestrator.ingest.sheets import SheetKind, detect_sheet
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

D = dt.date


#: A counter for the fixture's `source_row`. That column is the reading's **identity**
#: within its report (migration `0018`), because no combination of what a reading says
#: is unique across the corpus -- `TĐ Hạ Tầng.xlsx` lists the same activity twice,
#: identically. A fixture that left it at 0 therefore put every row of a report on the
#: same key and the database correctly refused. Numbered from a counter rather than
#: defaulted, because the default is exactly the bug.
_ROW_SERIAL = count()


def _reading(**over: object) -> ProgressReading:
    """One valid reading, so a test states only what it varies."""
    base: dict[str, object] = {
        "source_row": next(_ROW_SERIAL) + 100,
        "line_label": "1",
        "line_no": 1,
        "work_description": "Lắp đặt đường ống nước",
        "planned_start_on": D(2019, 4, 17),
        "planned_finish_on": D(2019, 4, 26),
        "planned_duration_days": 10,
        "actual_start_on": D(2019, 4, 17),
        "actual_finish_on": D(2019, 4, 26),
        "actual_duration_days": 10,
        "actual_updated": True,
        "completion_ratio": 0.8,
        "status_text": "YES",
        "is_adequate": True,
    }
    base.update(over)
    return ProgressReading(**base)  # type: ignore[arg-type]


class TestACleanReadingIsWritten:
    async def test_a_reading_becomes_a_row(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, reasons = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(),
            )
        assert snapshot_id is not None
        assert reasons == ()

    async def test_the_row_round_trips(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, _ = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(completion_ratio=0.65, section_label="I. Cấp nước"),
            )
        row = (
            await tenant.session.execute(
                text(
                    "SELECT work_description, completion_ratio, section_label, "
                    "planned_duration_days, actual_updated FROM progress_snapshots "
                    "WHERE id = :i"
                ),
                {"i": snapshot_id},
            )
        ).one()
        assert row[0] == "Lắp đặt đường ống nước"
        assert float(row[1]) == 0.65
        assert row[2] == "I. Cấp nước"
        assert int(row[3]) == 10
        assert row[4] is True

    async def test_unicode_survives_the_jsonb_round_trip(self, tenant: Tenant) -> None:
        """`raw_cells` goes through `jsonb` with `ensure_ascii=False`.

        Worth a test because the failure would be invisible: escaped `\\u0111` is
        valid JSON and reads back as the same string, so the *only* symptom would be
        a corpus note full of escapes in a report somebody reads.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, _ = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(raw_cells={"5": "SSA", "6": "Bê tông HB"}),
            )
        stored = (
            await tenant.session.execute(
                text("SELECT raw_cells FROM progress_snapshots WHERE id = :i"),
                {"i": snapshot_id},
            )
        ).scalar()
        assert stored == {"5": "SSA", "6": "Bê tông HB"}
        assert "Bê tông" in str(stored), "the note must not be escaped into unreadability"


class TestARefusedReadingBecomesNoRow:
    """The rule the service exists for.

    An unreadable figure written as if it had been read is the same failure as
    writing `0` for an unparsed quantity: a value indistinguishable from a real one.
    """

    async def test_a_reading_carrying_refusals_is_not_written(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, reasons = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(
                    refusals=("completion_ratio: '65.0' is outside 0..1; not rescaled",)
                ),
            )
        assert snapshot_id is None
        assert "not rescaled" in reasons[0]
        count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM progress_snapshots WHERE organization_id = :o"),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == 0, "a refused reading must leave no row behind"

    async def test_the_durations_are_rechecked_here_not_trusted(self, tenant: Tenant) -> None:
        """The reader applies the rule; the service applies it again.

        A reading with an exclusive duration and **no** refusals attached — a
        hand-built one, a test double, a future caller that skipped the reader — is
        still refused. Otherwise the guarantee would hold only for the one caller
        that happens to check.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, reasons = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(planned_duration_days=9),
            )
        assert snapshot_id is None
        assert any("counted inclusively" in r for r in reasons), reasons

    async def test_an_actual_duration_disagreement_is_caught_too(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, reasons = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(actual_duration_days=11),
            )
        assert snapshot_id is None
        assert any("actual_duration_days" in r for r in reasons), reasons

    async def test_a_duration_with_no_dates_is_written(self, tenant: Tenant) -> None:
        """Absent data is not a disagreement.

        A reported duration on its own is a claim the sheet made; with nothing to
        check it against, refusing it would discard a number the file stated.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            snapshot_id, reasons = await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(
                    planned_start_on=None,
                    planned_finish_on=None,
                    actual_start_on=None,
                    actual_finish_on=None,
                    completion_ratio=0.4,
                ),
            )
        assert snapshot_id is not None, reasons


class TestWritingAWholeReport:
    @staticmethod
    def _read(*readings: ProgressReading, skipped: int = 0) -> ProgressRead:
        return ProgressRead(
            readings=readings,
            skipped_sections=skipped,
            period_label="Tuần 1/Week 1",
            observed_on=D(2019, 8, 1),
        )

    async def test_clean_rows_are_written_and_refused_ones_are_not(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            outcome = await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(
                    _reading(line_label="1", line_no=1),
                    _reading(
                        line_label="2",
                        line_no=2,
                        refusals=("completion_ratio: refused",),
                    ),
                    _reading(line_label="3", line_no=3),
                ),
                report_ref="BOH-W32",
            )
        assert outcome.written == 2
        assert len(outcome.refused) == 1
        assert outcome.refused[0][0] == "2"

    async def test_the_preamble_date_becomes_the_observation_date(self, tenant: Tenant) -> None:
        """The report's date is the report's, from the sheet — not today.

        `observed_on` is nullable precisely so a reading nobody dated is left undated
        rather than misdated, and defaulting it to the clock would destroy the
        distinction the column exists to hold.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            outcome = await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading()),
                report_ref="BOH-W32",
            )
        observed = (
            await tenant.session.execute(
                text(
                    "SELECT observed_on FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-W32'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert observed == D(2019, 8, 1)
        assert outcome.written == 1

    async def test_skipped_sections_are_reported_not_counted_as_written(
        self, tenant: Tenant
    ) -> None:
        """The written count has to be checkable against the file's own rows.

        Roll-ups and headings are skipped by the reader; if they were counted as
        written, a report would claim more rows than the sheet has activities.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            outcome = await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading(), skipped=12),
                report_ref="BOH-W32",
            )
        assert outcome.written == 1
        assert outcome.skipped_sections == 12

    async def test_an_explicit_observation_date_overrides_the_preamble(
        self, tenant: Tenant
    ) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading()),
                report_ref="BOH-W32",
                observed_on=D(2020, 1, 6),
            )
        observed = (
            await tenant.session.execute(
                text(
                    "SELECT observed_on FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-W32'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert observed == D(2020, 1, 6)

    async def test_replace_overwrites_a_previous_reading_of_the_same_report(
        self, tenant: Tenant
    ) -> None:
        """A corrected file re-read should correct the rows, not accumulate them.

        Off by default, because a silent overwrite of 34 rows on a re-run is the kind
        of thing an operator should ask for.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading(line_label="1", line_no=1, completion_ratio=0.5)),
                report_ref="BOH-W32",
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading(line_label="1", line_no=1, completion_ratio=0.9)),
                report_ref="BOH-W32",
                replace=True,
            )
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT completion_ratio FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-W32'"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert len(rows) == 1, "replace must not leave the old row behind"
        assert float(rows[0][0]) == 0.9

    async def test_without_replace_a_second_read_of_the_same_line_is_refused(
        self, tenant: Tenant
    ) -> None:
        """A re-read cannot silently double a report, and it cannot silently
        overwrite one either — it is refused, and the caller is told to pass
        `replace=True`.

        I predicted accumulation here and was wrong: the unique index on
        `(organization_id, report_ref, section_label, line_label)` forbids it. The
        index is right and my expectation was not, and the first version of this test
        asserted `count == 2` behind an `if False else True`, which made it pass
        without asserting anything at all — the F87 shape, written by the person who
        wrote F87.
        """

        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(_reading(line_label="1", line_no=1, source_row=10)),
                report_ref="BOH-W32",
            )
            with pytest.raises(Exception) as excinfo:
                # `source_row=10` again: this is the *same* reading written twice, and
                # the row is its identity since `0018`. The fixture's counter would
                # otherwise hand out a fresh row and the two writes would be two
                # different readings, which are perfectly legal together.
                await write_report(
                    conn,
                    organization_id=tenant.organization_id,
                    read=self._read(_reading(line_label="1", line_no=1, source_row=10)),
                    report_ref="BOH-W32",
                )
        assert "uq_progress_snapshots_org_report_row" in str(excinfo.value), (
            "the refusal must name the constraint, so the caller knows to pass "
            f"replace=True rather than to guess. Got: {excinfo.value}"
        )

    async def test_the_same_label_in_two_sections_is_two_rows(self, tenant: Tenant) -> None:
        """The finding this migration exists for, as behaviour.

        Seven system headings each number their activities from 1, so `1` under
        `Zone A` and `1` under `Hệ thống cấp nước` are different activities. The real
        file has 34 of them and only 12 distinct labels, so a key without the section
        cannot hold a real report.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            outcome = await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=self._read(
                    _reading(line_label="1", line_no=1, section_label="Zone A"),
                    _reading(line_label="1", line_no=1, section_label="Hệ thống cấp nước"),
                ),
                report_ref="BOH-W32",
            )
        assert outcome.written == 2, "the same label in two sections is two activities"
        assert outcome.refused == ()


class TestAskingWhetherAReportExists:
    async def test_false_before_and_true_after(self, tenant: Tenant) -> None:
        """So an operator can be asked before 34 rows are overwritten."""
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            assert (
                await report_exists(conn, organization_id=tenant.organization_id, report_ref="R1")
                is False
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(),
            )
            assert (
                await report_exists(conn, organization_id=tenant.organization_id, report_ref="R1")
                is True
            )


class TestTheQueriesTheTableExistsFor:
    async def test_late_activities_are_ordered_most_late_first(self, tenant: Tenant) -> None:
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(
                    line_label="1",
                    line_no=1,
                    planned_start_on=D(2019, 4, 1),
                    planned_finish_on=D(2019, 4, 10),
                    planned_duration_days=10,
                    actual_start_on=D(2019, 4, 5),
                    actual_finish_on=D(2019, 4, 20),
                    actual_duration_days=16,
                ),
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(
                    line_label="2",
                    line_no=2,
                    planned_start_on=D(2019, 5, 1),
                    planned_finish_on=D(2019, 5, 10),
                    planned_duration_days=10,
                    actual_start_on=D(2019, 5, 1),
                    actual_finish_on=D(2019, 5, 30),
                    actual_duration_days=30,
                ),
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(line_label="3", line_no=3),
            )
            rows = await late_activities(conn, organization_id=tenant.organization_id)
        assert [(r["line_label"], r["days_late"], r["variance_days"]) for r in rows] == [
            ("2", 20, 20),
            ("1", 10, 6),
        ], "on time is absent from a lateness report"

    async def test_a_row_whose_actuals_were_never_recorded_is_not_listed(
        self, tenant: Tenant
    ) -> None:
        """The reason `actual_updated` exists, exercised at the query.

        Its variance is zero, so without the filter an unmeasured project reports as
        entirely on schedule — which is the reading this table is for.
        """
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(actual_updated=False),
            )
            rows = await late_activities(conn, organization_id=tenant.organization_id)
        assert rows == [], "zero variance from an unrecorded actual is not 'on time'"

    async def test_item_progress_returns_rows_in_line_order(self, tenant: Tenant) -> None:
        item_id = await _wbs_item(tenant)
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            for line, label in ((2, "2"), (1, "1")):
                await write_reading(
                    conn,
                    organization_id=tenant.organization_id,
                    report_ref="R1",
                    reading=_reading(line_label=label, line_no=line),
                    wbs_item_id=item_id,
                )
            rows = await item_progress(
                conn, organization_id=tenant.organization_id, wbs_item_id=item_id
            )
        assert [r["line_label"] for r in rows] == ["1", "2"]

    async def test_item_progress_leaves_variance_as_none_when_absent(self, tenant: Tenant) -> None:
        """Not coalesced to zero.

        An activity with no actual duration has no variance; zero is a claim that it
        finished on time, and the two must not share a value.
        """
        item_id = await _wbs_item(tenant)
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_reading(
                conn,
                organization_id=tenant.organization_id,
                report_ref="R1",
                reading=_reading(
                    actual_start_on=None,
                    actual_finish_on=None,
                    actual_duration_days=None,
                    actual_updated=None,
                ),
                wbs_item_id=item_id,
            )
            rows = await item_progress(
                conn, organization_id=tenant.organization_id, wbs_item_id=item_id
            )
        assert rows[0]["variance_days"] is None


async def _wbs_item(tenant: Tenant) -> str:
    """A WBS item in this tenant, since `wbs_item_id` is a composite foreign key."""
    await tenant.session.execute(
        text(
            "INSERT INTO units_dictionary (organization_id, code, name_vi, dimension) "
            "VALUES (:o, 'cai', 'Cái', 'count')"
        ),
        {"o": tenant.organization_id},
    )
    project_id = f"prj_{new_ulid_for_test()}"
    wbs_id = f"wbs_{new_ulid_for_test()}"
    item_id = f"wbi_{new_ulid_for_test()}"
    await tenant.session.execute(
        text(
            "INSERT INTO projects (id, organization_id, code, name, status) "
            "VALUES (:p, :o, :pc, 'P', 'active')"
        ),
        {"p": project_id, "o": tenant.organization_id, "pc": f"P-{project_id[-8:]}"},
    )
    await tenant.session.execute(
        text(
            "INSERT INTO wbs (id, organization_id, project_id, code, name) "
            "VALUES (:w, :o, :p, 'W1', 'WBS 1')"
        ),
        {"w": wbs_id, "o": tenant.organization_id, "p": project_id},
    )
    await tenant.session.execute(
        text(
            "INSERT INTO wbs_items (id, organization_id, wbs_id, code, description, "
            "unit_code, quantity, unit_rate, amount) "
            "VALUES (:i, :o, :w, '1.1', 'Item', 'cai', 1, 100, 100)"
        ),
        {"i": item_id, "o": tenant.organization_id, "w": wbs_id},
    )
    await tenant.session.commit()
    return item_id


def new_ulid_for_test() -> str:
    from ai_orchestrator.domain.ids import new_ulid

    return new_ulid()


# ---------------------------------------------------------------------------
# The real corpus file, end to end.
# ---------------------------------------------------------------------------

CORPUS_ROOT = Path("/home/vutun/pmo_project/reference_sheets")
NEEDS_FILE = pytest.mark.skipif(
    not CORPUS_ROOT.is_dir(),
    reason="the reference corpus is not present on this machine",
)


@NEEDS_FILE
class TestTheRealCorpusFileWrites:
    """detect → read → write, over the only real example of this document.

    Everything else in this file is synthetic. This class is the acceptance
    criterion: if the counts below are right, the vertical slice works on real data
    and the schema, the reader and the service agree with each other and with the file.
    """

    @staticmethod
    def _read_the_file():
        import warnings

        warnings.filterwarnings("ignore")
        import openpyxl

        from ai_orchestrator.ingest.progress_reader import read_progress

        matches = sorted(CORPUS_ROOT.rglob("TĐ BOH.xlsx"))
        book = openpyxl.load_workbook(matches[0], read_only=True, data_only=True)
        try:
            sheet = book.worksheets[0]
            rows = [tuple(r) for r in sheet.iter_rows(max_row=60, max_col=20, values_only=True)]
        finally:
            book.close()
        shapes = detect_sheet(rows)
        shape = next(s for s in shapes.shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS)
        return read_progress(rows, shape)

    async def test_the_file_writes_every_activity_it_contains(self, tenant: Tenant) -> None:
        read = self._read_the_file()
        assert read.readings, "the corpus file produced no activities"
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            outcome = await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=read,
                report_ref="BOH-TD1",
            )
        assert outcome.written == len(read.readings), (
            f"the file has {len(read.readings)} activities and {outcome.written} "
            f"were written; refusals: {outcome.refused[:3]}"
        )
        assert outcome.refused == (), "the real file is well behaved and writes clean"
        assert outcome.report_ref == "BOH-TD1"

    async def test_the_written_report_carries_the_period_and_date(self, tenant: Tenant) -> None:
        """`report_ref` is only derivable because the preamble was read."""
        read = self._read_the_file()
        assert read.period_label == "Tuần 1/Week 1"
        assert read.observed_on == D(2019, 8, 1)
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=read,
                report_ref="BOH-TD1",
            )
        row = (
            await tenant.session.execute(
                text(
                    "SELECT period_label, observed_on FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-TD1' LIMIT 1"
                ),
                {"o": tenant.organization_id},
            )
        ).one()
        assert row[0] == "Tuần 1/Week 1"
        assert row[1] == D(2019, 8, 1)

    async def test_every_written_duration_agrees_with_its_dates(self, tenant: Tenant) -> None:
        """Inclusive counting, on the file, verified through the write path.

        Not through the reader — the reader's own check could be wrong in the same
        direction twice. Read the rows back and compare against the dates.
        """
        read = self._read_the_file()
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=read,
                report_ref="BOH-TD1",
            )
        rows = (
            await tenant.session.execute(
                text(
                    "SELECT planned_duration_days, "
                    "planned_finish_on - planned_start_on + 1 "
                    "FROM progress_snapshots WHERE organization_id = :o "
                    "AND report_ref = 'BOH-TD1' "
                    "AND planned_start_on IS NOT NULL AND planned_finish_on IS NOT NULL"
                ),
                {"o": tenant.organization_id},
            )
        ).all()
        assert rows, "no dated rows were written"
        for stated, computed in rows:
            assert int(stated) == int(computed), (
                f"a written duration {stated} disagrees with its dates ({computed})"
            )

    async def test_no_actual_is_reported_as_ever_recorded(self, tenant: Tenant) -> None:
        """The finding the corpus forced, surviving the whole path.

        Every data row in this file has planned dates identical to its actual, so a
        lateness query over the written report must return nothing — and must return
        nothing *because* `actual_updated` is false, not by accident.
        """
        read = self._read_the_file()
        async with tenant.db.engine.begin() as conn:
            await conn.execute(
                text("SELECT set_config('app.current_tenant', :o, true)"),
                {"o": tenant.organization_id},
            )
            await write_report(
                conn,
                organization_id=tenant.organization_id,
                read=read,
                report_ref="BOH-TD1",
            )
            late = await late_activities(conn, organization_id=tenant.organization_id)
        assert late == [], (
            "this file's actual columns were never filled in, so no activity is late "
            "and none of them is evidence of being on time"
        )
        count = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM progress_snapshots "
                    "WHERE organization_id = :o AND report_ref = 'BOH-TD1' "
                    "AND actual_updated IS FALSE"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar()
        assert count == len(read.readings)
