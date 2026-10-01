"""Construction progress, as reported on the project's own sheets.

Migration `0014`. One table, and the design question it answers is the one
`docs/PRODUCT_GAP.md` §4a left open: **the `KH`/`TT` pairing.**

## The decision: planned and actual live on the same row

The corpus's construction-progress sheets pair them column for column on one row —
planned start, actual start, planned finish, actual finish, planned duration, actual
duration — and the question the sheet exists to answer is the difference between
them. So they are six columns of one row, not two tables.

The alternative is one `progress_observations` table with a `basis` discriminator
(`planned` / `actual`), which normalises better and is wrong here. The variance
question becomes a self-join on the activity *and* the basis, and the thing that
makes the sheet valuable — a plan and its outcome side by side, on one line a site
manager can point at — is reconstructed rather than stored. Splitting them also
makes an un-updated actual indistinguishable from a genuinely on-time one, which is
the failure `actual_updated` below exists to prevent.

This is deliberately *not* the same shape as `material_reconciliations`, and the two
must not be merged. That table models **milestones** for materials (requested,
ordered, expected, delivered) from the `VẬT TƯ` zone sheets. This one models
**durations** for construction activities. Different subject, different question,
different arithmetic.

## Two measurements from the only real file that shaped the columns

**`% Hoàn thành` holds a fraction, not a percentage.** The header says percentage
and the cells hold `0.65`, `0.8`, `0.9`, `0`. So `completion_ratio` is a ratio
constrained to `0..1`, and `domain/progress.completion_ratio` *refuses* a value
above 1 rather than dividing it by 100. Dividing is the tempting repair and it is
unjustifiable: a genuine `65` on a future sheet and a mis-keyed `0.65` on this one
look the same, and silently choosing between them is how a completion figure ends up
a hundred times out with no error raised anywhere. Refusing puts a person on it.

**`Số ngày` is an inclusive calendar day count, and it is a constraint.** Five for
five on the only real example (`2019-04-17`→`04-26` is `10`, exclusive would be `9`).
So `planned_duration_days` must equal `planned_finish_on - planned_start_on + 1`,
and likewise for the actual pair. A schedule counting working days will be refused
rather than quietly accepted, which is the same trade as `units_dictionary` having
no default: a refusal is recoverable, a wrong convention is invisible.

## `actual_updated` exists because of what the corpus actually contains

In `TĐ BOH.xlsx :: TĐ .BOH`, **every data row has planned dates identical to its
actual dates** — the actual columns were never filled in from the plan. A variance
computed from that corpus is 0 days for every activity, which is not "on time", it
is "nobody recorded the actual". The two are indistinguishable in the numbers, so
they are distinguished by a fact: whether the actual columns carry anything the
planned ones do not. A reader sets this; a variance of zero on a row where it is
false is a gap in the record, and the progress review is the place that should say so.

## What is a row

A row is an **activity with content**. The sheets are two-level — `A` / `BOH`, then
`I` / `Hệ thống cấp`, then `1`, `2`, `3` with data — and the section rows are
headings, not observations. `section_label` keeps the heading text so an activity
knows its parent, and the headings themselves are not stored: a section with no dates
and no completion is not a snapshot of anything.

`item_ref` is free text and `wbs_item_id` is a nullable foreign key, deliberately
both. The sheets carry `Hạng mục theo hợp đồng` — the contract's item — as text that
may name a WBS line not yet in the system, and refusing the reading until the WBS is
uploaded would mean losing the progress report, which is the one thing this table
exists to keep.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_orchestrator.persistence.base import Base
from ai_orchestrator.persistence.construction import (
    LONG,
    SHORT,
    ConstructionMixin,
    domain_args,
)

#: A ratio in `0..1`, 4 decimal places. Not `PERCENT` (6,3): this is deliberately
#: *not* a percentage, and the column name says so.
#:
#: Width 7 rather than the obvious 6, and the extra digit is load-bearing.
#: `Numeric(6, 4)` holds at most 99.9999, so a `100` would be refused by the *column
#: width* — `NumericValueOutOfRange` — rather than by the rule that says a completion
#: figure above 1 is not a ratio. The outcome is the same and the meaning is not: a
#: constraint that fires for a reason nobody wrote down is a constraint nobody can
#: reason about later. At width 7 the range check is what refuses, which is the rule
#: doing the work.
RATIO = Numeric(7, 4)

#: A whole number of days. `Integer` and not `Numeric` because the corpus has no
#: fractional durations and a half-day is a different convention that this column
#: should not be able to express by accident.
DAYS = Integer

#: The reporting period a row belongs to. `Tuần 1/Week 1` and a month name both
#: appear, and the sheet mixes them, so the text is kept verbatim rather than being
#: forced into a week-or-month choice the file does not make.
PERIOD = SHORT


class ProgressSnapshot(ConstructionMixin, Base):
    """One construction activity's planned and actual position, as reported.

    A snapshot rather than a running total: the corpus's sheets are *periodic*
    reports (`Tuần 1/Week 1`, a month column), so the same activity appears once per
    report and the history is the point. `observed_on` is therefore the date the
    report was made, not the date the work happened — and it is nullable, because a
    row lifted from a sheet whose period column was never filled in is still a true
    reading of that activity, just an undated one.
    """

    __tablename__ = "progress_snapshots"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    #: Groups the rows of one report. The sheets have no report identifier, so this
    #: is assigned by the reader from the file plus the period, and is what makes
    #: "replace the reading for report N" a single statement rather than a search.
    report_ref: Mapped[str] = mapped_column(SHORT, nullable=False)
    #: The reading's position in the sheet it came from, and its **identity** within
    #: its report. Added by migration `0018`.
    #:
    #: Positional because nothing derived will do: over 16 corpus sheets and 465
    #: readings, `(section_label, line_label)` collides on 3, adding `line_no` and
    #: `work_description` still collides on 1 — `TĐ Hạ Tầng.xlsx :: TĐ INF` lists the
    #: same activity twice, identically in every field. The row number is a fact about
    #: the file rather than an inference from it.
    #:
    #: `NOT NULL` and not nullable, because a NULL in a unique key is distinct from
    #: every other NULL in Postgres: a nullable column here would quietly stop being an
    #: identity for exactly the rows that had no value.
    source_row: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: The sheet's own `Stt/No`, verbatim.
    #:
    #: Not an integer, and this is the point. The sheets number their *sections*
    #: with letters and Roman numerals (`A`, `I`) and their activities with digits,
    #: in one column. An integer column either loses the section labels or renumbers
    #: them — and renumbering `I` to `1` collides with the real activity 1 directly
    #: beneath it. `line_no` is the derived integer, for ordering; this is the fact.
    line_label: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: The heading this activity sits under, e.g. `I. Hệ thống cấp nước`. The
    #: section rows themselves are not stored — they carry no dates and no
    #: completion, so they are not observations of anything.
    section_label: Mapped[str] = mapped_column(LONG, nullable=False, default="", server_default="")
    #: The project this activity belongs to.
    #:
    #: Scoped by a composite foreign key rather than `ForeignKey("projects.id")`.
    #: A bare FK is satisfied by *any* project id in the database, and that is a
    #: corrupt pointer rather than a leak — RLS stops one tenant reading another's
    #: project, so nothing is disclosed, and the damage shows up much later as a
    #: progress report scoped to one project that silently contains another
    #: tenant's activities. It was measured, not assumed: a test inserted a snapshot
    #: naming another tenant's project and the row was accepted.
    #:
    #: `NULL` is still allowed, and a composite FK is not enforced when any of its
    #: columns is NULL (MATCH SIMPLE), which is what makes the form usable — a
    #: progress row not yet assigned to a project is an ordinary thing to record.
    project_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The WBS line this activity delivers, when the system already knows it.
    #: Composite-scoped for the same reason, and nullable and paired with the free
    #: text below; see the module docstring.
    wbs_item_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The work package this reading belongs to. Added by migration `0017`.
    #:
    #: Distinct from `wbs_item_id`, which points at a *priced BOQ line* in
    #: `wbs_items`. The corpus's progress activities carry a window and a duration and
    #: no quantity and no rate, so there is nothing to write into `wbs_items` without
    #: inventing a number -- which is why the two halves of the construction record
    #: were unlinkable until this column existed.
    #:
    #: Nullable rather than backfilled: a reading taken before this migration has no
    #: node to point at, and a reading with no node is a true reading of the file, just
    #: an unplaced one. `readings` in the reader distinguishes exactly that case.
    wbs_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: `Hạng mục theo hợp đồng` verbatim. Kept whatever `wbs_item_id` says, because
    #: the two disagreeing is the finding — a sheet pointing at a WBS line the
    #: project has since renumbered is exactly what a progress review needs to see.
    item_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: `Công việc thi công` — the work, in the engineer's words. Not `wbs_items.
    #: description`, because this is what the sheet says today and the two are
    #: allowed to differ.
    work_description: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )
    #: `Hệ thống` — PW, LV, WD, AC or FP. The same five as `materials.system_code`
    #: and `rfqs.system_code`, checked against the same closed list, because a
    #: progress row that says `ZZ` is a typo that would split the report.
    system_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    #: `Khu vực thi công`. Free text: the corpus's zones are named per project and
    #: are not the controlled vocabulary in `zones`.
    zone_ref: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: `Tuần 1/Week 1`, a month name, or empty. Verbatim.
    period_label: Mapped[str] = mapped_column(PERIOD, nullable=False, default="", server_default="")
    #: The date the report was made. Nullable — see the class docstring.
    observed_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # -- the KH half ---------------------------------------------------------
    planned_start_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    planned_finish_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    planned_duration_days: Mapped[int | None] = mapped_column(DAYS, nullable=True)

    # -- the TT half ---------------------------------------------------------
    actual_start_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_finish_on: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual_duration_days: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    #: Whether anyone filled the actual columns in from the plan.
    #:
    #: The only real file has them byte-identical to the planned dates on every row,
    #: so a computed variance of zero is ambiguous between "on time" and "not
    #: recorded". This is the fact that separates them, and it is set by the reader
    #: because only the reader can see that the two columns held the same values.
    actual_updated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # -- measures ------------------------------------------------------------
    #: `% Hoàn thành` as a **ratio**, constrained to `0..1`. The header says
    #: percentage and the data says fraction; see the module docstring.
    completion_ratio: Mapped[float | None] = mapped_column(RATIO, nullable=True)
    #: `Hoàn thành tổng theo hạng mục` — completion rolled up to the contract item.
    #: Distinct from the row's own completion and stored separately, because a
    #: reader that conflates "this activity" with "the item it belongs to" reports
    #: every activity as a fraction of a total.
    item_completion_ratio: Mapped[float | None] = mapped_column(RATIO, nullable=True)
    #: `Tình trạng` verbatim, and a boolean derived from it.
    status_text: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    is_adequate: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: `HPNC KH` / `HPNC TT`. **The meaning is not established.**
    #:
    #: Two small integers per row in the one real file (2, 6, 6, 6, 3), a planned and
    #: an actual value, on a weekly progress sheet. Read alongside `Tháng` the
    #: candidates are handover, a week number, or a milestone index, and the corpus
    #: does not say which. They are stored as integers with the ambiguity recorded
    #: rather than being given a name that would imply an answer. Tập 3's handover
    #: and acceptance forms are where this should be settled.
    handover_planned: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    handover_actual: Mapped[int | None] = mapped_column(DAYS, nullable=True)
    #: `Phản hồi BQLDA` — the supervising engineer's comment. BQLDA is *Ban Quản lý
    #: Dự án*, the owner's project management department, and its comments are the
    #: one place in this corpus where a person explains why a number is the way it
    #: is. Free text, never parsed.
    engineer_comment: Mapped[str] = mapped_column(
        LONG, nullable=False, default="", server_default=""
    )
    #: `Tên bản vẽ` — the drawing this activity is detailed on.
    drawing_name: Mapped[str] = mapped_column(SHORT, nullable=False, default="", server_default="")
    #: Everything the reader did not have a column for, and could not therefore
    #: claim to understand. The same reasoning as `materials.raw_cells`: an
    #: unlabelled column is data, and a parser that assigns it a meaning is
    #: inventing. The material sheets carry `SSA | HBG | 2` under a header that
    #: names none of them.
    raw_cells: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    __table_args__ = domain_args(
        ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["projects.organization_id", "projects.id"],
            name="fk_progress_snapshots_organization_id_projects",
        ),
        ForeignKeyConstraint(
            ["organization_id", "wbs_id"],
            ["wbs.organization_id", "wbs.id"],
            name="fk_progress_snapshots_wbs",
        ),
        ForeignKeyConstraint(
            ["organization_id", "wbs_item_id"],
            ["wbs_items.organization_id", "wbs_items.id"],
            name="fk_progress_snapshots_organization_id_wbs_items",
        ),
        Index(
            "ix_progress_snapshots_org_late",
            "organization_id",
            "project_id",
            "planned_finish_on",
            "actual_finish_on",
        ),
        Index(
            "ix_progress_snapshots_org_project_observed",
            "organization_id",
            "project_id",
            "observed_on",
        ),
        Index(
            "ix_progress_snapshots_org_wbs_item",
            "organization_id",
            "wbs_item_id",
        ),
        Index(
            "ix_progress_snapshots_org_wbs",
            "organization_id",
            "wbs_id",
        ),
        Index(
            "uq_progress_snapshots_org_report_row",
            "organization_id",
            "report_ref",
            "source_row",
            unique=True,
        ),
        Index(
            "ix_progress_snapshots_org_section_line",
            "organization_id",
            "report_ref",
            "section_label",
            "line_label",
        ),
        CheckConstraint(
            "(((is_adequate IS NULL) OR (lower((status_text)::text) = ANY (ARRAY['yes'::text,"
            " 'no'::text, 'co'::text, 'có'::text, 'khong'::text, 'không'::text, 'y'::text, "
            "'n'::text]))))",
            name="status_flag_known",
        ),
        CheckConstraint(
            "(((system_code IS NULL) OR ((system_code)::text = ANY ((ARRAY['PW'::character va"
            "rying, 'LV'::character varying, 'WD'::character varying, "
            "'AC'::character varying, 'FP'::character varying])::text[]))))",
            name="system_code_known",
        ),
        CheckConstraint(
            "(((work_description <> ''::text) OR (planned_start_on IS NOT NULL) OR (actual_st"
            "art_on IS NOT NULL) OR (completion_ratio IS NOT NULL)))",
            name="row_is_an_observation",
        ),
        CheckConstraint(
            "(((actual_duration_days IS NULL) OR (actual_duration_days >= 1)))",
            name="actual_days_positive",
        ),
        CheckConstraint(
            "(((actual_duration_days IS NULL) OR (actual_start_on IS NULL) OR (actual_finish_"
            "on IS NULL) OR (actual_duration_days = ((actual_finish_on - actual_start_on) + 1"
            "))))",
            name="actual_days_match_dates",
        ),
        CheckConstraint(
            "(((actual_finish_on IS NULL) OR (actual_start_on IS NOT NULL)))",
            name="actual_finish_needs_a_start",
        ),
        CheckConstraint(
            "(((actual_finish_on IS NULL) OR (actual_start_on IS NULL) OR (actual_finish_on >"
            "= actual_start_on)))",
            name="actual_window_ordered",
        ),
        CheckConstraint(
            "(((completion_ratio IS NULL) OR ((completion_ratio >= (0)::numeric) AND (complet"
            "ion_ratio <= (1)::numeric))))",
            name="completion_in_range",
        ),
        CheckConstraint(
            "(((handover_actual IS NULL) OR (handover_actual >= 0)))",
            name="handover_actual_non_negative",
        ),
        CheckConstraint(
            "(((handover_planned IS NULL) OR (handover_planned >= 0)))",
            name="handover_planned_non_negative",
        ),
        CheckConstraint(
            "(((item_completion_ratio IS NULL) OR ((item_completion_ratio >= (0)::numeric) AN"
            "D (item_completion_ratio <= (1)::numeric))))",
            name="item_completion_in_range",
        ),
        CheckConstraint(
            "(((planned_duration_days IS NULL) OR (planned_duration_days >= 1)))",
            name="planned_days_positive",
        ),
        CheckConstraint(
            "(((planned_duration_days IS NULL) OR (planned_start_on IS NULL) OR (planned_fini"
            "sh_on IS NULL) OR (planned_duration_days = ((planned_finish_on - planned_start_o"
            "n) + 1))))",
            name="planned_days_match_dates",
        ),
        CheckConstraint(
            "(((planned_finish_on IS NULL) OR (planned_start_on IS NOT NULL)))",
            name="planned_finish_needs_a_start",
        ),
        CheckConstraint(
            "(((planned_finish_on IS NULL) OR (planned_start_on IS NULL) OR (planned_finish_o"
            "n >= planned_start_on)))",
            name="planned_window_ordered",
        ),
    )


__all__ = ["DAYS", "RATIO", "ProgressSnapshot"]
