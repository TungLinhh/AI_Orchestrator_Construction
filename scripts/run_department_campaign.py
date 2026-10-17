"""Bounded department campaign runner (C2).

Live free-model evaluation with a hard submission ledger. Reuses the live path
from scripts/evaluate_organization.run_cases (TaskExecutionService +
WorkflowEvidenceRuntime + tenant gateway). No automatic per-model reasoning
default exists: candidates send no `reasoning` key unless an explicit
operator value is configured (a global DOTS reasoning:false default was
measured to break tool calls with HTTP 400, submissions 19/20), so normal
tool tasks use provider-default reasoning.
Campaign-only restrictions applied in code, never in persisted config:
profiles are filtered to exactly MODEL_ORDER with fallback_profile=None, and
every physical provider POST (probes and tenacity retries included) is counted
in the ledger, resuming from the parent's used count, aborting past 60.
Round 4: MODEL_ORDER is DOTS-first/NVIDIA-fallback, reflecting the actual
scoped normal profile; the contract predeclares it and the receipt records the
resolved candidate order read from the built gateway post-filter.
Stops act on ACTUAL denial responses only; metadata-only key/quota limits never
pre-block free candidates. Modes: --dry-run (no network), --probe-only (1).
Runtimes: --runtime {evidence,pydantic} (default evidence). Pydantic mode runs
the same 7 cases through the ordinary product runtime instead of the evidence
repair harness, with identical independent grading and ledger accounting.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.evaluate_organization as eo
from scripts.organization_eval import corpus_hash, load_corpus

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_LEDGER_PATH = ROOT / ".orchestra/openrouter-completion-2026-10-16/submission-ledger.json"
DEFAULT_ORG = "org_01m50cga0npmx8beszhyhrqf6b"
CAMPAIGN_ID = "openrouter-completion-2026-10-16-c2"
DOTS_MODEL = "dots-studio/dots-3-note-preview:free"
NEMOTRON_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
INKLING_MODEL = "thinkingmachines/inkling-small:free"
# Round 4: DOTS-first/NVIDIA-fallback, matching the ACTUAL scoped normal
# profile (audit-recorded, exact requested free only). Never override back to
# NVIDIA-first: the filter sorts to this order and the receipt records the
# resolved order read from the built gateway.
MODEL_ORDER = [DOTS_MODEL, NEMOTRON_MODEL]
ALLOWED_MODELS = frozenset(MODEL_ORDER)
CASE_IDS = [
    "sales-normal-v1",
    "procurement-normal-v1",
    "qa-normal-v1",
    "design-normal-v1",
    "finance-normal-v1",
    "hr-normal-v1",
    "it-normal-v1",
]
EXPECTED_CASE_COUNT = len(CASE_IDS)
GLOBAL_MAX = 60
PER_CASE_MAX_SUBMISSIONS = 8
CASE_TIMEOUT_S = 360.0
WALLCLOCK_S = 3600.0
PROVIDER_TIMEOUT_CAP_S = 120.0

RUNTIME_EVIDENCE = "evidence"
RUNTIME_PYDANTIC = "pydantic"
RUNTIME_CHOICES = (RUNTIME_EVIDENCE, RUNTIME_PYDANTIC)
TASK_TITLE_PREFIX = {
    RUNTIME_EVIDENCE: "[MOCK EVAL] ",
    RUNTIME_PYDANTIC: "[C2-PYDRANTIC] ",
}

_BUDGET_LIMIT_SOURCES = frozenset(
    {"openrouter_guardrail_budget", "openrouter_workspace_budget", "openrouter_key_limit"}
)
_BUDGET_ERROR_TYPES = frozenset({"payment_required"})
_GUARDRAIL_ERROR_TYPES = frozenset({"permission_denied", "content_policy_violation", "refusal"})

# Classifications returned by classify_denial().
KIND_RETRYABLE = "retryable"
KIND_AUTH_TERMINAL = "auth_terminal"
KIND_BUDGET_TERMINAL = "budget_terminal"
KIND_POLICY_TERMINAL = "policy_terminal"
KIND_ENTITLEMENT_CONTINUE = "entitlement_continue"
KIND_UNAVAILABLE_RETRYABLE = "unavailable_retryable"


class BudgetAbort(RuntimeError):
    """Raised when the next physical submission would exceed the global budget."""


class CaseCapAbort(RuntimeError):
    """Raised when the next POST would exceed the per-case submission cap."""


class CostStop(RuntimeError):
    """Raised when a nonzero cost was reported: no new submissions allowed."""


class Ledger:
    """Code-enforced submission counter persisted to the shared ledger file."""

    def __init__(self, path: Path, owner: str, max_submissions: int = GLOBAL_MAX) -> None:
        self._path = path
        self._owner = owner
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"ledger at {path} must be a JSON object")
        used = raw.get("used")
        entries = raw.get("entries", [])
        if not isinstance(used, int) or isinstance(used, bool):
            raise ValueError(f"ledger at {path}: 'used' must be an int")
        if not isinstance(entries, list):
            raise ValueError(f"ledger at {path}: 'entries' must be a list")
        file_max = raw.get("max", max_submissions)
        if not isinstance(file_max, int) or isinstance(file_max, bool):
            raise ValueError(f"ledger at {path}: 'max' must be an int")
        if used < 0 or used > file_max:
            raise ValueError(
                f"ledger at {path}: used={used} outside [0, {file_max}]; refusing to resume"
            )
        if file_max < 1 or max_submissions < 1:
            raise ValueError("ledger limits must be positive")
        self._max = min(file_max, max_submissions)
        if used > self._max:
            raise ValueError("ledger usage exceeds requested limit; refusing resume")
        self._metadata = {k: v for k, v in raw.items() if k not in {"used", "max", "entries"}}
        self.used: int = used
        self.initial_used: int = used
        self._entries: list[dict[str, Any]] = list(entries)
        self._next_index: int = max((e.get("index", 0) for e in self._entries), default=0) + 1

    def acquire(self, model: str) -> int:
        """Reserve one physical submission. Raises BudgetAbort instead of exceeding max."""
        if self.used + 1 > self._max:
            raise BudgetAbort(f"ledger would exceed {self.used + 1} > {self._max}; aborting")
        self.used += 1
        index = self._next_index
        self._next_index += 1
        self._entries.append(
            {"index": index, "owner": self._owner, "model": model, "state": "submitted"}
        )
        self._persist()
        return index

    def mark(self, index: int, state: str) -> None:
        for entry in self._entries:
            if entry.get("index") == index:
                entry["state"] = state
                break
        self._persist()

    def per_case_delta_ok(self, before: int) -> bool:
        return self.used - before <= PER_CASE_MAX_SUBMISSIONS

    def _persist(self) -> None:
        self._path.write_text(
            json.dumps(
                self._metadata | {"used": self.used, "max": self._max, "entries": self._entries},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "max": self._max,
            "initial_used": self.initial_used,
            "new_entries": [e for e in self._entries if e.get("owner") == self._owner],
        }


class SubmissionGate:
    """Pre-POST guard: global cap, per-case cap, cost stop, deadline bound.

    Checked synchronously BEFORE each physical POST (including retries), so a
    denied reservation never touches the network. Single-threaded sequential
    execution (concurrency is capped to 1) makes check-then-increment atomic.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        per_case_max: int = PER_CASE_MAX_SUBMISSIONS,
        case_deadline_wall: float | None = None,
        campaign_deadline_wall: float | None = None,
    ) -> None:
        self.ledger = ledger
        self.per_case_max = per_case_max
        self.case_count = 0
        self.case_deadline_wall = case_deadline_wall
        self.campaign_deadline_wall = campaign_deadline_wall
        self.cost_stop = False

    def reset_case(self, case_deadline_wall: float) -> None:
        self.case_count = 0
        self.case_deadline_wall = case_deadline_wall

    def begin_case(self, case_timeout_s: float) -> float:
        """Start a per-case deadline now, bounded by the campaign deadline.

        Each case gets its own full `case_timeout_s` window measured from the
        moment the case starts (NOT from campaign start, which starved late
        cases: case 7 of 7 x 360s under a 3600s wallclock got ~0s). The window
        is capped by the campaign deadline so a case can never outlive the
        campaign. Returns the case deadline (monotonic seconds).
        """
        case_deadline_wall = case_deadline_for(
            time.monotonic(), case_timeout_s, self.campaign_deadline_wall
        )
        self.reset_case(case_deadline_wall)
        return case_deadline_wall

    def remaining_s(self, deadline_wall: float | None) -> float | None:
        if deadline_wall is None:
            return None
        return deadline_wall - time.monotonic()

    def attempt_timeout_s(self) -> float:
        """Per-attempt bound: min(provider cap, case remaining, campaign remaining)."""
        bounds = [PROVIDER_TIMEOUT_CAP_S]
        for wall in (self.case_deadline_wall, self.campaign_deadline_wall):
            remaining = self.remaining_s(wall)
            if remaining is not None:
                bounds.append(remaining)
        return min(bounds)

    def check_and_reserve(self, model: str) -> tuple[int, float]:
        """Reserve one submission or raise BEFORE any network I/O.

        Returns (ledger index, per-attempt timeout seconds).
        """
        if self.cost_stop:
            raise CostStop("nonzero cost reported; no new submissions allowed")
        timeout_s = self.attempt_timeout_s()
        if timeout_s <= 0:
            raise TimeoutError("no time left before case/campaign deadline; skipping POST")
        if self.case_count + 1 > self.per_case_max:
            raise CaseCapAbort(
                f"per-case cap would exceed {self.case_count + 1} > {self.per_case_max}"
            )
        index = self.ledger.acquire(str(model))
        self.case_count += 1
        return index, max(timeout_s, 0.001)


