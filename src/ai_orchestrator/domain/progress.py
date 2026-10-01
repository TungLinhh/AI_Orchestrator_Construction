"""Progress arithmetic, as pure functions.

Same split as `domain/gates.py`: the rules live here with no database, and
`application/` writes. The reason to bother is that these rules are *checkable
against the corpus* and therefore worth pinning, rather than being arithmetic that
gets re-derived at each call site and drifts.

## Inclusive day counting, and why it is the rule

The corpus's construction-progress sheets carry a planned start, a planned finish and
a `Số ngày` (number of days) for the same activity. Measured on every data row of
`TĐ BOH.xlsx :: TĐ .BOH`:

| Start | Finish | `Số ngày` | `finish - start + 1` |
|---|---|---|---|
| 2019-03-13 | 2019-03-14 | 2 | 2 |
| 2019-04-17 | 2019-04-26 | 10 | 10 |
| 2019-04-01 | 2019-04-20 | 20 | 20 |
| 2019-06-30 | 2019-07-30 | 31 | 31 |
| 2019-08-20 | 2019-09-03 | 15 | 15 |

Five for five, inclusive. The exclusive count would be 1, 9, 19, 30, 14 — off by one
on every row. So the convention is inclusive calendar days, and it is encoded as a
check constraint rather than left to each reader to infer, because a duration read
with the wrong convention is wrong by one day on every activity and nothing raises.

**This is a strict constraint and that is deliberate.** A schedule that counts
working days, or excludes holidays, will be *refused* rather than quietly accepted.
That is the same trade as `units_dictionary` having no default: a refusal is
recoverable by dropping a constraint and re-reading, while a duration computed on
the wrong convention is indistinguishable from a right one everywhere downstream.

## Variance, and the measurement that makes it suspect

`variance_days` is `actual_duration - planned_duration`, and it is the number a
progress review is actually about. It is computed here rather than stored, so there
is no column to fall out of date.

In the one real file, **planned and actual are identical on every row** — the actual
columns were never updated from the plan. So a variance computed from this corpus is
0 days for every activity, which is not "on time", it is "nobody filled in the
actual column". The two are indistinguishable in the data and must be
distinguished in the reader, which is why `actual_updated` exists on the table: a
variance of zero on a row whose actual dates are byte-identical to its planned dates
is a statement about the discipline, not about the project.
"""

from __future__ import annotations

import datetime as dt

#: `Tình trạng` in the corpus holds `YES` on every observed row. The header's name
#: means "status" and suggests a richer vocabulary, so the text is authoritative and
#: this is the set that maps to a boolean without guessing. Both `YES`/`NO` and
#: `CÓ`/`KHÔNG` appear across Vietnamese construction forms.
ADEQUATE_YES = ("yes", "co", "có", "y")
ADEQUATE_NO = ("no", "khong", "không", "n")


def expected_duration_days(start: dt.date, finish: dt.date) -> int:
    """Calendar days from `start` to `finish`, counting both ends.

    One for a same-day activity, because a task done between 08:00 and 17:00 on one
    day took one day. This is what the corpus means by `Số ngày`.
    """
    return (finish - start).days + 1


def duration_matches_dates(
    duration: int | None, start: dt.date | None, finish: dt.date | None
) -> bool:
    """Whether a reported duration agrees with the dates beside it.

    `True` whenever any of the three is missing: there is nothing to disagree with,
    and a constraint that fired on absent data would refuse every partially-filled
    row, which is most of a progress sheet in week one.
    """
    if duration is None or start is None or finish is None:
        return True
    return duration == expected_duration_days(start, finish)


def variance_days(planned: int | None, actual: int | None) -> int | None:
    """How many days longer the actual took than the plan allowed.

    `None` when either side is absent. A variance of `0` from a row where both sides
    are present *and equal* means on time; a variance of `0` where the actual was
    never recorded means nothing at all, and the caller has to tell those apart —
    which is what `actual_updated` is for.
    """
    if planned is None or actual is None:
        return None
    return actual - planned


def is_adequate(text: str | None) -> bool | None:
    """Map a `Tình trạng` cell to a boolean, or `None` if it says neither.

    `None` rather than `False` for an unrecognised value, because "the sheet said
    something I do not understand" and "the sheet said no" are different facts and
    collapsing them is how a sheet nobody maintains comes to read as a sheet where
    everything is deficient.
    """
    if text is None:
        return None
    token = text.strip().lower()
    if token in ADEQUATE_YES:
        return True
    if token in ADEQUATE_NO:
        return False
    return None


def completion_ratio(value: float | None) -> float | None:
    """Normalise a completion cell, refusing the ambiguous ones rather than scaling.

    The corpus header reads `% Hoàn thành` — "percentage complete" — and the cells
    hold `0.65`, `0.8`, `0.9`, `0`. A fraction, not a percentage. Reading `0.65` as
    0.65% would report five healthy activities as barely started.

    So a value in `0..1` is taken as a ratio, and anything above 1 is **refused**
    (`None`) rather than divided by 100. Dividing is the tempting repair and it is
    wrong: a genuine `65` on a future sheet and a mis-keyed `0.65` on this one are
    indistinguishable, and guessing which one we have is how a completion figure
    ends up a hundred times out with no error anywhere. The check constraint on
    `completion_ratio` then refuses the row, and a person decides.
    """
    if value is None:
        return None
    if value < 0:
        return None
    if value > 1:
        return None
    return value


__all__ = [
    "ADEQUATE_NO",
    "ADEQUATE_YES",
    "completion_ratio",
    "duration_matches_dates",
    "expected_duration_days",
    "is_adequate",
    "variance_days",
]
