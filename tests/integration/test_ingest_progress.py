"""The progress reader, and the corpus file it was designed from.

`TĐ BOH.xlsx :: TĐ .BOH` is the only sheet in the corpus with a `KH`/`TT` pair, and
it is the sole basis for the inclusive day count, the fraction-shaped completion
figure, and the two-level row structure. `TestAgainstTheRealSheet` runs the whole
path over it — detect, classify, read — and the five transcribed rows in
`TestTheMeasuredRows` are the same data with the arithmetic pinned.

The rest is synthetic, and the reason is stated in each class: the corpus cannot
exercise the cases that matter because the one real file is well behaved in all of
them.
"""

from __future__ import annotations

import datetime as dt
import warnings
from pathlib import Path

import pytest

from ai_orchestrator.ingest.progress_reader import ProgressReading, read_progress
from ai_orchestrator.ingest.sheets import (
    COLUMN_ROLES,
    SheetKind,
    detect_sheet,
    normalise,
    role_for,
)

HEADER = (
    "Stt/No",
    "Công việc thi công/Work",
    "% Hoàn thành/ completed Percentage",
    "Tình trạng",
    "Ngày bắt đầu KH/Beginning of day",
    "Ngày bắt đầu TT",
    "Ngày kết thúc Kh/End of day",
    "Ngày kết thúc TT",
    "Số ngày  KH",
    "Số ngày  TT",
    "HPNC KH",
    "HPNC TT",
    "Tháng",
)

D = dt.date


#: Column order, once. Every synthetic row in this file is thirteen positional
#: values, and writing them out longhand is how a test ends up with the date in the
#: completion column and no error anywhere. Named arguments against one order is the
#: cheapest defence, and it also means a row that deliberately leaves a column empty
#: says so by omission instead of by a run of `None`s.
def _row(
    # Defaulted because a preamble row has an empty `Stt/No` by definition --
    # that is what makes it a preamble rather than data.
    label: object = "",
    work: object = None,
    completion: object = None,
    status: object = None,
    p_start: object = None,
    a_start: object = None,
    p_finish: object = None,
    a_finish: object = None,
    p_days: object = None,
    a_days: object = None,
    handover_p: object = None,
    handover_a: object = None,
    period: object = None,
) -> tuple[object, ...]:
    return (
        label,
        work,
        completion,
        status,
        p_start,
        a_start,
        p_finish,
        a_finish,
        p_days,
        a_days,
        handover_p,
        handover_a,
        period,
    )


def _sheet(*data: tuple[object, ...]) -> tuple[list[tuple[object, ...]], object]:
    rows: list[tuple[object, ...]] = [HEADER, *data]
    found = detect_sheet(rows)
    assert found.shapes, found.rejected
    return rows, found.shapes[0]


#: The five data rows of `TĐ BOH.xlsx :: TĐ .BOH`, verbatim. The section rows
#: (`A`/`BOH`, `I`/`Hệ thống cấp`) are in the file and are deliberately absent here
#: so the arithmetic assertions are not diluted; `TestAgainstTheRealSheet` reads
#: them from the file itself.
MEASURED = [
    ("1", "Bể nước sinh hoạt", 0.65, D(2019, 3, 13), D(2019, 3, 14), 2, 2),
    ("2", "Lắp đặt đường ống", 0.8, D(2019, 4, 17), D(2019, 4, 26), 10, 6),
    ("3", "Bể STP: Lắp đặt", 0.9, D(2019, 4, 1), D(2019, 4, 20), 20, 6),
    ("4", "Lắp đặt đường ống", 0.0, D(2019, 6, 30), D(2019, 7, 30), 31, 6),
    ("5", "Lắp đặt bơm nước", 0.0, D(2019, 8, 20), D(2019, 9, 3), 15, 3),
]


def _measured_rows() -> list[tuple[object, ...]]:
    out: list[tuple[object, ...]] = []
    for label, work, completion, start, finish, days, handover in MEASURED:
        out.append(
            (
                label,
                work,
                completion,
                "YES",
                start,
                start,  # the file's actual column is identical to the planned one
                finish,
                finish,
                days,
                days,
                handover,
                handover,
                0,
            )
        )
    return out


