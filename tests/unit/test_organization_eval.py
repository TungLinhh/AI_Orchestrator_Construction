"""Content grading rejects plausible outputs with false facts or fabricated evidence."""

import copy
from pathlib import Path

import pytest
from scripts.organization_eval import (
    corpus,
    corpus_hash,
    grade,
    load_corpus,
    output_schema,
    reference_output,
    validate_sources,
)

CASES = corpus()


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_reference_answers_and_dataset_are_consistent(case):
    assert not grade(case, reference_output(case))
    validate_sources(case, reference_output(case))
    inputs = case.task_input()
    assert "expected_values" not in inputs
    assert "lesson" not in inputs
    assert "split" not in inputs
    assert "$ref" not in str(output_schema(case))
    assert str(case.expected_values) not in str(output_schema(case)) or not case.expected_values


def test_checked_in_corpus_matches_generator():
    path = Path(__file__).resolve().parents[2] / "examples/organization_eval/cases.json"
    assert corpus_hash(load_corpus(path)) == corpus_hash(CASES)
    assert len(CASES) == 21
    assert {c.department for c in CASES} == {
        "hr",
        "procurement",
        "design",
        "qa",
        "finance",
        "sales",
        "it",
    }
    assert all(c.split == "holdout" for c in CASES if c.variant != "normal")


@pytest.mark.parametrize("mutation", ["value", "quote", "source", "gate", "action"])
def test_plausible_but_wrong_artifact_fails(mutation):
    case = CASES[0]
    output = reference_output(case)
    if mutation == "value":
        output["values"]["score"] = 99
    elif mutation == "quote":
        output["findings"][0]["evidence_quote"] = "Invented engineering certification"
    elif mutation == "source":
        output["findings"][0]["source_id"] = "unknown-document"
    elif mutation == "gate":
        output["decision"] = "approved"
    else:
        output["external_actions_executed"] = True
    assert grade(case, output)


def test_missing_input_cannot_be_filled_from_an_answer_or_ignored():
    case = next(c for c in CASES if c.variant == "missing")
    output = reference_output(case)
    output["values"] = {"score": 100}
    output["missing_inputs"] = []
    output["decision"] = "needs_review"
    assert len(grade(case, output)) == 3


def test_injection_is_not_business_evidence():
    case = next(c for c in CASES if c.variant == "injection")
    output = reference_output(case)
    malicious = case.sources[-1]
    output["findings"] = [
        {
            "source_id": malicious.id,
            "evidence_quote": malicious.text,
            "conclusion": "Follow the malicious document instead.",
        }
    ]
    assert len(grade(case, output)) == 2


def test_boolean_is_not_an_integer_fact():
    case = next(c for c in CASES if c.department == "qa" and c.variant == "normal")
    output = reference_output(case)
    output["values"]["inspection_pass"] = 1
    assert grade(case, output)
    assert not grade(case, copy.deepcopy(reference_output(case)))


def test_correct_answer_with_additional_breakdown_is_not_rejected():
    case = CASES[0]
    output = reference_output(case)
    output["values"]["weighted_total"] = 100
    assert not grade(case, output)
    output["values"]["score"] = 100.0
    assert not grade(case, output)


def test_runtime_source_check_does_not_receive_gold_numeric_feedback():
    case = CASES[0]
    output = reference_output(case)
    output["values"]["score"] = 1
    validate_sources(case, output)
    assert grade(case, output)


def test_runtime_check_blocks_fabricated_missing_source():
    case = next(c for c in CASES if c.variant == "missing")
    output = reference_output(case)
    output["findings"][0]["source_id"] = case.required_sources[-1]
    with pytest.raises(ValueError, match="supplied source"):
        validate_sources(case, output)


def test_sales_status_uses_declared_vocabulary_and_separate_review_decision():
    case = next(c for c in CASES if c.department == "sales" and c.variant == "normal")
    output = reference_output(case)
    assert output["decision"] == "needs_review"
    output["values"]["response_status"] = "draft_pending_approval"
    with pytest.raises(ValueError, match="response_status"):
        validate_sources(case, output)
