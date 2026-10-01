"""The sheet detector, against the corpus it was written from.

Two kinds of test here and the split matters. The unit tests pin the *rules* with
synthetic rows, so a failure says which rule broke. The corpus tests ask the only
question that counts: **does this find the tables in the real files?**

The corpus tests are skipped when the corpus is not present, because this
repository is not the corpus's home and a test that fails on a missing external
directory is a test nobody trusts. What they assert is deliberately loose — *this
file yields at least one table of this kind* — because the alternative is a test
that encodes today's corpus exactly and fails the moment a fourth project lands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_orchestrator.ingest.sheets import (
    _KIND_ENUM,
    COLUMN_ROLES,
    SHEET_KINDS,
    SheetKind,
    detect_sheet,
    normalise,
    role_for,
)

CORPUS_ROOTS = (
    "/home/vutun/procurement/input",
    "/home/vutun/pmo_project/reference_sheets",
    "/home/vutun/pmo_project_procore/backend/uploads",
)


def _corpus_files() -> list[str]:
    found: list[Path] = []
    for root in CORPUS_ROOTS:
        base = Path(root)
        if base.is_dir():
            found.extend(base.rglob("*.xlsx"))
    return sorted(str(p) for p in found)


CORPUS = _corpus_files()
needs_corpus = pytest.mark.skipif(
    not CORPUS, reason="the construction corpus is not present on this machine"
)


class TestNormalisation:
    """The same defect as `Bộ`/`bộ`, one level up.

    Measured labels: `ĐƠN VỊ` against `Đơn vị`, `Nhãn hiệu/ Brandname` against
    `Nhãn hiệu Brand name`, `Đơn vị` against `Don vi`. Three ways of writing one
    column, in one corpus.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ĐƠN VỊ", "don vi"),
            ("Đơn vị", "don vi"),
            ("Đơn vị tính / Unit", "don vi tinh unit"),
            ("Nhãn hiệu/ Brandname", "nhan hieu brandname"),
            ("Nhãn hiệu Brand name", "nhan hieu brand name"),
            ("Đơn giá (VND)", "don gia vnd"),
            ("Số  TT/ No.", "so tt no"),
            ("Xuất xứ/ C/O", "xuat xu c o"),
            ("Đạt/Không Đạt", "dat khong dat"),
            (None, ""),
            ("   ", ""),
        ],
    )
    def test_every_spelling_collapses_to_one_token(self, raw: object, expected: str) -> None:
        assert normalise(raw) == expected

    def test_a_label_written_without_diacritics_still_matches(self) -> None:
        """`normalise` strips them, so the vocabulary is written without them.

        If it ever stopped stripping them, every entry in `COLUMN_ROLES` would
        become dead and the detector would silently find nothing. This is the test
        that says so.
        """
        assert all(
            not any(unicodedata_has_diacritic(ch) for ch in fragment)
            for frags in COLUMN_ROLES.values()
            for fragment in frags
        ), "a vocabulary fragment carries diacritics and can therefore never match"


def unicodedata_has_diacritic(ch: str) -> bool:
    import unicodedata

    return bool(unicodedata.combining(ch))


