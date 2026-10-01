"""Reading the construction-progress family, where the plan and its outcome share a row.

`ingest/reader.py` reads price schedules and checklists. This reads the `KH`/`TT`
sheets, and it is a separate module because the *hard parts are different* rather
than because the shape is: there are no prices to misread and no units to resolve,
but there are four things a progress sheet can get wrong that a price schedule
cannot, and each of them is a measured property of the one real file.

**A roll-up row is not an observation, and it does not look like one.** The sheets
are three-level — `A` / `BOH`, then `I` / `Hệ thống cấp nước`, then `1`, `2`, `3`
with data — and the first version of this module treated the outer two levels as
headings with nothing in them. They are not empty. In the real file:

    A  BOH   2019-03-13 -> 2019-09-20   Số ngày: (empty)

The project row carries a **window spanning the whole job** and no `Số ngày` at
all. So "has no dates" does not identify a roll-up, and a reader using that test
files the project and each system as two enormous activities — the single largest
duration in the report, invented from a summary row.

The discriminator that works is **`Số ngày` is empty** — and nothing else. Every
activity in the file is costed in days; no roll-up is. The obvious second signal, a
non-numeric `Stt`, was tried first and does not hold: the hierarchy is not
letters-then-digits, it is `A` / `BOH`, then `1` / `Hệ thống cấp thoát nước`, then
`2` / `Hệ thống thông gió và điều hòa` — **all numeric** — and then the activities
beneath them, which are numeric too. `2` is both a system heading and, three sheets
away, an activity.

So the rule is one signal, and it has a known cost: a row with a window and no
duration is either a roll-up or an activity nobody costed, and those are
indistinguishable. It is filed as a roll-up and **counted**, so the number of rows
that went that way is visible rather than assumed.

**`actual_updated` cannot be derived from the schema.** In the one real file the
actual dates are byte-identical to the planned dates on every row, so a variance of
zero is ambiguous between "on time" and "nobody filled it in". This module is the
only place that can see the two columns held the same values, so it records the
fact: a row where the actual columns carry nothing the planned ones do not is
`actual_updated = false`, and a progress review can then say "unrecorded" instead of
"on time".

**A completion figure above 1 is refused, not rescaled.** `% Hoàn thành` holds
`0.65`, `0.8`, `0.9`. A header saying "percentage" over fraction-shaped data is the
kind of contradiction that a reader resolves silently. This one does not: it hands
the value to `domain.progress.completion_ratio`, which refuses, and the refusal
travels with the row.

**A duration that disagrees with its dates is reported, not corrected.** The corpus
counts inclusively, five for five, and the schema enforces it. A reader that
"corrected" the duration would hide the disagreement that the constraint exists to
surface.

**The period is a row above the data, not a column beside it.** The first version read
`period_label` off each activity row and got `0` — on all thirty-five of them, because
the column is a month counter the sheet never advances. The real period and the
report's date are in the two rows *between* the header and the first activity:

    row 14   Stt/No | Công việc thi công | ...        <- the header
    row 15   Tuần 1/Week 1                             <- the period
    row 16   2019-08-01                                <- the date the report was made
    row 17   A   BOH   2019-03-13 -> 2019-09-20         <- the project roll-up

So the preamble is the run of rows after the header with an **empty `Stt/No`**, and
it is read once and applied to every activity. A per-row `period_label` of `0` is
treated as absent rather than stored, because thirty-five identical zeros is a column
nobody maintains and storing them would make `period_label` look populated. A
*different* per-row value is kept, so a sheet that does carry a period per row is not
flattened by a rule written for this one.

This is what makes `report_ref` derivable, which
`persistence/progress.py` has always claimed: the report is the file plus the period,
and the period is in the preamble.

## Dates arrive as three different things

`openpyxl` returns a real `datetime` for a cell Excel holds as a date, a string for
one a person typed, and `None` for an empty one — and this corpus contains all three
in the same column. `_as_date` handles each and **refuses** anything else rather
than guessing, because a date read from the wrong format is a date that is simply
wrong and looks fine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from ai_orchestrator.domain.progress import (
    completion_ratio,
    duration_matches_dates,
    is_adequate,
)
from ai_orchestrator.ingest.numbers import Refusal, parse_number
from ai_orchestrator.ingest.sheets import SheetKind, SheetShape

#: Date formats tried, in order, for a cell a person typed. The corpus's own typed
#: dates are `dd/mm/yyyy`; `yyyy-mm-dd` is what a spreadsheet round-trip produces and
#: is tried first because it is unambiguous.
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")


@dataclass(frozen=True, slots=True)
class ProgressReading:
    """One activity's planned and actual position, as read from a sheet.

    Every measured field is optional and nothing is defaulted. A row with only a
    planned start is a legitimate first reading; a row with a start and a guessed
    finish is not a reading at all.
    """

    #: The row's position in the sheet, absolute (not relative to the header).
    #:
    #: This is the reading's **identity within its report**, and it is positional
    #: because nothing else will do. Measured over the corpus — 16 progress sheets,
    #: 465 readings:
    #:
    #:     (section, line)                    collides on 3 sheets
    #:     (section, line, line_no)           collides on 1
    #:     (section, line, work_description)  collides on 1
    #:     all four together                   still collides on 1
    #:
    #: `TĐ Hạ Tầng.xlsx :: TĐ INF` has 68 readings of which **5 keys appear twice**,
    #: identical in section, line, line number and work description — the same activity
    #: listed twice in one sheet. No combination of the fields a reading carries is
    #: unique there, so the identity has to be where the row physically is. That is
    #: also the only identity that survives a file which repeats itself, and a
    #: duplicate here is a fact about the corpus rather than a reading error, so it is
    #: recorded rather than refused.
    source_row: int

    line_label: str
    line_no: int
    section_label: str = ""
    work_description: str = ""
    zone_ref: str = ""
    system_code: str = ""
    item_ref: str = ""
    period_label: str = ""
    drawing_name: str = ""
    engineer_comment: str = ""
    planned_start_on: dt.date | None = None
    planned_finish_on: dt.date | None = None
    planned_duration_days: int | None = None
    actual_start_on: dt.date | None = None
    actual_finish_on: dt.date | None = None
    actual_duration_days: int | None = None
    #: See the module docstring. `False` when the actual columns carry nothing the
    #: planned ones do not, `None` when there is no actual data at all to judge.
    actual_updated: bool | None = None
    completion_ratio: float | None = None
    item_completion_ratio: float | None = None
    status_text: str = ""
    is_adequate: bool | None = None
    handover_planned: int | None = None
    handover_actual: int | None = None
    refusals: tuple[str, ...] = ()
    raw_cells: dict[str, object] = field(default_factory=dict)

    def is_clean(self) -> bool:
        return not self.refusals


@dataclass(frozen=True, slots=True)
class ProgressRead:
    """What one sheet yielded, including what was deliberately not read.

    `skipped_sections` is reported rather than discarded silently: a reader that
    quietly drops rows is a reader whose row counts cannot be checked against the
    file.

    `period_label` and `observed_on` are the *report's*, read from the preamble rows
    between the header and the first activity, and applied to every reading. They
    live here rather than on each row because a report has one period and a sheet has
    one preamble, and repeating thirty-five copies of a fact that has one value is how
    a column stops meaning anything.
    """

    readings: tuple[ProgressReading, ...] = ()
    skipped_sections: int = 0
    period_label: str = ""
    observed_on: dt.date | None = None
    rejected: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        refusals: dict[str, int] = {}
        for r in self.readings:
            for refusal in r.refusals:
                key = refusal.split(":", 1)[0]
                refusals[key] = refusals.get(key, 0) + 1
        return {
            "rows": len(self.readings),
            "clean_rows": sum(1 for r in self.readings if r.is_clean()),
            "skipped_sections": self.skipped_sections,
            "period_label": self.period_label,
            "observed_on": self.observed_on.isoformat() if self.observed_on else None,
            "refusals_by_column": dict(sorted(refusals.items())),
            "rows_where_actual_was_never_updated": sum(
                1 for r in self.readings if r.actual_updated is False
            ),
        }


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).replace("\n", " ").strip()


def _as_date(value: object) -> tuple[dt.date | None, str | None]:
    """A cell as a date, or `None`, or a refusal explaining why not.

    The three shapes `openpyxl` produces for a date column are all handled: a real
    `datetime`, a `date`, and a string a person typed. Anything else is refused —
    an integer in a date column is a spreadsheet serial number or a typo, and
    converting it would produce a date that is confidently wrong.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, dt.datetime):
        return value.date(), None
    if isinstance(value, dt.date):
        return value, None
    text = str(value).strip()
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date(), None
        except ValueError:
            continue
    return None, f"date: {text!r} is not a date in any known format"


