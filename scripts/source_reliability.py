"""Rerunnable CLI holdout for source-workflow reliability (R0).

Live path only: drives the actual local source API (/api/v1 source
endpoints) and the real model path. It never substitutes a fake model,
never changes the model selection, and never spends on paid models via
this script (no model override is ever sent; the server default applies).

Same provider runtime policy as scripts/probe_source_workflow.py
(read-only reference, not modified here): localhost API only, real
uploaded/mock source files through the parser path, real task run,
artifact downloads verified by hash.

Evidence model (harness-review fixes):

* Per-attempt directories ``<output>/<case-id>/r<round>/`` hold every
  downloaded byte. Downloads are written BEFORE grading, so failed checks
  retain their files, and later rounds never overwrite earlier ones.
* The receipt plus ``checkpoints/<case>-r<round>.json`` are persisted after
  every phase (upload/create/manifest/extractions/run/provenance/download),
  and the intent record is written BEFORE any network call for the attempt.
  An interrupted run keeps task/run/manifest IDs.
* Server runtime provenance is observed, never assumed: actual ``model_used``
  from ``GET /executions/{id}`` and the ``GET /model/usage?task_id=`` ledger
  are fetched on EVERY task including failures and negatives. Fake, scripted
  or non-allowlisted models (or missing provenance on a completed task) are
  rejected as evidence. The client-side ``AO_MODEL_PROVIDER_DEFAULT`` env var
  is NOT evidence of server policy.
* The missing-source negative requires the exact expected HTTP status and
  domain error kind from the corpus. 500s, timeouts and network errors are
  rejection mismatches, never passes.
* The unsupported-source negative binds the non-ready file, attempts a run,
  and proves ``waiting_for_input`` with zero executions, zero ledger rows
  and zero artifacts before AND after the run attempt.
* Citations are verified against fetched extraction segments, the manifest
  canonical hash, original/extraction hashes, and the zip-vs-standalone
  byte equality -- not mere field presence.
* Execution/ledger totals report ``None`` when unknown; unknown is never
  filled as zero.

Pure helpers in this module (validation, failure classification,
provenance, citation/manifest/zip verification, latency aggregation) use
only the standard library so tests/unit/test_source_reliability.py can
exercise them without API or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import math
import os
import re
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REQUIRED_FORMATS = ("docx", "xlsx", "pdf", "json", "md", "zip")

FAILURE_CLASSES = (
    "refusal",
    "availability",
    "timeout",
    "validation",
    "input-blocked",
    "unknown",
)

FAILURE_CLASSIFICATION_DISCLAIMER = (
    "HEURISTIC, NOT DEFINITIVE: failure_class is assigned by keyword "
    "matching over persisted task/execution errors recorded before and "
    "during the run. It is an operator triage hint only and must never be "
    "presented as a definitive root cause."
)

CASE_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INJECTION_MARKER = "NOT AN INSTRUCTION"
WORKFLOWS = ("document_review", "quote_comparison")
KINDS = ("normal", "missing_source", "unsupported_source", "injection")

# Verified free candidates for the selected test tenant profile (runtime.json).
# The live path rejects any observed model outside this allowlist (unless the
# operator explicitly overrides --allowed-models, which is recorded verbatim
# in the receipt).
ALLOWED_FREE_MODELS = (
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "dots-studio/dots-3-note-preview:free",
)

# Substrings that mark a model id as fake/scripted rather than a real
# provider model. Any observed model_used containing one of these (case
# insensitive) is rejected as evidence.
FAKE_MODEL_MARKERS = ("fake", "script", "stub", "simulat", "placeholder", "dummy")

DEADLINE_MIN_S = 30
DEADLINE_MAX_S = 1800

TERMINAL_STATUSES = {
    "completed",
    "failed",
    "canceled",
    "waiting_for_input",
    "waiting_for_approval",
}


# ---------------------------------------------------------------------------
# Pure helpers (no I/O, no network, no model calls)
# ---------------------------------------------------------------------------


def canonical_corpus_bytes(corpus: dict[str, Any]) -> bytes:
    """Canonical JSON used for corpus identity (sorted keys, compact)."""
    return json.dumps(corpus, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def corpus_sha256(corpus: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_corpus_bytes(corpus)).hexdigest()


def validate_corpus(corpus: Any) -> list[str]:
    """Return a list of validation errors; empty means valid."""
    errors: list[str] = []
    if not isinstance(corpus, dict):
        return ["corpus must be a JSON object"]
    if corpus.get("synthetic") is not True:
        errors.append("corpus must be labeled synthetic (top-level synthetic: true)")
    if not corpus.get("corpus_name") or not isinstance(corpus.get("corpus_name"), str):
        errors.append("corpus_name must be a non-empty string")
    if not corpus.get("version") or not isinstance(corpus.get("version"), str):
        errors.append("version must be a non-empty string")
    cases = corpus.get("cases")
    if not isinstance(cases, list) or len(cases) < 6:
        errors.append("cases must be a list with at least 6 entries")
        return errors
    seen: set[str] = set()
    per_workflow: dict[str, int] = {}
    quote_outcomes: set[tuple[Any, Any]] = set()
    for index, case in enumerate(cases):
        where = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{where} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not CASE_ID_RE.match(case_id):
            errors.append(f"{where}.id must be a stable slug [a-z0-9-]")
        elif case_id in seen:
            errors.append(f"{where}.id {case_id!r} is duplicated")
        else:
            seen.add(case_id)
        workflow = case.get("workflow")
        if workflow not in WORKFLOWS:
            errors.append(f"{where}.workflow must be one of {list(WORKFLOWS)}")
        else:
            per_workflow[workflow] = per_workflow.get(workflow, 0) + 1
        kind = case.get("kind")
        if kind not in KINDS:
            errors.append(f"{where}.kind must be one of {list(KINDS)}")
        if not isinstance(case.get("goal"), str) or not case.get("goal", "").strip():
            errors.append(f"{where}.goal must be a non-empty string")
        expected = case.get("expected")
        if not isinstance(expected, dict):
            errors.append(f"{where}.expected must be an object")
            continue
        if kind in ("normal", "injection"):
            if expected.get("status") != "completed":
                errors.append(f"{where}.expected.status must be 'completed'")
            if kind == "injection":
                blob = json.dumps(case, ensure_ascii=False)
                if INJECTION_MARKER not in blob:
                    errors.append(
                        f"{where}: injection case must carry content explicitly "
                        f"marked {INJECTION_MARKER!r} as data"
                    )
                if case.get("human_review_required") is not True:
                    errors.append(f"{where}: injection case must require human review")
            if workflow == "quote_comparison":
                if not isinstance(expected.get("winner"), str) or not isinstance(
                    expected.get("total_vnd"), int
                ):
                    errors.append(
                        f"{where}.expected needs deterministic winner (str) and total_vnd (int)"
                    )
                else:
                    quote_outcomes.add((expected.get("winner"), expected.get("total_vnd")))
                if case.get("selection", {}).get("mode") != "mock":
                    source = case.get("source")
                    if not isinstance(source, dict) or not isinstance(source.get("csv_text"), str):
                        errors.append(
                            f"{where}: non-mock quote_comparison case must carry source.csv_text"
                        )
            else:
                if case.get("selection", {}).get("mode") != "mock" and not isinstance(
                    case.get("source"), dict
                ):
                    errors.append(f"{where}: non-mock case must carry a source payload")
        elif kind in ("missing_source", "unsupported_source"):
            if expected.get("status") != "rejected":
                errors.append(f"{where}.expected.status must be 'rejected'")
            if expected.get("rejection_expected") is not True:
                errors.append(f"{where}.expected.rejection_expected must be true")
            if kind == "missing_source":
                if not isinstance(expected.get("expected_http_status"), int):
                    errors.append(f"{where}.expected needs an exact expected_http_status (int)")
                if (
                    not isinstance(expected.get("expected_error_kind"), str)
                    or not expected.get("expected_error_kind", "").strip()
                ):
                    errors.append(f"{where}.expected needs an exact expected_error_kind (str)")
            else:
                if expected.get("expected_task_status") != "waiting_for_input":
                    errors.append(
                        f"{where}.expected.expected_task_status must be 'waiting_for_input'"
                    )
                if expected.get("expected_zero_spend") is not True:
                    errors.append(f"{where}.expected.expected_zero_spend must be true")
    for workflow in WORKFLOWS:
        if per_workflow.get(workflow, 0) < 3:
            errors.append(
                f"workflow {workflow!r} needs at least 3 cases, "
                f"found {per_workflow.get(workflow, 0)}"
            )
    if len(quote_outcomes) < 2:
        errors.append(
            "frozen corpus must carry at least two distinct computed quote "
            f"outcomes (winner, total_vnd); found {sorted(quote_outcomes)}"
        )
    return errors


_REFUSAL_RE = re.compile(r"refus|content.?filter|policy.?den|blocked by|not allowed|safety", re.I)
_AVAILABILITY_RE = re.compile(
    r"unavailable|gateway|429|503|5\d\d|timed? ?out|timeout|transport|connection|"
    r"no candidate|overloaded|rate.?limit|temporar",
    re.I,
)
_VALIDATION_RE = re.compile(
    r"validat|schema|grade|artifact|hash|mismatch|citation|finding|repair|exhaust|reopen", re.I
)


def classify_failure(record: dict[str, Any]) -> str:
    """Heuristic failure class. See FAILURE_CLASSIFICATION_DISCLAIMER.

    Inputs are persisted evidence only: task status/last_error, execution
    error_category, HTTP status of the failing call, and whether the poll
    loop hit its deadline. Never definitive.
    """
    if record.get("timeout_exceeded"):
        return "timeout"
    status = str(record.get("status") or "")
    if status == "waiting_for_input":
        return "input-blocked"
    http_status = record.get("http_status")
    if isinstance(http_status, int) and http_status in (404, 409, 422):
        return "input-blocked"
    text = " ".join(
        str(record.get(key) or "")
        for key in ("last_error", "error_category", "error_kind", "exception")
    )
    if _REFUSAL_RE.search(text):
        return "refusal"
    if _AVAILABILITY_RE.search(text):
        return "availability"
    if _VALIDATION_RE.search(text):
        return "validation"
    if status in ("failed", "canceled", "expired"):
        return "unknown"
    if status == "completed":
        return "unknown"
    return "unknown"


def percentile_nearest_rank(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile over a sorted copy. None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil((pct / 100.0) * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize_latencies(latencies_ms: list[float]) -> dict[str, Any]:
    """Accepted-artifact-only latency summary. No SLA implied."""
    return {
        "count": len(latencies_ms),
        "p50_ms": percentile_nearest_rank(latencies_ms, 50),
        "p95_ms": percentile_nearest_rank(latencies_ms, 95),
    }


def attempt_dir_for(output: Path | str, case_id: str, round_no: int) -> Path:
    """Per-attempt directory: later rounds never overwrite earlier ones."""
    return Path(output) / case_id / f"r{round_no}"


def checkpoint_path_for(output: Path | str, case_id: str, round_no: int) -> Path:
    """Checkpoint file holding the attempt (incl. task IDs) after each phase."""
    return Path(output) / "checkpoints" / f"{case_id}-r{round_no}.json"


def validate_deadline(value: Any) -> str | None:
    """None when the per-case deadline is within the sensible cap, else an error."""
    if isinstance(value, bool) or not isinstance(value, int):
        return f"deadline must be an int in [{DEADLINE_MIN_S}..{DEADLINE_MAX_S}]s, got {value!r}"
    if not DEADLINE_MIN_S <= value <= DEADLINE_MAX_S:
        return f"deadline must be in [{DEADLINE_MIN_S}..{DEADLINE_MAX_S}]s, got {value!r}"
    return None


def verify_artifact_bundle(
    artifacts_meta: list[dict[str, Any]],
    downloaded: dict[str, bytes],
) -> list[str]:
    """Pure checks over downloaded bytes: formats, hashes, pdf/zip shape.

    Office-content checks (docx paragraphs, xlsx rows) need third-party
    libraries and run in the live path, not here.
    """
    errors: list[str] = []
    formats = [a.get("format") for a in artifacts_meta]
    if set(formats) != set(REQUIRED_FORMATS):
        errors.append(f"expected formats {sorted(REQUIRED_FORMATS)}, got {sorted(formats)}")
    by_format = {a.get("format"): a for a in artifacts_meta}
    for fmt in REQUIRED_FORMATS:
        meta = by_format.get(fmt)
        body = downloaded.get(fmt)
        if meta is None or body is None:
            errors.append(f"missing artifact bytes for {fmt}")
            continue
        digest = hashlib.sha256(body).hexdigest()
        if digest != meta.get("sha256"):
            errors.append(f"{fmt}: sha256 mismatch (tampered or corrupt artifact)")
    pdf = downloaded.get("pdf")
    if pdf is not None and not pdf.startswith(b"%PDF-"):
        errors.append("pdf: missing %PDF- magic")
    zipped = downloaded.get("zip")
    if zipped is not None:
        try:
            with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
                if archive.testzip() is not None:
                    errors.append("zip: corrupt member")
                elif not any(n.startswith("sources/") for n in archive.namelist()):
                    errors.append("zip: missing sources/ listing")
        except zipfile.BadZipFile:
            errors.append("zip: not a zip archive")
    return errors


def check_citations(output: Any, min_findings: int = 1) -> list[str]:
    errors: list[str] = []
    if not isinstance(output, dict):
        return ["report output must be an object"]
    findings = output.get("findings")
    if not isinstance(findings, list) or len(findings) < min_findings:
        return [f"need at least {min_findings} grounded finding(s)"]
    for finding in findings:
        if not isinstance(finding, dict) or any(
            not isinstance(finding.get(k), str) or not finding.get(k, "").strip()
            for k in ("text", "source_id", "locator", "quote")
        ):
            errors.append("each finding needs non-empty text/source_id/locator/quote")
            break
    return errors


def check_quote_expected(output: Any, winner: str, total_vnd: int) -> list[str]:
    if not isinstance(output, dict):
        return ["report output must be an object"]
    errors: list[str] = []
    if output.get("winner") != winner:
        errors.append(f"winner {output.get('winner')!r} != expected {winner!r}")
    if output.get("total_vnd") != total_vnd:
        errors.append(f"total_vnd {output.get('total_vnd')!r} != expected {total_vnd!r}")
    return errors


def manifest_snapshot(manifest: dict[str, Any]) -> dict[str, Any]:
    """The server-signed payload: manifest minus its id and sha256 envelope."""
    return {k: v for k, v in manifest.items() if k not in ("id", "sha256")}


def manifest_snapshot_digest(snapshot: Any) -> str:
    """Canonical digest. Byte-identical canonical form to the server-side
    ``application.source_intake.digest`` (read-only reference, not imported
    here so unit tests stay dependency-free)."""
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def verify_manifest_digest(manifest: Any) -> list[str]:
    """The manifest canonical hash must recompute to the selected sha256."""
    if not isinstance(manifest, dict):
        return ["source manifest must be an object"]
    claimed = manifest.get("sha256")
    if not isinstance(claimed, str) or not claimed.strip():
        return ["source manifest is missing its sha256"]
    recomputed = manifest_snapshot_digest(manifest_snapshot(manifest))
    if recomputed != claimed:
        return [
            "manifest sha256 mismatch: recomputed "
            f"{recomputed[:16]}... != selected {claimed[:16]}... (tampered manifest)"
        ]
    return []


def verify_manifest_entries(
    manifest: Any,
    originals: dict[str, bytes],
    extractions: dict[str, Any],
) -> list[str]:
    """Every manifest entry's original bytes and extraction must hash to the
    recorded sha256 / extraction_sha256 (mirrors the server bind-time check,
    evaluated here over harness-fetched evidence)."""
    errors: list[str] = []
    if not isinstance(manifest, dict) or not isinstance(manifest.get("sources"), list):
        return ["source manifest has no sources list"]
    for entry in manifest["sources"]:
        if not isinstance(entry, dict):
            errors.append("manifest entry must be an object")
            continue
        sid = entry.get("id")
        body = originals.get(sid)
        if body is None:
            errors.append(f"source {sid!r}: original bytes were not fetched")
        elif hashlib.sha256(body).hexdigest() != entry.get("sha256"):
            errors.append(f"source {sid!r}: original sha256 mismatch (tampered or corrupt)")
        extraction = extractions.get(sid)
        if extraction is None:
            errors.append(f"source {sid!r}: extraction was not fetched")
        elif manifest_snapshot_digest(extraction) != entry.get("extraction_sha256"):
            errors.append(f"source {sid!r}: extraction_sha256 mismatch (tampered extraction)")
    return errors


def verify_citations(
    output: Any,
    manifest_sources: Any,
    extractions: dict[str, Any],
    min_findings: int = 1,
) -> list[str]:
    """Deep citation check over fetched evidence (mirrors server-side
    ``domain.source_report.validate_report``, evaluated here so the harness
    does not trust field presence alone):

    * every finding carries non-empty text/source_id/locator/quote;
    * every cited source_id is a selected manifest source;
    * every (source_id, locator) names a real extraction segment and the
      verbatim quote (>= 8 chars) occurs in that segment's text;
    * every selected source is covered by at least one finding.
    """
    errors: list[str] = []
    shape = check_citations(output, min_findings)
    if shape:
        return shape
    assert isinstance(output, dict)
    if not isinstance(manifest_sources, list) or not manifest_sources:
        return ["manifest carries no sources to cite"]
    manifest_ids = {s.get("id") for s in manifest_sources if isinstance(s, dict)}
    segments: dict[tuple[str, str], str] = {}
    for sid in manifest_ids:
        extraction = extractions.get(sid)
        if not isinstance(extraction, dict) or not isinstance(extraction.get("segments"), list):
            errors.append(f"source {sid!r}: extraction segments were not fetched")
            continue
        for segment in extraction["segments"]:
            if isinstance(segment, dict):
                segments[(sid, segment.get("locator"))] = segment.get("text", "")
    covered: set[str] = set()
    for finding in output["findings"]:
        assert isinstance(finding, dict)
        key = (finding["source_id"], finding["locator"])
        if finding["source_id"] not in manifest_ids:
            errors.append(
                f"finding cites unselected source {finding['source_id']!r} "
                "(citation outside the manifest)"
            )
            continue
        if key not in segments or not isinstance(segments[key], str):
            errors.append(
                f"finding cites unknown locator {finding['locator']!r} "
                f"for source {finding['source_id']!r}"
            )
            continue
        quote = finding["quote"]
        if len(quote) < 8 or quote not in segments[key]:
            errors.append(
                f"finding quote is not a verbatim excerpt (>= 8 chars) of "
                f"{finding['source_id']!r} / {finding['locator']!r}"
            )
            continue
        covered.add(finding["source_id"])
    for sid in sorted(manifest_ids):
        if sid not in covered:
            errors.append(f"selected source {sid!r} has no grounded finding")
    return errors


def verify_zip_consistency(
    zip_bytes: Any,
    standalone: dict[str, bytes],
    manifest: Any,
) -> list[str]:
    """The zip bundle must embed byte-identical reports, the selected
    manifest, and originals whose hashes match the manifest entries."""
    errors: list[str] = []
    if not isinstance(zip_bytes, (bytes, bytearray)) or not zip_bytes:
        return ["zip artifact bytes were not downloaded"]
    if not isinstance(manifest, dict):
        return ["source manifest must be an object for zip verification"]
    try:
        with zipfile.ZipFile(io.BytesIO(bytes(zip_bytes))) as archive:
            if archive.testzip() is not None:
                return ["zip: corrupt member"]
            names = set(archive.namelist())
            members = {name: archive.read(name) for name in names}
    except zipfile.BadZipFile:
        return ["zip: not a zip archive"]
    for ext in ("pdf", "docx", "xlsx", "json", "md"):
        member = members.get(f"report.{ext}")
        expected = standalone.get(ext)
        if member is None:
            errors.append(f"zip: missing report.{ext}")
        elif expected is None:
            errors.append(f"zip: standalone report.{ext} was not downloaded for comparison")
        elif member != expected:
            errors.append(f"zip: report.{ext} differs from the standalone download")
    embedded_manifest_raw = members.get("manifest.json")
    if embedded_manifest_raw is None:
        errors.append("zip: missing manifest.json")
    else:
        try:
            embedded = json.loads(embedded_manifest_raw.decode("utf-8"))
        except ValueError, UnicodeDecodeError:
            errors.append("zip: manifest.json is not valid JSON")
            embedded = None
        if embedded is not None:
            if manifest_snapshot_digest(manifest_snapshot(embedded)) != manifest.get("sha256"):
                errors.append("zip: embedded manifest hash != selected manifest sha256")
            if embedded.get("id") != manifest.get("id"):
                errors.append("zip: embedded manifest id != selected manifest id")
    sources = manifest.get("sources") if isinstance(manifest.get("sources"), list) else []
    for entry in sources:
        if not isinstance(entry, dict):
            continue
        name = f"sources/{entry.get('id')}/{entry.get('filename')}"
        member = members.get(name)
        if member is None:
            errors.append(f"zip: missing embedded original {name}")
        elif hashlib.sha256(member).hexdigest() != entry.get("sha256"):
            errors.append(f"zip: embedded original {name} hash != manifest sha256")
    return errors


def parse_http_error(exc: Any) -> dict[str, Any]:
    """Extract HTTP status + domain error kind from a client exception.

    Never raises: transport failures (no response attached) report
    ``has_response=False`` so the caller can reject them as rejection
    evidence instead of crashing.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is None or not isinstance(status, int):
        return {
            "has_response": False,
            "http_status": None,
            "kind": None,
            "message": str(exc)[:1500],
        }
    kind: Any = None
    message = str(exc)[:1500]
    try:
        body = response.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            kind = error.get("kind")
            if isinstance(error.get("message"), str) and error["message"].strip():
                message = error["message"][:1500]
    return {
        "has_response": True,
        "http_status": status,
        "kind": kind,
        "message": message,
    }