def enforce_model_order(gateway: Any, only_profiles: set[str] | None = None) -> Any:
    """Filter the campaign's profile(s) to exactly MODEL_ORDER; no escape hatch.

    CAMPAIGN-ONLY restriction (not product behavior): the product gateway would
    keep every configured candidate and fallback; the campaign narrows the
    scoped profile(s) to exactly MODEL_ORDER with fallback_profile=None so the
    60-submission budget measures exactly the two-candidate rotation. No
    reasoning is set here: candidates carry only explicit operator values
    (normally none), so DOTS tool calls use provider-default reasoning.

    ONLY the profiles named in `only_profiles` are constrained (campaign callers
    pass the runtime-resolved set from campaign_profile_names(); direct callers
    default to {"primary"}). Every other tenant profile is left completely
    untouched -- not filtered, not re-registered, not required to contain the
    campaign models.

    Why scoping matters: tenants carry unrelated profiles (proposer on a Gemma
    free model, a deterministic default, fast_general, ...). Requiring both
    campaign models in EVERY profile guaranteed a ValueError before any task
    ran, on profiles the campaign never uses. The 7 campaign cases all resolve
    to profile "primary" (every department agent's model_profile is "primary"
    in domain/agent_register.py and seed.py), so constraining exactly the used
    set keeps the campaign hermetic without breaking the rest of the tenant.
    """
    scope = set(only_profiles) if only_profiles is not None else {"primary"}
    for name, profile in list(gateway.profiles.items()):
        if name not in scope:
            continue  # unrelated tenant profile: leave loadable, do not touch
        # CAMPAIGN-ONLY: exact MODEL_ORDER filter + fallback_profile=None.
        # Product behavior keeps the tenant's full candidate list and fallback.
        kept = [c for c in profile.candidates if c.model in ALLOWED_MODELS]
        stray_free = [
            c.model
            for c in profile.candidates
            if c.model.endswith(":free")
            and c.model not in ALLOWED_MODELS
            and c.model != INKLING_MODEL
        ]
        if stray_free:
            raise ValueError(
                f"profile {name!r} contains undeclared free models after filter: {stray_free}"
            )
        ordered = sorted(kept, key=lambda c: MODEL_ORDER.index(c.model))
        if [c.model for c in ordered] != MODEL_ORDER:
            raise ValueError(
                f"profile {name!r} must contain exactly MODEL_ORDER, "
                f"got {[c.model for c in ordered]}"
            )
        gateway.register_profile(
            dataclasses.replace(
                profile,
                candidates=tuple(ordered),
                fallback_profile=None,
            )
        )
    for profile in gateway.profiles.values():
        if profile.name not in scope:
            continue  # unrelated tenant profile: its fallback is not ours to set
        if getattr(profile, "fallback_profile", None) is not None:
            raise ValueError(f"profile {profile.name!r}: fallback_profile must be None")
    return gateway


def resolved_candidate_order(gateway: Any, scope: set[str] | None = None) -> dict[str, list[str]]:
    """ACTUAL post-filter candidate order, read from the built gateway.

    Never echoes MODEL_ORDER: the receipt proves the filter applied to the
    real profile by reading profile candidates after enforce_model_order ran.
    Unscoped profiles are excluded; missing profiles are skipped, never
    invented.
    """
    profiles = gateway.profiles
    names = sorted(scope) if scope is not None else sorted(profiles)
    resolved: dict[str, list[str]] = {}
    for name in names:
        profile = profiles.get(name) if hasattr(profiles, "get") else None
        if profile is None:
            continue
        resolved[name] = [str(c.model) for c in profile.candidates]
    return resolved


