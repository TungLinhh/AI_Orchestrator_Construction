"""Writing construction progress, and reading it back.

`domain/progress.py` decides. This module writes, following `gate_operations.py`
exactly: the split exists because the domain purity test enforces it, and a business
rule that can reach a database needs a running system to be tested.

This is the **first domain area with a write path**, and it is deliberately the
smallest and the newest. Fifty-two tables existed before this file and none of them
could be reached from the product; `PRODUCT_GAP.md` §2 measures that three ways.
Building the pattern once, on the area whose reader is freshest and whose corpus row
count is known, is worth more than six copies of a pattern nobody has checked.

## The rule this module exists to enforce

**A reading with refusals is not written.** `ingest/progress_reader.py` reports what
it could not read — a completion figure above 1, a duration that disagrees with its
dates, a date in a format it does not recognise — and every one of those is a case
where the file said something the system does not understand. Writing the row anyway
puts an unreadable figure into a progress report as though it had been read, which
is the same failure as writing `0` for an unparsed quantity: a value that cannot be
told apart from a real one.

So a refused reading is returned to the caller with its reasons, and the count of
them is the output. Over the one real corpus file that is **34 written, 0 refused**,
which is a statement about the file. A file that produced refusals would produce a
number here instead, and the number is the finding.

## What is re-checked here rather than trusted from the reader

The reader already applies `domain.progress`'s rules. They are applied **again** on
the way in, because the reader is one caller and this is the layer that owns the
table. The checks are pure functions, so doing them twice is free, and the failure
mode this prevents is a second caller — a future API endpoint, an agent — reaching
`progress_snapshots` through a path that skipped the domain.

`observed_on` is a required parameter rather than defaulted to today, for the same
reason `gate_operations` requires `decided_at`: a reading's date is part of the
record and it belongs to whoever is reading the sheet, not to the clock. The purity
test allows `datetime.now()` in exactly one file.

## Why the SQL is module constants

Inherited from `gate_operations.py` and for the same reasons, which are recorded
there in full: `organization_id` is `varchar(40)` and an untyped bind makes Postgres
deduce `text` from one context and `varchar` from another, failing with
`AmbiguousParameterError` that names neither the column nor the cause. Every
statement needs `CAST(:o AS varchar(40))`, and naming the whole statement keeps that
cast in one place per query without any linter suppression.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.progress import duration_matches_dates

_INSERT_SNAPSHOT = """
INSERT INTO progress_snapshots (
    id, organization_id, report_ref, source_row, line_label, line_no, section_label,
    project_id, wbs_item_id, item_ref, work_description, system_code, zone_ref,
    period_label, drawing_name, observed_on,
    planned_start_on, planned_finish_on, planned_duration_days,
    actual_start_on, actual_finish_on, actual_duration_days, actual_updated,
    completion_ratio, item_completion_ratio,
    status_text, is_adequate, handover_planned, handover_actual,
    engineer_comment, raw_cells, source, source_actor
) VALUES (
    :i, CAST(:o AS varchar(40)), :report, :source_row, :label, :line_no, :section,
    :project, :wbs_item, :item_ref, :work, :system, :zone,
    :period, :drawing, :observed,
    :p_start, :p_finish, :p_days,
    :a_start, :a_finish, :a_days, :a_updated,
    :completion, :item_completion,
    :status, :adequate, :handover_p, :handover_a,
    :comment, CAST(:raw AS jsonb), :source, CAST(:actor AS varchar(255))
)
"""

_REPORT_EXISTS = """
SELECT 1 FROM progress_snapshots
WHERE organization_id = :o AND report_ref = :report
LIMIT 1
"""

_DELETE_REPORT = """
DELETE FROM progress_snapshots
WHERE organization_id = :o AND report_ref = :report
"""

#: The query the table exists for. `actual_updated IS TRUE` is in the `WHERE`
#: because a row whose actuals were never filled in has a variance of zero that means
#: "not recorded" — and listing it as on time is the failure `actual_updated` was
#: added to prevent. See `persistence/progress.py`.
_LATE_ACTIVITIES = """
SELECT line_label, work_description, section_label, zone_ref, system_code,
       planned_start_on, planned_finish_on,
       actual_start_on, actual_finish_on,
       planned_duration_days, actual_duration_days,
       (actual_duration_days - planned_duration_days) AS variance_days,
       (actual_finish_on - planned_finish_on) AS days_late,
       completion_ratio
