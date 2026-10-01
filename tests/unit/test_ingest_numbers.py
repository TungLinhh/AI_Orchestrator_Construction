"""The number parser, measured against the cases that break it.

Every table in the module docstring is here as an executable case, because the
whole point of the module is that the behaviour was wrong in a way that only
running it reveals. `1.234,5` reading as `1.234` is obvious in a table and
invisible in a code review.

The tests are written as *the value that must come out*, not as "does not
raise", for the same reason the database tests assert a named constraint: a test
that passes for the wrong reason is the failure mode this module exists to
prevent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ai_orchestrator.ingest.numbers import (
    Refusal,
    is_number,
    parse_number,
    parse_optional_number,
)


class TestVietnameseGrouping:
    """Dot groups thousands, comma separates decimals."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1.234,5", "1234.5"),
            ("12.500.000", "12500000"),
            ("1.234.567,89", "1234567.89"),
            ("1.000.000.000", "1000000000"),
            ("999,5", "999.5"),
            ("1.234.567", "1234567"),
        ],
    )
    def test_reads_vietnamese_notation(self, raw: str, expected: str) -> None:
        result = parse_number(raw)
        assert is_number(result), f"{raw!r} was refused: {result}"
        assert result == Decimal(expected)

    def test_a_bare_grouped_integer_is_not_in_this_table(self) -> None:
        """`1.234` is deliberately absent from the table above.

        It was in the first draft of it, asserting 1234, while a test further
        down asserted the same input is refused. Both cannot hold, and the
        refusal is the correct answer — the cell does not say which convention
        its document uses. The row is recorded here so the next person does not
        "fix" the missing case by adding it back.
        """
        result = parse_number("1.234")
        assert isinstance(result, Refusal)
        assert result.reason == "ambiguous_grouping"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1,234.56", "1234.56"),
            ("1,234,567.89", "1234567.89"),
            ("12,500,000", "12500000"),
            ("1234.5", "1234.5"),
        ],
    )
    def test_reads_english_notation(self, raw: str, expected: str) -> None:
        result = parse_number(raw)
        assert is_number(result), f"{raw!r} was refused: {result}"
        assert result == Decimal(expected)


class TestTheBugThatMotivatedThis:
    """The exact inputs the ported implementation got wrong, with the wrong
    answers beside them so a regression names the defect it reintroduced."""

    @pytest.mark.parametrize(
        ("raw", "wrong", "right"),
        [
            ("1.234,5", "1.234", "1234.5"),
            ("12.500.000", "12.5", "12500000"),
            ("1.234.567,89", "1.234", "1234567.89"),
            ("-1.234,5", "-1.234", "-1234.5"),
        ],
    )
    def test_does_not_reproduce_the_ported_bug(self, raw: str, wrong: str, right: str) -> None:
        result = parse_number(raw)
        assert is_number(result)
        assert result != Decimal(wrong), f"reproduced the {wrong} bug"
        assert result == Decimal(right)

    def test_a_thousandfold_error_is_the_whole_risk(self) -> None:
        """Stated as its own test because it is the reason the module exists.

        Twelve and a half million VND read as 12.5 is not a rounding difference.
        Every downstream calculation is correct arithmetic on a wrong input.
        """
        assert parse_number("12.500.000") == Decimal("12500000")


class TestSign:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-1.234,5", "-1234.5"),
            ("(1.234,5)", "-1234.5"),
            ("1.234,5-", "-1234.5"),
            ("-12,500,000", "-12500000"),
            ("(999)", "-999"),
        ],
    )
    def test_accounting_negatives(self, raw: str, expected: str) -> None:
        result = parse_number(raw)
        assert is_number(result), f"{raw!r} was refused: {result}"
        assert result == Decimal(expected)

    def test_double_negative_is_positive_not_refused(self) -> None:
        """`-(-5)` written as `--5` is a typo, and the two answers are not the
        same. Refusing beats returning a confidently wrong sign."""
        result = parse_number("--5")
        assert not is_number(result)
        assert isinstance(result, Refusal)
        assert result.reason == "not_a_number"