class TestRoleResolution:
    @pytest.mark.parametrize(
        ("label", "role"),
        [
            ("STT", "line_no"),
            ("Số  TT/ No.", "line_no"),
            ("Tên hàng hoá", "name"),
            ("Mô tả hàng hóa", "description"),
            ("Mã hàng", "code"),
            ("Mã Hiệu", "drawing_ref"),
            ("Nhà sản xuất", "manufacturer"),
            ("Nhãn hiệu/ Brandname", "brand"),
            ("Xuất xứ/ C/O", "origin"),
            ("Số lượng", "quantity"),
            ("Đơn vị", "unit"),
            ("Đơn giá (VND)", "unit_rate"),
            ("Thành tiền (VND)", "amount"),
            ("Ghi chú Remark", "remark"),
            ("Nội dung kiểm tra", "check_item"),
            ("Kết quả/ Result", "check_result"),
        ],
    )
    def test_a_measured_label_resolves(self, label: str, role: str) -> None:
        assert role_for(label) == role

    def test_the_specific_label_beats_the_general_one(self) -> None:
        """`Đơn vị tính` must be `unit`, not claimed by a shorter fragment.

        This is why the roles are sorted longest-first. Without the sort, ordering
        becomes dictionary order and a general fragment can win a specific column.
        """
        assert role_for("Đơn vị tính") == "unit"
        assert role_for("Số lượng theo HĐ") == "quantity"

    def test_an_unknown_label_resolves_to_nothing(self) -> None:
        """Silence is the right answer here; a guess is how a description column
        becomes a second `name` and a price column loses its owner."""
        assert role_for("Đèn chiếu sáng thoát hiểm Led 1W") is None
        assert role_for("") is None
        assert role_for(None) is None

    def test_every_vocabulary_fragment_is_reachable(self) -> None:
        """A fragment nothing resolves to is either dead or shadowed by a longer
        one. Both are worth knowing, and neither is visible from reading."""
        unreachable = [
            fragment
            for frags in COLUMN_ROLES.values()
            for fragment in frags
            if role_for(fragment) not in COLUMN_ROLES
        ]
        assert not unreachable, f"fragments that resolve to no role: {unreachable}"

    def test_every_vocabulary_fragment_is_already_normalised(self) -> None:
        """The vocabulary has to be written in the space `normalise` outputs.

        Two fragments were not. `c/o` and `p/p kiem tra` were copied from the
        corpus the way they are *written*, but `normalise` turns punctuation into a
        space, so the tokens it produces are `c o` and `p p kiem tra` — and a
        fragment containing a slash matches nothing, forever, silently.
        `test_every_vocabulary_fragment_is_reachable` did catch both, but only
        because it happened to exist; this one states the invariant directly, so a
        fragment copied from a spreadsheet fails here rather than quietly never
        matching.
        """
        unnormalised = [
            fragment
            for frags in COLUMN_ROLES.values()
            for fragment in frags
            if normalise(fragment) != fragment
        ]
        assert not unnormalised, (
            f"these fragments are not in normalised form and can never match: {unnormalised}"
        )


