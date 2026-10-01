"""Finding the table inside a workbook, by what is in it rather than what it is called.

The corpus defeats every approach based on file or sheet names. One file's first
sheet is a contract header and its eleventh is the item list. The same logical
document appears as `PL01`, `ĐXTT-NCC`, `CCXX` and `Sheet1` across four projects.
`CCXX` is a warranty commitment and `ĐXTT-NCC` is a payment proposal, and both are
one sheet in a one-sheet file with no other content to tell them apart.

So this module looks for a **header row** — a row whose cells are labels rather than
data — and classifies the sheet by *which columns it has*. A sheet is a price
schedule because it has a name, a quantity, a unit and a unit price, not because
somebody named it that.

## The matching problem is the same one the units had

Measured header labels from the corpus:

    STT              Số  TT/ No.        Stt. No.        STT No
    Tên hàng hoá     Mô tả hàng hóa    Tên hàng hoá/ Description of
    Mã hàng         Mã hàng hoá/ Model
    Nhãn hiệu/ Brandname          Nhãn hiệu Brand name
    Xuất xứ/ C/O     Nhà sản xuất / Xuất xứ
    Số lượng         Số lượng/ Amount
    Đơn vị           Đơn vị tính / Unit          ĐƠN VỊ
    Đơn giá (VND)    Thành tiền (VND)
    Nội dung kiểm tra Checking ite      P/P kiểm tra Checking method
    Kết quả/ Result  Ghi chú Remark     Ghi chú/ Note

Three inconsistencies in nine labels: case (`ĐƠN VỊ` against `Đơn vị`), the
bilingual separator (`X/Y` in one file, `X Y` in the next), and diacritics
(`Đơn vị` against `Don vi`). This is the same defect as `Bộ`/`bộ` in the units
column, and it has the same answer: normalise before comparing, and never store
what the spreadsheet said.

The normaliser strips diacritics, lowercases, removes punctuation, and collapses
whitespace, so all four of those reduce to `don vi`. Vocabularies are therefore
written without diacritics, which also means they cannot be *read* without them —
the comment on each role says what it is for.

## Why substring matching, and where it is wrong

Labels are matched by substring rather than by equality, because `Tên hàng hoá/ Model`
and `Mã hàng hoá/ Model` both have to be found by a vocabulary that is not a list of
every string anyone has ever typed. That admits false positives, and the mitigation
is that roles are tried **longest-first** so the most specific label wins: `don vi tinh`
is claimed by `unit` before `quantity`'s `so luong` can be considered, and a label
matching two roles is recorded as ambiguous rather than silently assigned.

The failure this cannot catch is a column whose name means something else entirely
and happens to contain a known fragment. That is a wrong mapping, not a crash, and
`raw_cells` on every row is what makes it recoverable.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

#: Label vocabulary, keyed by role. Written **without diacritics** because
#: `normalise` strips them, so an accented entry here could never match.
#:
#: Every fragment is a measured string from the corpus, not an invention. The
#: comment records what each role is for, since the vocabulary is unreadable
#: otherwise — and an unreadable vocabulary does not get corrected when a new
#: project spells a column differently.
COLUMN_ROLES: dict[str, tuple[str, ...]] = {
    # The row's own number, for a reader to check continuity. Corpus spells it
    # `STT`, `Số  TT/ No.`, `Stt. No.`, `STT No` -- all covered by `stt`.
    "line_no": ("stt", "so tt", "stt no"),
    # What the item is. The most overloaded column in the corpus: it appears as
    # `Tên hàng hoá`, `Description of`, and `Mô tả hàng hóa` for a *second*, longer
    # description column.
    "name": ("ten hang hoa", "ten hang hoa description of", "description of", "ten hang"),
    # The longer free-text description, when the sheet has both.
    "description": ("mo ta hang hoa", "mo ta"),
    # An item or model code. The corpus has none that are really material codes --
    # these are drawing and catalogue numbers -- which is why `materials.code` is
    # nullable and this is carried as a reference, not as an identity.
    "code": ("ma hang hoa model", "ma hang", "model"),
    # The drawing a line is detailed on. `Mã Hiệu`, and in the corpus it is an
    # unlabelled or ambiguously labelled column, so it is kept rather than guessed.
    "drawing_ref": ("ma hieu",),
    # The manufacturer, collapsed together with origin in the price appendix
    # (`Nhà sản xuất / Xuất xứ`) and separate in the GRN. Separate here, because
    # "is this locally made" is a compliance question the collapsed form cannot
    # answer.
    "manufacturer": ("nha san xuat",),
    "brand": ("nhan hieu", "brand name", "brandname", "brand"),
    # `C/O` in the corpus, and it normalises to `c o` because punctuation becomes a
    # space. Writing it as `c/o` here would be a fragment that can never match
    # anything, and it did: the vocabulary has to be written in the same space
    # `normalise` outputs, which is not obvious and is now asserted by
    # `test_every_vocabulary_fragment_is_already_normalised`.
    "origin": ("xuat xu", "c o"),
    "quantity": ("so luong",),
    # The unit of measure. This is the `Bộ`/`bộ` column's *label*, and it is
    # consistently spelled here even though the *values* are not.
    "unit": ("don vi tinh", "don vi", "unit"),
    "unit_rate": ("don gia", "unit price"),
    "amount": ("thanh tien",),
    "remark": ("ghi chu", "remark", "note"),
    # The inspection checklist block inside a goods receipt. `P/P` is "phương
    # pháp" — the method — and normalises to `p p`, not `p/p`.
    "check_item": ("noi dung kiem tra",),
    "check_method": ("p p kiem tra", "checking method"),
    "check_result": ("ket qua", "result"),
    # The delivery checklist block: what is being handed over, and whether the
    # quality paperwork is there.
    "handover_item": ("noi dung ban giao",),
    "quality_docs": ("ho so chat luong",),
    "pass_fail": ("dat khong dat", "dat khong dat"),
    # Scoping, on the material sheets.
    "zone": ("khu vuc", "area install", "area"),
    "item_ref": ("hang muc", "item"),
    "package_ref": ("hang muc bo phan",),
    # -- the construction-progress family -------------------------------------
    #
    # Measured from `TĐ BOH.xlsx :: TĐ .BOH`, the only sheet in the corpus with a
    # KH/TT pair, plus the 199 headers the dry run located and could not classify.
    #
    # `KH` is *kế hoạch* (plan) and `TT` is *thực tế* (actual), and the corpus pairs
    # them column for column. Two of these fragments are lower case in the file —
    # `Ngày kết thúc Kh` against `Ngày bắt đầu KH` in the same header row — which
    # is why `normalise` lowercases before matching and why the fragments here are
    # too. It is the `Bộ`/`bộ` defect one level up, and the same answer.
    #
    # `planned_start_on` and `actual_start_on` are deliberately separate roles and
    # not one role with a suffix. The whole design of `progress_snapshots` is that
    # the two halves live on one row, and a reader that merged them would have to
    # split them again to fill it.
    "work_description": ("cong viec thi cong", "work"),
    "completion_ratio": ("hoan thanh", "completed percentage"),
    "item_completion_ratio": ("hoan thanh tong theo hang muc",),
    "status_text": ("tinh trang",),
    "planned_start_on": ("ngay bat dau kh",),
    "actual_start_on": ("ngay bat dau tt",),
    "planned_finish_on": ("ngay ket thuc kh",),
    "actual_finish_on": ("ngay ket thuc tt",),
    "planned_duration_days": ("so ngay kh",),
    "actual_duration_days": ("so ngay tt",),
    "handover_planned": ("hpnc kh",),
    "handover_actual": ("hpnc tt",),
    "system_code": ("he thong",),
    "engineer_comment": ("phan hoi bqlda",),
    "drawing_name": ("ten ban ve",),
    "task_detail": ("chi tiet dau viec theo hd",),
    # `Tuần 1/Week 1` and `Tháng` both appear in the same sheet and the file does
    # not say which axis a given row belongs to, so both are the same role and the
    # text is kept verbatim rather than being forced into a week-or-month choice.
    "period_label": ("tuan", "thang"),
}

#: Roles sorted longest-fragment-first, so `don vi tinh` is tested before `don vi`
#: and a specific label always beats a general one. Built once at import; the
#: ordering is the whole point and recomputing it per sheet would be a silent
#: performance trap on a 200-sheet workbook.
_ROLES_BY_SPECIFICITY: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((fragment, role) for role, frags in COLUMN_ROLES.items() for fragment in frags),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)

#: Sheet classifications. Each names the roles that must be present, so a sheet is
#: identified by its shape. The threshold is deliberately "all of these", not "most":
#: a sheet missing the unit price is not a price schedule with a gap, it is
#: something else, and guessing would put wrong numbers into a BOQ.
SHEET_KINDS: dict[str, tuple[str, ...]] = {
    # `PL01` and its equivalents: the priced line list that becomes a contract's
    # price appendix. Needs the price, or it is not one.
    "price_schedule": ("name", "quantity", "unit", "unit_rate"),
    # The item block of a goods receipt: what arrived, of what brand, from where.
    "goods_lines": ("name", "quantity", "unit"),
    # The inspection checklist block of a goods receipt.
    "inspection_checklist": ("check_item", "check_result"),
    # The delivery handover checklist: quantity per contract, and whether the
    # quality documents are present.
    "delivery_checklist": ("handover_item", "unit"),
    # A pure list of materials with no price -- a `VẬT TƯ <zone>` sheet.
    "material_list": ("name", "quantity", "unit"),
    # The construction-progress sheet: a planned window, and the actual one beside
    # it. `planned_start_on` + `planned_finish_on` is the identifying pair, and
    # they are distinctive — no other kind in the corpus has either, so a sheet
    # carrying them is a progress sheet whatever else it has. A schedule carrying
    # only one of the two falls through to `unrecognised` with a reason, which is
    # the honest outcome: a half-window is not a reading.
    "construction_progress": ("planned_start_on", "planned_finish_on"),
}

#: A row needs at least this many labels to be a header. Measured: the sparsest
#: real header in the corpus is the delivery checklist's `Đạt/Không Đạt` block at
#: five labels. Three keeps a two-column note from being read as a table while
#: tolerating a header that lost a column to a merge.
MIN_HEADER_LABELS = 3

#: How far down a sheet to look for the header. The corpus's deepest measured
#: header is row 11, in a file whose rows 1-10 are a contract preamble.
MAX_HEADER_SCAN_ROWS = 30

#: How far below a header to look for data before deciding the header was a
#: coincidence. A header with nothing under it is a legend.
MIN_DATA_ROWS_AFTER_HEADER = 1

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_DIGITS = re.compile(r"^\d+$")

#: Letters that are **not** base-plus-combining-mark and so survive `NFD`
#: untouched. This is not a detail: `Đ` is the opening letter of most Vietnamese
#: technical vocabulary — `Đơn vị` (unit), `Đơn giá` (unit price), `Đạt` (pass),
#: `Địa điểm` (location), `Điện` (electrical) — and every one of them appears as a
#: column header in this corpus. A normaliser that strips diacritics by decomposing
#: and then discarding combining marks turns `Đơn vị` into `Đon vi`, which matches
#: nothing, so the detector silently finds no tables at all.
#:
#: Found by the normalisation test rather than by reading, which is the only reason
#: it is fixed: `Đ` looks like every other accented letter and is not one.
_STROKE_LETTERS = str.maketrans({"Đ": "D", "đ": "d", "Ø": "O", "ø": "o"})


def normalise(label: object) -> str:
    """Reduce a header cell to a comparable token.

    Strips diacritics, lowercases, drops punctuation and collapses whitespace, so
    `ĐƠN VỊ`, `Đơn vị tính / Unit` and `Don vi` all become `don vi`. Without the
    diacritic step the vocabulary below could not be written at all — every entry
    would need both spellings and one of them would be forgotten.
    """
    if label is None:
        return ""
    text = str(label).translate(_STROKE_LETTERS)
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = _PUNCTUATION.sub(" ", text.lower())
    return _WHITESPACE.sub(" ", text).strip()


def role_for(label: object) -> str | None:
    """The column role a header cell names, or `None`.

    Longest fragment wins, so `Đơn vị tính / Unit` resolves to `unit` rather than
    being claimed by a shorter fragment in another role.
    """
    token = normalise(label)
    if not token:
        return None
    for fragment, role in _ROLES_BY_SPECIFICITY:
        if fragment in token:
            return role
    return None


class SheetKind(StrEnum):
    """What a sheet turned out to be. Names, never guesses at content."""

    UNRECOGNISED = "unrecognised"
    PRICE_SCHEDULE = "price_schedule"
    GOODS_LINES = "goods_lines"
    INSPECTION_CHECKLIST = "inspection_checklist"
    DELIVERY_CHECKLIST = "delivery_checklist"
    MATERIAL_LIST = "material_list"
    CONSTRUCTION_PROGRESS = "construction_progress"


#: Which `SHEET_KINDS` entry means which enum member. Two vocabularies for one
#: concept is a smell, and it is here because `SHEET_KINDS` is data (declarative,
#: easy to extend from a new file) while the enum is a type (so a caller can
#: exhaustively match). The test asserts the two agree, which is the cheapest way
#: to keep a mapping honest.
_KIND_ENUM = {
    "price_schedule": SheetKind.PRICE_SCHEDULE,
    "goods_lines": SheetKind.GOODS_LINES,
    "inspection_checklist": SheetKind.INSPECTION_CHECKLIST,
    "delivery_checklist": SheetKind.DELIVERY_CHECKLIST,
    "material_list": SheetKind.MATERIAL_LIST,
    "construction_progress": SheetKind.CONSTRUCTION_PROGRESS,
}


@dataclass(frozen=True, slots=True)
class Column:
    """One header cell, resolved to a role.

    `index` is the column's position and `label` is what the sheet actually said.
    Both are kept: the role is a decision this module made, and the label is the
    evidence for it, so a wrong mapping can be diagnosed rather than guessed at.
    """

    index: int
    label: str
    role: str


@dataclass(frozen=True, slots=True)
class SheetShape:
    """A detected table: where its header is, what its columns mean, what it is.

    `header_row` is 1-based because that is how a spreadsheet addresses it, and
    every corpus reference to a cell is 1-based too. Off-by-one here would mean
    reading the header as data.
    """

    kind: SheetKind
    header_row: int
    columns: tuple[Column, ...]

    def column_index(self, role: str) -> int | None:
        """Where a role lives, or `None` if this sheet does not have it."""
        for column in self.columns:
            if column.role == role:
                return column.index
        return None

    def roles(self) -> frozenset[str]:
        return frozenset(c.role for c in self.columns)

    def describe(self) -> str:
        """One line naming the sheet's kind and its columns, for a dry-run report."""
        pairs = ", ".join(f"{c.role}={c.label!r}" for c in self.columns)
        return f"{self.kind} at row {self.header_row}: {pairs}"


