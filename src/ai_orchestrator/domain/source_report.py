"""Source-grounded document reports and exact VND comparison rules. No I/O."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

CLASSIFICATIONS = ("public", "internal", "confidential", "restricted", "secret")

#: Scripts a report must not *introduce*: the prose is written in the sources'
#: language, so a character from one of these ranges is leakage unless the
#: sources themselves contain it. This is the deterministic half of the
#: content-quality rule -- an observed live failure mixed the Chinese term
#: 筛查 into an otherwise Vietnamese finding while its quote stayed Vietnamese.
_FOREIGN_SCRIPT_RANGES: tuple[tuple[int, int], ...] = (
    (0x0370, 0x03FF),  # Greek
    (0x0400, 0x04FF),  # Cyrillic
    (0x0530, 0x058F),  # Armenian
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0900, 0x097F),  # Devanagari
    (0x0E00, 0x0E7F),  # Thai
    (0x3040, 0x30FF),  # Hiragana / Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7AF),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK compatibility ideographs
    (0x20000, 0x2FA1F),  # CJK extensions B-F and compatibility supplement
)


def foreign_script_leakage(text: str, source_characters: set[str]) -> list[str]:
    """Characters in *text* from a script the sources never use.

    Returns a sorted, de-duplicated list. A character is allowed when it falls
    outside every foreign range, or when the selected sources already contain
    it, so a genuinely multilingual source is not misflagged.
    """
    leaked: set[str] = set()
    for char in text:
        code = ord(char)
        if char in source_characters:
            continue
        if any(low <= code <= high for low, high in _FOREIGN_SCRIPT_RANGES):
            leaked.add(char)
    return sorted(leaked)


def highest_classification(values: list[str]) -> str:
    return max(("internal", *values), key=CLASSIFICATIONS.index)


def report_schema(comparison: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": {"type": "string", "minLength": 20},
        "findings": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    k: {"type": "string", "minLength": 1}
                    for k in ("text", "source_id", "locator", "quote")
                },
                "required": ["text", "source_id", "locator", "quote"],
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
    }
    if comparison:
        fields.update(winner={"type": ["string", "null"]}, total_vnd={"type": ["integer", "null"]})
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def compare_quotes(tables: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for table in tables for row in table["rows"]]
    if len(rows) < 2:
        raise ValueError(
            "Supply at least two CSV quotes with "
            "quote_id,item,quantity,unit_price,delivery,certified"
        )
    ids: set[str] = set()
    scope: set[tuple[str, int]] = set()
    totals = {}
    for row in rows:
        try:
            quote_id = str(row["quote_id"]).strip()
            item = str(row["item"]).strip()
            numbers = [Decimal(str(row[key])) for key in ("quantity", "unit_price", "delivery")]
            if any(
                not n.is_finite() or n < 0 or n != n.to_integral_value() or n > 10**12
                for n in numbers
            ):
                raise ValueError("Quote numbers must be bounded non-negative VND integers")
            quantity, unit_price, delivery = map(int, numbers)
            certified = str(row["certified"]).strip().lower()
        except (KeyError, InvalidOperation, TypeError) as exc:
            raise ValueError(
                "Invalid quote row; required: quote_id,item,quantity,unit_price,delivery,certified"
            ) from exc
        if (
            not quote_id
            or not item
            or quote_id in ids
            or quantity <= 0
            or certified not in {"true", "false"}
        ):
            raise ValueError(
                "Quote ID must be unique; item, positive quantity "
                "and true/false certified are required"
            )
        ids.add(quote_id)
        scope.add((item, quantity))
        if certified == "true":
            totals[quote_id] = quantity * unit_price + delivery
    if len(scope) != 1:
        raise ValueError(
            "Compare one item and the same requested quantity; "
            "normalize units/currency before running"
        )
    if not totals:
        return {"winner": None, "total_vnd": None}
    winner = min(totals, key=lambda key: (totals[key], key))
    return {"winner": winner, "total_vnd": totals[winner]}


def validate_report(
    output: dict[str, Any], sources: list[dict[str, Any]], expected: dict[str, Any] | None = None
) -> None:
    required = {"summary", "findings", "limitations"} | (set(expected) if expected else set())
    if (
        set(output) != required
        or not isinstance(output["summary"], str)
        or len(output["summary"].strip()) < 20
    ):
        raise ValueError("Return exactly the declared report fields and a substantive summary")
    findings = output["findings"]
    if not isinstance(findings, list) or not 1 <= len(findings) <= 20:
        raise ValueError("Report requires 1-20 grounded findings")
    if not isinstance(output["limitations"], list) or any(
        not isinstance(x, str) for x in output["limitations"]
    ):
        raise ValueError("Limitations must be a list of strings")
    segments = {
        (source["id"], s["locator"]): s["text"] for source in sources for s in source["segments"]
    }
    covered = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != {
            "text",
            "source_id",
            "locator",
            "quote",
        }:
            raise ValueError("Each finding needs text, source_id, locator and verbatim quote")
        if any(not isinstance(value, str) or not value.strip() for value in finding.values()):
            raise ValueError("Finding values must be non-empty strings")
        key = (finding["source_id"], finding["locator"])
        if (
            key not in segments
            or len(finding["quote"]) < 8
            or finding["quote"] not in segments[key]
        ):
            raise ValueError(
                "Citation must reference a selected source locator "
                "and an exact quote of at least 8 characters"
            )
        covered.add(finding["source_id"])
    if covered != {s["id"] for s in sources}:
        raise ValueError("Every selected source needs at least one grounded finding")
    # Surface-integrity guard: the report is written in the sources' language.
    # A quote can be an exact substring while the finding text around it leaks a
    # foreign script (the live 筛查 case), so check the prose, not the citation.
    source_characters = set("".join(segments.values()))
    prose = [output["summary"], *(f["text"] for f in findings), *output["limitations"]]
    leaked = sorted(
        {char for text in prose for char in foreign_script_leakage(text, source_characters)}
    )
    if leaked:
        raise ValueError(
            "Report prose uses characters from a script absent from the sources "
            "(write in the sources' language): " + "".join(leaked)
        )
    if expected is not None and any(
        output.get(key) != value or isinstance(output.get(key), bool)
        for key, value in expected.items()
    ):
        raise ValueError(
            "Quote winner/total must match the deterministic eligibility and VND calculation: "
            + str(expected)
        )
