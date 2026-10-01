"""Numbers as Vietnamese construction documents write them.

The single most consequential function in the ingest layer, and the one place
where being wrong is silent and expensive.

Vietnamese and English group digits identically but swap the two separators.
`1.234,5` is one thousand two hundred and thirty-four point five, not one point
two three four five, and `12.500.000` is twelve and a half million, not twelve
and a half. A quantity or a rate read with the wrong convention is wrong by a
factor of a thousand, the arithmetic downstream is perfectly correct, and nothing
raises. It surfaces months later as a final account that does not reconcile.

**This exists because the implementation being ported gets it wrong.** Its
`toFloat` is:

```javascript
const cleaned = v.replace(',', '.');
return parseFloat(cleaned);
```

One comma replaced, no thousands stripping. Measured behaviour, not inferred:

| Input | That implementation | Correct |
|---|---|---|
| `1.234,5` | `1.234` | `1234.5` |
| `12.500.000` | `12.5` | `12500000` |
| `1.234.567,89` | `1.234` | `1234567.89` |
| `-1.234,5` | `-1.234` | `-1234.5` |

The same repository documents a *different* module as refusing ambiguous numbers
rather than guessing, which is the right policy — but that module is the
reconciliation script, not the ingest path. Two code paths, one safe, and the
documentation described only the safe one. The port keeps the policy and applies
it everywhere.

The policy, stated once:

* A number is parsed, or it is refused. Never approximated.
* Ambiguity is refusal, not a coin flip. `1.234` could be 1234 in Vietnamese or
  1.234 in English, and a cell does not say which.
* Refusal is a value with a reason, not an exception and not a zero. A
  spreadsheet has headers, footers, merged cells and blank rows, and an ingest
  that throws on the first odd cell never finishes.

## How the convention is decided

Separator roles are read off the whole string, never assumed:

* **Both present** — the rightmost separator is the decimal point, the other
  groups thousands. Grouping never appears after a decimal separator, so this is
  not a heuristic.
* **One separator, appearing more than once** — grouping. `1.234.567` is
  1234567 under either convention, because a decimal separator cannot appear
  twice.
* **One separator, appearing once** — genuinely ambiguous *if* it looks like
  grouping, which means 1—3 digits before it and exactly 3 after. `1,234` and
  `12.345` are ambiguous. `1,5` and `1,2345` are not, because a fraction of one
  or four digits is not a thousands group.

That third rule is the whole reason this module refuses anything at all. Every
other case is decidable from the string alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_DOT = "."
_COMMA = ","

#: Characters that can legitimately appear in a numeric cell. Anything else is a
#: unit or a word, and is refused rather than stripped: stripping `m` off `12m`
#: produces the number 12 with the unit silently discarded, which is the exact
#: error the schema was built to make impossible arriving through the back door.
_ALLOWED = re.compile("^[0-9.,%()\\-\\s\u00a0\u202f]+$")
_PAREN = re.compile(r"^\((.+)\)$")
#: Digits and separators only, after the sign and unit decisions are made.
_NUMERIC_BODY = re.compile("^[0-9.,\u00a0\u202f]+$")

#: Excel error values. Named because `#N/A` in a quantity column means the
#: formula upstream failed, and coercing it to 0 records a measurement that never
#: happened — indistinguishable, from here on, from a real zero.
EXCEL_ERRORS = frozenset(
    {"#N/A", "#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!", "#GETTING_DATA"}
)

#: Whitespace variants used as thousands separators in Vietnamese spreadsheets.
#: Written as escapes rather than as literal characters: an invisible byte in a
#: source file cannot be reviewed, cannot be searched for, and looks identical
#: to an ordinary space in every diff. Naming them is the point.
_SPACES = (" ", "\u00a0", "\u202f")


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why a cell was not read as a number.

    A named reason rather than a bare `None`, because "this cell is not a number"
    and "this cell is a number I refuse to guess at" call for different handling:
    the first is normal, the second is a row somebody has to look at.
    """

    reason: str
    raw: str


def _strip_sign_and_scale(raw: str) -> tuple[str, bool, Decimal]:
    """Remove accounting sign notation and a percent suffix.

    Returns the digit body, whether it is negative, and any scale factor. A
    percent is a scale change rather than a refusal because `12%` is
    unambiguously 0.12 and refusing it would lose a cell that has one meaning.
    """
    text = raw.strip()
    negative = False

    paren = _PAREN.match(text)
    if paren:
        negative = True
        text = paren.group(1).strip()
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()

    scale = Decimal(1)
    if text.endswith("%"):
        text = text[:-1].strip()
        scale = Decimal("0.01")
    return text, negative, scale