@dataclass(slots=True)
class Detection:
    """Everything found in one sheet, including what was rejected.

    A sheet can hold more than one table -- the measured goods-receipt file has an
    inspection checklist at row 11 and an item list at row 29 -- so detection
    returns a list rather than a single shape. The rejections are kept because a
    sheet that produced nothing is information: it means the vocabulary does not
    cover this project, and a silent empty result reads as "no data".
    """

    shapes: tuple[SheetShape, ...] = ()
    rejected: tuple[str, ...] = field(default_factory=tuple)

    @property
    def kinds(self) -> tuple[SheetKind, ...]:
        return tuple(s.kind for s in self.shapes)


def _looks_like_a_label(value: object) -> bool:
    """Whether a cell could be a header label rather than data.

    A pure number is never a label. Nor is a cell so long it is obviously prose --
    `Đèn chiếu sáng thoát hiểm Led 1W` is data that happens to sit in a header
    row, and treating it as a label is how a description column gets mapped to
    `name` twice.
    """
    if value is None:
        return False
    if isinstance(value, (int, float, bool)):
        return False
    text = str(value).strip()
    if not text or _DIGITS.match(text):
        return False
    return len(text) <= 40


def _rows(raw: list[tuple[object, ...]]) -> list[tuple[object, ...]]:
    """Trim trailing all-empty rows, which openpyxl reports generously."""
    end = len(raw)
    while end > 0 and all(c in (None, "") for c in raw[end - 1]):
        end -= 1
    return raw[:end]