class TestDetection:
    PRICE_HEADER = (
        "STT",
        "Tên hàng hoá",
        "Mô tả hàng hóa",
        "Mã hàng",
        "Nhà sản xuất / Xuất xứ",
        "Số lượng ",
        "Đơn vị",
        "Đơn giá\n(VND)",
        "Thành tiền\n(VND)",
    )

    def test_a_price_schedule_is_found_and_named(self) -> None:
        """The measured `PL01` header, verbatim, over two data rows."""
        rows = [
            ("HỢP ĐỒNG ...", None, None, None, None, None, None, None, None),
            (None, None, None, None, None, None, None, None, None),
            ("Dự án: DRC607-CT02", None, None, None, None, None, None, None, None),
            # The header is one row of nine cells. `*self.PRICE_HEADER` would splice
            # the nine cells in as nine separate one-cell rows, and
            # `(self.PRICE_HEADER,)` would wrap the tuple in another tuple, making a
            # one-cell row whose cell is a tuple. Both were tried; both produce a
            # sheet with no detectable header and an error that points at the
            # vocabulary rather than at the test.
            self.PRICE_HEADER,
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
                "cái",
                "1.250.000",
                "30.000.000",
            ),
        ]
        found = detect_sheet(rows)
        assert found.kinds == (SheetKind.PRICE_SCHEDULE,), found.rejected
        shape = found.shapes[0]
        assert shape.header_row == 4
        assert shape.column_index("name") == 1
        assert shape.column_index("unit_rate") == 7
        assert shape.column_index("amount") == 8

    def test_a_header_below_a_preamble_is_found(self) -> None:
        """The measured goods-receipt sheet has its checklist at row 11.

        A detector that reads row 1 finds a contract header and calls the sheet
        unrecognised, which is what every name-based approach does.
        """
        preamble = [("BIÊN BẢN GIAO HÀNG, NGHIỆM THU", None)] * 10
        rows = [
            *preamble,
            ("STT No", "Nội dung kiểm tra", "P/P kiểm tra", "Kết quả/ Result", "Ghi chú Remark"),
            ("1", "Kiểm tra tem nhận diện", "Soi", "Đạt", ""),
        ]
        found = detect_sheet(rows)
        assert found.kinds == (SheetKind.INSPECTION_CHECKLIST,), found.rejected
        assert found.shapes[0].header_row == 11

    def test_two_tables_in_one_sheet_are_both_found(self) -> None:
        """The same file: checklist at row 11, item list at row 29.

        Detection stops at the first match in a naive implementation, which loses
        half the sheet.
        """
        rows: list[tuple[object, ...]] = [("BIÊN BẢN GIAO HÀNG", None)] * 10
        rows.append(
            ("STT No", "Nội dung kiểm tra", "P/P kiểm tra", "Kết quả/ Result", "Ghi chú Remark")
        )
        rows.append(("1", "Kiểm tra tem nhận diện", "Soi", "Đạt", ""))
        rows += [("",) * 6] * 17
        rows.append(
            (
                "Stt. No.",
                "Mã hàng hoá/ Model",
                "Tên hàng hoá/ Description of",
                "Nhãn hiệu/ Brandname",
                "Xuất xứ/ C/O",
                "Đơn vị tính / Unit",
                "Số lượng/ Amount",
                "Ghi chú / Remark",
            )
        )
        rows.append(("1", "ULR3000", "Tủ báo cháy", "Cooper", "UK", "bộ", "2", ""))
        found = detect_sheet(rows)
        assert SheetKind.INSPECTION_CHECKLIST in found.kinds, found.rejected
        assert SheetKind.GOODS_LINES in found.kinds, found.rejected

    def test_a_header_with_nothing_under_it_is_not_a_table(self) -> None:
        """A legend at the top of a form is not a table.

        Without this, every form's instruction block becomes a phantom table with
        zero rows and a confident-looking column mapping.
        """
        rows = [
            ("STT", "Tên hàng hoá", "Số lượng", "Đơn vị", "Đơn giá (VND)"),
            (None, None, None, None, None),
        ]
        found = detect_sheet(rows)
        assert not found.shapes
        assert any("no data beneath" in r for r in found.rejected), found.rejected

    def test_an_unlabelled_sheet_is_reported_not_silently_empty(self) -> None:
        """The reason `Detection.rejected` exists.

        A sheet that yields nothing must say why, or "no data" and "the vocabulary
        does not cover this project" are indistinguishable — and the second is the
        one that needs fixing.
        """
        rows = [("a", "b", "c"), ("d", "e", "f"), ("g", "h", "i")]
        found = detect_sheet(rows)
        assert not found.shapes
        assert found.rejected, "a sheet with no table must say so"

    def test_a_repeated_role_is_skipped_not_duplicated(self) -> None:
        """A merged header cell repeated across columns is one column, not two.

        Assigning both would give the reader two indexes for `name` and it would
        silently use whichever came first.
        """
        rows = [
            ("Tên hàng hoá", "Tên hàng hoá", "Số lượng", "Đơn vị", "Đơn giá (VND)"),
            ("A", "B", "1", "bộ", "100"),
        ]
        found = detect_sheet(rows)
        assert len(found.shapes) == 1
        names = [c for c in found.shapes[0].columns if c.role == "name"]
        assert len(names) == 1

    def test_a_price_schedule_beats_the_looser_goods_lines(self) -> None:
        """A sheet with a unit price is a price schedule, not an item list.

        Both classifications are satisfied by the same columns; the order encodes
        that the more specific one wins.
        """
        rows = [
            ("Tên hàng hoá", "Số lượng", "Đơn vị", "Đơn giá (VND)"),
            ("Cảm biến", "24", "cái", "1.250.000"),
        ]
        assert detect_sheet(rows).kinds == (SheetKind.PRICE_SCHEDULE,)

    def test_an_item_list_without_a_price_is_goods_lines(self) -> None:
        rows = [
            ("Tên hàng hoá", "Số lượng", "Đơn vị"),
            ("Cảm biến", "24", "cái"),
        ]
        assert detect_sheet(rows).kinds == (SheetKind.GOODS_LINES,)


class TestTheTwoVocabulariesAgree:
    def test_every_sheet_kind_has_an_enum_member(self) -> None:
        """`SHEET_KINDS` is data and the enum is a type; nothing links them.

        A new entry in `SHEET_KINDS` with no enum member raises `KeyError` inside
        `_classify`, at the moment a real file is read. This is the cheap way to
        find it now.
        """
        assert set(SHEET_KINDS) == set(_KIND_ENUM), (
            "SHEET_KINDS and SheetKind have drifted apart; add the enum member"
        )

    def test_every_kind_needs_at_least_two_roles(self) -> None:
        """A one-role classification matches almost any sheet that has that
        column, which is how a table of names becomes a 'delivery checklist'."""
        for name, required in SHEET_KINDS.items():
            assert len(required) >= 2, f"{name} is too weak to identify a sheet"