class TestTheHeaderIsMappedExactly:
    """Fourteen columns, thirteen roles, and one that is not ours.

    The mapping is asserted label by label because a wrong role here does not raise
    — it writes a planned date into an actual column and the variance comes out zero
    forever.
    """

    @pytest.mark.parametrize(
        ("label", "role"),
        [
            ("Stt/No", "line_no"),
            ("Công việc thi công/Work", "work_description"),
            ("% Hoàn thành/ completed Percentage", "completion_ratio"),
            ("Tình trạng", "status_text"),
            ("Ngày bắt đầu KH/Beginning of day", "planned_start_on"),
            ("Ngày bắt đầu TT", "actual_start_on"),
            ("Ngày kết thúc Kh/End of day", "planned_finish_on"),
            ("Ngày kết thúc TT", "actual_finish_on"),
            ("Số ngày  KH", "planned_duration_days"),
            ("Số ngày  TT", "actual_duration_days"),
            ("HPNC KH", "handover_planned"),
            ("HPNC TT", "handover_actual"),
            ("Tháng", "period_label"),
        ],
    )
    def test_each_measured_label_resolves(self, label: str, role: str) -> None:
        assert role_for(label) == role

    def test_the_lowercase_kh_still_resolves(self) -> None:
        """`Ngày kết thúc Kh` in the same row as `Ngày bắt đầu KH`.

        The `Bộ`/`bộ` defect one level up, and the reason `normalise` lowercases
        before matching rather than after.
        """
        assert normalise("Ngày kết thúc Kh") == "ngay ket thuc kh"
        assert normalise("Ngày bắt đầu KH") == "ngay bat dau kh"
        assert role_for("Ngày kết thúc Kh") == "planned_finish_on"
        assert role_for("Ngày bắt đầu KH") == "planned_start_on"

    def test_the_planned_and_actual_halves_are_distinct_roles(self) -> None:
        """Not one role with a suffix.

        The design of `progress_snapshots` is that both halves live on one row, and a
        reader that merged them would have to split them again to fill it.
        """
        assert role_for("Số ngày  KH") != role_for("Số ngày  TT")
        assert role_for("Ngày bắt đầu KH") != role_for("Ngày bắt đầu TT")

    def test_every_vocabulary_fragment_is_already_normalised(self) -> None:
        unnormalised = [f for frags in COLUMN_ROLES.values() for f in frags if normalise(f) != f]
        assert not unnormalised, f"fragments that can never match: {unnormalised}"

    def test_the_sheet_is_classified_as_construction_progress(self) -> None:
        _rows, shape = _sheet(*_measured_rows())
        assert shape.kind is SheetKind.CONSTRUCTION_PROGRESS

    def test_a_price_schedule_is_not_mistaken_for_progress(self) -> None:
        """The two families share no columns, and the classifier must not blur them."""
        rows = [
            ("STT", "Tên hàng hoá", "Số lượng", "Đơn vị", "Đơn giá (VND)"),
            ("1", "Cảm biến", "24", "cái", "1.250.000"),
        ]
        assert detect_sheet(rows).kinds == (SheetKind.PRICE_SCHEDULE,)


class TestTheMeasuredRows:
    """The five rows from the file, with the arithmetic pinned."""

    def test_all_five_read_with_no_refusals(self) -> None:
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert len(result.readings) == 5
        assert all(r.is_clean() for r in result.readings), [
            r.refusals for r in result.readings if r.refusals
        ]

    def test_the_durations_reproduce_the_file(self) -> None:
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert [r.planned_duration_days for r in result.readings] == [m[5] for m in MEASURED]

    def test_the_dates_reproduce_the_file(self) -> None:
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert [r.planned_start_on for r in result.readings] == [m[3] for m in MEASURED]
        assert [r.planned_finish_on for r in result.readings] == [m[4] for m in MEASURED]

    def test_the_completion_figures_stay_fractions(self) -> None:
        """`0.65` stays `0.65` and is never turned into 65 or into 0.65%."""
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert [r.completion_ratio for r in result.readings] == [m[2] for m in MEASURED]

    def test_the_handover_integers_are_kept_without_a_name(self) -> None:
        """`HPNC` values are recorded and their meaning is not invented."""
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert [r.handover_planned for r in result.readings] == [m[6] for m in MEASURED]

    def test_the_line_labels_are_the_file_s_own_text(self) -> None:
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert [r.line_label for r in result.readings] == ["1", "2", "3", "4", "5"]