def detect_sheet(
    raw_rows: list[tuple[object, ...]],
    *,
    max_header_scan: int = MAX_HEADER_SCAN_ROWS,
    min_labels: int = MIN_HEADER_LABELS,
) -> Detection:
    """Find every table in a sheet.

    Scans for header rows rather than assuming row 1, because the corpus puts a
    contract preamble above its tables: the goods-receipt sheet has its checklist
    header at row 11. It keeps scanning after a match rather than stopping, because
    one sheet holds both a checklist and an item list.

    A header is only accepted if at least `min_labels` cells resolve to *distinct*
    roles and at least one data row follows. The distinctness test is what stops a
    row of six unit-ish labels from being read as six columns of one thing.
    """
    rows = _rows(raw_rows)
    shapes: list[SheetShape] = []
    rejected: list[str] = []
    #: How many rows looked like a header candidate and were dropped for having too
    #: few distinct roles. Tracked rather than listed, because a sheet of ordinary
    #: data produces one of these per row and a hundred identical messages is not a
    #: diagnosis.
    thin_candidates = 0

    for index, row in enumerate(rows[:max_header_scan]):
        columns: list[Column] = []
        seen_roles: dict[str, int] = {}
        for position, cell in enumerate(row):
            if not _looks_like_a_label(cell):
                continue
            role = role_for(cell)
            if role is None or role in seen_roles:
                # A repeated role means a merged or duplicated header, not two
                # columns of the same thing. Skipped rather than guessed.
                continue
            seen_roles[role] = position
            columns.append(Column(index=position, label=str(cell).strip(), role=role))

        if len(seen_roles) < min_labels:
            if len(seen_roles):
                thin_candidates += 1
            continue
        if not _has_a_data_row(rows, index, {c.index for c in columns}):
            rejected.append(f"row {index + 1}: {len(seen_roles)} labels but no data beneath it")
            continue

        kind = _classify(frozenset(seen_roles))
        if kind is None:
            rejected.append(f"row {index + 1}: roles {sorted(seen_roles)} match no known sheet")
            continue
        shapes.append(SheetShape(kind=kind, header_row=index + 1, columns=tuple(columns)))

    if not shapes and not rejected:
        # The case that matters most and used to be silent. A sheet of ordinary
        # data produces no header candidate, so nothing was rejected, so `rejected`
        # was empty — and "the vocabulary does not cover this project" is
        # indistinguishable from "this sheet is just data". One line saying which
        # of the two it was is the difference between a report and a shrug.
        detail = (
            f"{thin_candidates} row(s) had labels but too few distinct roles"
            if thin_candidates
            else f"no row in the first {min(len(rows), max_header_scan)} had "
            f"{min_labels} or more recognisable column labels"
        )
        rejected.append(f"no table detected: {detail}")

    return Detection(shapes=tuple(shapes), rejected=tuple(rejected))