FROM progress_snapshots
WHERE organization_id = CAST(:o AS varchar(40))
  AND (CAST(:project AS varchar(40)) IS NULL
       OR project_id = CAST(:project AS varchar(40)))
  AND actual_updated IS TRUE
  AND actual_finish_on IS NOT NULL
  AND planned_finish_on IS NOT NULL
  AND actual_finish_on > planned_finish_on
ORDER BY actual_finish_on - planned_finish_on DESC, line_label
"""

#: Progress for one WBS item, which is how a PM asks the question.
_ITEM_PROGRESS = """
SELECT line_label, work_description, section_label,
       planned_start_on, planned_finish_on, planned_duration_days,
       actual_start_on, actual_finish_on, actual_duration_days,
       (actual_duration_days - planned_duration_days) AS variance_days,
       completion_ratio, actual_updated
FROM progress_snapshots
WHERE organization_id = CAST(:o AS varchar(40))
  AND wbs_item_id = CAST(:item AS varchar(40))
ORDER BY line_no, line_label
"""


@runtime_checkable
class ReadingLike(Protocol):
    """What `write_reading` needs from a row.

    A Protocol rather than a concrete import of `ingest.progress_reader.
    ProgressReading`, and the reason is a layering one: `application` and `ingest` are
    peers, and making the write path depend on the reader would mean the only thing
    that can write a progress row is the thing that reads spreadsheets. A second
    caller — an API request carrying a hand-built row, a test, a future agent —
    would then have to construct a reader output to be allowed to write, which is the
    wrong way round.

    Typing the parameter as `object` instead, which is what this was first, satisfies
    neither: mypy is right that `object` has no attributes, and silencing that with
    `getattr` would move a real type error to runtime.
    """

    line_label: str
    line_no: int
    section_label: str
    item_ref: str
    work_description: str
    system_code: str
    zone_ref: str
    period_label: str
    drawing_name: str
    engineer_comment: str
    planned_start_on: dt.date | None
    planned_finish_on: dt.date | None
    planned_duration_days: int | None
    actual_start_on: dt.date | None
    actual_finish_on: dt.date | None
    actual_duration_days: int | None
    actual_updated: bool | None
    completion_ratio: float | None
    item_completion_ratio: float | None
    status_text: str
    is_adequate: bool | None
    handover_planned: int | None
    handover_actual: int | None
    refusals: tuple[str, ...]
    raw_cells: dict[str, Any]


@runtime_checkable
class ReportLike(Protocol):
    """What `write_report` needs from a whole sheet's reading."""

    readings: tuple[ReadingLike, ...]
    skipped_sections: int
    period_label: str
    observed_on: dt.date | None


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What one report wrote, and what it would not.

    `refused` is a list of `(line_label, reasons)` rather than a count, because the
    reasons are the output. "3 rows refused" sends somebody to look at the file; the
    three reasons send them to the column.
    """

    report_ref: str
    written: int = 0
    refused: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Rows the reader skipped rather than refused: roll-ups and headings. Reported so
    #: the written count can be checked against the file's own row count.
    skipped_sections: int = 0
    ids: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "report_ref": self.report_ref,
            "written": self.written,
            "refused": len(self.refused),
            "skipped_sections": self.skipped_sections,
            "refusal_reasons": {label: list(r) for label, r in self.refused},
            "ids": list(self.ids),
        }


async def write_reading(
    conn: AsyncConnection,
    *,
    organization_id: str,
    report_ref: str,
    reading: ReadingLike,
    observed_on: dt.date | None = None,
    project_id: str | None = None,
    wbs_item_id: str | None = None,
    source: str = "import",
    source_actor: str = "ingest:progress_reader",
) -> tuple[str | None, tuple[str, ...]]:
    """Write one reading, or refuse it and say why.

    Returns `(id, ())` on success and `(None, reasons)` on refusal. The domain rules
    are applied here rather than trusted from the reader, because this is the layer
    that owns the table and the reader is only one of its callers.

    `source` defaults to `import` rather than `human`: a row that came out of a
    spreadsheet did not come from a person who typed it, and the provenance rule
    exists so that a later reader can tell the difference. An agent-proposed row
    would carry `agent_proposal` and a `proposal_id`, which the check constraint
    refuses without.
    """
    reasons: list[str] = list(reading.refusals or ())

    # Re-checked, not trusted. `duration_matches_dates` is pure and free, and the
    # reader is one caller rather than the only one.
    if not duration_matches_dates(
        reading.planned_duration_days, reading.planned_start_on, reading.planned_finish_on
    ):
        reasons.append(
            f"planned_duration_days: {reading.planned_duration_days} does not match "
            f"{reading.planned_start_on}..{reading.planned_finish_on} counted inclusively"
        )
    if not duration_matches_dates(
        reading.actual_duration_days, reading.actual_start_on, reading.actual_finish_on
    ):
        reasons.append(
            f"actual_duration_days: {reading.actual_duration_days} does not match "
            f"{reading.actual_start_on}..{reading.actual_finish_on} counted inclusively"
        )
    if reasons:
        return None, tuple(reasons)

    snapshot_id = f"prs_{new_ulid()}"
    # A savepoint rather than a top-level commit, so a failure part-way through a
    # report leaves the rows already written rather than a half-written report that
    # reads as complete. `gate_operations.py` does the same and says why.
    async with conn.begin_nested():
        await conn.execute(
            text(_INSERT_SNAPSHOT),
            {
                "i": snapshot_id,
                "o": organization_id,
                "report": report_ref,
                "label": reading.line_label,
                "line_no": reading.line_no,
                "source_row": getattr(reading, "source_row", 0),
                "section": reading.section_label,
                "project": project_id,
                "wbs_item": wbs_item_id,
                "item_ref": reading.item_ref,
                "work": reading.work_description,
                "system": reading.system_code or None,
                "zone": reading.zone_ref,
                "period": reading.period_label,
                "drawing": reading.drawing_name,
                "observed": observed_on,
                "p_start": reading.planned_start_on,
                "p_finish": reading.planned_finish_on,
                "p_days": reading.planned_duration_days,
                "a_start": reading.actual_start_on,
                "a_finish": reading.actual_finish_on,
                "a_days": reading.actual_duration_days,
                "a_updated": reading.actual_updated,
                "completion": reading.completion_ratio,
                "item_completion": reading.item_completion_ratio,
                "status": reading.status_text,
                "adequate": reading.is_adequate,
                "handover_p": reading.handover_planned,
                "handover_a": reading.handover_actual,
                "comment": reading.engineer_comment,
                "raw": _json(reading.raw_cells),
                "source": source,
                "actor": source_actor,
            },
        )
    return snapshot_id, ()


def _json(value: object) -> str:
    import json

    return json.dumps(value or {}, ensure_ascii=False, default=str)


async def write_report(
    conn: AsyncConnection,
    *,
    organization_id: str,
    read: ReportLike,
    report_ref: str,
    observed_on: dt.date | None = None,
    project_id: str | None = None,
    wbs_item_id: str | None = None,
    replace: bool = False,
    source: str = "import",
    source_actor: str = "ingest:progress_reader",
) -> WriteOutcome:
    """Write a whole `ProgressRead`, splitting what was read from what was not.

    `observed_on` falls back to the read's own preamble date and then to `None`.
    Both are deliberate: the preamble date is the report's date as the file states it,
    and `None` rather than today because a reading whose date nobody recorded has not
    been misdated, it has been left undated — and `observed_on` is nullable for
    exactly that.

    `replace` deletes the report's existing rows first, which is what re-reading a
    corrected file should do. It defaults off because a silent overwrite of 34 rows on
    a re-run is the kind of thing that should be asked for.
    """
    period = read.period_label or ""
    report_date = observed_on or read.observed_on

    if replace:
        async with conn.begin_nested():
            await conn.execute(
                text(_DELETE_REPORT),
                {"o": organization_id, "report": report_ref},
            )

    written: list[str] = []
    refused: list[tuple[str, tuple[str, ...]]] = []
    for reading in read.readings or ():
        snapshot_id, reasons = await write_reading(
            conn,
            organization_id=organization_id,
            report_ref=report_ref or period or f"report-{new_ulid()[:8]}",
            reading=reading,
            observed_on=report_date,
            project_id=project_id,
            wbs_item_id=wbs_item_id,
            source=source,
            source_actor=source_actor,
        )
        if snapshot_id:
            written.append(snapshot_id)
        else:
            refused.append((reading.line_label, reasons))

    return WriteOutcome(
        report_ref=report_ref or period,
        written=len(written),
        refused=tuple(refused),
        skipped_sections=int(read.skipped_sections or 0),
        ids=tuple(written),
    )


async def report_exists(conn: AsyncConnection, *, organization_id: str, report_ref: str) -> bool:
    """Whether a report has already been written.

    Separate from `write_report` so an operator can ask before replacing 34 rows,
    which is the difference between a confirmation dialog and a lost morning.
    """
    found = (
        await conn.execute(text(_REPORT_EXISTS), {"o": organization_id, "report": report_ref})
    ).scalar()
    return found is not None


async def late_activities(
    conn: AsyncConnection,
    *,
    organization_id: str,
    project_id: str | None = None,
) -> list[dict[str, object]]:
    """Activities that finished after their planned finish, most late first.

    This is the question the table exists to answer, and it is a subtraction across
    the two halves of one row — which is the reason planned and actual share a row
    rather than sitting in two tables.

    Two rows are excluded, and the distinction between them is the whole difficulty
    of a progress report:

    * **Never recorded** — `actual_updated IS NOT TRUE`. Its variance is zero because
      nothing was written, and listing it would report an unmeasured project as
      entirely on schedule. The real corpus file is entirely this: 34 activities, 0
      measured.
    * **On time** — `actual_finish_on > planned_finish_on` is false. Zero days late
      is a real measurement, but it is not lateness, and a list of thirty zeros is not
      a lateness report. The function is named for what it returns, so it filters.
    """
    result = await conn.execute(
        text(_LATE_ACTIVITIES),
        {"o": organization_id, "project": project_id},
    )
    rows = []
    for row in result.mappings().all():
        item = dict(row)
        for key in ("variance_days", "days_late"):
            if item.get(key) is not None:
                item[key] = int(item[key])
        if item.get("completion_ratio") is not None:
            item["completion_ratio"] = float(item["completion_ratio"])
        rows.append(item)
    return rows


async def item_progress(
    conn: AsyncConnection, *, organization_id: str, wbs_item_id: str
) -> list[dict[str, object]]:
    """One WBS item's activities in line order.

    `variance_days` is returned as-is and may be `None` when either duration is
    absent. It is deliberately not coalesced to zero: an activity with no actual
    duration has no variance, and zero is a claim that it finished on time.
    """
    result = await conn.execute(text(_ITEM_PROGRESS), {"o": organization_id, "item": wbs_item_id})
    rows = []
    for row in result.mappings().all():
        item = dict(row)
        if item.get("variance_days") is not None:
            item["variance_days"] = int(item["variance_days"])
        if item.get("completion_ratio") is not None:
            item["completion_ratio"] = float(item["completion_ratio"])
        rows.append(item)
    return rows


__all__ = [
    "ReadingLike",
    "ReportLike",
    "WriteOutcome",
    "item_progress",
    "late_activities",
    "readings_for_node",
    "report_exists",
    "write_reading",
    "write_report",
]


_SELECT_NODE_READINGS = """
SELECT id, report_ref, source_row, line_label, line_no, section_label,
       work_description, period_label, observed_on,
       planned_start_on, planned_finish_on, planned_duration_days,
       actual_start_on, actual_finish_on, actual_duration_days, actual_updated,
       completion_ratio, status_text, is_adequate
