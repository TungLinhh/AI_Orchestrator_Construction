"""Planning scenarios must not masquerade as live lifecycle acceptance."""

import json
from pathlib import Path

from scripts.organization_eval import corpus, corpus_hash, load_corpus

ROOT = Path(__file__).resolve().parents[2]
HISTORICAL_HASH = "bacd9e23552fe5a4927919b6ac827b3c95f6bd36a3944dc9689f339ccaba492f"


def test_historical_corpus_retains_receipt_identity():
    checked_in = load_corpus(ROOT / "examples/organization_eval/cases.json")
    assert corpus_hash(checked_in) == corpus_hash(corpus()) == HISTORICAL_HASH
    assert len(checked_in) == 21


def test_supplemental_rubrics_are_not_numeric_or_lifecycle_oracles():
    document = json.loads(
        (ROOT / "examples/organization_eval/supplemental_scenarios_v1.json").read_text()
    )
    cases = document["scenarios"]
    assert len({case["id"] for case in cases}) == len(cases) == 5
    assert {case["category"] for case in cases} == {
        "conflicting",
        "approval-gate",
        "cross-department",
        "missing-capability",
        "custom",
    }
    for case in cases:
        assert case["synthetic"] is True
        assert case["verification_status"] == "fixture_only_not_executed"
        assert case["lifecycle_proof"] is False
        assert case["evaluator"] == "independent_rubric_required"
        assert case["acceptance_checks"]
        assert "expected_values" not in case
        assert case["allowed_actions"] == ["read", "analyze", "draft"]
        assert all(source["text"] and source["id"] for source in case["sources"])
    custom = next(case for case in cases if case["category"] == "custom")
    assert "staging-mep" not in json.dumps(custom)
    onboarding = next(case for case in cases if case["category"] == "cross-department")
    assert "score" not in onboarding["goal"]