class TestSectionHeadingsAreNotObservations:
    """The sheets are two-level: `A`/`BOH`, then `I`/`Hệ thống`, then activities."""

    def test_a_section_row_is_skipped_and_counted(self) -> None:
        rows, shape = _sheet(
            _row("A", "BOH"),
            _row("I", "Hệ thống cấp nước"),
            *_measured_rows()[:2],
        )
        result = read_progress(rows, shape)
        assert len(result.readings) == 2
        assert result.skipped_sections == 2

    def test_an_activity_inherits_its_section(self) -> None:
        """Otherwise every activity is parentless.

        A progress report reads as a tree, and a list of nine pipe-laying tasks with
        no indication they are all under one system heading is nine tasks.
        """
        rows, shape = _sheet(
            _row("I", "Hệ thống cấp nước"),
            *_measured_rows()[:2],
        )
        result = read_progress(rows, shape)
        assert all(r.section_label == "Hệ thống cấp nước" for r in result.readings)

    def test_a_later_section_replaces_the_earlier_one(self) -> None:
        rows, shape = _sheet(
            _row("I", "Hệ thống cấp nước"),
            *_measured_rows()[:1],
            _row("II", "Hệ thống cấp điện"),
            *_measured_rows()[1:2],
        )
        result = read_progress(rows, shape)
        assert [r.section_label for r in result.readings] == [
            "Hệ thống cấp nước",
            "Hệ thống cấp điện",
        ]

    def test_a_costed_row_is_an_activity_however_little_else_it_has(self) -> None:
        """The discriminator is the duration, not the amount of data present.

        This test asserted "a row with a planned start is an activity" and was wrong
        twice: once because the rule was "no window", and again because the rule is
        "no `Số ngày`". A row carrying a duration is an activity even with no
        completion figure, no status and no actual half at all.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Bắt đầu mới",
                p_start=D(2019, 5, 1),
                p_finish=D(2019, 5, 10),
                p_days=10,
            )
        )
        result = read_progress(rows, shape)
        assert len(result.readings) == 1
        assert result.skipped_sections == 0

    def test_a_window_with_no_duration_is_a_rollup_and_is_counted(self) -> None:
        """The rule, and the case that is genuinely ambiguous.

        A row with a window and no `Số ngày` is either a roll-up or an activity
        nobody costed, and the corpus contains both signals for the first reading and
        no way to tell them apart. It is filed as a roll-up and *counted*, so the
        number of rows that went that way is visible rather than assumed.
        """
        rows, shape = _sheet(
            _row(
                "2",
                "Hệ thống thông gió và điều hòa",
                p_start=D(2019, 5, 21),
                a_start=D(2019, 5, 21),
                p_finish=D(2019, 9, 20),
                a_finish=D(2019, 9, 20),
            )
        )
        result = read_progress(rows, shape)
        assert not result.readings, (
            "a numeric label is not protection: the corpus numbers its system "
            "headings 1, 2, I and A interchangeably"
        )
        assert result.skipped_sections == 1


class TestActualUpdatedIsInferredNotAssumed:
    """The finding that made the column necessary."""

    def test_identical_planned_and_actual_dates_mean_never_updated(self) -> None:
        """The file's own state, on all five of its rows.

        A variance computed from this is zero for every activity, which is not "on
        time" — it is "nobody filled in the actual column".
        """
        rows, shape = _sheet(*_measured_rows())
        result = read_progress(rows, shape)
        assert all(r.actual_updated is False for r in result.readings)
        assert result.as_dict()["rows_where_actual_was_never_updated"] == 5

    def test_a_genuine_update_is_recognised(self) -> None:
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=D(2019, 4, 17),
                a_start=D(2019, 4, 20),
                p_finish=D(2019, 4, 26),
                a_finish=D(2019, 4, 29),
                p_days=10,
                a_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.actual_updated is True

    def test_the_same_day_written_two_ways_is_still_not_an_update(self) -> None:
        """A re-typed date is not a reading of progress.

        Comparing raw text would call this an update; comparing the parsed days
        correctly does not, and nobody who reformatted a column recorded anything.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=D(2019, 4, 17),
                a_start="17/04/2019",
                p_finish=D(2019, 4, 26),
                a_finish="26/04/2019",
                p_days=10,
                a_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.actual_updated is False
        assert reading.actual_start_on == reading.planned_start_on == D(2019, 4, 17)

    def test_a_planned_activity_with_no_actual_is_not_updated(self) -> None:
        """`False`, and this is the useful answer.

        The plan is filled and the actual column is empty. That is a gap in the
        record, not an unknown — "nobody wrote down what happened" and "we do not
        know what happened" are different, and only the first is true here.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Mới",
                completion=0.0,
                status="YES",
                p_start=D(2019, 5, 1),
                p_finish=D(2019, 5, 10),
                p_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.actual_updated is False

    def test_a_row_with_neither_plan_nor_actual_is_unknown(self) -> None:
        """`None`, and only here.

        With nothing on either side there is nothing to compare, and reporting
        `False` would file a brand-new activity as a reporting failure.
        """
        rows, shape = _sheet(
            ("1", "Mới", 0.9, "YES", None, None, D(2019, 6, 1), None, None, None, 6, 6, 0)
        )
        result = read_progress(rows, shape)
        assert not result.readings, "a row with no window and a duration is a roll-up"
        assert result.skipped_sections == 1


class TestRefusalsRatherThanCorrections:
    def test_an_exclusive_duration_is_reported_not_fixed(self) -> None:
        """Nine is what `finish - start` returns. The corpus says ten."""
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=D(2019, 4, 17),
                a_start=D(2019, 4, 17),
                p_finish=D(2019, 4, 26),
                a_finish=D(2019, 4, 26),
                p_days=9,
                a_days=9,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.planned_duration_days == 9, "the file's number is kept"
        assert any("counted inclusively" in r for r in reading.refusals)
        assert not reading.is_clean()

    def test_a_completion_above_one_is_refused_not_rescaled(self) -> None:
        """A genuine `65` and a mis-keyed `0.65` are indistinguishable.

        Dividing by 100 would silently pick one and be wrong about the other, so the
        value is dropped and the row says why.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=65.0,
                status="YES",
                p_start=D(2019, 4, 17),
                a_start=D(2019, 4, 17),
                p_finish=D(2019, 4, 26),
                a_finish=D(2019, 4, 26),
                p_days=10,
                a_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.completion_ratio is None
        assert any("not rescaled automatically" in r for r in reading.refusals)

    def test_an_unreadable_date_is_refused_rather_than_guessed(self) -> None:
        """A serial number in a date column is a spreadsheet artefact or a typo.

        Converting it would yield a date that is confidently wrong, which is worse
        than a row that says it could not read the cell.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=43466,
                p_finish=D(2019, 4, 26),
                p_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.planned_start_on is None
        assert any("not a date in any known format" in r for r in reading.refusals)

    def test_a_fractional_duration_is_refused(self) -> None:
        """`Số ngày` is whole days in the corpus; 2.5 is a different convention."""
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=D(2019, 4, 17),
                p_finish=D(2019, 4, 26),
                p_days=2.5,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.planned_duration_days is None
        assert any("not a whole number of days" in r for r in reading.refusals)


class TestDatesArriveInThreeShapes:
    """`openpyxl` returns a `datetime`, a string, or `None` in the same column."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (D(2019, 4, 17), D(2019, 4, 17)),
            (dt.datetime(2019, 4, 17, 8, 30), D(2019, 4, 17)),
            ("2019-04-17", D(2019, 4, 17)),
            ("17/04/2019", D(2019, 4, 17)),
            ("17-04-2019", D(2019, 4, 17)),
            ("17.04.2019", D(2019, 4, 17)),
            ("", None),
            (None, None),
        ],
    )
    def test_every_shape_a_date_column_holds_is_read(self, value: object, expected: object) -> None:
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start=value,
                p_finish=D(2019, 4, 26),
                p_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.planned_start_on == expected

    def test_an_ambiguous_numeric_date_is_refused(self) -> None:
        """`04/05/2019` is 4 May or 5 April and the corpus is Vietnamese.

        Day-first is the right default for this corpus, and it is applied
        deliberately rather than by a library's guess. A cell that is genuinely
        ambiguous is the reader's to refuse, not to resolve.
        """
        rows, shape = _sheet(
            _row(
                "1",
                "Lắp đặt",
                completion=0.8,
                status="YES",
                p_start="05/04/2019",
                p_finish=D(2019, 4, 26),
                p_days=10,
            )
        )
        (reading,) = read_progress(rows, shape).readings
        assert reading.planned_start_on == D(2019, 4, 5), "day-first, as the corpus writes"


