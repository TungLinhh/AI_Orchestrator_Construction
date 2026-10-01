"""Reading a detected table into rows, and refusing to guess the ones that matter.

`sheets.py` finds tables. This turns one into data, and the hard part is not the
plumbing — it is the four places a plausible-looking reader produces a wrong number
without raising, all of which are measured facts about this corpus rather than
hypotheticals.

**A quantity is not a quantity until its unit is resolved.** The corpus writes `Bộ`
17 times and `bộ` 7, `Cái` 34 and `cái` 6. Those are one unit written two ways, and
a reader that copies the label creates two units and a quantity reconciliation that
fails on a capital letter. So a row's unit is resolved to an ASCII code against
`units_dictionary`, and an unresolved unit is a **refusal**, not a default. There is
no default: defaulting to "pcs" would make `m³` of ducting reconcile against
fireplace pieces and the error would surface at final account.

**A price is `Decimal` or it is not a price.** `12.500.000` is twelve and a half
million. Read with the wrong separator convention it is twelve and a half, the
arithmetic downstream is perfectly correct, and nothing raises. `ingest/numbers.py`
exists for this and is called from here for the first time; before this it had no
caller at all, which was its own kind of defect.

**An unparsed number is recorded, not dropped and not zeroed.** A cell that looks
numeric and refuses to parse is a *fact about the file* — usually a merged cell, a
footnote marker, or a date. Writing `0` for it silently invents a free item; writing
nothing loses the fact that the file said something we could not read. So the row
carries its refusals and the caller decides.

**The line total is compared to quantity times rate, not overwritten by it.** The
corpus's own variation rules acknowledge the two do not always agree — a daywork
line or a provisional sum genuinely does not multiply out. A reader that computes
`quantity * rate` and calls it the total destroys the disagreement, which is the
only evidence that a line needs looking at.

## Dry run is the default, not a flag

`read_table` never writes. `plan_ingest` describes what *would* be written and is
what `scripts/ingest_corpus.py` prints. A first pass over 221 workbooks that writes
whatever the detector finds is a first pass that fills the material master with
confident garbage, and the cost of that is not recoverable by deleting rows later —
it is that nobody trusts the table afterwards.

## What this does not do

It does not create materials, suppliers, or units. It produces `IngestRow` objects
with everything unresolved kept unresolved, and a separate step decides what to do
with a row that still has a refusal in it. That separation is the point: the reader's
job is to be honest about what the file said, and deciding policy on top of that is
a different question with a different owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ai_orchestrator.ingest.numbers import Refusal, is_number, parse_number
from ai_orchestrator.ingest.sheets import SheetKind, SheetShape

#: The column that *names* a row, per sheet kind.
#:
#: This mapping is not tidiness — it is the difference between reading a table and
#: reading nothing. The first version of `read_table` tested for a `name` cell and
#: stopped at the first row without one, which is correct for a price schedule and
#: means **zero rows** for every checklist: an inspection checklist names its rows
#: in `Nội dung kiểm tra` and a delivery checklist in `Nội dung bàn giao`, and
#: neither has a `name` column at all. The dry run found three such tables and
#: reported 0 rows for every one, which reads as "these sheets are empty" rather
#: than "the reader does not know what a row is called here".
#:
#: A checklist's text lands in `IngestRow.name` so the row type stays uniform, and
#: `IngestRow.kind` records where it came from so a caller is not guessing.
LABEL_COLUMN: dict[SheetKind, str] = {
    SheetKind.PRICE_SCHEDULE: "name",
    SheetKind.GOODS_LINES: "name",
    SheetKind.MATERIAL_LIST: "name",
    SheetKind.INSPECTION_CHECKLIST: "check_item",
    SheetKind.DELIVERY_CHECKLIST: "handover_item",
}

#: The unit codes this reader will accept, and the spellings that map to them.
#:
#: Measured from the corpus: `Bộ` 17 and `bộ` 7, `Cái` 34 and `cái` 6, `Cuộn` 2,
#: `M` 1. Every key is already diacritic-stripped and lowercased, because that is
#: the form the unit label is compared in — the same normalisation the sheet
#: vocabulary uses, for the same reason.
#:
#: A unit absent from this table is a refusal. That is the design: the alternative
#: is a default, and a wrong default is a silent reconciliation failure three
#: tranches later.
UNIT_ALIASES: dict[str, str] = {
    "bo": "bo",
    "cai": "cai",
    "chong": "cai",
    "chuc": "cai",
    "m": "m",
    "m2": "m2",
    "m3": "m3",
    "cuon": "cuon",
    "kg": "kg",
    "tan": "tan",
    "hop": "hop",
    "thung": "thung",
    "lo": "lo",
    "can": "can",
    "cay": "cay",
    "bang": "bang",
}

#: How far below a header to read. A price schedule's rows are contiguous; a
#: contract's appendix has totals and signatures underneath that are not items.
MAX_DATA_ROWS = 2000


def resolve_unit(label: object) -> str | None:
    """Map a unit label to its ASCII code, or `None` if it is not one we know.

    Normalisation is the whole function, and it is the same normalisation the header
    vocabulary uses: `Bộ` and `bộ` are one unit, and a copy of the label would make
    them two. `None` means "unresolved" and is never replaced with a default.
    """
    from ai_orchestrator.ingest.sheets import normalise

    token = normalise(label)
    if not token:
        return None
    # `m2` is written `m²` and `M2` in the same corpus; `normalise` leaves the
    # superscript alone, so the digit forms are the ones registered.
    return UNIT_ALIASES.get(token)


@dataclass(frozen=True, slots=True)
class IngestRow:
    """One line of a detected table, with everything unresolved still unresolved.

    `quantity` and `unit_rate` are `None` when the cell was not a number, and
    `unit_code` is `None` when the unit is not one this reader knows. Nothing here
    is defaulted, because a default is indistinguishable from a real value once it
    is in the database.
    """

    line_no: int
    name: str
    #: The sheet's own `STT` cell, verbatim.
    #:
    #: Separate from `line_no` because the two disagree. `line_no` is an integer and
    #: falls back to row order when the cell is not a number; the inspection
    #: checklist marks its *sections* with Roman numerals (`I. Hồ sơ - tài liệu`)
    #: and its items with digits. Turning `I` into `1` states an ordering the file
    #: does not, and it collides with the real item 1 immediately below it. So the
    #: text is kept and the integer is derived, never the other way round.
    line_label: str = ""
    #: Which table this row came from. Carried because `name` holds a price-schedule
    #: item on one sheet and an inspection step on another, and a caller that cannot
    #: tell them apart will write a fire-alarm cabinet into a receipt's checklist.
    kind: str = ""
    code: str = ""
    description: str = ""
    brand: str = ""
    origin_country: str = ""
    manufacturer: str = ""
    drawing_ref: str = ""
    zone_ref: str = ""
    quantity: Decimal | None = None
    unit_code: str | None = None
    unit_rate: Decimal | None = None
    amount: Decimal | None = None
    remark: str = ""
    #: What could not be read, and from which column. Empty means the row is clean,
    #: which is a claim worth making because it is checkable.
    refusals: tuple[str, ...] = ()
    #: The cells this reader did not have a column for. Kept whole, for the same
    #: reason `materials.raw_cells` is: an unlabelled column is data, and a parser
    #: that assigns it a meaning is inventing.
    raw_cells: dict[str, object] = field(default_factory=dict)

    def is_clean(self) -> bool:
        return not self.refusals and self.unit_code is not None

    def amount_disagrees_with_quantity_times_rate(
        self, tolerance: Decimal = Decimal("0.01")
    ) -> bool:
        """Whether the sheet's own total contradicts its own quantity and rate.

        Only meaningful when all three are present. A daywork line or a provisional
        sum legitimately will not multiply out, and the disagreement is the evidence
        that a human needs to look — which is why this reports rather than corrects.
        """
        if self.quantity is None or self.unit_rate is None or self.amount is None:
            return False
        expected = self.quantity * self.unit_rate
        return abs(expected - self.amount) > tolerance


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").strip()


def _number(value: object, refusals: list[str], column: str) -> Decimal | None:
    """Parse a cell, recording a refusal rather than substituting anything.

    `parse_number` handles the Vietnamese separator convention; `is_number` keeps
    blanks and prose out of it so a genuinely non-numeric cell does not produce a
    refusal that nobody can act on.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if is_number(value):
        return Decimal(str(value)).quantize(Decimal("0.000001"))
    parsed = parse_number(value)
    if isinstance(parsed, Refusal):
        refusals.append(f"{column}: {_text(value)!r} refused as {parsed.reason}")
        return None
    return parsed


