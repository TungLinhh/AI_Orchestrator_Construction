"""Reading detected tables, and the four ways a reader is wrong without raising.

`tests/integration/test_ingest_sheets.py` establishes that the detector finds the
tables. This file is about what happens next, and the tests are organised by the
failure mode rather than by the function, because the failure modes are the reason
this module is careful:

* a unit written two ways becomes two units
* `12.500.000` read with the wrong convention is wrong by a factor of a thousand
* an unparsed cell becomes a zero, and a zero is a free item
* a line total is overwritten by `quantity * rate`, and the disagreement was the
  only evidence the line needed a human

Each of those produces a plausible row. None raises.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ai_orchestrator.ingest.reader import (
    UNIT_ALIASES,
    IngestRow,
    plan_ingest,
    read_table,
    resolve_unit,
)
from ai_orchestrator.ingest.sheets import SheetKind, detect_sheet

HEADER = (
    "STT",
    "Tên hàng hoá",
    "Mô tả hàng hóa",
    "Mã hàng",
    "Nhà sản xuất / Xuất xứ",
    "Số lượng",
    "Đơn vị",
    "Đơn giá (VND)",
    "Thành tiền (VND)",
)


def _table(*data: tuple[object, ...]) -> tuple[list[tuple[object, ...]], object]:
    """A one-header price schedule over `data`, and its detected shape."""
    rows: list[tuple[object, ...]] = [HEADER, *data]
    found = detect_sheet(rows)
    assert found.shapes, found.rejected
    return rows, found.shapes[0]


class TestUnitsAreResolvedNotCopied:
    """`Bộ` 17 times and `bộ` 7. One unit, two spellings."""

    @pytest.mark.parametrize(
        ("label", "code"),
        [
            ("Bộ", "bo"),
            ("bộ", "bo"),
            ("BỘ", "bo"),
            ("Cái", "cai"),
            ("cái", "cai"),
            ("Cuộn", "cuon"),
            ("M", "m"),
            ("M2", "m2"),
            ("M3", "m3"),
        ],
    )
    def test_every_spelling_maps_to_one_code(self, label: str, code: str) -> None:
        assert resolve_unit(label) == code

    def test_a_unit_we_do_not_know_resolves_to_nothing(self) -> None:
        """Not to a default.

        Defaulting to `cai` would make a cubic metre of ducting reconcile against
        fireplace pieces, and the error would surface at final account rather than
        at import.
        """
        assert resolve_unit("thùng dầu") is None  # two words, not a unit
        assert resolve_unit("") is None
        assert resolve_unit(None) is None

    def test_an_unknown_unit_becomes_a_refusal_on_the_row(self) -> None:
        rows, shape = _table(("1", "Ống gió", "", "", "", "10", "thùng dầu", "500000", "5000000"))
        (row,) = read_table(rows, shape)
        assert row.unit_code is None
        assert any("not a known unit" in r for r in row.refusals), row.refusals
        assert not row.is_clean()

    def test_the_two_spellings_produce_identical_codes_in_one_table(self) -> None:
        """The reconciliation case, stated directly.

        Two rows, one in `Bộ` and one in `bộ`, must be the same unit — otherwise
        summing their quantities across the two is adding apples to oranges and the
        disagreement shows up as a materials variance nobody can explain.
        """
        rows, shape = _table(
            ("1", "Tủ báo cháy", "", "", "", "2", "Bộ", "18000000", "36000000"),
            ("2", "Cảm biến", "", "", "", "24", "bộ", "1250000", "30000000"),
        )
        first, second = read_table(rows, shape)
        assert first.unit_code == second.unit_code == "bo"
        assert first.is_clean() and second.is_clean()

    def test_every_alias_key_is_already_normalised(self) -> None:
        """Same invariant as the header vocabulary, for the same reason.

        A key that is not in normalised form matches nothing, and it is invisible:
        the unit simply comes back unresolved and the row is refused.
        """
        from ai_orchestrator.ingest.sheets import normalise

        unnormalised = [k for k in UNIT_ALIASES if normalise(k) != k]
        assert not unnormalised, f"unit aliases that can never match: {unnormalised}"


class TestNumbersUseTheVietnameseConvention:
    """`12.500.000` is twelve and a half million."""

    def test_a_dotted_group_is_thousands_not_decimals(self) -> None:
        rows, shape = _table(
            ("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18.000.000", "36.000.000")
        )
        (row,) = read_table(rows, shape)
        assert row.unit_rate == Decimal("18000000")
        assert row.amount == Decimal("36000000")
        assert row.refusals == ()

    def test_a_comma_decimal_is_a_decimal(self) -> None:
        rows, shape = _table(
            ("1", "Ống đồng", "", "", "", "1.234,5", "m", "1.000.000", "1.234.500.000")
        )
        (row,) = read_table(rows, shape)
        assert row.quantity == Decimal("1234.5")
        assert row.amount == Decimal("1234500000")

    def test_a_native_numeric_cell_is_taken_as_is(self) -> None:
        """Excel already knows what a float is, and re-parsing it would be silly.

        The separator logic exists for *text* cells, which is where a spreadsheet
        stores a number a human typed.
        """
        rows, shape = _table((1, "Cảm biến", "", "", "", 24, "cái", 1250000, 30000000))
        (row,) = read_table(rows, shape)
        assert row.quantity == Decimal("24")
        assert row.unit_rate == Decimal("1250000")
        assert row.refusals == ()


class TestUnreadableCellsAreRecordedNotSubstituted:
    def test_a_non_numeric_quantity_is_a_refusal_and_no_value(self) -> None:
        rows, shape = _table(("1", "Cảm biến", "", "", "", "vô hạn", "cái", "1250000", "30000000"))
        (row,) = read_table(rows, shape)
        assert row.quantity is None
        assert any("quantity" in r for r in row.refusals), row.refusals

    def test_a_zero_is_never_substituted_for_a_refusal(self) -> None:
        """The specific harm.

        `0` is a legitimate quantity and a free item. Writing it where the file said
        something unreadable invents a free item, and the row looks clean.
        """
        rows, shape = _table(("1", "Cảm biến", "", "", "", "x", "cái", "1250000", "30000000"))
        (row,) = read_table(rows, shape)
        assert row.quantity is None
        assert row.quantity != Decimal("0")
        assert not row.is_clean()

    def test_an_empty_cell_is_not_a_refusal(self) -> None:
        """Blank is not a failure; acting on it would bury the real ones."""
        rows, shape = _table(("1", "Tủ báo cháy", "", "", "", "2", "bộ", "", "36000000"))
        (row,) = read_table(rows, shape)
        assert row.unit_rate is None
        assert row.refusals == ()

    def test_an_unlabelled_column_is_kept_verbatim(self) -> None:
        """`SSA | HBG | 2` under a header that names none of them.

        A reader that drops unknown columns loses data; one that guesses a meaning
        for them invents it. `raw_cells` is the third option and it already exists
        on `materials` for the same reason.
        """
        # The tenth header cell is blank, which is the measured case: the
        # `VẬT TƯ` sheets carry `SSA | HBG | 2` under a header that names none of
        # them. A header of "Ghi chú thêm" would not do -- it normalises to
        # "ghi chu them" and is claimed by the `remark` role, so it is a known
        # column and correctly excluded.
        header = (*HEADER, "")
        rows = [
            header,
            ("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000", "SSA"),
        ]
        shape = detect_sheet(rows).shapes[0]
        (row,) = read_table(rows, shape)
        assert row.raw_cells == {"9": "SSA"}


class TestTheSheetTotalIsNotRecomputed:
    def test_a_total_that_disagrees_is_reported_not_corrected(self) -> None:
        """Daywork lines and provisional sums do not multiply out.

        That is legitimate, and it is also the only signal that a line needs
        looking at. Overwriting the total with `quantity * rate` destroys it.
        """
        rows, shape = _table(
            ("1", "Công nhân lắp đặt", "", "", "", "10", "ngày công", "0", "8500000")
        )
        (row,) = read_table(rows, shape)
        assert row.amount == Decimal("8500000")
        assert row.amount_disagrees_with_quantity_times_rate() is True

    def test_a_total_that_agrees_is_quiet(self) -> None:
        rows, shape = _table(("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"))
        (row,) = read_table(rows, shape)
        assert row.amount_disagrees_with_quantity_times_rate() is False

    def test_a_missing_rate_cannot_disagree(self) -> None:
        """Three of four absent means there is nothing to compare, not a mismatch."""
        row = IngestRow(line_no=1, name="X", quantity=Decimal("2"), amount=Decimal("5"))
        assert row.amount_disagrees_with_quantity_times_rate() is False


class TestTablesEndAtTheFirstBlankRow:
    def test_a_section_break_is_not_read_through(self) -> None:
        """A gap in a contract's price appendix is a new section.

        Reading across it produces one continuous item list with continuous line
        numbers — a wrong BOQ rather than a missing one, and much harder to notice.
        """
        rows, shape = _table(
            ("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"),
            (None, None, None, None, None, None, None, None, None),
            ("1", "Ống đồng", "", "", "", "50", "m", "200000", "10000000"),
        )
        parsed = read_table(rows, shape)
        assert [r.name for r in parsed] == ["Tủ báo cháy"]

    def test_line_numbers_fall_back_to_row_order(self) -> None:
        """A sheet with a blank `STT` still has an order.

        Losing it makes rows indistinguishable. The fallback is the physical order,
        which is a fact about the file rather than an invention.
        """
        rows, shape = _table(
            (None, "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"),
            ("x", "Cảm biến", "", "", "", "24", "cái", "1250000", "30000000"),
        )
        first, second = read_table(rows, shape)
        assert (first.line_no, second.line_no) == (1, 2)

    def test_a_real_line_number_is_preferred(self) -> None:
        rows, shape = _table(("7", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"))
        (row,) = read_table(rows, shape)
        assert row.line_no == 7


class TestThePlanSaysMoreThanACount:
    def _plan(self, *data: tuple[object, ...]) -> dict[str, object]:
        rows, shape = _table(*data)
        return plan_ingest(rows, shape)

    def test_a_clean_table_reports_clean(self) -> None:
        plan = self._plan(
            ("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"),
            ("2", "Cảm biến", "", "", "", "24", "cái", "1250000", "30000000"),
        )
        assert plan["rows"] == 2
        assert plan["clean_rows"] == 2
        assert plan["refusals_by_column"] == {}
        assert plan["amounts_disagreeing_with_qty_x_rate"] == 0

    def test_refusals_are_grouped_by_the_column_that_caused_them(self) -> None:
        """Which column is broken is the question that decides what to fix.

        A single "3 rows had problems" sends somebody to look at the whole file.
        """
        plan = self._plan(
            ("1", "A", "", "", "", "x", "cái", "100", "100"),
            ("2", "B", "", "", "", "2", "thùng dầu", "100", "200"),
            ("3", "C", "", "", "", "y", "cái", "100", "200"),
        )
        assert plan["refusals_by_column"] == {"quantity": 2, "unit": 1}
        assert plan["rows"] == 3
        assert plan["clean_rows"] == 0

    def test_the_plan_samples_rows_so_a_reader_can_eyeball_it(self) -> None:
        """A count is not reviewable. Five rows are.

        This is the output the dry run prints, and it is the only thing standing
        between a vocabulary change and a confidently wrong material master.
        """
        plan = self._plan(
            ("1", "Tủ báo cháy ULR3000", "", "ULR3000", "", "2", "bộ", "18000000", "36000000")
        )
        (sample,) = plan["sample"]
        assert sample["name"] == "Tủ báo cháy ULR3000"
        assert sample["quantity"] == "2"
        assert sample["unit"] == "bo"
        assert sample["refusals"] == []

    def test_disagreeing_totals_are_counted_separately(self) -> None:
        """A different question from a refusal, and a different fix.

        A refusal means the reader could not understand the file. A disagreeing
        total means it understood the file and the file is internally inconsistent.
        Conflating them sends somebody to fix the wrong thing.
        """
        plan = self._plan(
            ("1", "Tủ báo cháy", "", "", "", "2", "bộ", "18000000", "36000000"),
            ("2", "Công nhân", "", "", "", "10", "ngày công", "0", "8500000"),
        )
        assert plan["amounts_disagreeing_with_qty_x_rate"] == 1
        assert plan["clean_rows"] == 1


class TestThePriceAppendixShapeEndToEnd:
    def test_the_measured_header_reads_without_a_single_refusal(self) -> None:
        """The acceptance criterion, stated as one test.

        The real `PL01` header, the real Vietnamese price format, the real `bộ`
        casing, and `Bộ` in the next row. Every column resolves, every number
        parses, and the row comes out clean. If this passes, the path from a
        workbook to a `po_items` row is sound apart from the write.
        """
        rows, shape = _table(
            (
                "1",
                "Tủ trung tâm báo cháy",
                "ULR3000 8 loop",
                "ULR3000",
                "Cooper by Eaton/ UK",
                "1",
                "bộ",
                "125.000.000",
                "125.000.000",
            ),
            (
                "2",
                "Cảm biến khói",
                "",
                "MK-10",
                "Notifier/ USA",
                "24",
                "Cái",
                "1.250.000",
                "30.000.000",
            ),
        )
        assert shape.kind is SheetKind.PRICE_SCHEDULE
        first, second = read_table(rows, shape)
        assert first.is_clean(), first.refusals
        assert second.is_clean(), second.refusals
        assert first.unit_rate == Decimal("125000000")
        assert first.amount == Decimal("125000000")
        # Different units, on purpose: a cabinet is a `bộ` and a detector is a
        # `cái`, in this corpus and in the file above. An earlier version of this
        # assertion demanded they matched, which was a claim about the data rather
        # than about the reader.
        assert first.unit_code == "bo"
        assert second.unit_code == "cai"
        assert first.code == "ULR3000"
        assert first.manufacturer == "Cooper by Eaton/ UK"
        assert first.brand == "" and first.origin_country == "", (
            "the price appendix collapses manufacturer and origin into one column, "
            "so neither can be recovered; the GRN sheet is where they are separate"
        )


class TestChecklistsAreReadByTheirOwnLabelColumn:
    """The three checklist kinds have no `name` column at all.

    `read_table` originally tested for a `name` cell and stopped at the first row
    without one. That is right for a price schedule and means **zero rows** for
    every inspection checklist, whose rows are named in `Nội dung kiểm tra`, and
    for every delivery checklist, whose rows are named in `Nội dung bàn giao`.

    The corpus cannot catch this on its own, which is why these rows are
    synthetic. The three real files are `Mẫu` — blank templates — and the dry run
    correctly reports 0 rows for them. A *filled* checklist is the case that breaks,
    and no filled checklist exists in the corpus.
    """

    CHECK_HEADER = (
        "STT No",
        "Nội dung kiểm tra",
        "P/P kiểm tra",
        "Kết quả/ Result",
        "Ghi chú Remark",
    )

    def test_a_filled_inspection_checklist_is_read(self) -> None:
        rows: list[tuple[object, ...]] = [
            ("BIÊN BẢN GIAO HÀNG, NGHIỆM THU",) + (None,) * 4,
            *[(None,) * 5] * 9,
            self.CHECK_HEADER,
            # The measured sub-header: nothing in the label column, content in three
            # others. Stopping here is what made a filled checklist unreadable.
            (None, None, None, "Đạt", "Không đạt"),
            ("I", "Hồ sơ - tài liệu", None, None, None),
            ("1", "Biên bản giao hàng", "Mắt thường", "Đạt", ""),
            ("2", "Phiếu bảo hành", "Mắt thường", "Đạt", ""),
            ("3", "Chứng nhận xuất xứ", "Kiểm tra bản gốc", "Không đạt", "Thiếu bản gốc"),
        ]
        shape = detect_sheet(rows).shapes[0]
        assert shape.kind is SheetKind.INSPECTION_CHECKLIST
        parsed = read_table(rows, shape)
        assert [r.name for r in parsed] == [
            # The section heading is a real line in the checklist and is kept as
            # one -- dropping it would lose which section each item belongs to.
            # What matters is that it is not silently renumbered.
            "Hồ sơ - tài liệu",
            "Biên bản giao hàng",
            "Phiếu bảo hành",
            "Chứng nhận xuất xứ",
        ], "the sub-header row must be skipped, not treated as the end of the table"
        assert [r.line_label for r in parsed] == ["I", "1", "2", "3"]

    def test_a_checklist_row_carries_its_method_and_result(self) -> None:
        """Otherwise a passed inspection and a failed one are the same row."""
        rows: list[tuple[object, ...]] = [
            *[(None,) * 5] * 9,
            self.CHECK_HEADER,
            ("3", "Chứng nhận xuất xứ", "Kiểm tra bản gốc", "Không đạt", "Thiếu bản gốc"),
        ]
        (row,) = read_table(rows, detect_sheet(rows).shapes[0])
        assert "method: Kiểm tra bản gốc" in row.remark
        assert "result: Không đạt" in row.remark

    def test_a_delivery_checklist_is_read_by_its_own_column(self) -> None:
        rows: list[tuple[object, ...]] = [
            *[(None,) * 6] * 18,
            (
                "Stt",
                "Nội dung bàn giao",
                "Đơn vị tính",
                "Số lượng theo HĐ",
                "Hồ sơ chất lượng",
                "Đạt/Không Đạt",
            ),
            ("1", "Bản vẽ as built", "bộ", "2", "Có", "Đạt"),
        ]
        shape = detect_sheet(rows).shapes[0]
        assert shape.kind is SheetKind.DELIVERY_CHECKLIST
        (row,) = read_table(rows, shape)
        assert row.name == "Bản vẽ as built"
        assert row.quantity == Decimal("2")
        assert row.unit_code == "bo"
        assert "outcome: Đạt" in row.remark

    def test_a_row_records_which_table_it_came_from(self) -> None:
        """`name` holds a price item on one sheet and an inspection step on another.

        A caller that cannot tell them apart writes a fire-alarm cabinet into a
        receipt's checklist, so the kind travels with the row.
        """
        rows: list[tuple[object, ...]] = [
            *[(None,) * 5] * 9,
            self.CHECK_HEADER,
            ("1", "Biên bản giao hàng", "Mắt thường", "Đạt", ""),
        ]
        (row,) = read_table(rows, detect_sheet(rows).shapes[0])
        assert row.kind == "inspection_checklist"

    def test_a_genuinely_blank_row_still_ends_the_table(self) -> None:
        """The other half. A sub-header is skipped; a section break is not.

        Without this the reader would run past a section break and merge two
        sections into one list of items with continuous line numbers.
        """
        rows: list[tuple[object, ...]] = [
            *[(None,) * 5] * 9,
            self.CHECK_HEADER,
            ("1", "Biên bản giao hàng", "Mắt thường", "Đạt", ""),
            (None, None, None, None, None),
            ("2", "Phiếu bảo hành", "Mắt thường", "Đạt", ""),
        ]
        parsed = read_table(rows, detect_sheet(rows).shapes[0])
        assert [r.name for r in parsed] == ["Biên bản giao hàng"]