def _has_a_data_row(rows: list[tuple[object, ...]], header_index: int, columns: set[int]) -> bool:
    """Whether anything follows the header in those columns.

    `MIN_DATA_ROWS_AFTER_HEADER` is 1, so this asks for one row and not a
    threshold — a header with a single line under it is still a table, and a
    contract with one material line is common.
    """
    for row in rows[header_index + 1 :]:
        populated = sum(1 for index in columns if index < len(row) and row[index] not in (None, ""))
        if populated >= 2:
            return True
    return False


def _classify(roles: frozenset[str]) -> SheetKind | None:
    """Name the sheet by the roles it has.

    Checked in a fixed order and the first match wins, so the order encodes
    precedence: a sheet with a unit price is a price schedule even though it also
    satisfies the looser `goods_lines`. `delivery_checklist` and
    `inspection_checklist` are checked before the generic ones because their
    distinguishing roles are unambiguous, whereas `goods_lines` and `material_list`
    are the same shape and differ only in intent.
    """
    for name in (
        "price_schedule",
        "inspection_checklist",
        "delivery_checklist",
        "construction_progress",
        "goods_lines",
        "material_list",
    ):
        if all(role in roles for role in SHEET_KINDS[name]):
            return _KIND_ENUM[name]
    return None


__all__ = [
    "COLUMN_ROLES",
    "MAX_HEADER_SCAN_ROWS",
    "MIN_HEADER_LABELS",
    "SHEET_KINDS",
    "Column",
    "Detection",
    "SheetKind",
    "SheetShape",
    "detect_sheet",
    "normalise",
    "role_for",
]