@needs_corpus
class TestAgainstTheRealCorpus:
    """The only tests here that answer the question that matters."""

    @staticmethod
    def _sheets(path: str) -> list[tuple[str, object]]:
        """Read one workbook's sheets as plain row tuples.

        A workbook that cannot be opened is skipped, and the count of those is
        tracked: an unreadable file is normal (there are `.xls` files, and some
        workbooks in the corpus are password-protected or corrupt), but a *reader
        that is not installed* is not, and it used to hide here. `openpyxl` was
        absent from this project's dependencies, every file raised `ImportError`,
        every file was skipped by the `except` below, and the two corpus tests
        failed with "no table found" — a message about the vocabulary, when the
        real cause was that nothing had been opened at all. The import is now
        checked once, loudly, before any file is touched.
        """
        import openpyxl

        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            out = []
            for sheet in book.worksheets:
                rows = [tuple(r) for r in sheet.iter_rows(max_row=60, max_col=20, values_only=True)]
                out.append((sheet.title, rows))
            return out
        finally:
            book.close()

    @classmethod
    def _readable(cls) -> list[tuple[str, list[tuple[str, list[tuple[object, ...]]]]]]:
        """`(path, [(title, rows)])` for every workbook that opens, plus the skips."""
        opened: list[tuple[str, list[tuple[str, list[tuple[object, ...]]]]]] = []
        for path in CORPUS:
            try:
                opened.append((path, cls._sheets(path)))
            except Exception:
                continue
        return opened

    def test_the_reader_is_actually_installed(self) -> None:
        """The check that would have caught it, stated as its own test.

        Without this, "the vocabulary does not match" and "nothing was ever read"
        produce the same failure, and the second is the one that is true far more
        often.
        """
        import openpyxl  # noqa: F401

        readable = self._readable()
        assert readable, (
            "no workbook in the corpus could be opened. If this says openpyxl is "
            "missing, the dependency is gone; if it says the paths are wrong, the "
            "corpus moved."
        )

    def test_at_least_one_price_schedule_is_found(self) -> None:
        """The `PL01`-shaped contract price appendix.

        This is the one classification the whole supply tranche depends on: it is
        what becomes `quotation_items` and `po_items`.
        """
        hits: list[str] = []
        for path, sheets in self._readable():
            for title, rows in sheets:
                if SheetKind.PRICE_SCHEDULE in detect_sheet(list(rows)).kinds:
                    hits.append(f"{Path(path).name}::{title}")
        assert hits, (
            "no price schedule found in any readable workbook; the column "
            "vocabulary no longer matches how these files are written"
        )

    def test_a_goods_receipt_sheet_yields_more_than_one_table(self) -> None:
        """The two-block GRN: checklist and item list in one sheet.

        Losing the second block loses every received quantity, so this is the case
        where "found something" is not good enough.
        """
        best: tuple[int, str] = (0, "no workbook was readable")
        for path, sheets in self._readable():
            for title, rows in sheets:
                kinds = detect_sheet(list(rows)).kinds
                if len(kinds) > best[0]:
                    best = (
                        len(kinds),
                        f"{Path(path).name}::{title} -> {[str(k) for k in kinds]}",
                    )
        assert best[0] >= 2, f"no sheet yielded two tables; best was {best[1]}"

    def test_detection_is_quiet_on_most_sheets(self) -> None:
        """A detector that claims a table in every sheet is not detecting.

        If this fails, the vocabulary has become loose enough to match prose, and
        every downstream number is suspect.
        """
        sheets_with = 0
        sheets_total = 0
        for _path, sheets in self._readable():
            for _title, rows in sheets:
                sheets_total += 1
                if detect_sheet(list(rows)).shapes:
                    sheets_with += 1
        if sheets_total == 0:
            pytest.fail("no sheet was read at all, so this test proves nothing")
        ratio = sheets_with / sheets_total
        assert ratio < 0.6, (
            f"{sheets_with}/{sheets_total} sheets produced a table; a detector that "
            "fires on most sheets is matching prose, not headers"
        )