def check_expected_rejection(
    parsed: dict[str, Any],
    expected_status: Any,
    expected_kind: Any,
) -> list[str]:
    """Exact-match gate for the missing-source negative.

    A 500, a timeout, a connection error, or a wrong domain kind is a
    rejection MISMATCH (recorded, never a pass) -- only the exact expected
    HTTP status + domain error kind from the corpus counts.
    """
    if not parsed.get("has_response"):
        return [
            "no HTTP response (network/timeout/provider outage) must never "
            "count as the expected missing-source rejection"
        ]
    errors: list[str] = []
    if parsed.get("http_status") != expected_status:
        errors.append(
            f"HTTP {parsed.get('http_status')} != expected {expected_status} "
            "(rejection mismatch: 5xx/provider/network failures are never a pass)"
        )
    if expected_kind is not None and parsed.get("kind") != expected_kind:
        errors.append(
            f"domain error kind {parsed.get('kind')!r} != expected {expected_kind!r} "
            "(rejection mismatch)"
        )
    return errors


def _known_number(value: Any) -> float | None:
    """A non-negative finite number, else None. Unknown stays unknown."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def summarize_provenance(
    executions: Any,
    usage_items: Any,
) -> dict[str, Any]:
    """Sum actual tokens/cost over execution details + the usage ledger.

    The execution aggregate and ledger describe the same calls. Only ledger
    rows contribute to totals; execution aggregates stay separate for reconciliation.
    Unknown ledger totals remain None even when the execution has default zeros.
    """
    exec_rows = executions if isinstance(executions, list) else []
    ledger_rows = usage_items if isinstance(usage_items, list) else []

    def totals(rows: list[Any]) -> dict[str, Any]:
        valid = [r for r in rows if isinstance(r, dict)]
        measured: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "cost_usd"):
            values = [_known_number(r.get(key)) for r in valid]
            known = [value for value in values if value is not None]
            measured[key] = sum(known) if known else None
        return measured

    execution_totals = totals(exec_rows)
    ledger_totals = totals(ledger_rows)
    return {
        "execution_count": len(exec_rows),
        "ledger_count": len(ledger_rows),
        "tokens": {
            key: ledger_totals[key] for key in ("input_tokens", "output_tokens", "reasoning_tokens")
        },
        "cost_usd": ledger_totals["cost_usd"],
        "execution_totals": execution_totals,
        "ledger_totals": ledger_totals,
        "accounting_source": "usage_ledger" if ledger_rows else "unavailable",
        "accounting_scope": (
            "Recorded ledger rows only; calls rejected before usage recording may be absent"
        ),
        "models": sorted(
            {
                r["model_used"]
                for r in (*exec_rows, *ledger_rows)
                if isinstance(r, dict) and isinstance(r.get("model_used"), str)
            }
        ),
    }


def profile_policy_errors(profile_name: Any, profiles: Any, allowed_models: list[str]) -> list[str]:
    """Check the owner's published profile chain before any model task is created."""
    catalogue = {p.get("name"): p for p in profiles if isinstance(p, dict)}
    visited: set[str] = set()
    while profile_name:
        if not isinstance(profile_name, str) or profile_name in visited:
            return ["Model profile fallback is invalid or cyclic"]
        visited.add(profile_name)
        profile = catalogue.get(profile_name)
        if profile is None:
            return [f"Cannot verify model policy for profile {profile_name!r}"]
        candidates = profile.get("providers", [])
        if not candidates:
            return [f"Profile {profile_name!r} has no model candidates"]
        for candidate in candidates:
            model = candidate.get("model", "")
            if (
                candidate.get("provider") != "openrouter"
                or model not in allowed_models
                or not model.endswith(":free")
            ):
                return [f"Profile {profile_name!r} contains an unapproved/non-free candidate"]
        profile_name = profile.get("fallback_profile")
    return [] if visited else ["The owner has no verifiable model profile"]