def campaign_profile_names(selected: Any) -> set[str]:
    """Resolve exactly the gateway profile(s) the campaign cases will use.

    Each corpus case carries a department slug; scenarios.agent_for maps the
    slug to the department agent, and domain/agent_register.REGISTER maps the
    agent to its model_profile. The union over the selected cases is the
    constraint scope for enforce_model_order(). Anything this cannot resolve
    (offline fakes, schema drift) falls back to {"primary"} -- the profile
    every department agent declares in domain/agent_register.py and seed.py.
    """
    try:
        from ai_orchestrator.application import scenarios
        from ai_orchestrator.domain import agent_register

        by_name = {spec.name: spec.model_profile for spec in agent_register.REGISTER}
        names = set()
        for case in selected:
            slug = getattr(case, "department", None)
            if slug is None:
                continue
            try:
                names.add(by_name[scenarios.agent_for(slug)])
            except KeyError:
                continue
        if names:
            return names
    except Exception:
        pass
    return {"primary"}


def _utcnow() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def resolve_runtime(value: Any) -> str:
    """Validate a campaign runtime name. Unknown values raise, never fall back.

    There is deliberately no fallback: substituting ReferenceRuntime or
    ScriptedRuntime for a misspelled mode would run a different experiment
    while the receipt claims the requested one.
    """
    mode = str(value or "").strip().lower()
    if mode in RUNTIME_CHOICES:
        return mode
    raise ValueError(
        f"unknown campaign runtime {value!r}; expected one of {sorted(RUNTIME_CHOICES)}"
    )


def build_campaign_runtime(mode: str, *, gateway: Any = None, case: Any = None) -> Any:
    """Construct the TaskExecutionService runtime for a campaign mode.

    Evidence uses the WorkflowEvidenceRuntime repair harness (gateway +
    independent source validator); pydantic uses the ordinary product runtime
    (PydanticAIRuntime, normal context/gateway/tools, no repair loop). Unknown
    modes raise via resolve_runtime BEFORE any runtime is constructed, so
    ReferenceRuntime/ScriptedRuntime are never substituted.
    """
    resolved = resolve_runtime(mode)
    if resolved == RUNTIME_PYDANTIC:
        from ai_orchestrator.agent_runtime import PydanticAIRuntime

        return PydanticAIRuntime()
    if gateway is None or case is None:
        raise ValueError("evidence runtime requires gateway and case")
    from scripts.organization_eval import validate_sources

    from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime

    return WorkflowEvidenceRuntime(
        gateway, lambda value, current=case: validate_sources(current, value)
    )


def task_title_for(case_id: str, runtime_mode: str) -> str:
    """Synthetic-task title with a per-runtime prefix so rounds stay distinct."""
    return TASK_TITLE_PREFIX[resolve_runtime(runtime_mode)] + case_id


def _as_error(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    error = body.get("error")
    if isinstance(error, dict):
        return error
    return {}


def canonical_error_type(error: dict[str, Any]) -> str | None:
    """Canonical error_type: error.metadata.error_type wins over error.type.

    Reads error.error_type and error.type as fallbacks only.
    """
    if not isinstance(error, dict):
        return None
    metadata = error.get("metadata")
    if isinstance(metadata, dict) and metadata.get("error_type"):
        return str(metadata["error_type"])
    if error.get("error_type"):
        return str(error["error_type"])
    if error.get("type"):
        return str(error["type"])
    return None


def _budget_denied(status: int | None, error: dict[str, Any]) -> bool:
    if not error:
        return False
    if status == 402:
        return True
    try:
        if int(str(error.get("code") or "").strip()) == 402:
            return True
    except TypeError, ValueError:
        pass
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if str(metadata.get("limit_source") or "").strip() in _BUDGET_LIMIT_SOURCES:
        return True
    for error_type in (
        metadata.get("error_type"),
        error.get("error_type"),
        error.get("type"),
    ):
        if str(error_type or "").strip().lower() in _BUDGET_ERROR_TYPES:
            return True
    message = str(error.get("message") or "")
    lowered = message.lower()
    if "of $" in message and "used" in lowered:
        return True
    return "budget" in lowered and any(
        word in lowered for word in ("exceed", "exhaust", "deplet", "limit")
    )


def _guardrail_denied(error: dict[str, Any]) -> bool:
    if not error:
        return False
    metadata = error.get("metadata")
    candidates: list[Any] = [error.get("error_type"), error.get("type")]
    if isinstance(metadata, dict):
        if metadata.get("patterns"):
            return True
        candidates.append(metadata.get("error_type"))
    lowered = [str(item or "").strip().lower() for item in candidates]
    if any(item in _GUARDRAIL_ERROR_TYPES for item in lowered):
        return True
    message = str(error.get("message") or "").lower()
    return "content_filter" in message or "content filter" in message


def classify_denial(status: int | None, body: Any) -> dict[str, Any]:
    """Classify a denial the way providers.py does. Returns kind + action.

    - 429 -> rate-limited, retryable
    - 401 -> terminal auth
    - 402 / budget-403 -> terminal budget, stop new submissions
    - guardrail/refusal-403 -> terminal policy for that request only
    - other 403 (e.g. Inkling entitlement) -> record and continue, next candidate
    - 5xx -> retryable unavailable
    """
    error = _as_error(body if isinstance(body, dict) else {})
    try:
        code = int(str(error.get("code") or "").strip())
    except TypeError, ValueError:
        code = None
    effective = status if status is not None else code
    message = str(error.get("message") or "").lower()
    if effective == 401 or ("invalid" in message and "key" in message) or "unauthorized" in message:
        return {"kind": KIND_AUTH_TERMINAL, "action": "stop_new_submissions"}
    if effective == 429 or "rate limit" in message or "throttle" in message:
        return {"kind": KIND_RETRYABLE, "action": "retry_within_budget"}
    if effective == 402:
        # Payment required is a budget denial by definition (providers.py):
        # terminal even with an empty body.
        return {"kind": KIND_BUDGET_TERMINAL, "action": "stop_new_submissions"}
    if _budget_denied(effective, error):
        return {"kind": KIND_BUDGET_TERMINAL, "action": "stop_new_submissions"}
    if effective == 403 and _guardrail_denied(error):
        return {"kind": KIND_POLICY_TERMINAL, "action": "fail_request_only"}
    if effective == 403:
        return {"kind": KIND_ENTITLEMENT_CONTINUE, "action": "next_candidate"}
    if effective is not None and 500 <= effective <= 599:
        return {"kind": KIND_UNAVAILABLE_RETRYABLE, "action": "retry_within_budget"}
    if effective is not None and effective >= 400:
        return {"kind": KIND_UNAVAILABLE_RETRYABLE, "action": "retry_within_budget"}
    return {"kind": KIND_UNAVAILABLE_RETRYABLE, "action": "retry_within_budget"}


def classify_http200_error_body(body: Any) -> dict[str, Any] | None:
    """Classify an HTTP-200 body carrying no choices but an error object.

    Mirrors providers._error_envelope + _error_envelope_error: a body WITH
    choices is an answer (returns None); a choiceless body with an error
    payload classifies by the same rules as the matching HTTP status (429 ->
    retryable, 401 -> terminal auth, 402/budget -> terminal budget,
    guardrail-403 -> terminal policy, other 403 -> entitlement-continue,
    503/5xx -> retryable).
    """
    if not isinstance(body, dict):
        return None
    if body.get("choices"):
        return None
    error = body.get("error")
    if error is None:
        return None
    envelope = error if isinstance(error, dict) else {"message": str(error)[:500]}
    try:
        code = int(str(envelope.get("code") or "").strip())
    except TypeError, ValueError:
        code = None
    return classify_denial(code, {"error": envelope})


def _redacted_denial(status: int | None, body: Any) -> dict[str, Any]:
    """Denial signal with secrets stripped: code/message-prefix/type/source/status."""
    error = _as_error(body if isinstance(body, dict) else {})
    metadata = error.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    classification = classify_denial(status, {"error": error} if error else body)
    return {
        "code": error.get("code"),
        "message_prefix": str(error.get("message") or "")[:120],
        "error_type": canonical_error_type(error),
        "limit_source": metadata.get("limit_source"),
        "status": status,
        "kind": classification["kind"],
        "action": classification["action"],
    }


def extract_raw_usage(body: Any) -> dict[str, Any]:
    """Raw usage verbatim as reported. Cost missing -> 'unknown', never zero."""
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost": usage.get("cost", "unknown"),
    }