def _line_no(value: object, fallback: int) -> int:
    """The row's own number, when it is one.

    Falls back to the physical row order. A sheet whose `STT` column is blank or
    non-numeric still has an order, and losing it would make the rows
    indistinguishable — but the fallback is recorded by the caller so a reader never
    presents an invented line number as the document's.
    """
    parsed = parse_number(value)
    if isinstance(parsed, Refusal):
        return fallback
    return int(parsed)


def read_table(
    rows: list[tuple[object, ...]],
    shape: SheetShape,
    *,
    max_rows: int = MAX_DATA_ROWS,
) -> list[IngestRow]:
    """Read one detected table into `IngestRow`s.

    Stops at the first blank row rather than skipping it. A gap in the middle of a
    price schedule is a section break on the contract's appendix, and continuing
    across it merges two sections into one list of items with a continuous line
    numbering — which is a wrong BOQ rather than a missing one.
    """
    out: list[IngestRow] = []
    start = shape.header_row  # 1-based; `rows` is 0-based, so this indexes past it
    label_role = LABEL_COLUMN.get(shape.kind, "name")

    for row in rows[start : start + max_rows]:
        name = _text(_cell(row, shape, label_role))
        if not name:
            # A row with no label is either a sub-header or the end of the table,
            # and telling them apart matters. The measured inspection checklist has
            # a sub-header on its first data row — `Đạt | Không đạt | Không áp
            # dụng` spans three columns with nothing in the label column — and
            # stopping there would read **zero rows of a filled checklist**. A
            # section break, by contrast, is a row with nothing in it at all.
            if _any_content(row):
                continue
            break
        refusals: list[str] = []

        quantity = _number(_cell(row, shape, "quantity"), refusals, "quantity")
        rate = _number(_cell(row, shape, "unit_rate"), refusals, "unit_rate")
        amount = _number(_cell(row, shape, "amount"), refusals, "amount")

        unit_label = _text(_cell(row, shape, "unit"))
        unit_code = resolve_unit(unit_label) if unit_label else None
        if unit_label and unit_code is None:
            refusals.append(f"unit: {unit_label!r} is not a known unit")

        known = {c.index for c in shape.columns}
        raw = {
            str(i): cell for i, cell in enumerate(row) if cell not in (None, "") and i not in known
        }

        out.append(
            IngestRow(
                line_no=_line_no(_cell(row, shape, "line_no"), len(out) + 1),
                line_label=_text(_cell(row, shape, "line_no")),
                name=name,
                kind=str(shape.kind),
                code=_text(_cell(row, shape, "code")),
                description=_text(_cell(row, shape, "description")),
                brand=_text(_cell(row, shape, "brand")),
                origin_country=_text(_cell(row, shape, "origin")),
                manufacturer=_text(_cell(row, shape, "manufacturer")),
                drawing_ref=_text(_cell(row, shape, "drawing_ref")),
                zone_ref=_text(_cell(row, shape, "zone")),
                quantity=quantity,
                unit_code=unit_code,
                unit_rate=rate,
                amount=amount,
                remark="; ".join(
                    p
                    for p in (
                        _check_detail(row, shape),
                        _text(_cell(row, shape, "remark")),
                    )
                    if p
                ),
                refusals=tuple(refusals),
                raw_cells=raw,
            )
        )

    return out