class TestRefusal:
    """Refusal is a value with a reason, not an exception and not a zero."""

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (None, "empty"),
            ("", "empty"),
            ("   ", "empty"),
            ("Tổng", "not_a_number"),
            ("abc", "not_a_number"),
            ("#N/A", "excel_error"),
            ("#DIV/0!", "excel_error"),
            (True, "boolean"),
            (False, "boolean"),
        ],
    )
    def test_refuses_with_a_named_reason(self, raw: object, reason: str) -> None:
        result = parse_number(raw)
        assert not is_number(result)
        assert isinstance(result, Refusal)
        assert result.reason == reason

    def test_excel_error_is_not_silently_zero(self) -> None:
        """`#N/A` in a quantity column means the formula upstream failed.

        Treating it as text and coercing to 0 records a measurement that never
        happened, and 0 is indistinguishable from a real zero for the rest of
        the pipeline.
        """
        assert not is_number(parse_number("#N/A"))

    def test_boolean_is_not_one(self) -> None:
        """`bool` subclasses `int` in Python, so `True` becomes 1 for free.

        A `TRUE` cell in a quantity column is a spreadsheet convention, not a
        measurement of one.
        """
        assert not is_number(parse_number(True))


class TestAmbiguity:
    def test_a_grouped_integer_is_ambiguous_and_refused(self) -> None:
        """`1.234` is 1234 in Vietnamese and 1.234 in English.

        Nothing in the cell says which, and a document's convention cannot be
        inferred from one cell. Refusing is the only safe answer; a coin flip
        here is a 1000x error with a 50% chance.
        """
        result = parse_number("1.234")
        assert not is_number(result)
        assert isinstance(result, Refusal)
        assert result.reason == "ambiguous_grouping"

    def test_unambiguous_grouping_is_still_read(self) -> None:
        """Two separators cannot be a single decimal, so there is no ambiguity.

        `1.234.567` is unambiguously 1234567 in either convention, and refusing
        it would throw away the most common shape in the corpus.
        """
        assert parse_number("1.234.567") == Decimal("1234567")

    def test_five_digit_plain_number_is_not_ambiguous(self) -> None:
        """The ambiguity rule is about *separators*, not magnitude.

        `12345` has no separator and means twelve thousand three hundred and
        forty-five. A rule that refused anything long would refuse half the
        corpus.
        """
        assert parse_number("12345") == Decimal("12345")


class TestScale:
    def test_percentage_becomes_a_fraction(self) -> None:
        result = parse_number("12%")
        assert is_number(result)
        assert result == Decimal("0.12")

    def test_percentage_with_grouping(self) -> None:
        assert parse_number("1,5%") == Decimal("0.015")

    def test_non_numeric_suffix_is_refused_not_stripped(self) -> None:
        """`m2`, `kg`, `cái` are units, and the unit belongs in `unit_code`.

        Stripping them would turn a length of `12m` into the number 12 with the
        unit silently discarded, which is the exact error the schema was built
        to make impossible — arriving through the back door.
        """
        for raw in ("12m", "12m2", "5kg", "3cái"):
            result = parse_number(raw)
            assert not is_number(result), f"{raw!r} silently became {result}"


class TestPrecision:
    def test_float_input_does_not_become_a_binary_approximation(self) -> None:
        """A float has already been rounded. `str` recovers the written form."""
        assert parse_number(0.1) == Decimal("0.1")
        # The float constructor is the point: this is the approximation the
        # strict path avoids.
        assert parse_number(0.1) != Decimal(0.1)  # noqa: RUF032

    def test_scale_is_preserved(self) -> None:
        assert parse_number("120.50") == Decimal("120.50")
        assert str(parse_number("120.50")) == "120.50"

    def test_integers_pass_through_as_integers(self) -> None:
        assert parse_number(42) == Decimal("42")

    def test_decimal_passes_through_unchanged(self) -> None:
        value = Decimal("1234.5678")
        assert parse_number(value) is value


class TestOptional:
    def test_optional_discards_the_reason(self) -> None:
        assert parse_optional_number("1.234,5") == Decimal("1234.5")
        assert parse_optional_number("Tổng") is None

    def test_optional_is_distinguishable_from_zero_by_the_strict_variant(self) -> None:
        """Why both exist. `parse_optional_number("")` and
        `parse_optional_number(0)` both give a falsy answer, and only one of
        them is a measurement."""
        assert parse_optional_number("") is None
        assert parse_optional_number(0) == Decimal("0")
        assert not is_number(parse_number(""))