def strict_usage_check(raw_usage: dict[str, Any]) -> None:
    """Mirror the strict parser's reasoning<=completion invariant.

    Runs on the preserved raw counts so a parse failure keeps provenance of
    the counts that failed (e.g. reasoning > completion).
    """
    try:
        completion = int(raw_usage.get("completion_tokens") or 0)
        reasoning = int(raw_usage.get("reasoning_tokens") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric usage counts: {raw_usage!r}") from exc
    if not 0 <= reasoning <= completion:
        raise ValueError(
            "inconsistent completion/reasoning usage: "
            f"reasoning_tokens={reasoning} completion_tokens={completion}"
        )


def reported_cost_nonzero(raw_usage: dict[str, Any]) -> bool:
    """True only when the provider reported an explicit nonzero cost."""
    cost = raw_usage.get("cost", "unknown")
    if cost == "unknown" or cost is None:
        return False
    try:
        return float(cost) != 0.0
    except TypeError, ValueError:
        return False


def case_deadline_for(
    case_start_wall: float, case_timeout_s: float, campaign_deadline_wall: float | None
) -> float:
    """Per-case deadline: full case window from THIS case's start, campaign-bounded."""
    deadline = case_start_wall + case_timeout_s
    if campaign_deadline_wall is not None:
        deadline = min(deadline, campaign_deadline_wall)
    return deadline


def attempt_timeout_s(case_remaining_s: float | None, campaign_remaining_s: float | None) -> float:
    """Per-attempt bound: min(provider cap, case remaining, campaign remaining)."""
    bounds = [PROVIDER_TIMEOUT_CAP_S]
    if case_remaining_s is not None:
        bounds.append(case_remaining_s)
    if campaign_remaining_s is not None:
        bounds.append(campaign_remaining_s)
    return min(bounds)


def campaign_exit_code(
    case_results: list[dict[str, Any]],
    stop_reason: str | None = None,
    cost_stop: bool = False,
) -> int:
    """Exit 0 iff 7/7 passed AND no resource/policy stop latched.

    A latched stop (nonzero-cost stop, terminal budget/auth/guardrail denial,
    deadline abort with pending work) means the campaign did not run clean even
    when 7 passing results are present, so it must surface as nonzero.
    """
    if len(case_results) != EXPECTED_CASE_COUNT:
        return 2
    if not all(r.get("passed") for r in case_results):
        return 2
    if cost_stop or stop_reason:
        return 2
    return 0


def _install_counting(ledger: Ledger, observer: list[dict[str, Any]], gate: SubmissionGate) -> Any:
    """Wrap httpx POSTs so every physical inference submission is ledger-counted.

    The gate's check-then-reserve runs BEFORE each physical POST (retries
    included, since they re-enter POST), so caps bind at the HTTP boundary.
    The observer persists raw usage verbatim plus the redacted, classified
    denial; a reported nonzero cost latches the gate shut for new submissions.
    """
    import httpx

    original_post = httpx.AsyncClient.post

    async def counted_post(self: httpx.AsyncClient, url: Any, **kwargs: Any) -> httpx.Response:
        body = kwargs.get("json") or {}
        is_inference = "chat/completions" in str(url)
        if not is_inference:
            return await original_post(self, url, **kwargs)
        model = body.get("model", "?") if isinstance(body, dict) else "?"
        index = gate.check_and_reserve(model)[0]
        timeout_s = gate.attempt_timeout_s()
        existing = kwargs.get("timeout")
        if isinstance(existing, (int, float)) and not isinstance(existing, bool):
            kwargs["timeout"] = max(min(float(existing), timeout_s), 0.001)
        else:
            kwargs["timeout"] = max(timeout_s, 0.001)
        try:
            response = await original_post(self, url, **kwargs)
        except asyncio.CancelledError:
            # Cancellation is BaseException: `except Exception` misses it.
            # Record a cancelled ledger entry (cost stays unknown, never
            # zero-filled) and re-raise without swallowing.
            ledger.mark(index, "cancelled")
            observer.append(
                {
                    "model": model,
                    "status": None,
                    "state": "cancelled",
                    "raw_usage": {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "reasoning_tokens": None,
                        "total_tokens": None,
                        "cost": "unknown",
                    },
                    "note": "CancelledError",
                }
            )
            raise
        except Exception as exc:
            ledger.mark(index, f"transport_error:{type(exc).__name__}")
            observer.append(
                {
                    "model": model,
                    "status": None,
                    "raw_usage": {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "reasoning_tokens": None,
                        "total_tokens": None,
                        "cost": "unknown",
                    },
                    "note": type(exc).__name__,
                }
            )
            raise
        ledger.mark(index, f"http_{response.status_code}")
        record: dict[str, Any] = {"model": model, "status": response.status_code}
        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        parsed = parsed if isinstance(parsed, dict) else {}
        # Keep a lookup reference for investigating provider accounting without
        # retaining message bodies, reasoning text, or authentication headers.
        for field in ("id", "provider"):
            if isinstance(parsed.get(field), str):
                record[field] = parsed[field][:256]
        choices = parsed.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish = choices[0].get("finish_reason")
            if isinstance(finish, str):
                record["finish_reason"] = finish[:64]
        # Capture raw usage + sanitized diagnosis BEFORE any strict parsing,
        # so a counts failure (e.g. reasoning > completion) keeps provenance.
        raw_usage = extract_raw_usage(parsed)
        record["raw_usage"] = raw_usage
        try:
            strict_usage_check(raw_usage)
        except ValueError as exc:
            record["usage_parse_note"] = str(exc)
        if (
            response.status_code in (401, 402, 403, 429)
            or (response.status_code == 200 and classify_http200_error_body(parsed))
            or response.status_code >= 500
        ):
            record["denial"] = _redacted_denial(response.status_code, parsed)
        if reported_cost_nonzero(raw_usage):
            gate.cost_stop = True
            record["cost_stop"] = True
            ledger.mark(index, "nonzero_cost_stop")
        observer.append(record)
        return response

    return original_post, counted_post


def _select_cases(dataset: Path) -> tuple[list[Any], str]:
    cases = load_corpus(dataset)
    by_id = {c.id: c for c in cases}
    missing = [cid for cid in CASE_IDS if cid not in by_id]
    if missing:
        raise ValueError(f"contract cases missing from corpus: {missing}")
    selected = [by_id[cid] for cid in CASE_IDS]
    return selected, corpus_hash(cases)


def _write_contract(
    output_dir: Path, args: argparse.Namespace, started_at: str, deadline_utc: str
) -> Path:
    contract = {
        "campaign_id": CAMPAIGN_ID,
        "run_id": args.run_id,
        "org": args.org,
        "runtime": getattr(args, "runtime", RUNTIME_EVIDENCE),
        "dataset": str(args.dataset),
        "cases": list(CASE_IDS),
        "model_order": list(MODEL_ORDER),
        "model_order_note": (
            "round 4: DOTS-first/NVIDIA-fallback, supersedes round-3 "
            "NVIDIA-first order; filters the actual scoped normal profile, "
            "never overrides back to NVIDIA-first"
        ),
        "started_at_utc": started_at,
        "deadline_utc": deadline_utc,
        "wallclock_s": min(args.wallclock_s, WALLCLOCK_S),
        "case_timeout_s": min(args.case_timeout_s, CASE_TIMEOUT_S),
        "provider_timeout_cap_s": PROVIDER_TIMEOUT_CAP_S,
        "per_case_max_submissions": PER_CASE_MAX_SUBMISSIONS,
        "global_max_submissions": GLOBAL_MAX,
        "ledger_path": str(args.ledger_path),
    }
    path = output_dir / f"campaign-c2-round-{args.round}-contract.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    return path


def _write_receipt(output_dir: Path, round_num: int, receipt: dict[str, Any]) -> Path:
    out = output_dir / f"campaign-c2-round-{round_num}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return out


EXCERPT_MAX_CHARS = 2000


def _truncate_field(value: Any, limit: int = EXCERPT_MAX_CHARS) -> tuple[str, bool, int]:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit], len(text) > limit, len(text)