def _any_content(row: tuple[object, ...]) -> bool:
    """Whether a row has anything in it, anywhere.

    Used to tell a sub-header from a section break. A sub-header has content in
    columns the reader is not using for the row's label — the checklist's
    `Đạt / Không đạt / Không áp dụng` spans are exactly that — and a break has
    nothing at all.
    """
    return any(cell not in (None, "") for cell in row)


def _check_detail(row: tuple[object, ...], shape: SheetShape) -> str:
    """An inspection step's method and result, in one readable line.

    Only for the two checklist kinds. A price schedule has no such columns and this
    returns empty, so the caller's `or` falls through to the sheet's own remark
    column unchanged.
    """
    if shape.kind not in (SheetKind.INSPECTION_CHECKLIST, SheetKind.DELIVERY_CHECKLIST):
        return ""
    parts = [
        f"{label}: {value}"
        for role, label in (
            ("check_method", "method"),
            ("check_result", "result"),
            ("quality_docs", "quality docs"),
            ("pass_fail", "outcome"),
        )
        if (value := _text(_cell(row, shape, role)))
    ]
    return "; ".join(parts)


def _cell(row: tuple[object, ...], shape: SheetShape, role: str) -> object:
    index = shape.column_index(role)
    if index is None or index >= len(row):
        return None
    return row[index]


def plan_ingest(rows: list[tuple[object, ...]], shape: SheetShape) -> dict[str, Any]:
    """What reading this table *would* produce, without deciding anything about it.

    The report is shaped around the three questions a person needs answered before
    trusting an import: how many rows, how many are clean, and what the unclean ones
    say. A count alone is not enough — 200 rows with 180 refusals and 200 rows with
    2 refusals are the same number and opposite conclusions.
    """
    parsed = read_table(rows, shape)
    refusals: dict[str, int] = {}
    for row in parsed:
        for refusal in row.refusals:
            key = refusal.split(":", 1)[0]
            refusals[key] = refusals.get(key, 0) + 1
    disagreeing = sum(1 for r in parsed if r.amount_disagrees_with_quantity_times_rate())
    return {
        "kind": str(shape.kind),
        "header_row": shape.header_row,
        "columns": {c.role: c.label for c in shape.columns},
        "rows": len(parsed),
        "clean_rows": sum(1 for r in parsed if r.is_clean()),
        "refusals_by_column": dict(sorted(refusals.items())),
        "amounts_disagreeing_with_qty_x_rate": disagreeing,
        "sample": [
            {
                "line_no": r.line_no,
                "name": r.name,
                "quantity": str(r.quantity) if r.quantity is not None else None,
                "unit": r.unit_code,
                "unit_rate": str(r.unit_rate) if r.unit_rate is not None else None,
                "amount": str(r.amount) if r.amount is not None else None,
                "refusals": list(r.refusals),
            }
            for r in parsed[:5]
        ],
    }


__all__ = [
    "LABEL_COLUMN",
    "MAX_DATA_ROWS",
    "UNIT_ALIASES",
    "IngestRow",
    "plan_ingest",
    "read_table",
    "resolve_unit",
]
