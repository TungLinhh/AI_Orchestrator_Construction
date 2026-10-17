"""Exact-quote contracts over canonical task sources. No I/O or gold answers."""

from __future__ import annotations

from typing import Any

MAX_SOURCES = 100
MAX_SOURCE_TEXT = 200_000
MAX_SOURCE_TOTAL = 1_000_000


def has_quote_contract(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    root = schema.get("properties", {})
    if not isinstance(root, dict):
        return False
    findings = root.get("findings", {})
    if not isinstance(findings, dict):
        return False
    items = findings.get("items", {})
    if not isinstance(items, dict):
        return False
    properties = items.get("properties", {})
    return isinstance(properties, dict) and {"source_id", "evidence_quote"} <= properties.keys()


def canonical_sources(value: Any) -> list[dict[str, str]]:
    """Copy only identifiers/text; reject ambiguous or oversized material."""
    if not isinstance(value, list) or len(value) > MAX_SOURCES:
        raise ValueError("Canonical sources must be a bounded list")
    result = []
    identifiers: set[str] = set()
    total = 0
    for source in value:
        if not isinstance(source, dict):
            raise ValueError("Canonical sources require source identifiers and text")
        identifier, text = source.get("id"), source.get("text")
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 255:
            raise ValueError("Canonical source identifier is invalid")
        if not isinstance(text, str) or not text or len(text) > MAX_SOURCE_TEXT:
            raise ValueError("Canonical source text is missing or too large")
        if identifier in identifiers:
            raise ValueError("Canonical source identifiers must be unique")
        total += len(text)
        if total > MAX_SOURCE_TOTAL:
            raise ValueError("Canonical source total exceeds its limit")
        identifiers.add(identifier)
        result.append({"id": identifier, "text": text})
    return result


def validate_source_quotes(schema: Any, task_input: Any, output: Any) -> None:
    """Check declared exact quotations, without claiming semantic correctness."""
    if (
        not has_quote_contract(schema)
        or not isinstance(task_input, dict)
        or "sources" not in task_input
    ):
        return
    sources = {source["id"]: source["text"] for source in canonical_sources(task_input["sources"])}
    findings = output.get("findings") if isinstance(output, dict) else None
    if not isinstance(findings, list):
        raise ValueError("The declared findings must be a list")
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Each finding requires source_id and evidence_quote")
        identifier, quote = finding.get("source_id"), finding.get("evidence_quote")
        if not isinstance(identifier, str) or identifier not in sources:
            raise ValueError("Finding source_id must identify a supplied source")
        if not isinstance(quote, str) or not quote.strip() or quote not in sources[identifier]:
            raise ValueError(
                "evidence_quote must be verbatim source.text; "
                "keep source labels outside the quotation"
            )