class TestTheReaderRefusesTheWrongShape:
    def test_a_price_schedule_is_rejected_outright(self) -> None:
        """Rather than returning zero rows that look like an empty sheet."""
        rows = [
            ("STT", "Tên hàng hoá", "Số lượng", "Đơn vị", "Đơn giá (VND)"),
            ("1", "Cảm biến", "24", "cái", "1.250.000"),
        ]
        shape = detect_sheet(rows).shapes[0]
        with pytest.raises(ValueError, match="reads construction_progress"):
            read_progress(rows, shape)


# ---------------------------------------------------------------------------
# The real file.
# ---------------------------------------------------------------------------

CORPUS_ROOT = Path("/home/vutun/pmo_project/reference_sheets")


def _find_sheet_file() -> Path | None:
    matches = sorted(CORPUS_ROOT.rglob("TĐ BOH.xlsx")) if CORPUS_ROOT.is_dir() else []
    return matches[0] if matches else None


NEEDS_FILE = pytest.mark.skipif(
    _find_sheet_file() is None,
    reason="the reference corpus is not present on this machine",
)


@NEEDS_FILE
class TestAgainstTheRealSheet:
    """The whole path, over the only real example of this document.

    Everything above is transcribed from this file. This class reads it.
    """

    @staticmethod
    def _rows() -> tuple[str, list[tuple[object, ...]]]:
        warnings.filterwarnings("ignore")
        import openpyxl

        book = openpyxl.load_workbook(_find_sheet_file(), read_only=True, data_only=True)
        try:
            sheet = book.worksheets[0]
            return sheet.title, [
                tuple(r) for r in sheet.iter_rows(max_row=60, max_col=20, values_only=True)
            ]
        finally:
            book.close()

    def test_the_header_is_found_at_the_measured_row(self) -> None:
        title, rows = self._rows()
        found = detect_sheet(rows)
        progress = [s for s in found.shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS]
        assert progress, f"no progress table in '{title}'; rejected={found.rejected}"
        assert progress[0].header_row == 14, "the measured header row"

    def test_all_eight_kh_tt_columns_are_mapped(self) -> None:
        """Four dates and two durations, planned and actual.

        Missing one of the six means every variance the report shows is wrong in a
        way that looks entirely reasonable.
        """
        _title, rows = self._rows()
        shape = next(
            s for s in detect_sheet(rows).shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS
        )
        for role in (
            "planned_start_on",
            "actual_start_on",
            "planned_finish_on",
            "actual_finish_on",
            "planned_duration_days",
            "actual_duration_days",
        ):
            assert shape.column_index(role) is not None, f"{role} was not mapped"

    def test_the_five_measured_rows_are_read_cleanly(self) -> None:
        _title, rows = self._rows()
        shape = next(
            s for s in detect_sheet(rows).shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS
        )
        result = read_progress(rows, shape)
        assert len(result.readings) >= 5
        assert all(r.is_clean() for r in result.readings), [
            (r.line_label, r.refusals) for r in result.readings if r.refusals
        ][:3]

    def test_the_real_file_reports_that_no_actual_was_ever_recorded(self) -> None:
        """The measurement, read from the file rather than asserted in a comment.

        Every data row in this file has planned dates identical to its actual. A
        reader that did not notice would file five activities as on time.
        """
        _title, rows = self._rows()
        shape = next(
            s for s in detect_sheet(rows).shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS
        )
        result = read_progress(rows, shape)
        assert result.as_dict()["rows_where_actual_was_never_updated"] == len(result.readings)

    def test_the_real_durations_match_the_real_dates(self) -> None:
        """Inclusive counting, on the file, five rows or however many it has."""
        _title, rows = self._rows()
        shape = next(
            s for s in detect_sheet(rows).shapes if s.kind is SheetKind.CONSTRUCTION_PROGRESS
        )
        for r in read_progress(rows, shape).readings:
            if r.planned_start_on and r.planned_finish_on:
                assert (
                    r.planned_duration_days == (r.planned_finish_on - r.planned_start_on).days + 1
                ), f"row {r.line_label}: {r.refusals}"