def sanitized_model_excerpt(
    *,
    response_text: Any = "",
    tool_arguments: Any = "",
    source_ids: Any = (),
) -> dict[str, Any]:
    """Bounded sanitized excerpt of MODEL OUTPUT only, kept privately per case.

    Captured BEFORE parsing/grading so failures stay debuggable. Response text
    and tool-call arguments are each truncated to EXCERPT_MAX_CHARS; source
    IDs are kept verbatim. NO gold values can enter: the signature takes no
    case/gold/expected params, only model output plus source IDs. No
    keys/headers are ever included; content is synthetic-only by construction
    (task titles carry synthetic markers).
    """
    response, response_truncated, response_chars = _truncate_field(response_text)
    tools, tool_truncated, tool_chars = _truncate_field(tool_arguments)
    if isinstance(source_ids, (list, tuple)):
        ids = [str(s) for s in source_ids]
    elif source_ids is None:
        ids = []
    else:
        ids = [str(source_ids)]
    return {
        "response_text": response,
        "response_truncated": response_truncated,
        "response_chars": response_chars,
        "tool_arguments": tools,
        "tool_truncated": tool_truncated,
        "tool_chars": tool_chars,
        "source_ids": ids,
    }


def excerpt_sha256(excerpt: dict[str, Any]) -> str:
    """Content hash of a sanitized excerpt for the incremental JSONL."""
    return hashlib.sha256(
        json.dumps(excerpt, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


def incremental_jsonl_path(output_dir: Path, round_num: int) -> Path:
    return Path(output_dir) / f"campaign-c2-round-{round_num}-incremental.jsonl"


def incremental_record_for(result: dict[str, Any]) -> dict[str, Any]:
    """One debuggable JSONL record per case: id, pass/fail, failures, excerpt hash."""
    failures = list(result.get("failures") or [])
    if not failures and result.get("timeout"):
        failures = ["per-case timeout: no result produced"]
    excerpt = result.get("sanitized_model_excerpt")
    if not isinstance(excerpt, dict):
        excerpt = sanitized_model_excerpt()
    return {
        "case_id": result.get("case_id"),
        "passed": bool(result.get("passed", False)),
        "failures": failures,
        "grader_failures": list(result.get("grader_failures", failures)),
        "excerpt_sha256": excerpt_sha256(excerpt),
        "excerpt_truncated": bool(
            excerpt.get("response_truncated") or excerpt.get("tool_truncated")
        ),
    }


def append_incremental_record(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    """Append one JSONL line; mid-campaign failures stay readable if we abort."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return record


def ensure_case_retention(result: dict[str, Any], case: Any = None) -> dict[str, Any]:
    """Attach round-4 retention fields to a case result when absent.

    Pydantic results already carry them (built pre-grading inside
    _run_pydantic_case); evidence results gain a bounded excerpt of their own
    model output here, so the helper is trivially shared. Never injects gold:
    excerpt input is model output only.
    """
    if not isinstance(result.get("sanitized_model_excerpt"), dict):
        source_ids: Any = []
        if case is not None:
            source_ids = getattr(case, "required_sources", []) or []
        result["sanitized_model_excerpt"] = sanitized_model_excerpt(
            response_text=result.get("output"),
            source_ids=source_ids,
        )
    if "grader_failures" not in result:
        result["grader_failures"] = list(result.get("failures", []))
    return result


async def _run_pydantic_case(db: Any, org: str, case: Any, digest: str) -> dict[str, Any]:
    """Run one campaign case through the ORDINARY product runtime.

    PydanticAIRuntime with normal context/gateway, the agent's normal tools,
    and normal gateway validation (caller_validates_output=False path, so
    gateway completion checks apply). No evidence repair loop, no
    submit_evidence tool. Task shape (task_input, output_schema, constraints,
    budget, agent assignment via scenarios.agent_for) and independent grading
    (grade + validate_sources on the persisted task.output) match the evidence
    path, so the round measures the product runtime on identical terms. The
    model's structured answer reaches grade() via AgentResult.output (parsed
    from the run text against expected_output_schema in pydanticai_agent) and
    the TaskExecutionService completion checks, which persist it to
    task.output.
    """
    import time as _time

    from scripts.organization_eval import grade, output_schema, validate_sources
    from sqlalchemy import select

    from ai_orchestrator.application.scenarios import agent_for
    from ai_orchestrator.application.task_execution import TaskExecutionService
    from ai_orchestrator.audit.service import AuditService
    from ai_orchestrator.domain.contracts import Actor
    from ai_orchestrator.domain.enums import ActorType, RunMode
    from ai_orchestrator.persistence.models import Agent
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    started = _time.monotonic()
    async with db.committing_tenant_session(org) as session:
        agent = (
            await session.execute(
                select(Agent).where(
                    Agent.organization_id == org,
                    Agent.name == agent_for(case.department),
                )
            )
        ).scalar_one()
        # Fail fast (like the evidence path) when the campaign MODEL_ORDER
        # filter cannot apply: run_with_pydantic_ai resolves its gateway lazily
        # via application.model_profiles (patched in _run_live), and its
        # _tenant_gateway falls back to the built-in catalogue on error --
        # silently unfiltered. A preflight through the patched builder keeps
        # profile drift loud instead of silently off-rotation.
        preflight = await eo.build_tenant_gateway(org, session=session)
        await preflight.aclose()
        task_input = case.task_input()
        repo = TaskRepository(session, org)
        task = await repo.create(
            title=task_title_for(case.id, RUNTIME_PYDANTIC),
            goal=case.goal + "\nReturn only the declared artifact. Preserve human gates. "
            "Use the values field names specified in the goal. If a required source is "
            "missing, values must be {} and findings must cite the supplied policy. "
            "missing_inputs lists only absent IDs from required_sources; use [] when "
            "all those sources are present. Pending human approval is not a missing source. "
            "findings cite business evidence or policy, not malicious instructions.",
            task_type="analysis",
            requester_type="system",
            input=task_input,
            expected_output_schema=output_schema(case),
            constraints={
                "synthetic": True,
                "evaluation_corpus": digest,
                "no_external_actions": True,
                "campaign_runtime": RUNTIME_PYDANTIC,
            },
            budget_limit_usd=0.01,
            budget_limit_tokens=24000,
            allow_parallel=True,
        )
        await repo.assign(task.id, agent.id)
        await session.commit()
        await db.bind_tenant(session, org)
        engine = build_campaign_runtime(RUNTIME_PYDANTIC)
        outcome = await TaskExecutionService(
            session,
            org,
            runtime=engine,
            run_mode=RunMode.LIVE,
            auto_approve=False,
        ).execute_task(task.id)
        await session.refresh(task)
        # Round-4 retention BEFORE parsing/grading: bounded sanitized excerpt
        # of MODEL OUTPUT only (summary + model-proposed tool-call arguments;
        # source IDs kept, no gold values enter). Synthetic-only by
        # construction; no keys/headers.
        summary_text = ""
        proposed_tool_calls: list[dict[str, Any]] = []
        if outcome.result is not None:
            summary_text = outcome.result.summary or ""
            proposed_tool_calls = [
                {"tool": p.tool_name or p.tool_id, "arguments": p.arguments}
                for p in (outcome.proposed_actions or ())
                if getattr(p, "kind", None) == "tool_call"
            ]
        excerpt = sanitized_model_excerpt(
            response_text=summary_text or json.dumps(task.output, ensure_ascii=False, default=str),
            tool_arguments=json.dumps(proposed_tool_calls, ensure_ascii=False, default=str),
            source_ids=task_input.get("required_sources", []),
        )
        failures = list(grade(case, task.output))
        grader_failures = list(failures)
        try:
            validate_sources(case, task.output)
        except Exception as exc:
            failures.append(f"Independent source validation: {exc}")
        if task.status != "completed":
            failures.append("Execution did not complete: " + task.status)
        result = {
            "case_id": case.id,
            "campaign_runtime": RUNTIME_PYDANTIC,
            "output_schema_sha256": hashlib.sha256(
                json.dumps(output_schema(case), sort_keys=True).encode()
            ).hexdigest(),
            "department": case.department,
            "split": case.split,
            "variant": case.variant,
            "task_id": task.id,
            "execution_status": outcome.status.value,
            "last_error": task.last_error,
            "failure_category": task.failure_category,
            "passed": not failures,
            "failures": failures,
            "grader_failures": grader_failures,
            "sanitized_model_excerpt": excerpt,
            "duration_s": round(_time.monotonic() - started, 3),
            "output": task.output,
            "candidate_lesson_count": len(task_input.get("sandbox_candidate_lessons", [])),
            "agent_id": agent.id,
        }
        await AuditService(session, org).record(
            actor=Actor(id="organization-evaluator", kind=ActorType.SYSTEM),
            action="evaluation.graded",
            resource_type="task",
            resource_id=task.id,
            task_id=task.id,
            context={
                "synthetic": True,
                "corpus_sha256": digest,
                "runtime": RUNTIME_PYDANTIC,
                "passed": not failures,
                "failures": failures,
                "split": case.split,
            },
        )
        await session.commit()
        print(case.id, "PASS" if not failures else "FAIL", task.id, flush=True)
        return result


async def _run_live(args: argparse.Namespace) -> int:
    from ai_orchestrator.persistence.session import Database

    args.runtime = resolve_runtime(getattr(args, "runtime", RUNTIME_EVIDENCE))
    case_timeout = min(args.case_timeout_s, CASE_TIMEOUT_S)
    wallclock = min(args.wallclock_s, WALLCLOCK_S)
    started_wall = time.monotonic()
    started_at = _utcnow()
    deadline_wall = started_wall + wallclock
    deadline_utc = (
        (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=wallclock))
        .isoformat()
        .replace("+00:00", "Z")
    )

    # Predeclare the contract BEFORE any network I/O.
    _write_contract(args.output_dir, args, started_at, deadline_utc)

    ledger = Ledger(args.ledger_path, owner=f"c2-{args.run_id}")
    gate = SubmissionGate(ledger, campaign_deadline_wall=deadline_wall)
    observer: list[dict[str, Any]] = []
    selected, digest = _select_cases(args.dataset)
    receipt: dict[str, Any] = {
        "receipt_version": 1,
        "campaign_id": CAMPAIGN_ID,
        "round": args.round,
        "run_id": args.run_id,
        "runtime": args.runtime,
        "synthetic": True,
        "started_at_utc": started_at,
        "deadline_utc": deadline_utc,
        "wallclock_s": wallclock,
        "model_order": list(MODEL_ORDER),
        "resolved_model_order": None,
        "reasoning_source": (
            "none automatic: candidates send no reasoning key unless explicitly "
            "configured; DOTS tool calls use provider-default reasoning "
            "(a global reasoning:false default broke tools, submissions 19/20)"
        ),
        "inkling": "excluded from auto-rotation; single re-probe only if budget allows",
        "per_case_max_submissions": PER_CASE_MAX_SUBMISSIONS,
        "corpus_sha256": digest,
        "partial": True,
    }

    real_builder = eo.build_tenant_gateway
    import ai_orchestrator.application.model_profiles as model_profiles

    real_mp_builder = model_profiles.build_tenant_gateway
    campaign_scope = campaign_profile_names(selected)

    async def builder_with_campaign_scope(organization_id: str, *, session: Any = None) -> Any:
        gateway = await real_builder(organization_id, session=session)
        # CAMPAIGN-ONLY restrictions follow (order filter, fallback=None,
        # timeout cap); product behavior outside the campaign keeps the full
        # tenant gateway untouched.
        enforce_model_order(gateway, only_profiles=campaign_scope)
        # Round 4: receipt records the ACTUAL resolved order read post-filter
        # from the built gateway, never echoed from MODEL_ORDER.
        receipt["resolved_model_order"] = resolved_candidate_order(gateway, campaign_scope)
        for provider in gateway._providers.values():
            if provider._timeout_s > PROVIDER_TIMEOUT_CAP_S:
                provider._timeout_s = PROVIDER_TIMEOUT_CAP_S
        return gateway

    eo.build_tenant_gateway = builder_with_campaign_scope  # type: ignore[method-assign]
    # PydanticAIRuntime resolves its gateway lazily inside pydanticai_agent
    # (_tenant_gateway imports application.model_profiles at call time), so
    # the same campaign scope must be patched at the source module too;
    # patching only eo.build_tenant_gateway would leave pydantic rounds
    # unfiltered.
    model_profiles.build_tenant_gateway = builder_with_campaign_scope  # type: ignore[method-assign]
    import httpx

    original_post, counted_post = _install_counting(ledger, observer, gate)
    httpx.AsyncClient.post = counted_post  # type: ignore[method-assign]
    try:
        db = Database.from_settings()
        try:
            case_results: list[dict[str, Any]] = []
            stop_reason: str | None = None
            incremental_path = incremental_jsonl_path(args.output_dir, args.round)
            try:
                for case in selected:
                    if time.monotonic() > deadline_wall:
                        stop_reason = "wallclock budget exhausted before next case"
                        break
                    if ledger.used + PER_CASE_MAX_SUBMISSIONS > GLOBAL_MAX:
                        stop_reason = "insufficient global budget left for another case"
                        break
                    if gate.cost_stop:
                        stop_reason = "nonzero cost reported; new submissions stopped"
                        break
                    before = ledger.used
                    # Per-case deadline: full case window from THIS case's start,
                    # bounded by the campaign deadline (never from campaign start).
                    case_start = time.monotonic()
                    case_deadline = case_deadline_for(case_start, case_timeout, deadline_wall)
                    gate.reset_case(case_deadline)
                    try:
                        if args.runtime == RUNTIME_PYDANTIC:
                            single = await asyncio.wait_for(
                                _run_pydantic_case(db, args.org, case, digest),
                                timeout=max(case_deadline - case_start, 0.001),
                            )
                            ensure_case_retention(single, case)
                            append_incremental_record(
                                incremental_path, incremental_record_for(single)
                            )
                            case_results.append(single)
                        else:
                            report = await asyncio.wait_for(
                                eo.run_cases(db, args.org, [case], "live", dataset_digest=digest),
                                timeout=max(case_deadline - case_start, 0.001),
                            )
                            for single in report["results"]:
                                ensure_case_retention(single, case)
                                append_incremental_record(
                                    incremental_path, incremental_record_for(single)
                                )
                            case_results.extend(report["results"])
                    except (BudgetAbort, CaseCapAbort, CostStop) as exc:
                        stop_reason = f"submission budget reached: {exc}"
                        break
                    except TimeoutError:
                        stop_reason = f"per-case timeout ({case_timeout}s) on {case.id}"
                        timed_out: dict[str, Any] = {"case_id": case.id, "timeout": True}
                        ensure_case_retention(timed_out, case)
                        append_incremental_record(
                            incremental_path, incremental_record_for(timed_out)
                        )
                        case_results.append(timed_out)
                        break
                    if not ledger.per_case_delta_ok(before):
                        stop_reason = (
                            f"per-case submission cap exceeded on {case.id} "
                            f"({ledger.used - before} > {PER_CASE_MAX_SUBMISSIONS})"
                        )
                        break
                    new_obs = [
                        o
                        for o in observer
                        if o.get("denial") and o["denial"].get("action") == "stop_new_submissions"
                    ]
                    cost_hit = next((o for o in observer if o.get("cost_stop")), None)
                    if new_obs:
                        detail = new_obs[-1]["denial"]
                        stop_reason = f"actual provider denial, stopping: {detail}"
                        break
                    if cost_hit is not None:
                        stop_reason = "nonzero cost reported by provider; new submissions stopped"
                        break
                    # Policy/entitlement denials are per-request: recorded in
                    # the observer, campaign continues with the next case.
            except Exception as exc:
                stop_reason = f"runner exception: {type(exc).__name__}: {exc}"
                receipt["runner_error"] = str(exc)[:500]
            receipt.update(
                {
                    "partial": False,
                    "ledger": ledger.snapshot(),
                    "provider_denials_observed": [o for o in observer if o.get("denial")],
                    "observer": observer,
                    "stop_reason": stop_reason,
                    "cases": case_results,
                    "exit_code": campaign_exit_code(case_results, stop_reason, gate.cost_stop),
                    "cost_note": (
                        "cost unknown-never-zero: failed calls without provider "
                        "usage stay 'unknown'"
                    ),
                }
            )
            out = _write_receipt(args.output_dir, args.round, receipt)
            print(
                f"round {args.round}: {len(case_results)} cases; "
                f"ledger {ledger.snapshot()['used']}/{GLOBAL_MAX}; {out}"
            )
            return campaign_exit_code(case_results, stop_reason, gate.cost_stop)
        finally:
            await db.dispose()
    finally:
        # Partial receipt even on exception: best-effort save if not yet written.
        try:
            if receipt.get("partial"):
                receipt.update(
                    {
                        "ledger": ledger.snapshot(),
                        "observer": observer,
                        "cases": [],
                        "exit_code": 2,
                    }
                )
                receipt.setdefault("stop_reason", None)
                _write_receipt(args.output_dir, args.round, receipt)
        except Exception:
            pass
        httpx.AsyncClient.post = original_post  # type: ignore[method-assign]
        eo.build_tenant_gateway = real_builder  # type: ignore[method-assign]
        model_profiles.build_tenant_gateway = real_mp_builder  # type: ignore[method-assign]


async def _run_probe_only(args: argparse.Namespace) -> int:
    """One small DOTS reasoning-disabled probe. Counts exactly 1 submission."""
    from ai_orchestrator.config.settings import get_settings
    from ai_orchestrator.models.gateway import ModelCandidate, ModelRequest
    from ai_orchestrator.models.providers import OpenAICompatibleProvider

    settings = get_settings()
    ledger = Ledger(args.ledger_path, owner=f"c2-probe-{args.run_id}")
    gate = SubmissionGate(ledger)
    observer: list[dict[str, Any]] = []
    import httpx

    original_post, counted_post = _install_counting(ledger, observer, gate)
    httpx.AsyncClient.post = counted_post  # type: ignore[method-assign]
    try:
        candidate = ModelCandidate(
            provider="openrouter", model=DOTS_MODEL, reasoning={"enabled": False}
        )
        request = ModelRequest(
            profile="probe", prompt="Reply with exactly: probe ok.", max_output_tokens=256
        )
        payload = request.to_provider_payload(candidate)
        assert payload["reasoning"] == {"enabled": False}, payload
        provider = OpenAICompatibleProvider(
            "openrouter",
            api_key=settings.openrouter_api_key.get_secret_value() or None,
            base_url="https://openrouter.ai/api/v1",
            timeout_s=min(settings.model_request_timeout_s, PROVIDER_TIMEOUT_CAP_S),
            max_retries=0,
            extra_headers={"HTTP-Referer": "https://localhost/c2-probe", "X-Title": "c2-probe"},
        )
        try:
            response = await provider.complete(candidate, request)
        finally:
            await provider.aclose()
        print(
            json.dumps(
                {
                    "model": DOTS_MODEL,
                    "reasoning": payload["reasoning"],
                    "text": response.text[:500],
                    "raw_usage": response.raw_usage,
                    "cost_usd": str(response.cost_usd),
                    "ledger": ledger.snapshot(),
                },
                indent=2,
                default=str,
            )
        )
        return 0
    finally:
        httpx.AsyncClient.post = original_post  # type: ignore[method-assign]


def _validate_ledger_state(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"ledger at {path} must be a JSON object")
    used, max_v, entries = raw.get("used"), raw.get("max"), raw.get("entries", [])
    if not isinstance(used, int) or isinstance(used, bool):
        raise ValueError("ledger 'used' must be an int")
    if not isinstance(max_v, int) or isinstance(max_v, bool):
        raise ValueError("ledger 'max' must be an int")
    if not isinstance(entries, list):
        raise ValueError("ledger 'entries' must be a list")
    if not 0 <= used <= max_v:
        raise ValueError(f"ledger used={used} outside [0, {max_v}]")
    return raw


def _run_dry_run(args: argparse.Namespace) -> int:
    runtime_mode = resolve_runtime(getattr(args, "runtime", RUNTIME_EVIDENCE))
    selected, digest = _select_cases(args.dataset)
    ledger_raw = _validate_ledger_state(args.ledger_path)
    from ai_orchestrator.application.model_profiles import _candidate
    from ai_orchestrator.models.gateway import ModelRequest

    # Normal product path, offline: legacy tenant-style entries (NO reasoning
    # keys, mirroring persisted rows) flow through the config boundary with no
    # automatic default, so tool tasks use provider-default reasoning.
    # Listed DOTS-first to mirror MODEL_ORDER and the actual normal profile.
    warnings: list[str] = []
    loaded = [
        _candidate(e, where="dry-run primary", warn=warnings.append)
        for e in (
            {"model": DOTS_MODEL, "provider": "openrouter"},
            {"model": NEMOTRON_MODEL, "provider": "openrouter"},
        )
    ]
    assert all(c is not None for c in loaded)
    dots = next(c for c in loaded if c.model == DOTS_MODEL)  # type: ignore[union-attr]
    nemotron = next(c for c in loaded if c.model == NEMOTRON_MODEL)  # type: ignore[union-attr]
    assert dots is not None and nemotron is not None
    dots_payload = ModelRequest(profile="primary", prompt="dry-run").to_provider_payload(dots)
    nemotron_payload = ModelRequest(profile="primary", prompt="dry-run").to_provider_payload(
        nemotron
    )
    verification = (
        "verified: no automatic reasoning default "
        "(no reasoning key on either payload; explicit operator values only)"
    )
    print(verification)
    plan = {
        "campaign_id": CAMPAIGN_ID,
        "run_id": args.run_id,
        "org": args.org,
        "runtime": runtime_mode,
        "mode": "dry-run (no network, ledger untouched)",
        "corpus_sha256": digest,
        "cases": [c.id for c in selected],
        "model_order": MODEL_ORDER,
        "dots_reasoning": dots_payload.get("reasoning"),
        "reasoning_verification": verification,
        "nemotron_has_reasoning_key": "reasoning" in nemotron_payload,
        "ledger_resume_used": ledger_raw["used"],
        "ledger_max": GLOBAL_MAX,
        "per_case_max_submissions": PER_CASE_MAX_SUBMISSIONS,
        "case_timeout_s": min(args.case_timeout_s, CASE_TIMEOUT_S),
        "wallclock_s": min(args.wallclock_s, WALLCLOCK_S),
    }
    print(json.dumps(plan, indent=2))
    assert "reasoning" not in dots_payload
    assert "reasoning" not in nemotron_payload
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--run-id", default="manual")
    parser.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER_PATH)
    parser.add_argument(
        "--dataset", type=Path, default=ROOT / "examples/organization_eval/cases.json"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument(
        "--runtime",
        choices=sorted(RUNTIME_CHOICES),
        default=RUNTIME_EVIDENCE,
        help="campaign runtime: evidence repair harness or ordinary product runtime",
    )
    parser.add_argument("--inkling-reprobe", action="store_true")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / ".orchestra/openrouter-completion-2026-10-16",
    )
    parser.add_argument("--case-timeout-s", type=float, default=CASE_TIMEOUT_S)
    parser.add_argument("--wallclock-s", type=float, default=WALLCLOCK_S)
    parser.add_argument("--concurrency", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.runtime = resolve_runtime(getattr(args, "runtime", RUNTIME_EVIDENCE))
    if args.case_timeout_s > CASE_TIMEOUT_S:
        print(
            f"case-timeout {args.case_timeout_s}s exceeds {CASE_TIMEOUT_S}s; clamping",
            file=sys.stderr,
        )
    if args.wallclock_s > WALLCLOCK_S:
        print(
            f"wallclock {args.wallclock_s}s exceeds {WALLCLOCK_S}s; clamping",
            file=sys.stderr,
        )
    if args.concurrency != 1:
        # Sequential execution is the instrument: concurrent cases would share
        # the ledger and the provider key in ways the receipt cannot attribute.
        print(f"concurrency {args.concurrency} requested; capping to sequential (1)")
        args.concurrency = 1
    if args.dry_run:
        return _run_dry_run(args)
    if args.probe_only:
        return asyncio.run(_run_probe_only(args))
    return asyncio.run(_run_live(args))


if __name__ == "__main__":
    raise SystemExit(main())