def _as_int(value: object, refusals: list[str], column: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        refusals.append(f"{column}: {value!r} is a boolean, not a count")
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        refusals.append(f"{column}: {value!r} is not a whole number of days")
        return None
    parsed = parse_number(value)
    if isinstance(parsed, Refusal):
        refusals.append(f"{column}: {text_of(value)!r} refused as {parsed.reason}")
        return None
    if parsed != parsed.to_integral_value():
        refusals.append(f"{column}: {text_of(value)!r} is not a whole number of days")
        return None
    return int(parsed)


def _as_ratio(value: object, refusals: list[str], column: str) -> float | None:
    """A completion cell, handed to the domain rule rather than guessed at here.

    `domain.progress.completion_ratio` refuses anything above 1. A refusal from it
    is recorded with the column name so the row says *which* figure was ambiguous.

    Returns a value or `None` and appends to `refusals` — **not** a `(value, error)`
    pair. `_as_date` returns a pair because its caller wants to inspect the reason
    before deciding whether it is worth recording; this one has no such choice, and
    an earlier version unpacked it as a pair anyway, which turned every caller into
    a `TypeError`.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        refusals.append(f"{column}: {value!r} is a boolean, not a completion")
        return None
    # `parse_number` first, not `float(value)`: a completion typed as `0,65` is the
    # Vietnamese decimal comma and `float()` would raise on it, while the number
    # parser reads it. Going through the parser also means one definition of "is
    # this a number" for the whole ingest rather than one per column type.
    parsed = parse_number(value)
    if isinstance(parsed, Refusal):
        refusals.append(f"{column}: {text_of(value)!r} refused as {parsed.reason}")
        return None
    number = float(parsed)
    ratio = completion_ratio(number)
    if ratio is None:
        refusals.append(
            f"{column}: {text_of(value)!r} is outside 0..1; the header says percentage "
            f"and the column holds a fraction, so this is not rescaled automatically"
        )
        return None
    return ratio


def text_of(value: object) -> str:
    return "" if value is None else str(value).replace("\n", " ").strip()


def _cell(row: tuple[object, ...], shape: SheetShape, role: str) -> object:
    index = shape.column_index(role)
    if index is None or index >= len(row):
        return None
    return row[index]


def _is_actual_distinct(planned: object, actual: object) -> bool | None:
    """Whether the actual column says anything the planned one does not.

    This is the whole of `actual_updated`, and it can only be computed here: it needs
    to see that two columns the schema stores separately held the same values. The
    comparison is on the raw cell text rather than on parsed dates, so a cell
    formatted differently but meaning the same day does not count as an update —
    which is the correct reading, because nobody re-typed the date.
    """
    p, a = text_of(planned), text_of(actual)
    if not p and not a:
        return None
    if not a:
        return False
    if not p:
        return True
    if p == a:
        return False
    # Same day written two ways still means nobody updated it.
    pd, _ = _as_date(planned)
    ad, _ = _as_date(actual)
    # Same day written two ways still means nobody updated it, so the answer is
    # "equal dates" rather than the negated form of the check above.
    return not (pd is not None and ad is not None and pd == ad)


def _read_preamble(rows: list[tuple[object, ...]], shape: SheetShape) -> tuple[str, dt.date | None]:
    """The report's period and date, from the rows between header and first activity.

    The preamble is the run of rows after the header whose `Stt/No` is empty, which
    is what the measured file has: a period on the first, a date on the second, and
    the project roll-up immediately after. The run ends at the first row with a
    number in it, so a heading further down the sheet cannot be mistaken for one.

    A cell that parses as a date is the report date; anything else non-empty is the
    period label. That split is measured rather than assumed — the sheet puts a date
    in the same column as a week number, and the only way to tell them apart is to try
    to parse one.
    """
    period = ""
    observed: dt.date | None = None
    for row in rows[shape.header_row :]:
        if _text(_cell(row, shape, "line_no")):
            break  # the preamble ends at the first numbered row
        raw = _cell(row, shape, "period_label")
        if raw in (None, ""):
            continue
        parsed, _ = _as_date(raw)
        if parsed is not None and observed is None:
            observed = parsed
        elif not period:
            period = _text(raw)
    return period, observed


def _own_or_report_period(own: str, report_period: str) -> str:
    """A row's own period if it has a real one, otherwise the report's.

    The measured file writes `0` in this column on all thirty-five activity rows — a
    month counter nobody advances — and storing thirty-five identical zeros would make
    `period_label` look populated while carrying no information. A *different* value
    is kept, so a sheet that genuinely carries a period per row is not flattened by a
    rule written for this one.
    """
    if own and own.strip() not in {"0", "0.0"}:
        return own
    return report_period


def _is_rollup(planned_days: object, actual_days: object) -> bool:
    """Whether a row is a level of the hierarchy rather than an activity.

    A missing `Số ngày` on both halves. That is the whole rule, and the label plays
    no part in it: the corpus numbers its system headings `1`, `2`, `I` and `A`
    interchangeably, so a label test would file a system heading as an activity with
    a 123-day duration — the largest number in the report, invented from a summary
    row.

    A row with a window and no duration is either a roll-up or an activity nobody
    costed. The two are indistinguishable, so the row is filed as a roll-up and
    counted in `skipped_sections` rather than being resolved on a guess.
    """
    return planned_days in (None, "") and actual_days in (None, "")


def read_progress(rows: list[tuple[object, ...]], shape: SheetShape) -> ProgressRead:
    """Read one construction-progress table.

    Roll-up rows are skipped, counted, and their label carried forward onto the
    activities beneath them, because losing that would leave every activity without
    its parent and a progress report is a tree rather than a list.
    """
    if shape.kind is not SheetKind.CONSTRUCTION_PROGRESS:
        raise ValueError(
            f"read_progress got a {shape.kind!s} shape; it reads "
            f"{SheetKind.CONSTRUCTION_PROGRESS!s} and nothing else"
        )

    out: list[ProgressReading] = []
    skipped = 0
    current_section = ""
    known = {c.index for c in shape.columns}
    report_period, observed_on = _read_preamble(rows, shape)

    for source_row, row in enumerate(rows[shape.header_row :], start=shape.header_row):
        refusals: list[str] = []
        label = _text(_cell(row, shape, "line_no"))
        raw_planned_days = _cell(row, shape, "planned_duration_days")
        raw_actual_days = _cell(row, shape, "actual_duration_days")

        if _is_rollup(raw_planned_days, raw_actual_days):
            heading = _text(_cell(row, shape, "work_description"))
            if heading:
                current_section = heading
            skipped += 1
            continue

        planned_start, p_start_err = _as_date(_cell(row, shape, "planned_start_on"))
        planned_finish, p_finish_err = _as_date(_cell(row, shape, "planned_finish_on"))
        actual_start, a_start_err = _as_date(_cell(row, shape, "actual_start_on"))
        actual_finish, a_finish_err = _as_date(_cell(row, shape, "actual_finish_on"))
        for err in (p_start_err, p_finish_err, a_start_err, a_finish_err):
            if err:
                refusals.append(err)
        completion = _as_ratio(_cell(row, shape, "completion_ratio"), refusals, "completion_ratio")

        if planned_start is None and planned_finish is None and completion is None:
            # No window, no figure, no duration: a heading with nothing in it.
            # Kept as the parent of what follows.
            heading = _text(_cell(row, shape, "work_description"))
            if heading:
                current_section = heading
            skipped += 1
            continue

        if label and not label.strip().isdigit():
            refusals.append(
                f"line_no: {label!r} is not a number but the row carries a duration; "
                f"it is read as an activity, and the hierarchy may have another level"
            )

        planned_days = _as_int(
            _cell(row, shape, "planned_duration_days"), refusals, "planned_duration_days"
        )
        actual_days = _as_int(
            _cell(row, shape, "actual_duration_days"), refusals, "actual_duration_days"
        )
        # Reported, never corrected — the schema refuses these, and a reader that
        # fixed them would hide the disagreement the constraint exists to surface.
        if not duration_matches_dates(planned_days, planned_start, planned_finish):
            refusals.append(
                f"planned_duration_days: {planned_days} does not match "
                f"{planned_start}..{planned_finish} counted inclusively"
            )
        if not duration_matches_dates(actual_days, actual_start, actual_finish):
            refusals.append(
                f"actual_duration_days: {actual_days} does not match "
                f"{actual_start}..{actual_finish} counted inclusively"
            )

        item_completion = _as_ratio(
            _cell(row, shape, "item_completion_ratio"), refusals, "item_completion_ratio"
        )

        status = _text(_cell(row, shape, "status_text"))
        line_label = label
        line_no = _as_int(_cell(row, shape, "line_no"), [], "line_no")

        out.append(
            ProgressReading(
                source_row=source_row,
                line_label=line_label,
                line_no=line_no if line_no is not None else len(out) + 1,
                section_label=current_section,
                work_description=_text(_cell(row, shape, "work_description")),
                zone_ref=_text(_cell(row, shape, "zone")),
                system_code=_text(_cell(row, shape, "system_code")),
                item_ref=_text(_cell(row, shape, "item_ref")),
                period_label=_own_or_report_period(
                    _text(_cell(row, shape, "period_label")), report_period
                ),
                drawing_name=_text(_cell(row, shape, "drawing_name")),
                engineer_comment=_text(_cell(row, shape, "engineer_comment")),
                planned_start_on=planned_start,
                planned_finish_on=planned_finish,
                planned_duration_days=planned_days,
                actual_start_on=actual_start,
                actual_finish_on=actual_finish,
                actual_duration_days=actual_days,
                actual_updated=_is_actual_distinct(
                    _cell(row, shape, "planned_start_on"),
                    _cell(row, shape, "actual_start_on"),
                ),
                completion_ratio=completion,
                item_completion_ratio=item_completion,
                status_text=status,
                is_adequate=is_adequate(status),
                handover_planned=_as_int(
                    _cell(row, shape, "handover_planned"), refusals, "handover_planned"
                ),
                handover_actual=_as_int(
                    _cell(row, shape, "handover_actual"), refusals, "handover_actual"
                ),
                refusals=tuple(refusals),
                raw_cells={
                    str(i): c for i, c in enumerate(row) if c not in (None, "") and i not in known
                },
            )
        )

    return ProgressRead(
        readings=tuple(out),
        skipped_sections=skipped,
        period_label=report_period,
        observed_on=observed_on,
    )


__all__ = [
    "DATE_FORMATS",
    "ProgressRead",
    "ProgressReading",
    "read_progress",
]