def check_model_provenance(
    *,
    executions: Any,
    usage_items: Any,
    allowed_models: Any,
    task_completed: bool,
) -> list[str]:
    """Reject fake/scripted/non-allowlisted models and missing provenance.

    * every observed ``model_used`` (executions AND ledger) must be a member
      of the verified free allowlist and must not look fake/scripted;
    * a completed task with no executions and no ledger rows has missing
      provenance and is rejected;
    * executions/usage rows with no ``model_used`` anywhere are missing
      provenance and are rejected;
    * a non-completed task with zero executions and zero ledger rows is
      evidence of no spend, not a provenance failure (observed + recorded).
    """
    errors: list[str] = []
    allowed = list(allowed_models) if isinstance(allowed_models, (list, tuple)) else []
    exec_rows = executions if isinstance(executions, list) else []
    ledger_rows = usage_items if isinstance(usage_items, list) else []
    rows = [r for r in (*exec_rows, *ledger_rows) if isinstance(r, dict)]
    observed = [r.get("model_used") for r in rows if isinstance(r.get("model_used"), str)]
    observed = [m for m in observed if m.strip()]
    if task_completed and not exec_rows:
        errors.append("completed task has no executions: provenance missing")
    if task_completed and not ledger_rows:
        errors.append("completed task has no usage ledger: provenance missing")
    if rows and not observed:
        return ["executions/usage rows exist but model_used is missing everywhere"]
    for model in observed:
        lowered = model.lower()
        if any(marker in lowered for marker in FAKE_MODEL_MARKERS):
            errors.append(f"model {model!r} looks fake/scripted: refusing it as live evidence")
        elif not model.endswith(":free"):
            errors.append(f"model {model!r} is non-free; an allowlist override cannot permit it")
        elif model not in allowed:
            errors.append(
                f"model {model!r} is not in the verified free allowlist {sorted(allowed)}: "
                "refusing it as live evidence (no paid-model spend via this harness)"
            )
    return errors


