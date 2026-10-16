"""Unit tests for the source-reliability holdout harness.

Pure functions only: no API calls, no model calls, no network, no clock
dependence. The live CLI in scripts/source_reliability.py is exercised
only by hand against localhost; it is never invoked here.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import sys
import zipfile
from csv import DictReader
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_orchestrator.domain.source_report import compare_quotes

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from source_reliability import (
    ALLOWED_FREE_MODELS,
    DEADLINE_MAX_S,
    DEADLINE_MIN_S,
    FAILURE_CLASSES,
    FAILURE_CLASSIFICATION_DISCLAIMER,
    _run_case,
    attempt_dir_for,
    build_intent_record,
    check_citations,
    check_expected_rejection,
    check_model_provenance,
    check_quote_expected,
    checkpoint_path_for,
    classify_failure,
    corpus_sha256,
    is_full_pass,
    manifest_snapshot,
    manifest_snapshot_digest,
    parse_http_error,
    summarize_latencies,
    summarize_provenance,
    validate_corpus,
    validate_deadline,
    verify_artifact_bundle,
    verify_citations,
    verify_manifest_digest,
    verify_manifest_entries,
    verify_zip_consistency,
)

CORPUS_PATH = Path(__file__).resolve().parents[2] / "examples" / "source_holdout" / "corpus.json"

EXPECTED_CASE_IDS = [
    "dr-normal-001",
    "dr-normal-002",
    "dr-missing-001",
    "dr-injection-001",
    "qc-normal-001",
    "qc-normal-002",
    "qc-unsupported-001",
    "qc-injection-001",
]


def _load_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


# --- corpus validation -----------------------------------------------------


def test_checked_in_corpus_is_valid_with_stable_ids_and_version():
    corpus = _load_corpus()
    assert validate_corpus(corpus) == []
    assert corpus["version"] == "2"
    assert corpus["synthetic"] is True
    assert [c["id"] for c in corpus["cases"]] == EXPECTED_CASE_IDS
    by_workflow: dict[str, int] = {}
    for case in corpus["cases"]:
        by_workflow[case["workflow"]] = by_workflow.get(case["workflow"], 0) + 1
    assert by_workflow["document_review"] >= 3
    assert by_workflow["quote_comparison"] >= 3


def test_corpus_hash_is_stable_and_hex():
    first = corpus_sha256(_load_corpus())
    second = corpus_sha256(_load_corpus())
    assert first == second
    assert len(first) == 64 and all(ch in "0123456789abcdef" for ch in first)


def test_corpus_validation_rejects_too_few_cases_and_duplicates():
    corpus = _load_corpus()
    corpus["cases"] = corpus["cases"][:5]
    assert validate_corpus(corpus) != []
    corpus = _load_corpus()
    corpus["cases"][1]["id"] = corpus["cases"][0]["id"]
    errors = validate_corpus(corpus)
    assert any("duplicated" in e for e in errors)


def test_corpus_validation_rejects_unmarked_injection():
    corpus = _load_corpus()
    injected = next(c for c in corpus["cases"] if c["id"] == "dr-injection-001")
    blob = json.dumps(injected)
    assert "NOT AN INSTRUCTION" in blob  # marker genuinely present in fixture
    # Targeted scrub: remove the marker from the injection case only.
    scrubbed = copy.deepcopy(corpus)
    target = next(c for c in scrubbed["cases"] if c["id"] == "dr-injection-001")
    target["source"]["paragraphs"] = [
        p.replace("NOT AN INSTRUCTION", "DIRECTIVE") for p in target["source"]["paragraphs"]
    ]
    errors = validate_corpus(scrubbed)
    assert any("NOT AN INSTRUCTION" in e for e in errors)


def test_corpus_validation_rejects_missing_deterministic_expected():
    corpus = _load_corpus()
    target = next(c for c in corpus["cases"] if c["id"] == "qc-normal-001")
    del target["expected"]["total_vnd"]
    assert validate_corpus(corpus) != []


# --- failure classification -------------------------------------------------


def test_failure_classes_cover_required_taxonomy():
    assert set(FAILURE_CLASSES) == {
        "refusal",
        "availability",
        "timeout",
        "validation",
        "input-blocked",
        "unknown",
    }
    assert "NOT DEFINITIVE" in FAILURE_CLASSIFICATION_DISCLAIMER


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": "waiting_for_input"}, "input-blocked"),
        ({"status": None, "http_status": 404, "last_error": "nope"}, "input-blocked"),
        ({"status": None, "http_status": 422, "last_error": "bad bind"}, "input-blocked"),
        ({"status": "failed", "last_error": "refused by content filter"}, "refusal"),
        ({"status": "failed", "last_error": "POLICY_DENIED by provider"}, "refusal"),
        ({"status": "failed", "last_error": "gateway 503 no candidate"}, "availability"),
        ({"status": "failed", "last_error": "connection timed out"}, "availability"),
        ({"status": "failed", "last_error": "artifact hash mismatch"}, "validation"),
        ({"status": "failed", "last_error": "grade schema violation"}, "validation"),
        ({"status": "failed", "last_error": "mystery", "timeout_exceeded": True}, "timeout"),
        ({"status": "failed", "last_error": "something unheard of"}, "unknown"),
    ],
)
def test_classify_failure(record, expected):
    assert classify_failure(record) == expected


# --- latency aggregation ----------------------------------------------------


def test_summarize_latencies_accepted_only_percentiles():
    summary = summarize_latencies([100.0, 200.0, 300.0, 400.0])
    assert summary["count"] == 4
    assert summary["p50_ms"] == 200.0
    assert summary["p95_ms"] == 400.0


def test_summarize_latencies_empty_has_no_percentiles():
    summary = summarize_latencies([])
    assert summary == {"count": 0, "p50_ms": None, "p95_ms": None}


# --- artifact verification ---------------------------------------------------


def _bundle(formats_bodies: dict[str, bytes]) -> tuple[list[dict], dict[str, bytes]]:
    meta = [
        {"format": fmt, "sha256": hashlib.sha256(body).hexdigest()}
        for fmt, body in formats_bodies.items()
    ]
    return meta, dict(formats_bodies)


def _good_bodies() -> dict[str, bytes]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("sources/brief.txt", "synthetic")
    return {
        "docx": b"docx-bytes",
        "xlsx": b"xlsx-bytes",
        "pdf": b"%PDF-1.7 synthetic",
        "json": b'{"output": {}}',
        "md": b"# synthetic",
        "zip": buffer.getvalue(),
    }


def test_verify_artifact_bundle_accepts_good_bundle():
    meta, bodies = _bundle(_good_bodies())
    assert verify_artifact_bundle(meta, bodies) == []


def test_verify_artifact_bundle_rejects_tampered_bytes():
    meta, bodies = _bundle(_good_bodies())
    bodies["pdf"] = b"%PDF-1.7 synthetic TAMPERED"
    errors = verify_artifact_bundle(meta, bodies)
    assert any("pdf" in e and "mismatch" in e for e in errors)


def test_verify_artifact_bundle_rejects_missing_format_and_bad_zip():
    bodies = _good_bodies()
    del bodies["md"]
    meta, _ = _bundle(_good_bodies())
    errors = verify_artifact_bundle(meta, bodies)
    assert any("md" in e for e in errors)
    meta2, bodies2 = _bundle(_good_bodies())
    bodies2["zip"] = b"not a zip at all"
    errors2 = verify_artifact_bundle(meta2, bodies2)
    assert any("zip" in e for e in errors2)


# --- report checks ------------------------------------------------------------


def test_check_citations_and_quote_expected():
    output = {
        "summary": "synthetic summary with enough length here",
        "findings": [{"text": "t", "source_id": "s", "locator": "l", "quote": "verbatim quote"}],
        "limitations": [],
        "winner": "A",
        "total_vnd": 1520000,
    }
    assert check_citations(output) == []
    assert check_quote_expected(output, "A", 1520000) == []
    assert check_citations({"findings": []}) != []
    assert check_quote_expected(output, "B", 1520000) != []
    assert check_quote_expected(output, "A", 1) != []


# --- intent / pass rules -------------------------------------------------------


def test_intent_record_carries_identity_before_run():
    intent = build_intent_record(
        run_id="run-1",
        case_id="dr-normal-001",
        workflow="document_review",
        kind="normal",
        round_no=1,
        corpus_name="source_holdout",
        corpus_version="1",
        corpus_sha256_hex="ab" * 32,
        plan={"deadline_s": 240, "subset": False},
    )
    assert intent["record"] == "intent"
    assert intent["corpus"]["sha256"] == "ab" * 32
    assert intent["round"] == 1


def test_failed_only_reruns_never_count_as_full_pass():
    assert is_full_pass(is_subset=False, expectations_met=True) is True
    assert is_full_pass(is_subset=True, expectations_met=True) is False
    assert is_full_pass(is_subset=False, expectations_met=False) is False


# --- frozen corpus v2: synthetic label, exact negative pins, distinct quotes -


def test_corpus_v2_pins_exact_negative_expectations():
    corpus = _load_corpus()
    missing = next(c for c in corpus["cases"] if c["id"] == "dr-missing-001")
    assert missing["expected"]["expected_http_status"] == 404
    assert missing["expected"]["expected_error_kind"] == "NOT_FOUND"
    unsupported = next(c for c in corpus["cases"] if c["id"] == "qc-unsupported-001")
    assert unsupported["expected"]["expected_task_status"] == "waiting_for_input"
    assert unsupported["expected"]["expected_zero_spend"] is True


def test_corpus_quote_outcomes_are_distinct_and_match_domain_math():
    corpus = _load_corpus()
    pairs: list[tuple] = []
    for case in corpus["cases"]:
        if case["workflow"] != "quote_comparison":
            continue
        if "winner" not in case.get("expected", {}):
            continue  # negatives carry spend pins, not quote outcomes
        source = case.get("source") or {}
        if "csv_text" in source:
            rows = list(DictReader(io.StringIO(source["csv_text"])))
            computed = compare_quotes([{"rows": rows}])
            assert computed["winner"] == case["expected"]["winner"]
            assert computed["total_vnd"] == case["expected"]["total_vnd"]
            pairs.append((computed["winner"], computed["total_vnd"]))
        else:
            pairs.append((case["expected"]["winner"], case["expected"]["total_vnd"]))
    assert len(set(pairs)) >= 2, f"frozen corpus needs distinct quote outcomes: {pairs}"


def test_corpus_validation_requires_synthetic_label():
    corpus = _load_corpus()
    del corpus["synthetic"]
    assert any("synthetic" in e for e in validate_corpus(corpus))


def test_corpus_validation_requires_exact_negative_pins():
    corpus = _load_corpus()
    missing = next(c for c in corpus["cases"] if c["id"] == "dr-missing-001")
    del missing["expected"]["expected_http_status"]
    assert any("expected_http_status" in e for e in validate_corpus(corpus))
    corpus = _load_corpus()
    unsupported = next(c for c in corpus["cases"] if c["id"] == "qc-unsupported-001")
    unsupported["expected"]["expected_zero_spend"] = False
    assert any("expected_zero_spend" in e for e in validate_corpus(corpus))


def test_corpus_validation_rejects_single_quote_outcome():
    corpus = _load_corpus()
    for case in corpus["cases"]:
        if case["workflow"] == "quote_comparison" and "winner" in case.get("expected", {}):
            case["expected"]["winner"] = "A"
            case["expected"]["total_vnd"] = 1520000
    assert any("distinct computed quote" in e for e in validate_corpus(corpus))


# --- attempt dirs, checkpoints, deadline -------------------------------------


def test_attempt_dirs_are_per_round_and_never_overwrite(tmp_path):
    first = attempt_dir_for(tmp_path, "dr-normal-001", 1)
    second = attempt_dir_for(tmp_path, "dr-normal-001", 2)
    assert first != second
    assert str(first).endswith("dr-normal-001/r1")
    first.mkdir(parents=True)
    (first / "report.json").write_bytes(b'{"round": 1}')
    second.mkdir(parents=True)
    (second / "report.json").write_bytes(b'{"round": 2}')
    assert (first / "report.json").read_bytes() == b'{"round": 1}'


def test_checkpoint_paths_are_per_round_and_carry_task_ids(tmp_path):
    first = checkpoint_path_for(tmp_path, "qc-normal-001", 1)
    second = checkpoint_path_for(tmp_path, "qc-normal-001", 2)
    assert first != second
    first.parent.mkdir(parents=True)
    first.write_text(json.dumps({"task_id": "tsk_round_1"}) + "\n")
    assert json.loads(first.read_text())["task_id"] == "tsk_round_1"


def test_validate_deadline_bounds():
    assert validate_deadline(30) is None
    assert validate_deadline(240) is None
    assert validate_deadline(1800) is None
    assert validate_deadline(0) is not None
    assert validate_deadline(29) is not None
    assert validate_deadline(1801) is not None
    assert validate_deadline(-5) is not None
    assert validate_deadline("240") is not None
    assert DEADLINE_MIN_S == 30 and DEADLINE_MAX_S == 1800


# --- manifest digest + entries ------------------------------------------------


def _manifest_fixture() -> tuple[dict, dict[str, bytes], dict[str, dict]]:
    extraction = {
        "parser": "source-v1",
        "state": "ready",
        "segments": [
            {"locator": "lines:1-3", "text": "Valve-9 needs HSE certificate number 12345"},
        ],
        "tables": [],
        "warnings": [],
    }
    original = b"Valve-9 needs HSE certificate number 12345"
    manifest = {
        "version": 1,
        "sources": [
            {
                "id": "src_test1",
                "filename": "brief.txt",
                "sha256": hashlib.sha256(original).hexdigest(),
                "extraction_sha256": manifest_snapshot_digest(extraction),
            }
        ],
        "id": "smf_test1",
        "sha256": "",
    }
    manifest["sha256"] = manifest_snapshot_digest(manifest_snapshot(manifest))
    return manifest, {"src_test1": original}, {"src_test1": extraction}


def test_verify_manifest_digest_accepts_selected_manifest():
    manifest, _, _ = _manifest_fixture()
    assert verify_manifest_digest(manifest) == []


def test_verify_manifest_digest_rejects_tamper_and_missing_hash():
    manifest, _, _ = _manifest_fixture()
    tampered = copy.deepcopy(manifest)
    tampered["sources"][0]["filename"] = "other.txt"
    assert any("mismatch" in e for e in verify_manifest_digest(tampered))
    missing = copy.deepcopy(manifest)
    del missing["sha256"]
    assert verify_manifest_digest(missing) != []


def test_verify_manifest_entries_checks_originals_and_extractions():
    manifest, originals, extractions = _manifest_fixture()
    assert verify_manifest_entries(manifest, originals, extractions) == []
    bad_bytes = dict(originals, src_test1=b"tampered bytes")
    assert any(
        "original sha256" in e for e in verify_manifest_entries(manifest, bad_bytes, extractions)
    )
    bad_extraction = copy.deepcopy(extractions)
    bad_extraction["src_test1"]["segments"][0]["text"] = "rewritten text here"
    assert any(
        "extraction_sha256" in e
        for e in verify_manifest_entries(manifest, originals, bad_extraction)
    )
    assert any("not fetched" in e for e in verify_manifest_entries(manifest, {}, {}))


# --- deep citations ------------------------------------------------------------


def _citation_fixture() -> tuple[dict, list[dict], dict[str, dict]]:
    manifest, _, extractions = _manifest_fixture()
    output = {
        "summary": "synthetic summary with enough length for the schema",
        "findings": [
            {
                "text": "Valve-9 lacks a valid HSE certificate",
                "source_id": "src_test1",
                "locator": "lines:1-3",
                "quote": "HSE certificate number 12345",
            }
        ],
        "limitations": [],
    }
    return output, manifest["sources"], extractions


def test_verify_citations_accepts_grounded_finding():
    output, sources, extractions = _citation_fixture()
    assert verify_citations(output, sources, extractions) == []


def test_verify_citations_rejects_unselected_source_bad_locator_and_paraphrase():
    output, sources, extractions = _citation_fixture()
    foreign = copy.deepcopy(output)
    foreign["findings"][0]["source_id"] = "src_unknown"
    assert any("unselected" in e for e in verify_citations(foreign, sources, extractions))
    bad_locator = copy.deepcopy(output)
    bad_locator["findings"][0]["locator"] = "page:99"
    assert any("locator" in e for e in verify_citations(bad_locator, sources, extractions))
    paraphrase = copy.deepcopy(output)
    paraphrase["findings"][0]["quote"] = "the vendor seems fine overall"
    assert any("verbatim" in e for e in verify_citations(paraphrase, sources, extractions))
    short = copy.deepcopy(output)
    short["findings"][0]["quote"] = "HSE"
    assert verify_citations(short, sources, extractions) != []


def test_verify_citations_requires_every_source_covered():
    output, sources, extractions = _citation_fixture()
    sources = [*sources, {"id": "src_uncited"}]
    extractions = dict(extractions, src_uncited={"segments": []})
    assert any("no grounded finding" in e for e in verify_citations(output, sources, extractions))


# --- zip consistency ------------------------------------------------------------


def _zip_fixture() -> tuple[bytes, dict[str, bytes], dict]:
    manifest, _, _ = _manifest_fixture()
    manifest["sources"][0]["sha256"] = hashlib.sha256(b"original-bytes").hexdigest()
    manifest["sha256"] = manifest_snapshot_digest(manifest_snapshot(manifest))
    standalone = {
        "pdf": b"%PDF-1.7 synthetic",
        "docx": b"docx-bytes",
        "xlsx": b"xlsx-bytes",
        "json": json.dumps({"output": {}, "manifest": manifest}).encode(),
        "md": b"# synthetic",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for ext, body in standalone.items():
            archive.writestr(f"report.{ext}", body)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr("sources/src_test1/brief.txt", b"original-bytes")
    return buffer.getvalue(), standalone, manifest


def test_verify_zip_consistency_accepts_matching_bundle():
    zipped, standalone, manifest = _zip_fixture()
    assert verify_zip_consistency(zipped, standalone, manifest) == []


def test_verify_zip_consistency_rejects_divergent_report_and_original():
    zipped, standalone, manifest = _zip_fixture()
    tampered_standalone = dict(standalone, md=b"# rewritten")
    assert any(
        "report.md" in e for e in verify_zip_consistency(zipped, tampered_standalone, manifest)
    )
    wrong_manifest = copy.deepcopy(manifest)
    wrong_manifest["sha256"] = "0" * 64
    assert any("manifest" in e for e in verify_zip_consistency(zipped, standalone, wrong_manifest))


# --- HTTP error parsing + exact rejection gate ----------------------------------


class _FakeResponse:
    def __init__(self, status: int, kind: str | None, message: str) -> None:
        self.status_code = status
        self._body: dict | None = (
            {
                "error": {
                    "kind": kind,
                    "category": "test",
                    "message": message,
                    "details": {},
                    "retryable": False,
                }
            }
            if kind is not None
            else None
        )

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class _FakeHTTPError(Exception):
    def __init__(self, status: int, kind: str | None, message: str) -> None:
        super().__init__(f"{status} {message}")
        self.response = _FakeResponse(status, kind, message)


def test_parse_http_error_extracts_status_and_domain_kind():
    parsed = parse_http_error(_FakeHTTPError(404, "NOT_FOUND", "Source not found"))
    assert parsed == {
        "has_response": True,
        "http_status": 404,
        "kind": "NOT_FOUND",
        "message": "Source not found",
    }


def test_parse_http_error_marks_network_failures_as_no_response():
    parsed = parse_http_error(ConnectionError("connection refused"))
    assert parsed["has_response"] is False
    assert parsed["http_status"] is None


def test_check_expected_rejection_requires_exact_match():
    assert (
        check_expected_rejection(
            parse_http_error(_FakeHTTPError(404, "NOT_FOUND", "x")), 404, "NOT_FOUND"
        )
        == []
    )
    mismatch_500 = check_expected_rejection(
        parse_http_error(_FakeHTTPError(500, "INTERNAL", "boom")), 404, "NOT_FOUND"
    )
    assert any("mismatch" in e for e in mismatch_500)
    mismatch_network = check_expected_rejection(
        parse_http_error(TimeoutError("timed out")), 404, "NOT_FOUND"
    )
    assert any("never" in e for e in mismatch_network)
    mismatch_kind = check_expected_rejection(
        parse_http_error(_FakeHTTPError(404, "VALIDATION", "other")), 404, "NOT_FOUND"
    )
    assert any("kind" in e for e in mismatch_kind)


# --- provenance: totals never zero-fill, models verified --------------------------


def test_summarize_provenance_sums_actual_tokens_and_cost():
    executions = [
        {
            "id": "ex_1",
            "model_used": ALLOWED_FREE_MODELS[0],
            "input_tokens": 100,
            "output_tokens": 50,
            "reasoning_tokens": 10,
            "cost_usd": 0.001,
        },
        {
            "id": "ex_2",
            "model_used": ALLOWED_FREE_MODELS[0],
            "input_tokens": 200,
            "output_tokens": 70,
            "reasoning_tokens": 0,
            "cost_usd": 0.002,
        },
    ]
    usage = [
        {
            "model_used": ALLOWED_FREE_MODELS[0],
            "input_tokens": 300,
            "output_tokens": 120,
            "reasoning_tokens": 10,
            "cost_usd": 0.003,
        }
    ]
    summary = summarize_provenance(executions, usage)
    assert summary["execution_count"] == 2
    assert summary["ledger_count"] == 1
    assert summary["tokens"]["input_tokens"] == 300
    assert summary["tokens"]["output_tokens"] == 120
    assert summary["cost_usd"] == pytest.approx(0.003)
    assert summary["execution_totals"]["input_tokens"] == 300
    assert summary["ledger_totals"]["input_tokens"] == 300
    assert summary["models"] == [ALLOWED_FREE_MODELS[0]]


def test_summarize_provenance_reports_none_never_zero_when_unknown():
    summary = summarize_provenance([{"id": "ex_1"}], [])
    assert summary["tokens"] == {
        "input_tokens": None,
        "output_tokens": None,
        "reasoning_tokens": None,
    }
    assert summary["cost_usd"] is None
    assert summary["models"] == []


def test_check_model_provenance_rejects_fake_nonfree_and_missing():
    allowed = list(ALLOWED_FREE_MODELS)
    good_exec = [{"id": "ex_1", "model_used": ALLOWED_FREE_MODELS[0]}]
    assert (
        check_model_provenance(
            executions=good_exec,
            usage_items=[{"model_used": ALLOWED_FREE_MODELS[0]}],
            allowed_models=allowed,
            task_completed=True,
        )
        == []
    )
    fake = [{"id": "ex_1", "model_used": "fake-scripted-local"}]
    assert any(
        "fake" in e
        for e in check_model_provenance(
            executions=fake, usage_items=[], allowed_models=allowed, task_completed=True
        )
    )
    paid = [{"id": "ex_1", "model_used": "some-vendor/paid-model"}]
    assert any(
        "allowlist" in e
        for e in check_model_provenance(
            executions=paid, usage_items=[], allowed_models=allowed, task_completed=True
        )
    )
    assert any(
        "missing" in e
        for e in check_model_provenance(
            executions=[], usage_items=[], allowed_models=allowed, task_completed=True
        )
    )
    assert any(
        "missing" in e
        for e in check_model_provenance(
            executions=[{"id": "ex_1"}], usage_items=[], allowed_models=allowed, task_completed=True
        )
    )
    # A failed task with zero executions/ledger rows is evidence of no spend.
    assert (
        check_model_provenance(
            executions=[], usage_items=[], allowed_models=allowed, task_completed=False
        )
        == []
    )


# --- mocked HTTP contract: negatives, checkpoints ----------------------------------


def _missing_case() -> dict:
    return next(c for c in _load_corpus()["cases"] if c["id"] == "dr-missing-001")


def _unsupported_case() -> dict:
    return next(c for c in _load_corpus()["cases"] if c["id"] == "qc-unsupported-001")


def _run_harness_case(call, case: dict) -> tuple[dict, list[str]]:
    attempt: dict = {"run_id": "test-run", "case_id": case["id"], "round": 1, "failures": []}
    phases: list[str] = []

    def checkpoint(phase: str) -> None:
        phases.append(f"{phase}:{attempt.get('task_id', '-')}")
        checkpoint_path_for("/tmp", case["id"], 1)

    asyncio.run(
        _run_case(
            call,
            None,
            SimpleNamespace(),
            {"id": "agent_1"},
            case,
            attempt,
            "digest",
            0.0,
            checkpoint=checkpoint,
            allowed_models=tuple(ALLOWED_FREE_MODELS),
        )
    )
    return attempt, phases


def test_missing_source_exact_404_is_the_only_pass():
    async def call(method: str, path: str, **kwargs) -> dict:
        raise _FakeHTTPError(404, "NOT_FOUND", "Source not found")

    attempt, phases = _run_harness_case(call, _missing_case())
    assert attempt["expectation_met"] is True
    assert attempt["rejection"] == {"http_status": 404, "kind": "NOT_FOUND"}
    assert "rejection_mismatch" not in attempt
    assert any(p.startswith("negative:") for p in phases)


def test_missing_source_500_is_a_rejection_mismatch_not_a_pass():
    async def call(method: str, path: str, **kwargs) -> dict:
        raise _FakeHTTPError(500, "INTERNAL", "provider exploded")

    attempt, _ = _run_harness_case(call, _missing_case())
    assert attempt["expectation_met"] is not True
    assert attempt["rejection_mismatch"] is True
    assert any("mismatch" in f["error"] for f in attempt["failures"])


def test_missing_source_network_error_is_a_rejection_mismatch():
    async def call(method: str, path: str, **kwargs) -> dict:
        raise TimeoutError("upstream timed out")

    attempt, _ = _run_harness_case(call, _missing_case())
    assert attempt["expectation_met"] is not True
    assert attempt["rejection_mismatch"] is True


def test_missing_source_acceptance_checkpoints_the_stray_task():
    async def call(method: str, path: str, **kwargs) -> dict:
        return {"id": "tsk_stray", "status": "created"}

    attempt, phases = _run_harness_case(call, _missing_case())
    assert attempt["expectation_met"] is not True
    assert attempt["task_id"] == "tsk_stray"
    assert any(p == "negative:tsk_stray" for p in phases)


def _unsupported_call_no_spend() -> object:
    async def call(method: str, path: str, **kwargs) -> dict:
        if (method, path) == ("POST", "/source-files"):
            return {"id": "src_9", "state": "unsupported"}
        if (method, path) == ("POST", "/source-workflows"):
            return {"id": "tsk_9", "status": "waiting_for_input"}
        if path == "/tasks/tsk_9":
            return {
                "id": "tsk_9",
                "status": "waiting_for_input",
                "last_error": "Every source must be readable",
            }
        if path == "/tasks/tsk_9/sources":
            return {"manifest": None}
        if path == "/tasks/tsk_9/executions":
            return {"items": []}
        if path == "/model/usage":
            assert kwargs.get("params", {}).get("task_id") == "tsk_9"
            return {"items": [], "count": 0, "total": 0}
        if path == "/tasks/tsk_9/artifacts":
            return {"items": [], "reason": "not completed"}
        if (method, path) == ("POST", "/tasks/tsk_9/run"):
            raise _FakeHTTPError(409, "PRECONDITION", "needs input")
        raise AssertionError(f"unexpected call {method} {path}")

    return call


def test_unsupported_negative_proves_waiting_for_input_with_zero_spend():
    attempt, phases = _run_harness_case(_unsupported_call_no_spend(), _unsupported_case())
    assert attempt["expectation_met"] is True
    assert attempt["failure_class"] == "input-blocked"
    assert attempt["task_id"] == "tsk_9"
    assert attempt["prespend"]["executions"] == []
    assert attempt["prespend"]["usage"]["items"] == []
    assert attempt["postspend"]["executions"] == []
    assert attempt["postspend"]["usage"]["items"] == []
    assert any(p == "create:tsk_9" for p in phases)
    assert any(p.startswith("postspend:") for p in phases)


def test_unsupported_negative_fails_when_an_execution_exists():
    base = _unsupported_call_no_spend()

    async def call(method: str, path: str, **kwargs) -> dict:
        if path == "/tasks/tsk_9/executions":
            return {"items": [{"id": "ex_sneaky", "status": "completed"}]}
        return await base(method, path, **kwargs)

    attempt, _ = _run_harness_case(call, _unsupported_case())
    assert attempt["expectation_met"] is not True
    assert any("execution" in f["error"] for f in attempt["failures"])


def test_same_calls_in_execution_and_ledger_are_not_counted_twice():
    execution = [
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "reasoning_tokens": 10,
            "cost_usd": 0.003,
            "model_used": ALLOWED_FREE_MODELS[0],
        }
    ]
    ledger = [
        {
            "input_tokens": 60,
            "output_tokens": 25,
            "reasoning_tokens": 6,
            "cost_usd": 0.001,
            "model_used": ALLOWED_FREE_MODELS[0],
        },
        {
            "input_tokens": 40,
            "output_tokens": 15,
            "reasoning_tokens": 4,
            "cost_usd": 0.002,
            "model_used": ALLOWED_FREE_MODELS[0],
        },
    ]
    measured = summarize_provenance(execution, ledger)
    assert measured["tokens"]["input_tokens"] == 100
    assert measured["tokens"]["output_tokens"] == 40
    assert measured["cost_usd"] == pytest.approx(0.003)


def test_completed_evidence_requires_usage_ledger_not_execution_alone():
    errors = check_model_provenance(
        executions=[{"model_used": ALLOWED_FREE_MODELS[0]}],
        usage_items=[],
        allowed_models=ALLOWED_FREE_MODELS,
        task_completed=True,
    )
    assert any("ledger" in e for e in errors)


def test_nonfree_model_cannot_be_enabled_by_allowlist_override():
    errors = check_model_provenance(
        executions=[{"model_used": "vendor/paid"}],
        usage_items=[{"model_used": "vendor/paid"}],
        allowed_models=["vendor/paid"],
        task_completed=True,
    )
    assert any("free" in e for e in errors)


def test_missing_dependency_fails_before_http_client_and_model_calls(tmp_path, monkeypatch):
    import asyncio

    import httpx
    import source_reliability as harness

    original = harness.importlib.import_module

    def missing_domain(name):
        if name == "ai_orchestrator.domain.source_report":
            raise ModuleNotFoundError("No module named ai_orchestrator")
        return original(name)

    def forbidden_client(*args, **kwargs):
        pytest.fail("Dependency preflight must precede every API/model call")

    monkeypatch.setattr(harness.importlib, "import_module", missing_domain)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    args = harness._parse_args(
        ["--org", "synthetic", "--output", str(tmp_path / "missing-dependency")]
    )
    monkeypatch.delenv("AO_MODEL_PROVIDER_DEFAULT", raising=False)
    assert asyncio.run(harness._amain(args)) == 2
    receipt = json.loads((args.output / "run-receipt.json").read_text())
    assert receipt["setup_error"]["phase"] == "runtime_dependencies"
    assert receipt["attempts"] == [] and not receipt["passed"]


def test_preflight_profile_chain_rejects_paid_hidden_fallback():
    from source_reliability import profile_policy_errors

    profiles = [
        {
            "name": "primary",
            "providers": [{"provider": "openrouter", "model": ALLOWED_FREE_MODELS[0]}],
            "fallback_profile": "other",
        },
        {
            "name": "other",
            "providers": [{"provider": "openrouter", "model": "vendor/paid"}],
            "fallback_profile": None,
        },
    ]
    assert profile_policy_errors("primary", profiles, list(ALLOWED_FREE_MODELS))
    profiles[1]["providers"][0]["model"] = ALLOWED_FREE_MODELS[1]
    assert profile_policy_errors("primary", profiles, list(ALLOWED_FREE_MODELS)) == []
    profiles[1]["fallback_profile"] = "primary"
    assert profile_policy_errors("primary", profiles, list(ALLOWED_FREE_MODELS))