class TestTheReaderAndTheTableAgreeOnNames:
    """The seam where a reader's output becomes a row, checked before anything is written.

    A rename on either side of this boundary is silent. The reader builds a
    `ProgressReading`, something later maps it onto a row, and a field called
    `planned_start` that no column accepts is either dropped or — worse — matched to
    `planned_start_on` by a hand-written mapping nobody re-reads. Neither dataclass is
    wrong on its own, so nothing raises at the point of the mistake.

    The check is a name comparison rather than a behaviour test, and that is
    deliberate: the write path does not exist yet (`PRODUCT_GAP.md` §4b), so this is
    what stands in for it until it does.
    """

    #: Reader fields that deliberately have no column, and why. Named explicitly so
    #: the asymmetry is accounted for rather than discovered later.
    NOT_COLUMNS = {
        # A refusal is not data. It is the reader saying "I could not understand
        # this cell", and a row carrying one would put an unreadable figure into a
        # progress report as though it had been read. The write path is expected to
        # route a row with refusals to a review queue instead of to the table — which
        #: is the same decision `ingest/reader.py` makes for a price schedule.
        "refusals",
    }

    def test_every_reader_field_has_a_column(self) -> None:
        import dataclasses

        from ai_orchestrator.persistence.progress import ProgressSnapshot

        columns = set(ProgressSnapshot.__table__.columns.keys())
        produced = {
            f.name for f in dataclasses.fields(ProgressReading) if f.name not in self.NOT_COLUMNS
        }
        missing = sorted(produced - columns)
        assert not missing, (
            f"the reader produces these and the table has no column for them: {missing}"
        )

    def test_every_excluded_field_is_still_accounted_for(self) -> None:
        """An exclusion list that grows silently is an exclusion list that lies.

        `refusals` is the only member and it is a deliberate decision, so the count is
        pinned: a second exclusion needs a sentence saying why, which is the point of
        writing the number down.
        """
        unexpected = sorted(self.NOT_COLUMNS - {"refusals"})
        assert not unexpected, (
            f"reader fields excluded from the column check without a reason: {unexpected}"
        )

    def test_every_progress_column_is_produced_by_the_reader(self) -> None:
        """The other direction: a column nothing fills is a comment with a type.

        `actual_updated`, `handover_planned` and `item_completion_ratio` are the three
        most likely to be declared and then never populated.
        """
        import dataclasses

        from ai_orchestrator.persistence.progress import ProgressSnapshot

        produced = {f.name for f in dataclasses.fields(ProgressReading)}
        # Columns the write path owns rather than the reader: identity, provenance,
        # the row's position in its report, and the pointers into the project and WBS.
        owned_elsewhere = {
            "id",
            "organization_id",
            "source",
            "source_actor",
            "proposal_id",
            "created_at",
            "updated_at",
            "report_ref",
            "line_label",
            "line_no",
            "project_id",
            "wbs_item_id",
            # Added by migration `0017`, and the only column here owned by a service
            # *other than* `write_reading`. A reading is written before the WBS tree
            # that contains it exists -- the sheet is read first, and the tree is
            # derived from the same read -- so `wbs_operations.write_structure` fills
            # this in afterwards, matching on `(report_ref, line_label)`. Listed here
            # rather than produced by the reader because the reader has no tree to
            # point at.
            "wbs_id",
            "item_ref",
            "observed_on",
        }
        unfilled = sorted(
            set(ProgressSnapshot.__table__.columns.keys()) - produced - owned_elsewhere
        )
        assert not unfilled, f"columns no reader field populates: {unfilled}"