def _group_is_well_formed(digits: str) -> bool:
    """True when `digits` is a leading group of 1—3 followed by 3-digit groups."""
    parts = digits.split(_DOT)
    if not 1 <= len(parts[0]) <= 3:
        return False
    return all(len(p) == 3 for p in parts[1:])


def _decide(text: str, raw: str) -> tuple[str, str] | Refusal:
    """Resolve separators into `(integer_digits, fraction_digits)`.

    `text` is the digit body with all spaces already removed. Returns a
    `Refusal` when the convention cannot be decided from the string alone.
    """
    dots = text.count(_DOT)
    commas = text.count(_COMMA)

    if dots and commas:
        # Both present: the rightmost is the decimal point. Grouping cannot
        # follow a decimal point, so this is decided rather than guessed.
        decimal_at = max(text.rfind(_DOT), text.rfind(_COMMA))
        whole, fraction = text[:decimal_at], text[decimal_at + 1 :]
        grouping = _COMMA if text[decimal_at] == _DOT else _DOT
        if not whole or not fraction:
            return Refusal("not_a_number", raw)
        if grouping in whole and not _group_is_well_formed(whole.replace(_COMMA, _DOT)):
            return Refusal("malformed_grouping", raw)
        return whole.replace(grouping, "").replace(_DOT, "").replace(_COMMA, ""), fraction

    separator = _DOT if dots else (_COMMA if commas else "")
    if not separator:
        return text, ""

    if text.count(separator) > 1:
        # Grouping: a decimal separator cannot appear twice, so there is no
        # ambiguity to resolve.
        if not _group_is_well_formed(text.replace(_COMMA, _DOT)):
            return Refusal("malformed_grouping", raw)
        return text.replace(_DOT, "").replace(_COMMA, ""), ""

    whole, tail = text.split(separator, 1)
    if not whole or not tail:
        return Refusal("not_a_number", raw)
    # Ambiguous exactly when it could be a thousands group: 1—3 digits before,
    # exactly 3 after. `1,5` and `1,2345` are decimals and stay readable.
    if len(whole) <= 3 and len(tail) == 3:
        return Refusal("ambiguous_grouping", raw)
    return whole, tail


def parse_number(raw: object) -> Decimal | Refusal:
    """Parse a spreadsheet cell into a `Decimal`, or explain why not.

    Returns `Decimal` rather than `float` on purpose. Money and quantities go
    into `NUMERIC` columns, and a float would already have lost precision before
    reaching the database — the same class of silent error as the separator
    problem, one layer further down.
    """
    if raw is None:
        return Refusal("empty", "")
    if isinstance(raw, bool):
        # `bool` subclasses `int`, so `True` becomes 1 for free. A `TRUE` cell
        # in a quantity column is a spreadsheet convention, not a measurement.
        return Refusal("boolean", str(raw))
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, float):
        # `str` recovers the written form: 0.1 becomes Decimal("0.1") rather
        # than Decimal(0.1000000000000000055).
        return Decimal(str(raw))
    if not isinstance(raw, str):
        return Refusal("unsupported_type", str(raw))

    if not raw.strip():
        return Refusal("empty", raw)
    if raw.strip().upper() in EXCEL_ERRORS:
        return Refusal("excel_error", raw.strip())
    if not _ALLOWED.match(raw.strip()):
        return Refusal("not_a_number", raw)

    body, negative, scale = _strip_sign_and_scale(raw)
    for space in _SPACES:
        body = body.replace(space, "")
    if not body or not _NUMERIC_BODY.match(body):
        return Refusal("not_a_number", raw)

    decided = _decide(body, raw)
    if isinstance(decided, Refusal):
        return decided
    whole, fraction = decided

    try:
        value = Decimal(f"{whole}.{fraction}" if fraction else whole)
    except InvalidOperation:  # pragma: no cover - the regexes prevent this
        return Refusal("unparseable", raw)
    return (-value if negative else value) * scale


def is_number(value: object) -> bool:
    """Whether `value` is a number rather than a refusal."""
    return isinstance(value, Decimal)


def parse_optional_number(raw: object) -> Decimal | None:
    """`parse_number`, discarding the reason.

    For columns where a non-numeric cell means "blank" — a header, a merged
    cell, a total row. Never use it on a cell whose value matters, because it
    makes a refusal indistinguishable from a zero.
    """
    result = parse_number(raw)
    return result if isinstance(result, Decimal) else None


__all__ = [
    "EXCEL_ERRORS",
    "Refusal",
    "is_number",
    "parse_number",
    "parse_optional_number",
]