FROM progress_snapshots
WHERE organization_id = CAST(:o AS varchar(40))
  -- Every appearance of a bind is cast, *including* the `IS NULL` ones. A bare
  -- `:node IS NULL` is a separate bind from the `:node` inside the `CAST`, and
  -- asyncpg has no type to give it -- so with `node_id=None` the statement fails with
  -- `AmbiguousParameterError: could not determine data type of parameter $2` before
  -- it reads a single row. An optional filter is not optional to type.
  AND (CAST(:node AS varchar(40)) IS NULL OR wbs_id = CAST(:node AS varchar(40)))
  AND (CAST(:report AS varchar(128)) IS NULL
       OR report_ref = CAST(:report AS varchar(128)))
ORDER BY report_ref, source_row
LIMIT :limit
"""


async def readings_for_node(
    conn: ReadConnection,
    *,
    organization_id: str,
    node_id: str | None = None,
    report_ref: str | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """The readings attached to a node, in the order the sheet had them.

    **Ordered by `source_row`**, which is the identity migration `0018` introduced, and
    which is the only order that is unambiguous: `line_label` repeats under every
    system heading, and `TĐ Hạ Tầng.xlsx` lists the same activity twice at different
    rows. Anything else reorders or drops rows that the file genuinely contains.

    `actual_updated` travels with every row and is the field that says whether the
    actual columns mean anything. A consumer that reads `actual_finish_on` without it
    will read the corpus's copied planned date as a measurement — 100% of the real
    file's actual columns are byte-identical to the planned ones.
    """
    if limit < 1 or limit > 1000:
        raise ValueError(f"limit must be between 1 and 1000, got {limit}")
    rows = (
        (
            await conn.execute(
                text(_SELECT_NODE_READINGS),
                {"o": organization_id, "node": node_id, "report": report_ref, "limit": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