class TestThePeriodLivesInThePreamble:
    """The report's period and date are rows *above* the data, not a column beside it.

        Measured from `TĐ BOH.xlsx :: TĐ .BOH`:

            row 14   Stt/No | Công việc thi công | ...          header
            row 15   Tuần 1/Week 1                               the period
            row 16   2019-08-01                                  the report date
            row 17   A   BOH   2019-03-13 -> 2019-09-20           the project roll-up
            row 20   1   Bể nước sinh hoạt ...   period = 0     an activity

    The first version read `period_label` off each activity row and got `0` on all
    thirty-five of them. That is a month counter nobody advances, and storing
    thirty-five identical zeros makes a column look populated while carrying nothing.
    """

    def _with_preamble(self, *tail: tuple[object, ...]) -> list[tuple[object, ...]]:
        """A sheet whose two preamble rows carry the period and the date."""
        return [HEADER, _row(period="Tuần 1/Week 1"), _row(period="2019-08-01"), *tail]

    def test_the_period_and_date_are_read_from_the_preamble(self) -> None:
        rows = self._with_preamble(*_measured_rows()[:1])
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert result.period_label == "Tuần 1/Week 1"
        assert result.observed_on == D(2019, 8, 1)

    def test_the_report_period_is_applied_to_every_activity(self) -> None:
        """A report has one period; repeating it thirty-five times is how a column
        stops meaning anything."""
        rows = self._with_preamble(*_measured_rows()[:3])
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert [r.period_label for r in result.readings] == ["Tuần 1/Week 1"] * 3

    def test_a_zero_period_on_an_activity_is_treated_as_absent(self) -> None:
        """The measured value, and the reason this rule exists."""
        rows = self._with_preamble(*_measured_rows()[:1])
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert result.readings[0].period_label == "Tuần 1/Week 1"
        assert result.readings[0].period_label != "0"

    def test_a_real_per_row_period_is_not_flattened(self) -> None:
        """A different rule would lose data on a sheet that does carry one per row.

        The exclusion is specifically `0`, not "anything that is not the report's
        value" — a sheet with a genuine per-row period must not be flattened by a rule
        written for this one.
        """
        row = _row(
            "1",
            "Lắp đặt",
            completion=0.8,
            status="YES",
            p_start=D(2019, 4, 17),
            p_finish=D(2019, 4, 26),
            p_days=10,
            period="Tháng 3",
        )
        rows = [HEADER, _row(period="Tuần 9/Week 9"), row]
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert result.period_label == "Tuần 9/Week 9", "the report's own period"
        assert result.readings[0].period_label == "Tháng 3", "and the row keeps its own"

    def test_the_preamble_ends_at_the_first_numbered_row(self) -> None:
        """Otherwise a week label further down the sheet becomes the report's."""
        rows = [
            HEADER,
            _row(period="Tuần 1/Week 1"),
            *_measured_rows()[:1],
            _row(period="Tuần 2/Week 2"),
        ]
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert result.period_label == "Tuần 1/Week 1"
        assert result.skipped_sections >= 1, "the later week row is data-less and skipped"

    def test_a_sheet_with_no_preamble_reports_no_period(self) -> None:
        """`None` and `""` rather than a guess."""
        rows = [HEADER, *_measured_rows()[:1]]
        shape = detect_sheet(rows).shapes[0]
        result = read_progress(rows, shape)
        assert result.period_label == ""
        assert result.observed_on is None

    def test_the_summary_reports_the_period(self) -> None:
        """It is in `as_dict` because the dry run prints that and nothing else."""
        rows = self._with_preamble(*_measured_rows()[:1])
        shape = detect_sheet(rows).shapes[0]
        summary = read_progress(rows, shape).as_dict()
        assert summary["period_label"] == "Tuần 1/Week 1"
        assert summary["observed_on"] == "2019-08-01"
