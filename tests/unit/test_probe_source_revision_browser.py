"""Offline unit tests for the source-revision browser probe harness.

Pure helpers and tmp-filesystem contracts only: no browser, no API, no
model calls, no network, no clock dependence. The live probe in
scripts/probe_source_revision_browser.py is exercised only by hand against
localhost; it is never launched here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import probe_source_revision_browser as probe


def _viewport_out() -> dict:
    return {
        "viewport": "desktop",
        "passed": False,
        "errors": [],
        "warnings": [],
        "http": [],
        "screenshots": {},
        "checks": {},
        "skipped": [],
        "passed_with_skip": False,
    }


# --- T1: SKIP vs PASS accounting (pure) ---------------------------------------


def test_all_true_checks_have_no_skip():
    checks = {"history_visible": True, "start_offered": True, "no_run_started": True}
    summary = probe.summarize_checks(checks)
    assert summary == {"total": 3, "skipped": [], "passed_with_skip": False}


def test_skip_value_is_flagged_not_counted_as_pass():
    checks = {
        "history_visible": True,
        "pagination_preserves_selection": "SKIP",
        "revision_more_hidden": True,
    }
    summary = probe.summarize_checks(checks)
    assert summary["total"] == 3
    assert summary["skipped"] == ["pagination_preserves_selection"]
    assert summary["passed_with_skip"] is True


def test_finalize_marks_passed_with_skip_but_still_passed():
    out = _viewport_out()
    out["checks"] = {"history_visible": True, "pagination_preserves_selection": "SKIP"}
    probe.finalize_viewport(out)
    assert out["passed"] is True
    assert out["skipped"] == ["pagination_preserves_selection"]
    assert out["passed_with_skip"] is True


def test_finalize_without_skip_is_plain_pass():
    out = _viewport_out()
    out["checks"] = {"history_visible": True}
    probe.finalize_viewport(out)
    assert out["passed"] is True
    assert out["passed_with_skip"] is False


def test_finalize_with_errors_fails_even_with_skip():
    out = _viewport_out()
    out["errors"].append("pageerror: boom")
    out["checks"] = {"pagination_preserves_selection": "SKIP"}
    probe.finalize_viewport(out)
    assert out["passed"] is False
    assert out["passed_with_skip"] is False
    assert out["skipped"] == ["pagination_preserves_selection"]


# --- T2: no-overwrite contract (tmp only) --------------------------------------


def test_prepare_output_dir_refuses_existing_without_touching(tmp_path):
    target = tmp_path / "probe-out"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    error = probe.prepare_output_dir(target)
    assert error is not None
    assert error["phase"] == "output_dir"
    assert "overwrite" in error["reason"]
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert sorted(p.name for p in target.iterdir()) == ["sentinel.txt"]


def test_prepare_output_dir_refuses_existing_file_path(tmp_path):
    target = tmp_path / "taken"
    target.write_text("x", encoding="utf-8")
    error = probe.prepare_output_dir(target)
    assert error is not None and error["phase"] == "output_dir"
    assert target.read_text(encoding="utf-8") == "x"


def test_prepare_output_dir_creates_missing_nested_dir(tmp_path):
    target = tmp_path / "fresh" / "nested"
    assert probe.prepare_output_dir(target) is None
    assert target.is_dir()


# --- T3: injected negatives stay separate from unexpected HTTP ------------------
# Fault injection here is real: an unexpected status is recorded as a genuine
# viewport error and maps to a nonzero exit, not merely logged.


def test_deliberate_negative_statuses_do_not_fail():
    assert probe.classify_http_status(201) == "ok"
    assert probe.classify_http_status(409) == "expected_negative"
    assert probe.classify_http_status(422) == "expected_negative"


def test_forbidden_and_server_errors_are_unexpected():
    for status in (400, 403, 404, 500, 503):
        assert probe.classify_http_status(status) == "unexpected", status


def test_expected_negative_http_is_evidence_not_error():
    out = _viewport_out()
    entry = probe.record_http(
        out,
        kind="expected_negative_http",
        url="http://127.0.0.1:8112/api/v1/tasks/t/source-revisions",
        status=409,
        body='{"detail": "conflict"}',
        stage="idempotent_replay",
    )
    assert entry["status"] == 409
    assert entry["stage"] == "idempotent_replay"
    assert out["errors"] == []
    probe.finalize_viewport(out)
    assert out["passed"] is True


def test_unexpected_http_is_a_real_failure_with_nonzero_exit():
    out = _viewport_out()
    out["checks"] = {"history_visible": True}
    entry = probe.record_http(
        out,
        kind="unexpected_http",
        url="http://127.0.0.1:8112/api/v1/source-files/mock",
        status=503,
        body="upstream unavailable" * 100,
        stage="mock_replacement",
    )
    assert entry["url"].endswith("/source-files/mock")
    assert entry["status"] == 503
    assert len(entry["body"]) == 400
    assert entry["stage"] == "mock_replacement"
    assert len(out["errors"]) == 1 and "503" in out["errors"][0]
    finalized = probe.finalize_viewport(out)
    assert finalized["passed"] is False
    assert probe.receipt_exit_code({"passed": False}) == 1
    assert probe.receipt_exit_code({"passed": False}) != 0


def test_receipt_exit_codes_distinguish_setup_failure_and_pass():
    assert probe.receipt_exit_code({"passed": True}) == 0
    assert probe.receipt_exit_code({"passed": False}) == 1
    assert probe.receipt_exit_code({"passed": False, "setup_error": {"phase": "x"}}) == 2


# --- T4: console errors fail, warnings do not ------------------------------------


def test_console_error_fails_the_viewport():
    out = _viewport_out()
    out["checks"] = {"history_visible": True}
    probe.record_console(out, "error", "Uncaught TypeError: cannot read properties")
    assert len(out["errors"]) == 1
    assert "console.error" in out["errors"][0]
    probe.finalize_viewport(out)
    assert out["passed"] is False


def test_console_warning_is_kept_without_failing():
    out = _viewport_out()
    out["checks"] = {"history_visible": True}
    probe.record_console(out, "warning", "slow resource")
    assert out["errors"] == []
    assert len(out["warnings"]) == 1
    probe.finalize_viewport(out)
    assert out["passed"] is True


def test_console_info_and_empty_types_are_ignored():
    assert probe.classify_console("log") == "info"
    assert probe.classify_console("") == "info"
    out = _viewport_out()
    probe.record_console(out, "log", "hello")
    assert out["errors"] == [] and out["warnings"] == []


def test_console_entry_is_bounded():
    out = _viewport_out()
    probe.record_console(out, "error", "x" * 5000)
    assert len(out["errors"][0]) <= probe.CONSOLE_BODY_LIMIT


# --- T5: incremental append-only receipt survives a mid-run kill -----------------


def test_stage_log_survives_kill_and_appends_across_runs(tmp_path):
    output = tmp_path / "probe-out"
    assert probe.prepare_output_dir(output) is None
    probe.append_stage(output, {"stage": "init", "passed": False})
    probe.write_receipt(output, {"passed": False, "stages": ["init"]})
    # Simulated kill here: process dies after init, stages.log stays on disk.
    probe.append_stage(output, {"stage": "fixture", "passed": False})
    lines = (output / "stages.log").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["stage"] for line in lines] == ["init", "fixture"]
    assert all("ts" in json.loads(line) for line in lines)
    # A later rerun must append, never rewrite the log.
    probe.append_stage(output, {"stage": "viewport:desktop", "passed": True})
    lines = (output / "stages.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    receipt = json.loads((output / "acceptance.json").read_text(encoding="utf-8"))
    assert receipt["passed"] is False
    assert receipt["stages"] == ["init"]


def test_base_receipt_carries_synthetic_and_review_flags():
    receipt = probe.base_receipt()
    assert receipt["synthetic"] is True
    assert receipt["passed"] is False
    assert receipt["human_review_required"] is True
    assert receipt["stages"] == [] and receipt["viewports"] == {}


ABORT_TEXT = "Failed to load resource: net::ERR_FAILED"


def test_one_armed_abort_console_is_separate_evidence():
    out = _viewport_out()
    armed = {"http://localhost/fixture": 1}
    probe.record_console(
        out, "error", ABORT_TEXT, location_url="http://localhost/fixture", expected_aborts=armed
    )
    assert out["errors"] == []
    assert len(out["expected_console"]) == 1
    assert armed["http://localhost/fixture"] == 0


def test_unrelated_url_abort_still_fails():
    out = _viewport_out()
    armed = {"http://localhost/fixture": 1}
    probe.record_console(
        out, "error", ABORT_TEXT, location_url="http://localhost/other", expected_aborts=armed
    )
    assert len(out["errors"]) == 1
    assert armed["http://localhost/fixture"] == 1


def test_other_console_error_on_fixture_url_still_fails():
    out = _viewport_out()
    armed = {"http://localhost/fixture": 1}
    probe.record_console(
        out,
        "error",
        "Unexpected application error",
        location_url="http://localhost/fixture",
        expected_aborts=armed,
    )
    assert len(out["errors"]) == 1
    assert armed["http://localhost/fixture"] == 1


def test_second_identical_abort_is_not_silently_ignored():
    out = _viewport_out()
    armed = {"http://localhost/fixture": 1}
    for _ in range(2):
        probe.record_console(
            out, "error", ABORT_TEXT, location_url="http://localhost/fixture", expected_aborts=armed
        )
    assert len(out["errors"]) == 1
    assert len(out["expected_console"]) == 1


def test_saved_midprobe_exception_never_becomes_pass():
    out = _viewport_out()
    out["checks"] = {"first_stage": True}
    out["exception"] = "SYNTHETIC_ROOT_MIDPROBE_ABORT"
    assert probe.finalize_viewport(out)["passed"] is False


def complete_receipt():
    return {
        "errors": [],
        "viewports": {name: {"passed": True, "errors": []} for name in ("desktop", "mobile")},
    }


def test_success_requires_all_expected_viewports():
    receipt = complete_receipt()
    receipt["viewports"].pop("mobile")
    assert probe.finalize_receipt(receipt)["passed"] is False


def test_global_failure_cannot_be_masked_by_passing_viewports():
    receipt = complete_receipt()
    receipt["errors"].append("viewport loop failed")
    assert probe.finalize_receipt(receipt)["passed"] is False


def test_nested_exception_cannot_be_masked_by_stale_pass_flag():
    receipt = complete_receipt()
    receipt["viewports"]["desktop"]["exception"] = "late failure"
    assert probe.finalize_receipt(receipt)["passed"] is False


def test_complete_error_free_receipt_passes():
    assert probe.finalize_receipt(complete_receipt())["passed"] is True
