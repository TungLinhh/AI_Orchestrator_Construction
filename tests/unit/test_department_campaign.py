"""Offline tests for scripts/run_department_campaign.py.

No network, no DB: fake transports, counters, and pure-helper assertions only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.run_department_campaign as c2


def _write_ledger(tmp_path: Path, used: int, max_v: int = 60) -> Path:
    path = tmp_path / "submission-ledger.json"
    entries = [
        {"index": i + 1, "owner": "parent", "model": "m", "state": "submitted"} for i in range(used)
    ]
    path.write_text(json.dumps({"used": used, "max": max_v, "entries": entries}), encoding="utf-8")
    return path


def _pass_results(n: int = 7, failed: set[int] | None = ()) -> list[dict[str, Any]]:
    failed = set(failed or ())
    return [
        {"case_id": c2.CASE_IDS[i], "passed": i not in failed, "failures": []} for i in range(n)
    ]


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- ledger / counter --------------------------------------------------------


def test_counter_limit_abort_at_60(tmp_path: Path) -> None:
    ledger = c2.Ledger(_write_ledger(tmp_path, 59), owner="c2-test")
    ledger.acquire("m")  # 60th ok
    assert ledger.used == 60
    with pytest.raises(c2.BudgetAbort):
        ledger.acquire("m")
    assert ledger.used == 60


def test_ledger_resume_validates_state(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"used": 61, "max": 60, "entries": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        c2.Ledger(path, owner="c2-test")
    path.write_text(json.dumps({"used": 3, "max": 60, "entries": {}}), encoding="utf-8")
    with pytest.raises(ValueError):
        c2.Ledger(path, owner="c2-test")


def test_per_case_cap_enforced_pre_post(tmp_path: Path) -> None:
    import httpx

    ledger = c2.Ledger(_write_ledger(tmp_path, 3), owner="c2-test")
    gate = c2.SubmissionGate(ledger)
    observer: list[dict[str, Any]] = []
    calls: list[str] = []

    async def stub_post(self: Any, url: Any, **kwargs: Any) -> _FakeResponse:
        calls.append(str(url))
        return _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}], "usage": {}})

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = stub_post  # type: ignore[method-assign]
    try:
        _, counted = c2._install_counting(ledger, observer, gate)
        url = "https://openrouter.ai/api/v1/chat/completions"
        for _ in range(8):
            _run(counted(object(), url, json={"model": "m"}))
        assert len(calls) == 8
        assert ledger.used == 3 + 8
        with pytest.raises(c2.CaseCapAbort):
            _run(counted(object(), url, json={"model": "m"}))
        assert len(calls) == 8  # no 9th POST
        assert ledger.used == 3 + 8  # no 9th reservation
    finally:
        httpx.AsyncClient.post = real_post  # type: ignore[method-assign]


def test_global_cap_checked_before_post(tmp_path: Path) -> None:
    import httpx

    ledger = c2.Ledger(_write_ledger(tmp_path, 60), owner="c2-test")
    gate = c2.SubmissionGate(ledger)
    observer: list[dict[str, Any]] = []
    calls: list[str] = []

    async def stub_post(self: Any, url: Any, **kwargs: Any) -> _FakeResponse:
        calls.append(str(url))
        return _FakeResponse(200, {})

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = stub_post  # type: ignore[method-assign]
    try:
        _, counted = c2._install_counting(ledger, observer, gate)
        with pytest.raises(c2.BudgetAbort):
            _run(
                counted(
                    object(),
                    "https://x/chat/completions",
                    json={"model": "m"},
                )
            )
        assert calls == []
    finally:
        httpx.AsyncClient.post = real_post  # type: ignore[method-assign]


# --- exit code ----------------------------------------------------------------


def test_exit_zero_only_on_7_of_7_pass() -> None:
    assert c2.campaign_exit_code(_pass_results(7)) == 0
    assert c2.campaign_exit_code(_pass_results(6)) != 0
    assert c2.campaign_exit_code(_pass_results(7, failed={3})) != 0
    assert c2.campaign_exit_code([]) != 0
    assert c2.campaign_exit_code([{"case_id": "x", "timeout": True}]) != 0


# --- HTTP200 error-body classification ----------------------------------------


def test_http200_envelope_503_retryable() -> None:
    body = {"error": {"code": 503, "message": "Service temporarily overloaded"}}
    out = c2.classify_http200_error_body(body)
    assert out is not None and out["kind"] == c2.KIND_UNAVAILABLE_RETRYABLE


def test_http200_envelope_401_terminal() -> None:
    body = {"error": {"code": 401, "message": "Invalid API key"}}
    out = c2.classify_http200_error_body(body)
    assert out is not None and out["kind"] == c2.KIND_AUTH_TERMINAL


def test_http200_envelope_budget_terminal() -> None:
    for error in (
        {"code": 402, "message": "Payment required"},
        {
            "code": 403,
            "message": "denied",
            "metadata": {"limit_source": "openrouter_key_limit"},
        },
        {
            "code": 403,
            "message": "denied",
            "metadata": {"error_type": "payment_required"},
        },
    ):
        out = c2.classify_http200_error_body({"error": error})
        assert out is not None and out["kind"] == c2.KIND_BUDGET_TERMINAL, error


def test_http200_envelope_entitlement_continues() -> None:
    body = {
        "error": {
            "code": 403,
            "message": "inkling-small:free is only available on agentic harnesses",
        }
    }
    out = c2.classify_http200_error_body(body)
    assert out is not None and out["kind"] == c2.KIND_ENTITLEMENT_CONTINUE
    assert out["action"] == "next_candidate"


def test_http200_body_with_choices_is_answer() -> None:
    body = {"choices": [{"message": {"content": "ok"}}], "error": {"code": 503}}
    assert c2.classify_http200_error_body(body) is None


# --- denial classification -----------------------------------------------------


def test_classify_denial_statuses() -> None:
    assert c2.classify_denial(429, {"error": {"message": "slow down"}})["kind"] == (
        c2.KIND_RETRYABLE
    )
    assert c2.classify_denial(401, {})["kind"] == c2.KIND_AUTH_TERMINAL
    assert c2.classify_denial(402, {})["kind"] == c2.KIND_BUDGET_TERMINAL
    assert c2.classify_denial(503, {})["kind"] == c2.KIND_UNAVAILABLE_RETRYABLE
    guardrail = {"error": {"code": 403, "metadata": {"error_type": "permission_denied"}}}
    out = c2.classify_denial(403, guardrail)
    assert out["kind"] == c2.KIND_POLICY_TERMINAL
    assert out["action"] == "fail_request_only"
    inkling = {"error": {"code": 403, "message": "only available on agentic harnesses"}}
    out = c2.classify_denial(403, inkling)
    assert out["kind"] == c2.KIND_ENTITLEMENT_CONTINUE


def test_canonical_error_type_prefers_metadata() -> None:
    error = {
        "type": "fallback",
        "error_type": "middle",
        "metadata": {"error_type": "canonical"},
    }
    assert c2.canonical_error_type(error) == "canonical"
    assert c2.canonical_error_type({"type": "only-type"}) == "only-type"
    assert c2.canonical_error_type({}) is None


def test_redaction_keeps_no_secrets() -> None:
    body = {"error": {"code": 403, "message": "m" * 200, "metadata": {"error_type": "x"}}}
    redacted = c2._redacted_denial(403, body)
    assert set(redacted) == {
        "code",
        "message_prefix",
        "error_type",
        "limit_source",
        "status",
        "kind",
        "action",
    }
    assert len(redacted["message_prefix"]) <= 120


# --- raw usage / cost -----------------------------------------------------------


def test_raw_usage_verbatim_cost_unknown_never_zero() -> None:
    body = {"usage": {"prompt_tokens": 37, "completion_tokens": 168, "total_tokens": 205}}
    raw = c2.extract_raw_usage(body)
    assert raw["cost"] == "unknown"
    assert raw["prompt_tokens"] == 37
    assert c2.extract_raw_usage({})["cost"] == "unknown"
    assert c2.extract_raw_usage({"usage": {"cost": 0}})["cost"] == 0


def test_reported_cost_nonzero() -> None:
    assert c2.reported_cost_nonzero({"cost": 0.001}) is True
    assert c2.reported_cost_nonzero({"cost": 0}) is False
    assert c2.reported_cost_nonzero({"cost": "unknown"}) is False
    assert c2.reported_cost_nonzero({}) is False


def test_nonzero_cost_stops_new_submissions(tmp_path: Path) -> None:
    import httpx

    ledger = c2.Ledger(_write_ledger(tmp_path, 3), owner="c2-test")
    gate = c2.SubmissionGate(ledger)
    observer: list[dict[str, Any]] = []

    async def stub_post(self: Any, url: Any, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            200,
            {
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.002},
            },
        )

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = stub_post  # type: ignore[method-assign]
    try:
        _, counted = c2._install_counting(ledger, observer, gate)
        _run(counted(object(), "https://x/chat/completions", json={"model": "m"}))
        assert observer[0]["raw_usage"]["cost"] == 0.002
        assert observer[0]["cost_stop"] is True
        with pytest.raises(c2.CostStop):
            gate.check_and_reserve("m")
    finally:
        httpx.AsyncClient.post = real_post  # type: ignore[method-assign]


def test_strict_usage_check_preserves_failing_counts() -> None:
    raw = {
        "prompt_tokens": 37,
        "completion_tokens": 168,
        "reasoning_tokens": 176,
        "total_tokens": 205,
        "cost": 0,
    }
    with pytest.raises(ValueError, match=r"176.*168|168.*176"):
        c2.strict_usage_check(raw)
    c2.strict_usage_check(
        {"completion_tokens": 168, "reasoning_tokens": 100}
    )  # consistent: no raise


# --- deadline bounds ---------------------------------------------------------------


def test_attempt_timeout_takes_minimum() -> None:
    assert c2.attempt_timeout_s(360.0, 3600.0) == c2.PROVIDER_TIMEOUT_CAP_S
    assert c2.attempt_timeout_s(30.0, 3600.0) == 30.0
    assert c2.attempt_timeout_s(360.0, 20.0) == 20.0
    assert c2.attempt_timeout_s(None, None) == c2.PROVIDER_TIMEOUT_CAP_S


def test_gate_refuses_post_past_deadline(tmp_path: Path) -> None:
    import time as _time

    ledger = c2.Ledger(_write_ledger(tmp_path, 3), owner="c2-test")
    gate = c2.SubmissionGate(ledger, campaign_deadline_wall=_time.monotonic() - 1.0)
    with pytest.raises(TimeoutError):
        gate.check_and_reserve("m")
    assert ledger.used == 3


# --- model order ---------------------------------------------------------------------


@dataclasses.dataclass
class _FakeCandidate:
    provider: str
    model: str
    reasoning: Any = None


@dataclasses.dataclass
class _FakeProfile:
    name: str
    candidates: tuple
    fallback_profile: Any = "something"


class _FakeGateway:
    def __init__(self, profiles: dict[str, _FakeProfile]) -> None:
        self._store = dict(profiles)
        self._providers: dict[str, Any] = {}

    @property
    def profiles(self) -> dict[str, _FakeProfile]:
        return dict(self._store)

    def register_profile(self, profile: _FakeProfile) -> None:
        self._store[profile.name] = profile


def _gateway_with(*models: str) -> _FakeGateway:
    profile = _FakeProfile(
        name="primary",
        candidates=tuple(
            _FakeCandidate(
                provider="openrouter",
                model=m,
                # Explicit value (not a product default): the campaign order
                # filter must preserve candidate fields verbatim.
                reasoning={"enabled": False} if m == c2.DOTS_MODEL else None,
            )
            for m in models
        ),
    )
    return _FakeGateway({"primary": profile})


def test_enforce_model_order_exact() -> None:
    assert c2.MODEL_ORDER == [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]  # round 4: DOTS-first
    # Input order is NVIDIA-first: the filter must still resolve DOTS-first.
    gw = _gateway_with(c2.NEMOTRON_MODEL, c2.DOTS_MODEL, c2.INKLING_MODEL)
    c2.enforce_model_order(gw)
    profile = gw.profiles["primary"]
    assert [c.model for c in profile.candidates] == [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]
    assert profile.fallback_profile is None
    dots = next(c for c in profile.candidates if c.model == c2.DOTS_MODEL)
    assert dots.reasoning == {"enabled": False}


def test_enforce_model_order_rejects_stray_free() -> None:
    gw = _gateway_with(c2.NEMOTRON_MODEL, c2.DOTS_MODEL, "other/model:free")
    with pytest.raises(ValueError, match="undeclared free models"):
        c2.enforce_model_order(gw)


def test_enforce_model_order_requires_both() -> None:
    gw = _gateway_with(c2.NEMOTRON_MODEL)
    with pytest.raises(ValueError, match="exactly MODEL_ORDER"):
        c2.enforce_model_order(gw)


# --- per-case deadlines (reviewer blocker 1) -------------------------------------


def test_case_deadline_for_full_window_per_case() -> None:
    import time as _time

    campaign_deadline = _time.monotonic() + 3600.0
    case1_start = _time.monotonic()
    first = c2.case_deadline_for(case1_start, 360.0, campaign_deadline)
    assert first - case1_start == pytest.approx(360.0)
    # Case 1 consumes 300s of wallclock; case 2 must still get its FULL window.
    case2_start = case1_start + 300.0
    second = c2.case_deadline_for(case2_start, 360.0, campaign_deadline)
    assert second - case2_start == pytest.approx(360.0)
    # Contrast with the old campaign-start bug: started_wall + timeout leaves ~60s.
    assert (case1_start + 360.0) - case2_start == pytest.approx(60.0)


def test_case_deadline_for_bounded_by_campaign_deadline() -> None:
    import time as _time

    now = _time.monotonic()
    assert c2.case_deadline_for(now, 360.0, now + 20.0) == pytest.approx(now + 20.0)
    assert c2.case_deadline_for(now, 360.0, None) == pytest.approx(now + 360.0)


def test_gate_begin_case_renews_window_after_prior_case(tmp_path: Path) -> None:
    import time as _time

    ledger = c2.Ledger(_write_ledger(tmp_path, 0), owner="c2-test")
    gate = c2.SubmissionGate(ledger, campaign_deadline_wall=_time.monotonic() + 3600.0)
    gate.begin_case(60.0)
    assert gate.attempt_timeout_s() == pytest.approx(60.0, abs=1.0)
    # Simulate 300s elapsing: the old window is dead ...
    gate.case_deadline_wall = _time.monotonic() - 300.0
    with pytest.raises(TimeoutError):
        gate.check_and_reserve("m")
    # ... but the next case starts a fresh full window.
    gate.begin_case(60.0)
    assert gate.attempt_timeout_s() == pytest.approx(60.0, abs=1.0)
    gate.check_and_reserve("m")
    assert ledger.used == 1


# --- profile scope (reviewer blocker 2) -------------------------------------------


def _realistic_tenant_gateway() -> _FakeGateway:
    def profile(name: str, *models: str) -> _FakeProfile:
        return _FakeProfile(
            name=name,
            candidates=tuple(
                _FakeCandidate(
                    provider="openrouter",
                    model=m,
                    # Same product default as above: the DOTS candidate in a
                    # tenant gateway carries reasoning={"enabled": False}.
                    reasoning={"enabled": False} if m == c2.DOTS_MODEL else None,
                )
                for m in models
            ),
        )

    return _FakeGateway(
        {
            "primary": profile("primary", c2.NEMOTRON_MODEL, c2.DOTS_MODEL),
            "proposer": profile("proposer", "google/gemma-3-27b-it:free"),
            "default": profile("default", "anthropic/claude-3.5-haiku"),
            "fast_general": profile("fast_general", "meta-llama/llama-3.3-70b-instruct"),
        }
    )


def test_enforce_model_order_constrains_only_campaign_profiles() -> None:
    gw = _realistic_tenant_gateway()
    before = {
        name: ([c.model for c in p.candidates], p.fallback_profile)
        for name, p in gw.profiles.items()
    }
    c2.enforce_model_order(gw)  # default scope: exactly the campaign profile(s)
    primary = gw.profiles["primary"]
    assert [c.model for c in primary.candidates] == [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]
    assert primary.fallback_profile is None
    dots = next(c for c in primary.candidates if c.model == c2.DOTS_MODEL)
    assert dots.reasoning == {"enabled": False}
    # Unrelated tenant profiles: untouched and still loadable.
    assert set(gw.profiles) == {"primary", "proposer", "default", "fast_general"}
    for name in ("proposer", "default", "fast_general"):
        after = gw.profiles[name]
        assert [c.model for c in after.candidates] == before[name][0]
        assert after.fallback_profile == before[name][1] == "something"


def test_enforce_model_order_explicit_scope_leaves_rest() -> None:
    gw = _realistic_tenant_gateway()
    c2.enforce_model_order(gw, only_profiles={"primary"})
    assert [c.model for c in gw.profiles["primary"].candidates] == [
        c2.DOTS_MODEL,
        c2.NEMOTRON_MODEL,
    ]
    assert [c.model for c in gw.profiles["proposer"].candidates] == ["google/gemma-3-27b-it:free"]


def test_campaign_profile_names_resolve_to_primary() -> None:
    from types import SimpleNamespace

    slugs = ["sales", "procurement", "qa", "design", "finance", "hr", "it"]
    cases = [SimpleNamespace(department=slug) for slug in slugs]
    assert c2.campaign_profile_names(cases) == {"primary"}


def test_campaign_profile_names_fallback_without_department() -> None:
    assert c2.campaign_profile_names([object()]) == {"primary"}
    assert c2.campaign_profile_names([]) == {"primary"}


# --- exit code + stop reason (reviewer blocker 3) ----------------------------------


def test_exit_code_matrix_with_stops() -> None:
    clean = _pass_results(7)
    assert c2.campaign_exit_code(clean) == 0
    assert c2.campaign_exit_code(clean, None, False) == 0
    # 7 passes BUT a resource/policy stop latched => nonzero.
    assert c2.campaign_exit_code(clean, "nonzero cost reported; new submissions stopped") != 0
    assert c2.campaign_exit_code(clean, "nonzero cost reported", True) != 0
    assert c2.campaign_exit_code(clean, None, True) != 0
    assert c2.campaign_exit_code(clean, "actual provider denial, stopping: budget_terminal") != 0
    assert c2.campaign_exit_code(clean, "wallclock budget exhausted before next case") != 0
    assert c2.campaign_exit_code(clean, "runner exception: ValueError: boom") != 0
    # Short / failed campaigns stay nonzero with or without a stop.
    assert c2.campaign_exit_code(_pass_results(6), None, False) != 0
    assert c2.campaign_exit_code(_pass_results(7, failed={3}), None, False) != 0
    assert c2.campaign_exit_code([], "wallclock budget exhausted before next case") != 0


# --- cancellation observer (F2) --------------------------------------------------------


def test_counted_post_cancelled_records_unknown_cost_and_reraises(tmp_path: Path) -> None:
    import httpx

    ledger = c2.Ledger(_write_ledger(tmp_path, 3), owner="c2-test")
    gate = c2.SubmissionGate(ledger)
    observer: list[dict[str, Any]] = []

    async def stub_post(self: Any, url: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError()

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = stub_post  # type: ignore[method-assign]
    try:
        _, counted = c2._install_counting(ledger, observer, gate)
        with pytest.raises(asyncio.CancelledError):
            _run(counted(object(), "https://x/chat/completions", json={"model": "m"}))
        # Ledger entry recorded as cancelled; cost stays unknown, never zero-filled.
        assert len(observer) == 1
        record = observer[0]
        assert record["model"] == "m"
        assert record["status"] is None
        assert record.get("state") == "cancelled"
        assert record["raw_usage"]["cost"] == "unknown"
        persisted = json.loads((tmp_path / "submission-ledger.json").read_text(encoding="utf-8"))
        states = [e.get("state") for e in persisted["entries"] if e.get("owner") == "c2-test"]
        assert "cancelled" in states
    finally:
        httpx.AsyncClient.post = real_post  # type: ignore[method-assign]


# --- --runtime option (ordinary product runtime vs evidence harness) --------------


def test_resolve_runtime_rejects_unknown_without_fallback(monkeypatch: Any) -> None:
    import scripts.evaluate_organization as eo

    import ai_orchestrator.agent_runtime as rt

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("fallback runtime constructed for unknown mode")

    monkeypatch.setattr(eo, "ReferenceRuntime", _boom)
    monkeypatch.setattr(rt, "ScriptedRuntime", _boom)
    for bad in ("bogus", "", "REFERENCE", "scripted", "mock", "live", None):
        with pytest.raises(ValueError):
            c2.resolve_runtime(bad)
        with pytest.raises(ValueError):
            c2.build_campaign_runtime(bad, gateway=object(), case=object())
    assert c2.resolve_runtime("evidence") == "evidence"
    assert c2.resolve_runtime("pydantic") == "pydantic"


def test_parser_rejects_unknown_runtime() -> None:
    with pytest.raises(SystemExit):
        c2.build_parser().parse_args(["--runtime", "bogus"])
    assert c2.build_parser().parse_args(["--runtime", "pydantic"]).runtime == "pydantic"
    assert c2.build_parser().parse_args([]).runtime == "evidence"


def test_build_campaign_runtime_constructs_no_fallback() -> None:
    from ai_orchestrator.agent_runtime import PydanticAIRuntime
    from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime

    pydantic = c2.build_campaign_runtime("pydantic")
    assert isinstance(pydantic, PydanticAIRuntime)
    assert pydantic.name == "pydantic_ai"
    evidence = c2.build_campaign_runtime("evidence", gateway=object(), case=object())
    assert isinstance(evidence, WorkflowEvidenceRuntime)
    with pytest.raises(ValueError):
        c2.build_campaign_runtime("evidence")


def test_dry_run_pydantic_prints_plan_without_touching_ledger(tmp_path: Path, capsys: Any) -> None:
    ledger = _write_ledger(tmp_path, 5)
    before = ledger.read_text(encoding="utf-8")
    assert c2.main(["--dry-run", "--runtime", "pydantic", "--ledger-path", str(ledger)]) == 0
    assert '"runtime": "pydantic"' in capsys.readouterr().out
    assert ledger.read_text(encoding="utf-8") == before


def test_dry_run_default_runtime_is_evidence(tmp_path: Path, capsys: Any) -> None:
    ledger = _write_ledger(tmp_path, 5)
    assert c2.main(["--dry-run", "--ledger-path", str(ledger)]) == 0
    assert '"runtime": "evidence"' in capsys.readouterr().out


def test_contract_and_titles_record_runtime(tmp_path: Path) -> None:
    from types import SimpleNamespace

    base = {
        "run_id": "r1",
        "org": "o",
        "dataset": "d",
        "round": 3,
        "wallclock_s": 60.0,
        "case_timeout_s": 60.0,
        "ledger_path": tmp_path / "l.json",
        "output_dir": tmp_path,
        "runtime": "pydantic",
    }
    contract = json.loads(
        c2._write_contract(tmp_path, SimpleNamespace(**base), "t0", "t1").read_text(
            encoding="utf-8"
        )
    )
    assert contract["runtime"] == "pydantic"
    evidence_args = SimpleNamespace(**{**base, "runtime": "evidence"})
    contract = json.loads(
        c2._write_contract(tmp_path, evidence_args, "t0", "t1").read_text(encoding="utf-8")
    )
    assert contract["runtime"] == "evidence"
    pydantic_title = c2.task_title_for("sales-normal-v1", "pydantic")
    evidence_title = c2.task_title_for("sales-normal-v1", "evidence")
    assert pydantic_title.startswith("[C2-PYDRANTIC]")
    assert evidence_title != pydantic_title
    with pytest.raises(ValueError):
        c2.task_title_for("sales-normal-v1", "bogus")


# --- round 4: DOTS-first order + resolved-order receipt ---------------------------


def test_resolved_candidate_order_reads_gateway_not_constant() -> None:
    # Pre-filter gateway in NVIDIA-first input order: the helper must report
    # the ACTUAL gateway state, not echo MODEL_ORDER.
    gw = _gateway_with(c2.NEMOTRON_MODEL, c2.DOTS_MODEL)
    assert c2.resolved_candidate_order(gw, {"primary"}) == {
        "primary": [c2.NEMOTRON_MODEL, c2.DOTS_MODEL]
    }
    c2.enforce_model_order(gw)
    assert c2.resolved_candidate_order(gw, {"primary"}) == {
        "primary": [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]
    }
    # Unscoped profiles excluded; missing profiles skipped, never invented.
    assert c2.resolved_candidate_order(gw, {"primary", "absent"}) == {
        "primary": [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]
    }
    assert c2.resolved_candidate_order(gw, set()) == {}


def test_dry_run_shows_dots_first_model_order(tmp_path: Path, capsys: Any) -> None:
    ledger = _write_ledger(tmp_path, 32)
    assert c2.main(["--dry-run", "--ledger-path", str(ledger)]) == 0
    out = capsys.readouterr().out
    plan = json.loads(out[out.index("{") :])
    assert plan["model_order"] == [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]


def test_contract_predeclares_dots_first_and_supersedes_round3(tmp_path: Path) -> None:
    from types import SimpleNamespace

    args = SimpleNamespace(
        run_id="r4",
        org="o",
        dataset="d",
        round=4,
        wallclock_s=60.0,
        case_timeout_s=60.0,
        ledger_path=tmp_path / "l.json",
        output_dir=tmp_path,
        runtime="pydantic",
    )
    contract = json.loads(
        c2._write_contract(tmp_path, args, "t0", "t1").read_text(encoding="utf-8")
    )
    assert contract["model_order"] == [c2.DOTS_MODEL, c2.NEMOTRON_MODEL]
    assert "supersedes round-3" in contract["model_order_note"]


# --- round 4: sanitized excerpt retention ------------------------------------------


def test_sanitized_excerpt_truncates_to_2000() -> None:
    long_text = "x" * 5000
    excerpt = c2.sanitized_model_excerpt(
        response_text=long_text,
        tool_arguments={"call": "y" * 5000},
        source_ids=["policy-1", "ledger-2"],
    )
    assert len(excerpt["response_text"]) == 2000
    assert excerpt["response_text"] == long_text[:2000]
    assert excerpt["response_truncated"] is True
    assert excerpt["response_chars"] == 5000
    assert len(excerpt["tool_arguments"]) == 2000
    assert excerpt["tool_truncated"] is True
    assert excerpt["source_ids"] == ["policy-1", "ledger-2"]
    short = c2.sanitized_model_excerpt(response_text="ok")
    assert short["response_text"] == "ok"
    assert short["response_truncated"] is False
    assert short["tool_arguments"] == ""
    assert c2.sanitized_model_excerpt()["response_text"] == ""


def test_sanitized_excerpt_takes_no_gold() -> None:
    import inspect

    params = set(inspect.signature(c2.sanitized_model_excerpt).parameters)
    assert params == {"response_text", "tool_arguments", "source_ids"}
    assert not params & {"gold", "case", "expected", "ground_truth"}
    excerpt = c2.sanitized_model_excerpt(response_text={"values": {}}, source_ids=[])
    assert "gold" not in json.dumps(excerpt)


def test_ensure_case_retention_attaches_excerpt_and_grader_failures() -> None:
    from types import SimpleNamespace

    case = SimpleNamespace(required_sources=["policy-1"])
    evidence_style: dict[str, Any] = {
        "case_id": "qa-normal-v1",
        "passed": False,
        "failures": ["values mismatch"],
        "output": {"values": {}},
    }
    c2.ensure_case_retention(evidence_style, case)
    assert evidence_style["sanitized_model_excerpt"]["source_ids"] == ["policy-1"]
    assert evidence_style["grader_failures"] == ["values mismatch"]
    # Pydantic results (built pre-grading) are left untouched.
    existing = {"response_text": "kept", "source_ids": []}
    pydantic_style: dict[str, Any] = {
        "case_id": "hr-normal-v1",
        "passed": True,
        "failures": [],
        "grader_failures": [],
        "sanitized_model_excerpt": existing,
    }
    c2.ensure_case_retention(pydantic_style, case)
    assert pydantic_style["sanitized_model_excerpt"] == existing


def test_incremental_jsonl_appends_one_line_per_case(tmp_path: Path) -> None:
    path = c2.incremental_jsonl_path(tmp_path, 4)
    assert path.name == "campaign-c2-round-4-incremental.jsonl"
    first = {
        "case_id": "design-normal-v1",
        "passed": True,
        "failures": [],
        "sanitized_model_excerpt": c2.sanitized_model_excerpt(response_text="ok"),
    }
    second = {
        "case_id": "sales-normal-v1",
        "passed": False,
        "failures": ["ambiguous HTTP400 from fallback candidate"],
        "sanitized_model_excerpt": c2.sanitized_model_excerpt(response_text="z" * 3000),
    }
    for result in (first, second):
        c2.append_incremental_record(path, c2.incremental_record_for(result))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    for record in records:
        assert {"case_id", "passed", "failures", "excerpt_sha256"} <= set(record)
    assert [r["case_id"] for r in records] == ["design-normal-v1", "sales-normal-v1"]
    assert records[0]["passed"] is True and records[0]["failures"] == []
    assert records[1]["failures"] == ["ambiguous HTTP400 from fallback candidate"]
    assert records[0]["excerpt_sha256"] == c2.excerpt_sha256(first["sanitized_model_excerpt"])
    assert records[1]["excerpt_truncated"] is True
    # Timeout results without failures still get a debuggable line.
    timeout_record = c2.incremental_record_for({"case_id": "it-normal-v1", "timeout": True})
    assert timeout_record["passed"] is False
    assert timeout_record["failures"] != []
