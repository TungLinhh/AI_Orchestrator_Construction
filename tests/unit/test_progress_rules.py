"""Progress arithmetic, with the corpus's own numbers as the fixtures.

The five rows in `TestTheMeasuredFileAgrees` are transcribed from
`TĐ BOH.xlsx :: TĐ .BOH`, the only progress sheet in the corpus with a `KH`/`TT`
pair. They are the whole basis for the inclusive-day-count convention, so they are
asserted here as data rather than described in a comment — a convention stated only
in prose is a convention that gets re-derived wrongly.

`TestCompletionRefusesRatherThanRescales` is the one worth reading twice: the
header says percentage, the cells hold a fraction, and the two possible repairs are
both wrong.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ai_orchestrator.domain.progress import (
    completion_ratio,
    duration_matches_dates,
    expected_duration_days,
    is_adequate,
    variance_days,
)

D = dt.date


class TestTheMeasuredFileAgrees:
    """Five rows from the corpus, inclusive counting, five for five.

    Exclusive counting would give 1, 9, 19, 30, 14 — off by one on every single row,
    which is the kind of error that survives review because each individual number
    looks plausible.
    """

    @pytest.mark.parametrize(
        ("start", "finish", "so_ngay"),
        [
            (D(2019, 3, 13), D(2019, 3, 14), 2),
            (D(2019, 4, 17), D(2019, 4, 26), 10),
            (D(2019, 4, 1), D(2019, 4, 20), 20),
            (D(2019, 6, 30), D(2019, 7, 30), 31),
            (D(2019, 8, 20), D(2019, 9, 3), 15),
        ],
    )
    def test_inclusive_counting_reproduces_the_file(
        self, start: dt.date, finish: dt.date, so_ngay: int
    ) -> None:
        assert expected_duration_days(start, finish) == so_ngay

    @pytest.mark.parametrize(
        ("start", "finish"),
        [
            (D(2019, 4, 17), D(2019, 4, 26)),
            (D(2019, 6, 30), D(2019, 7, 30)),
        ],
    )
    def test_exclusive_counting_would_be_wrong_on_every_row(
        self, start: dt.date, finish: dt.date
    ) -> None:
        assert (finish - start).days != expected_duration_days(start, finish)

    def test_a_same_day_activity_is_one_day(self) -> None:
        """Not zero.

        A task done between 08:00 and 17:00 took one day, and the corpus's
        convention has to agree or the constraint refuses ordinary rows.
        """
        assert expected_duration_days(D(2019, 3, 13), D(2019, 3, 13)) == 1

    def test_a_month_long_activity_counts_the_days(self) -> None:
        """Crossing a month boundary is where an off-by-one hides best."""
        assert expected_duration_days(D(2019, 1, 30), D(2019, 2, 2)) == 4


class TestDurationAgreement:
    def test_agreement_is_reported_when_all_three_are_present(self) -> None:
        assert duration_matches_dates(10, D(2019, 4, 17), D(2019, 4, 26)) is True
        assert duration_matches_dates(9, D(2019, 4, 17), D(2019, 4, 26)) is False

    @pytest.mark.parametrize(
        ("duration", "start", "finish"),
        [
            (None, D(2019, 4, 17), D(2019, 4, 26)),
            (10, None, D(2019, 4, 26)),
            (10, D(2019, 4, 17), None),
            (None, None, None),
        ],
    )
    def test_absent_data_agrees_with_itself(
        self, duration: int | None, start: dt.date | None, finish: dt.date | None
    ) -> None:
        """Otherwise the constraint fires on every partially-filled row.

        A progress sheet in week one is almost entirely empty cells, and a rule that
        refuses those refuses the sheets it most needs to accept.
        """
        assert duration_matches_dates(duration, start, finish) is True


class TestVariance:
    def test_variance_is_actual_minus_planned(self) -> None:
        assert variance_days(10, 14) == 4
        assert variance_days(10, 10) == 0
        assert variance_days(14, 10) == -4

    def test_an_unrecorded_actual_has_no_variance(self) -> None:
        """`None`, not `0`.

        This is the distinction the corpus forces. In the only real file every
        planned date equals its actual, so a computed variance of 0 days is ambiguous
        between "on time" and "nobody filled it in" — and returning 0 for the second
        case is how a project that has not been measured reports as on schedule.
        """
        assert variance_days(10, None) is None
        assert variance_days(None, 10) is None

    def test_zero_and_none_are_not_the_same_answer(self) -> None:
        """Stated as its own test, because collapsing them is the bug."""
        assert variance_days(10, 10) == 0
        assert variance_days(10, None) is None
        assert variance_days(10, 10) != variance_days(10, None)


class TestCompletionRefusesRatherThanRescales:
    """`% Hoàn thành` says percentage. The cells hold `0.65`, `0.8`, `0.9`, `0`."""

    @pytest.mark.parametrize("value", [0.0, 0.65, 0.8, 0.9, 1.0])
    def test_a_fraction_is_taken_as_a_ratio(self, value: float) -> None:
        assert completion_ratio(value) == value

    @pytest.mark.parametrize("value", [1.5, 65.0, 100.0])
    def test_anything_above_one_is_refused(self, value: float) -> None:
        """Not divided by 100.

        A genuine `65` on a future sheet and a mis-keyed `0.65` on this one are
        indistinguishable, and silently choosing between them is how a completion
        figure ends up a hundred times out with no error raised anywhere. Refusing
        puts a person on it, which is the only correct outcome for an ambiguity this
        real.
        """
        assert completion_ratio(value) is None

    def test_a_negative_is_refused(self) -> None:
        assert completion_ratio(-0.1) is None

    def test_none_stays_none(self) -> None:
        """An empty cell is not a zero per cent.

        Zero is a real measurement — an activity that has not started — and
        distinguishing it from "not recorded" is the whole value of the column.
        """
        assert completion_ratio(None) is None
        assert completion_ratio(0.0) == 0.0
        assert completion_ratio(None) != completion_ratio(0.0)

    def test_the_boundary_is_exactly_one(self) -> None:
        """1.0 is 100% complete and is accepted; 1.0001 is refused.

        The boundary is worth pinning because the corpus's maximum observed value is
        `0.9`, so nothing in the data demonstrates that 1.0 is legal and the check
        constraint has to be the thing that says so.
        """
        assert completion_ratio(1.0) == 1.0
        assert completion_ratio(1.0001) is None


class TestTheStatusFlag:
    @pytest.mark.parametrize("text", ["YES", "yes", "Yes", "CÓ", "có", "Y"])
    def test_affirmative_spellings_map_to_true(self, text: str) -> None:
        assert is_adequate(text) is True

    @pytest.mark.parametrize("text", ["NO", "no", "KHÔNG", "không", "N"])
    def test_negative_spellings_map_to_false(self, text: str) -> None:
        assert is_adequate(text) is False

    def test_surrounding_whitespace_is_ignored(self) -> None:
        """The corpus pads cells, and a padded `YES` is still a yes."""
        assert is_adequate("  YES  ") is True

    def test_anything_else_is_none_not_false(self) -> None:
        """`Đang thực hiện` is neither adequate nor not.

        The header says `Tình trạng` — "status" — and the only observed value is
        `YES`, so the vocabulary is almost certainly wider than two entries.
        Returning `False` for an unrecognised status would make a sheet nobody
        maintains read as a sheet where everything is deficient.
        """
        assert is_adequate("Đang thực hiện") is None
        assert is_adequate("") is None
        assert is_adequate(None) is None

    def test_unknown_is_distinguishable_from_negative(self) -> None:
        """Stated separately, because this is the distinction that matters."""
        assert is_adequate("Đang thực hiện") is None
        assert is_adequate("NO") is False
        assert is_adequate("Đang thực hiện") != is_adequate("NO")