def build_intent_record(
    *,
    run_id: str,
    case_id: str,
    workflow: str,
    kind: str,
    round_no: int,
    corpus_name: str,
    corpus_version: str,
    corpus_sha256_hex: str,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Intent record persisted BEFORE the run starts for this attempt."""
    return {
        "record": "intent",
        "run_id": run_id,
        "case_id": case_id,
        "workflow": workflow,
        "kind": kind,
        "round": round_no,
        "corpus": {
            "name": corpus_name,
            "version": corpus_version,
            "sha256": corpus_sha256_hex,
        },
        "plan": plan,
        "monotonic_start_s": time.monotonic(),
        "wall_start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def is_full_pass(
    *,
    is_subset: bool,
    expectations_met: bool,
) -> bool:
    """A run is a full pass only over the whole corpus with every case
    meeting its expectation. Subset/failed-only reruns never count."""
    return (not is_subset) and expectations_met


# ---------------------------------------------------------------------------
# Live CLI (local API + real model path; never run by unit tests)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", default="http://127.0.0.1:8100")
    parser.add_argument("--org", required=True, help="Organization ID (x-organization-id).")
    parser.add_argument("--output", type=Path, required=True, help="New run directory.")
    parser.add_argument("--rounds", type=int, default=1, choices=(1, 2, 3))
    parser.add_argument(
        "--deadline",
        type=int,
        default=240,
        help=f"Seconds per case attempt ({DEADLINE_MIN_S}..{DEADLINE_MAX_S}).",
    )
    parser.add_argument(
        "--allowed-models",
        nargs="*",
        default=list(ALLOWED_FREE_MODELS),
        help="Verified free model allowlist; every observed model_used must match.",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path("examples/source_holdout/corpus.json"),
    )
    parser.add_argument("--only", nargs="*", default=None, help="Subset of case IDs (rerun).")
    parser.add_argument("--rerun-of", default=None, help="Prior receipt this run follows up on.")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    import httpx

    if urlparse(args.base).hostname not in {"localhost", "127.0.0.1"}:
        print("refusing non-localhost --base", file=sys.stderr)
        return 2
    deadline_problem = validate_deadline(args.deadline)
    if deadline_problem is not None:
        print(deadline_problem, file=sys.stderr)
        return 2
    allowed_models = list(args.allowed_models or [])
    if not allowed_models:
        print("refusing to run with an empty --allowed-models list", file=sys.stderr)
        return 2
    if any(not isinstance(model, str) or not model.endswith(":free") for model in allowed_models):
        print("refusing non-free --allowed-models entries", file=sys.stderr)
        return 2
    if args.output.exists():
        print(f"refusing to overwrite existing --output {args.output}", file=sys.stderr)
        return 2
    provider_guard = os.environ.get("AO_MODEL_PROVIDER_DEFAULT", "")
    if provider_guard == "fake":
        print(
            "refusing to run with AO_MODEL_PROVIDER_DEFAULT=fake: a fake model "
            "fallback would invalidate live reliability evidence",
            file=sys.stderr,
        )
        return 2
    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    problems = validate_corpus(corpus)
    if problems:
        print("corpus invalid:", file=sys.stderr)
        for problem in problems:
            print(" -", problem, file=sys.stderr)
        return 2
    digest = corpus_sha256(corpus)
    cases = corpus["cases"]
    wanted = set(args.only) if args.only else None
    if wanted:
        unknown = wanted - {c["id"] for c in cases}
        if unknown:
            print(f"unknown case IDs: {sorted(unknown)}", file=sys.stderr)
            return 2
        cases = [c for c in cases if c["id"] in wanted]
    is_subset = wanted is not None

    args.output.mkdir(parents=True, exist_ok=False)
    run_id = args.output.name
    receipt: dict[str, Any] = {
        "run_id": run_id,
        "synthetic": True,
        "provider_policy": {
            "local_api_only": True,
            "model_override_sent": False,
            "fake_fallback": "refused",
            "note": "Server default model path; this script sends no model "
            "selection and refuses AO_MODEL_PROVIDER_DEFAULT=fake. Observed "
            "model_used + the usage ledger are checked against allowed_models "
            "on EVERY task; the client env var is not evidence of server policy.",
        },
        "allowed_models": sorted(allowed_models),
        "allowed_models_source": "runtime.json verified free candidates for the "
        "selected test tenant profile unless --allowed-models overrode them",
        "corpus": {
            "name": corpus["corpus_name"],
            "version": corpus["version"],
            "sha256": digest,
            "case_ids": [c["id"] for c in cases],
            "frozen_case_count": len(corpus["cases"]),
        },
        "rounds": args.rounds,
        "deadline_s": args.deadline,
        "subset": sorted(wanted) if wanted else None,
        "rerun_of": args.rerun_of,
        "failure_classification": FAILURE_CLASSIFICATION_DISCLAIMER,
        "attempts": [],
        "passed": False,
    }
    receipt_path = args.output / "run-receipt.json"

    def save() -> None:
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")

    save()
    try:
        for module in ("ai_orchestrator.domain.source_report", "docx", "openpyxl"):
            importlib.import_module(module)
    except ImportError as exc:
        receipt["setup_error"] = {
            "phase": "runtime_dependencies",
            "error": str(exc),
            "remedy": "Run with uv run python scripts/source_reliability.py from the repository",
        }
        save()
        print(json.dumps({"passed": False, "setup_error": receipt["setup_error"]}))
        return 2

    async with httpx.AsyncClient(
        base_url=args.base,
        headers={"x-organization-id": args.org},
        timeout=60,
    ) as client:

        async def call(method: str, path: str, **kwargs: Any) -> Any:
            response = await client.request(method, "/api/v1" + path, **kwargs)
            response.raise_for_status()
            return response.json()

        try:
            agents = await call("GET", "/agents?limit=200")
            owner_candidates = [
                a for a in agents.get("items", []) if a.get("name") == "Procurement Agent"
            ]
            if not owner_candidates:
                raise RuntimeError("Procurement Agent not found among active agents")
            owner = owner_candidates[0]
            profiles = await call("GET", "/model-profiles", params={"limit": 200})
            receipt["server_model_policy"] = profiles
            policy_errors = profile_policy_errors(
                owner.get("model_profile"), profiles.get("items", []), allowed_models
            )
            if policy_errors:
                raise RuntimeError("; ".join(policy_errors))
            save()
        except Exception as exc:
            receipt["setup_error"] = {
                "phase": "startup",
                "error": str(exc)[:1500],
                **parse_http_error(exc),
            }
            receipt["passed"] = False
            save()
            print(json.dumps({"passed": False, "setup_error": receipt["setup_error"]}))
            return 2

        for round_no in range(1, args.rounds + 1):
            for case in cases:
                attempt: dict[str, Any] = {
                    "run_id": run_id,
                    "case_id": case["id"],
                    "workflow": case["workflow"],
                    "kind": case["kind"],
                    "round": round_no,
                    "attempt_dir": str(attempt_dir_for(args.output, case["id"], round_no)),
                    "expectation_met": False,
                    "accepted_artifact": False,
                    "failures": [],
                }
                receipt["attempts"].append(attempt)

                def make_checkpoint(
                    attempt_ref: dict[str, Any], round_ref: int, case_ref: str
                ) -> Callable[[str], None]:
                    def _checkpoint(phase: str) -> None:
                        attempt_ref["last_checkpoint_phase"] = phase
                        save()
                        ckpt_path = checkpoint_path_for(args.output, case_ref, round_ref)
                        ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                        ckpt_path.write_text(
                            json.dumps(attempt_ref, ensure_ascii=False, indent=2) + "\n"
                        )

                    return _checkpoint

                checkpoint = make_checkpoint(attempt, round_no, case["id"])
                intent = build_intent_record(
                    run_id=run_id,
                    case_id=case["id"],
                    workflow=case["workflow"],
                    kind=case["kind"],
                    round_no=round_no,
                    corpus_name=corpus["corpus_name"],
                    corpus_version=corpus["version"],
                    corpus_sha256_hex=digest,
                    plan={
                        "deadline_s": args.deadline,
                        "subset": is_subset,
                        "allowed_models": sorted(allowed_models),
                    },
                )
                (args.output / f"intent-{case['id']}-r{round_no}.json").write_text(
                    json.dumps(intent, ensure_ascii=False, indent=2) + "\n"
                )
                # Intent is persisted BEFORE any run call below.
                start = time.monotonic()
                try:
                    await _run_case(
                        call,
                        client,
                        args,
                        owner,
                        case,
                        attempt,
                        digest,
                        start,
                        checkpoint=checkpoint,
                        allowed_models=tuple(allowed_models),
                    )
                except Exception as exc:  # recorded, never swallowed silently
                    attempt["failures"].append({"phase": "harness", "error": str(exc)[:1500]})
                    attempt["failure_class"] = classify_failure(
                        {
                            "status": attempt.get("status"),
                            "last_error": attempt.get("last_error") or str(exc)[:1500],
                            "http_status": attempt.get("http_status"),
                            "timeout_exceeded": attempt.get("timeout_exceeded", False),
                        }
                    )
                attempt["elapsed_s"] = time.monotonic() - start
                checkpoint("attempt-done")

    accepted_latencies = [
        a["latency_to_validated_artifact_ms"]
        for a in receipt["attempts"]
        if a.get("accepted_artifact") and a.get("latency_to_validated_artifact_ms") is not None
    ]
    by_class: dict[str, int] = {}
    for attempt in receipt["attempts"]:
        if not attempt.get("expectation_met"):
            cls = attempt.get("failure_class") or classify_failure(
                {
                    "status": attempt.get("status"),
                    "last_error": attempt.get("last_error"),
                    "timeout_exceeded": attempt.get("timeout_exceeded", False),
                }
            )
            attempt["failure_class"] = cls
            by_class[cls] = by_class.get(cls, 0) + 1
    receipt["measurements"] = {
        "denominator": len(receipt["attempts"]),
        "accepted_artifacts": sum(1 for a in receipt["attempts"] if a.get("accepted_artifact")),
        "expectations_met": sum(1 for a in receipt["attempts"] if a.get("expectation_met")),
        "latency_accepted_only_ms": summarize_latencies(accepted_latencies),
        "failures_by_class": by_class,
    }
    receipt["quality"] = {
        "runtime_validators": "hashes, formats, citations against fetched segments, "
        "deterministic totals, provenance allowlist, zero-spend proofs",
        "human_pending": [
            a["case_id"]
            for a in receipt["attempts"]
            if _needs_human(a, cases) and a.get("expectation_met")
        ],
        "expert_acceptance": "NOT CLAIMED: runtime validators are necessary but not "
        "sufficient. Human-pending cases (injection content) stay PENDING until a "
        "human signs; no experts are forged by this receipt, no R0 quality "
        "closure is claimed, and no SLA is implied.",
    }
    met = all(a.get("expectation_met") for a in receipt["attempts"])
    receipt["passed"] = bool(is_full_pass(is_subset=is_subset, expectations_met=met))
    if is_subset:
        receipt["pass_note"] = (
            "Subset/failed-only rerun: expectations may be met per case, but the "
            "run is never counted as a full pass. Six-case (or any partial) "
            "acceptance is never full acceptance."
        )
    save()
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "measurements": receipt["measurements"],
                "human_pending": receipt["quality"]["human_pending"],
            }
        )
    )
    return 0 if receipt["passed"] else 1


def _needs_human(attempt: dict[str, Any], cases: list[dict[str, Any]]) -> bool:
    case = next(c for c in cases if c["id"] == attempt["case_id"])
    return bool(case.get("human_review_required"))


async def _collect_provenance(call: Any, task_id: str) -> tuple[list[Any], dict[str, Any]]:
    """Fetch execution details + the model usage ledger for one task.

    Used on EVERY task path (completed, failed, negative): provenance is
    observed evidence, and the fetch itself is recorded even when empty.
    """
    executions = (await call("GET", f"/tasks/{task_id}/executions")).get("items", [])
    detailed: list[Any] = []
    for execution in executions:
        try:
            detailed.append(await call("GET", f"/executions/{execution['id']}"))
        except Exception as exc:
            detailed.append({"id": execution.get("id"), "_detail_error": parse_http_error(exc)})
    try:
        usage = await call("GET", "/model/usage", params={"task_id": task_id, "limit": 200})
    except Exception as exc:
        usage = {"items": [], "count": 0, "_ledger_error": parse_http_error(exc)}
    return detailed, usage


async def _collect_zero_spend(call: Any, task_id: str) -> dict[str, Any]:
    """Spending evidence for a task that must not have run a model."""
    executions = (await call("GET", f"/tasks/{task_id}/executions")).get("items", [])
    try:
        usage = await call("GET", "/model/usage", params={"task_id": task_id, "limit": 200})
    except Exception as exc:
        usage = {"items": [], "count": 0, "_ledger_error": parse_http_error(exc)}
    artifacts = await call("GET", f"/tasks/{task_id}/artifacts")
    return {"executions": executions, "usage": usage, "artifacts": artifacts}


async def _run_case(
    call: Any,
    client: Any,
    args: argparse.Namespace,
    owner: dict[str, Any],
    case: dict[str, Any],
    attempt: dict[str, Any],
    digest: str,
    start: float,
    *,
    checkpoint: Callable[[str], None] | None = None,
    allowed_models: tuple[str, ...] = (),
) -> None:
    from docx import Document as DocxDocument
    from openpyxl import load_workbook

    def _mark(phase: str) -> None:
        if checkpoint is not None:
            checkpoint(phase)

    kind = case["kind"]
    expected = case.get("expected", {})
    if kind == "missing_source":
        await _run_missing_source(call, case, attempt, expected, _mark)
        return
    if kind == "unsupported_source":
        await _run_unsupported_source(call, case, attempt, expected, _mark)
        return
    await _run_positive(
        call,
        client,
        args,
        owner,
        case,
        attempt,
        start,
        _mark,
        allowed_models,
        DocxDocument,
        load_workbook,
    )


async def _idem_headers(attempt: dict[str, Any]) -> dict[str, str]:
    return {
        "Idempotency-Key": f"{attempt.get('run_id')}-{attempt.get('case_id')}"
        f"-r{attempt.get('round')}"
    }


async def _run_missing_source(
    call: Any,
    case: dict[str, Any],
    attempt: dict[str, Any],
    expected: dict[str, Any],
    mark: Callable[[str], None],
) -> None:
    """The missing-source negative: only the EXACT expected HTTP status +
    domain error kind counts. Anything else (acceptance, 500, timeout,
    network error, wrong kind) is a recorded rejection mismatch."""
    selection = {"mode": "sources", "file_ids": case["selection"]["file_ids"]}
    attempt["create_selection"] = selection
    mark("intent")
    try:
        task = await call(
            "POST",
            "/source-workflows",
            json={
                "title": f"[SYNTHETIC SOURCE HOLDOUT] {case['id']}",
                "goal": case["goal"],
                "workflow": case["workflow"],
                "owner_agent_id": None,
                "selection": selection,
            },
            headers=await _idem_headers(attempt),
        )
    except Exception as exc:
        parsed = parse_http_error(exc)
        attempt["negative_evidence"] = parsed
        attempt["http_status"] = parsed.get("http_status")
        attempt["last_error"] = parsed.get("message")
        mark("negative")
        mismatch = check_expected_rejection(
            parsed, expected.get("expected_http_status"), expected.get("expected_error_kind")
        )
        if mismatch:
            attempt["failures"].extend({"phase": "negative", "error": m} for m in mismatch)
            attempt["rejection_mismatch"] = True
            attempt["failure_class"] = "unknown"
            attempt["expectation_met"] = False
            return
        # Designed behavior: the server rejected the unknown file, exactly.
        attempt["rejection"] = {
            "http_status": parsed.get("http_status"),
            "kind": parsed.get("kind"),
        }
        attempt["expectation_met"] = True
        attempt["failure_class"] = "input-blocked"
        return
    # The server ACCEPTED a reference to a file that does not exist.
    attempt["task_id"] = task.get("id")
    attempt["failures"].append(
        {"phase": "create", "error": "missing file reference was accepted (task created)"}
    )
    attempt["rejection_mismatch"] = True
    attempt["expectation_met"] = False
    mark("negative")


async def _run_unsupported_source(
    call: Any,
    case: dict[str, Any],
    attempt: dict[str, Any],
    expected: dict[str, Any],
    mark: Callable[[str], None],
) -> None:
    """The unsupported-source negative: bind the non-ready file, attempt a
    run, and prove waiting_for_input with zero executions, zero ledger rows
    and zero artifacts both before AND after the run attempt."""
    src = case["source"]
    raw = bytes.fromhex(src["bytes_hex"])
    uploaded = await call(
        "POST",
        "/source-files",
        files={"file": (src["filename"], raw)},
    )
    attempt["upload_state"] = uploaded.get("state")
    attempt["upload_id"] = uploaded.get("id")
    mark("upload")
    if uploaded.get("state") == "ready":
        attempt["failures"].append({"phase": "upload", "error": "unsupported file admitted"})
        attempt["expectation_met"] = False
        return
    task = await call(
        "POST",
        "/source-workflows",
        json={
            "title": f"[SYNTHETIC SOURCE HOLDOUT] {case['id']}",
            "goal": case["goal"],
            "workflow": case["workflow"],
            "owner_agent_id": None,
            "selection": {"mode": "sources", "file_ids": [uploaded["id"]]},
        },
        headers=await _idem_headers(attempt),
    )
    attempt["task_id"] = task.get("id")
    mark("create")
    status_task = await call("GET", f"/tasks/{task['id']}")
    attempt["status"] = status_task.get("status")
    attempt["last_error"] = status_task.get("last_error")
    manifest = await call("GET", f"/tasks/{task['id']}/sources")
    attempt["source_manifest"] = manifest
    mark("manifest")
    attempt["prespend"] = await _collect_zero_spend(call, task["id"])
    mark("prespend")
    try:
        await call("POST", f"/tasks/{task['id']}/run")
        attempt["run_attempted"] = True
    except Exception as exc:
        attempt["run_attempted"] = False
        attempt["run_refusal"] = parse_http_error(exc)
    mark("run")
    status_after = await call("GET", f"/tasks/{task['id']}")
    attempt["status_after_run"] = status_after.get("status")
    attempt["last_error"] = status_after.get("last_error")
    attempt["postspend"] = await _collect_zero_spend(call, task["id"])
    mark("postspend")

    problems: list[str] = []
    expected_status = expected.get("expected_task_status", "waiting_for_input")
    if attempt["status"] != expected_status:
        problems.append(
            f"task status {attempt['status']!r} != expected {expected_status!r} "
            "(unsupported source must park as waiting_for_input)"
        )
    if isinstance(manifest, dict) and manifest.get("manifest") is not None:
        problems.append("manifest must stay absent for an unbound (non-ready) task")
    for label in ("prespend", "postspend"):
        spend = attempt[label]
        if spend["executions"]:
            problems.append(f"{label}: {len(spend['executions'])} execution(s) exist; zero allowed")
        ledger_items = spend["usage"].get("items", [])
        if ledger_items:
            problems.append(f"{label}: {len(ledger_items)} usage ledger row(s); zero allowed")
        if spend["artifacts"].get("items"):
            problems.append(f"{label}: artifacts exist for an unexecuted task; zero allowed")
    if problems:
        attempt["failures"].extend({"phase": "negative", "error": p} for p in problems)
        attempt["rejection_mismatch"] = True
        attempt["expectation_met"] = False
        return
    # Designed behavior: parked for input, no model spend on either side.
    attempt["expectation_met"] = True
    attempt["failure_class"] = "input-blocked"


async def _run_positive(
    call: Any,
    client: Any,
    args: argparse.Namespace,
    owner: dict[str, Any],
    case: dict[str, Any],
    attempt: dict[str, Any],
    start: float,
    mark: Callable[[str], None],
    allowed_models: tuple[str, ...],
    DocxDocument: Any,
    load_workbook: Any,
) -> None:
    workflow = case["workflow"]
    expected = case.get("expected", {})

    source_ids: list[str] = []
    if case.get("selection", {}).get("mode") == "mock":
        seed = case.get("selection", {}).get("mock_seed", 1)
        files = (await call("POST", f"/source-files/mock?workflow={workflow}&seed={seed}"))["items"]
        source_ids = [f["id"] for f in files]
        attempt["mock_seed"] = seed
    else:
        src = case["source"]
        packet = io.BytesIO()
        if workflow == "quote_comparison":
            import zipfile as zf

            with zf.ZipFile(packet, "w") as archive:
                archive.writestr(src["filename"], src["csv_text"])
            name = case["id"] + ".zip"
        else:
            doc = DocxDocument()
            for paragraph in src["paragraphs"]:
                doc.add_paragraph(paragraph)
            buffer = io.BytesIO()
            doc.save(buffer)
            import zipfile as zf

            with zf.ZipFile(packet, "w") as archive:
                archive.writestr(src["entry_name"], buffer.getvalue())
            name = src["archive_name"]
        uploaded = await call("POST", "/source-files", files={"file": (name, packet.getvalue())})
        if uploaded.get("state") != "ready":
            attempt["upload_state"] = uploaded.get("state")
            attempt["failures"].append({"phase": "upload", "error": json.dumps(uploaded)[:1500]})
            mark("upload")
            return
        source_ids = [uploaded["id"]]
    attempt["source_ids"] = source_ids
    mark("upload")

    task = await call(
        "POST",
        "/source-workflows",
        json={
            "title": f"[SYNTHETIC SOURCE HOLDOUT] {case['id']}",
            "goal": case["goal"],
            "workflow": workflow,
            "owner_agent_id": owner["id"],
            "selection": {
                "mode": "mock" if case.get("selection", {}).get("mode") == "mock" else "sources",
                "file_ids": source_ids,
            },
        },
        headers=await _idem_headers(attempt),
    )
    attempt["task_id"] = task["id"]
    mark("create")
    manifest = await call("GET", f"/tasks/{task['id']}/sources")
    attempt["source_manifest"] = manifest
    selected = manifest.get("manifest") or {}
    attempt["source_manifest_sha256"] = selected.get("sha256") or manifest.get("sha256")
    attempt["source_version"] = selected.get("version", "unknown")
    mark("manifest")

    # Fetch per-source evidence: extraction segments + original bytes.
    extractions: dict[str, Any] = {}
    originals: dict[str, bytes] = {}
    for entry in selected.get("sources", []):
        sid = entry.get("id")
        detail = await call("GET", f"/source-files/{sid}")
        extractions[sid] = detail.get("extraction")
        response = await client.get("/api/v1/source-files/" + sid + "/download")
        response.raise_for_status()
        originals[sid] = response.content
    attempt["extraction_states"] = {
        sid: (extractions[sid] or {}).get("state") for sid in extractions
    }
    mark("extractions")

    import asyncio as _asyncio

    await call("POST", f"/tasks/{task['id']}/run")
    mark("run")
    deadline = start + args.deadline
    task_status: dict[str, Any] = task
    while time.monotonic() < deadline:
        task_status = await call("GET", f"/tasks/{task['id']}")
        if task_status["status"] in TERMINAL_STATUSES:
            break
        await _asyncio.sleep(3)
    else:
        attempt["timeout_exceeded"] = True
    attempt["status"] = task_status.get("status")
    attempt["last_error"] = task_status.get("last_error")
    mark("poll")

    # Provenance is observed on EVERY task, failures included.
    detailed, usage = await _collect_provenance(call, task["id"])
    attempt["executions"] = detailed
    usage_items = usage.get("items", []) if isinstance(usage, dict) else []
    attempt["usage_ledger"] = usage_items
    if isinstance(usage, dict) and usage.get("_ledger_error"):
        attempt["usage_ledger_error"] = usage["_ledger_error"]
    task_completed = task_status.get("status") == "completed"
    attempt["provenance_totals"] = summarize_provenance(detailed, usage_items)
    provenance_errors = check_model_provenance(
        executions=detailed,
        usage_items=usage_items,
        allowed_models=allowed_models,
        task_completed=task_completed,
    )
    attempt["provenance_errors"] = provenance_errors
    mark("provenance")
    if provenance_errors:
        attempt["failures"].extend({"phase": "provenance", "error": e} for e in provenance_errors)
        if not task_completed:
            attempt["failure_class"] = classify_failure(
                {
                    "status": task_status.get("status"),
                    "last_error": task_status.get("last_error"),
                    "timeout_exceeded": attempt.get("timeout_exceeded", False),
                }
            )
        return
    if not task_completed:
        attempt["failure_class"] = classify_failure(
            {
                "status": task_status.get("status"),
                "last_error": task_status.get("last_error"),
                "error_category": next(
                    (
                        e.get("error_category")
                        for e in detailed
                        if isinstance(e, dict) and e.get("error_category")
                    ),
                    None,
                ),
                "timeout_exceeded": attempt.get("timeout_exceeded", False),
            }
        )
        # Runtime errors stay failures: a non-completed normal/injection case
        # never meets its expectation here.
        return

    artifacts = (await call("GET", f"/tasks/{task['id']}/artifacts"))["items"]
    downloaded: dict[str, bytes] = {}
    for artifact in artifacts:
        response = await client.get("/api/v1/source-artifacts/" + artifact["id"] + "/download")
        response.raise_for_status()
        downloaded[artifact["format"]] = response.content
    # Retain every downloaded byte BEFORE grading: failed checks keep evidence,
    # and the per-round directory never overwrites earlier rounds.
    attempt_dir = attempt_dir_for(args.output, case["id"], attempt["round"])
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for fmt, body in downloaded.items():
        (attempt_dir / f"report.{fmt}").write_bytes(body)
    mark("download")

    bundle_errors = verify_artifact_bundle(artifacts, downloaded)
    if bundle_errors:
        attempt["failures"].extend({"phase": "artifact", "error": e} for e in bundle_errors)
        return
    office_errors = _verify_office_content(DocxDocument, load_workbook, downloaded)
    if office_errors:
        attempt["failures"].extend({"phase": "artifact", "error": e} for e in office_errors)
        return
    manifest_errors = verify_manifest_digest(selected)
    manifest_errors += verify_manifest_entries(selected, originals, extractions)
    if manifest_errors:
        attempt["failures"].extend({"phase": "manifest", "error": e} for e in manifest_errors)
        return
    result = json.loads(downloaded["json"])
    output = result.get("output", result)
    attempt["output"] = output
    report_manifest = result.get("manifest") or {}
    attempt["report_manifest_sha256"] = report_manifest.get("sha256")
    if report_manifest.get("sha256") != attempt["source_manifest_sha256"]:
        attempt["failures"].append(
            {
                "phase": "manifest",
                "error": "report JSON manifest hash != selected source manifest sha256",
            }
        )
        return
    problems = verify_citations(
        output, selected.get("sources"), extractions, expected.get("min_findings", 1)
    )
    if workflow == "quote_comparison":
        problems += check_quote_expected(output, expected["winner"], expected["total_vnd"])
    problems += verify_zip_consistency(downloaded.get("zip"), downloaded, selected)
    problems += _server_report_crosscheck(output, extractions, expected, workflow)
    if problems:
        attempt["failures"].extend({"phase": "grade", "error": p} for p in problems)
        attempt["failure_class"] = classify_failure(
            {"status": "completed", "last_error": "; ".join(problems)}
        )
        return
    attempt["latency_to_validated_artifact_ms"] = (time.monotonic() - start) * 1000.0
    attempt["accepted_artifact"] = True
    attempt["expectation_met"] = True


def _server_report_crosscheck(
    output: Any,
    extractions: dict[str, Any],
    expected: dict[str, Any],
    workflow: str,
) -> list[str]:
    """Read-only reuse of the server's own report validator, over the real
    fetched extraction segments.

    Imports ``domain.source_report.validate_report`` (pure, no I/O) and runs
    it with ``sources=[{id, segments}]`` built from the harness-fetched
    extractions plus the deterministic expected totals. Agreement means the
    harness verdict matches what the server itself would accept; disagreement
    is recorded as a grading failure, never silently dropped. A missing
    import is a harness failure. CLI dependency preflight prevents model calls
    in an interpreter that cannot load this validator.
    ``application`` imports are deliberately NOT attempted here (heavy server
    chain); manifest digest agreement is already proven byte-identically by
    ``verify_manifest_digest``.
    """
    if not isinstance(output, dict):
        return ["server crosscheck skipped: report output is not an object"]
    try:
        from ai_orchestrator.domain.source_report import validate_report
    except Exception as exc:  # pragma: no cover - environment dependent
        return [f"server crosscheck skipped: cannot import domain validator ({exc})"]
    sources: list[dict[str, Any]] = []
    for sid, extraction in extractions.items():
        segments = extraction.get("segments") if isinstance(extraction, dict) else None
        if not isinstance(segments, list):
            return [f"server crosscheck skipped: segments missing for source {sid!r}"]
        sources.append({"id": sid, "segments": segments})
    expected_totals = None
    if workflow == "quote_comparison":
        expected_totals = {
            "winner": expected.get("winner"),
            "total_vnd": expected.get("total_vnd"),
        }
    try:
        validate_report(output, sources, expected_totals)
    except Exception as exc:
        return [f"server validator disagrees with harness pass: {exc}"]
    return []


def _verify_office_content(
    DocxDocument: Any, load_workbook: Any, downloaded: dict[str, bytes]
) -> list[str]:
    errors: list[str] = []
    if len(DocxDocument(io.BytesIO(downloaded["docx"])).paragraphs) <= 3:
        errors.append("docx: too few paragraphs")
    book = load_workbook(io.BytesIO(downloaded["xlsx"]))
    if book.active.max_row <= 3:
        errors.append("xlsx: too few rows")
    book.close()
    with zipfile.ZipFile(io.BytesIO(downloaded["zip"])) as archive:
        if archive.testzip() is not None:
            errors.append("zip: corrupt member")
        names = archive.namelist()
        if not any(n.startswith("sources/") for n in names):
            errors.append("zip: missing sources/ listing")
        attempt_listing = sorted(names)
        if not attempt_listing:
            errors.append("zip: empty listing")
    return errors


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    import asyncio

    return asyncio.run(_amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
