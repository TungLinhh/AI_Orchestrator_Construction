"""A source label is not part of a source quotation."""

import pytest

from ai_orchestrator.domain.source_evidence import (
    canonical_sources,
    has_quote_contract,
    validate_source_quotes,
)

SCHEMA = {
    "properties": {
        "findings": {
            "items": {
                "properties": {
                    "source_id": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                }
            }
        }
    }
}
INPUT = {
    "sources": [{"id": "document:1", "text": "Signed policy applies to all staff."}],
    "required_sources": ["document:1", "not-yet-supplied"],
}


@pytest.mark.parametrize("quote", ["Signed policy", "Signed policy applies to all staff."])
def test_exact_source_substrings_are_valid(quote):
    validate_source_quotes(
        SCHEMA, INPUT, {"findings": [{"source_id": "document:1", "evidence_quote": quote}]}
    )


@pytest.mark.parametrize(
    "identifier,quote",
    [
        ("document:1", "[document:1] Signed policy applies to all staff."),
        ("document:1", "Policy approved for production"),
        ("not-yet-supplied", "Signed policy"),
        ("document:1", ""),
    ],
)
def test_fabricated_or_missing_source_cannot_support_a_finding(identifier, quote):
    with pytest.raises(ValueError):
        validate_source_quotes(
            SCHEMA, INPUT, {"findings": [{"source_id": identifier, "evidence_quote": quote}]}
        )


def test_other_contracts_and_noncanonical_source_workflows_are_unchanged():
    validate_source_quotes({"properties": {"findings": {"type": "string"}}}, INPUT, {})
    validate_source_quotes(SCHEMA, {"source_manifest_id": "manifest"}, {})
    assert not has_quote_contract({"properties": {"findings": {"items": []}}})


@pytest.mark.parametrize(
    "sources",
    [
        [{"id": "x", "text": "first"}, {"id": "x", "text": "second"}],
        [{"id": "x", "text": "a" * 200001}],
        [{"id": "", "text": "text"}],
    ],
)
def test_canonical_material_is_bounded_and_unambiguous(sources):
    with pytest.raises(ValueError):
        canonical_sources(sources)


def test_only_canonical_material_is_copied():
    assert canonical_sources([{"id": "x", "text": "text", "expected_values": {"gold": 42}}]) == [
        {"id": "x", "text": "text"}
    ]
