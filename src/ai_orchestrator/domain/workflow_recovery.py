"""Multi-step workflow checkpoint recovery.

SRC-REV-01 lets a *terminal root* source task spawn a fresh revision: the old
task is never re-run and never inherits output, spend or approval. This module
answers the missing question for a *multi-stage run* that must resume or whose
source changed mid-flight: which stages may resume, which must rerun, which
approvals are invalidated, and which outputs must never be reused.

Pure decision core. No I/O, no clock, no sibling-layer imports (enforced by
tests/unit/test_domain_purity.py). It decides; `application/workflow_lifecycle.py`
and `api/sources.py` write the checkpoints, invalidate approvals and enforce
budgets — that wiring is deliberately out of scope here.

Rules, in priority order:
  1. Malformed/missing input      -> BLOCK with a reason, never a silent default.
  2. External write receipt        -> RECONCILE (never blindly replay or reuse).
  3. Stale source version or hash  -> RERUN, reuse forbidden.
  4. Completed against current     -> RESUME (no duplicate side effect).
  5. No reusable result            -> RERUN.
  6. An invalidated approval must never satisfy a later gate.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class StageStatus(StrEnum):
    """Lifecycle position of one stage inside a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCELED = "canceled"


class RecoveryDecision(StrEnum):
    """What may happen to a stage when a run resumes."""

    RESUME = "resume"
    RERUN = "rerun"
    RECONCILE = "reconcile"
    BLOCK = "block"


@dataclass(frozen=True, slots=True)
class StageRecord:
    """A stage as recorded in a checkpoint: what it ran against and what it consumed.

    `source_version`/`input_hash` are None for a stage that never ran. `approvals`
    holds the approval keys gated at or after this stage.
    """

    key: str
    status: StageStatus
    source_version: str | None = None
    input_hash: str | None = None
    #: True when the stage produced an external write receipt that must be reconciled
    has_external_write_receipt: bool = False
    approvals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StageDecision:
    stage_key: str
    action: RecoveryDecision
    reason: str


@dataclass(frozen=True, slots=True)
class RunRecoveryPlan:
    resume_point: str | None
    stages_to_rerun: tuple[str, ...]
    stages_to_reconcile: tuple[str, ...]
    invalidated_approvals: tuple[str, ...]
    blocked: bool
    reasons: tuple[str, ...]
    current_source_version: str | None = None


def _blank(value: object) -> bool:
    """True when a value is absent or not a usable, non-empty string."""
    return not isinstance(value, str) or not value.strip()


def _decide_stage(
    stage: object,
    *,
    current_source_version: str,
    evaluate_input_hash: bool,
    current_input_hash: str | None,
) -> StageDecision:
    # Rule 1: malformed or missing input is an explicit BLOCK, never a default.
    if not isinstance(stage, StageRecord):
        return StageDecision("?", RecoveryDecision.BLOCK, "stage is not a StageRecord")
    if _blank(stage.key):
        return StageDecision(str(stage.key), RecoveryDecision.BLOCK, "stage key is missing")
    if not isinstance(stage.status, StageStatus):
        return StageDecision(stage.key, RecoveryDecision.BLOCK, "stage status is unknown")
    if _blank(current_source_version):
        return StageDecision(stage.key, RecoveryDecision.BLOCK, "current source version is missing")

    # Rule 2: an external write receipt forces reconciliation, never a blind replay.
    if stage.has_external_write_receipt:
        return StageDecision(
            stage.key, RecoveryDecision.RECONCILE, "external write receipt requires reconciliation"
        )

    # Rule 5: a stage without a reusable result simply has to run.
    if stage.status is not StageStatus.COMPLETED:
        return StageDecision(
            stage.key,
            RecoveryDecision.RERUN,
            f"stage is {stage.status.value}, not a reusable result",
        )

    # Completed: its cached output is only reusable if we can prove it ran against
    # the current source version (and, at stage level, the current input hash).
    if _blank(stage.source_version):
        return StageDecision(
            stage.key, RecoveryDecision.BLOCK, "completed stage is missing its source version"
        )
    if stage.source_version != current_source_version:
        return StageDecision(
            stage.key,
            RecoveryDecision.RERUN,
            f"ran against source {stage.source_version!r}, "
            f"current is {current_source_version!r}; reuse forbidden",
        )
    if evaluate_input_hash:
        if _blank(current_input_hash):
            return StageDecision(stage.key, RecoveryDecision.BLOCK, "current input hash is missing")
        if _blank(stage.input_hash):
            return StageDecision(
                stage.key, RecoveryDecision.BLOCK, "completed stage is missing its input hash"
            )
        if stage.input_hash != current_input_hash:
            return StageDecision(
                stage.key,
                RecoveryDecision.RERUN,
                f"consumed input {stage.input_hash!r}, "
                f"current is {current_input_hash!r}; reuse forbidden",
            )
    return StageDecision(
        stage.key, RecoveryDecision.RESUME, "completed against the current source and inputs"
    )


def plan_stage_recovery(
    stage: StageRecord, *, current_source_version: str, current_input_hash: str
) -> StageDecision:
    """Decide one stage: RESUME / RERUN / RECONCILE / BLOCK, with an explicit reason.

    This entry point also enforces the consumed-input-hash rule; the run-level
    planner skips it because it has no per-stage current hash to compare against.
    """
    return _decide_stage(
        stage,
        current_source_version=current_source_version,
        evaluate_input_hash=True,
        current_input_hash=current_input_hash,
    )


def plan_run_recovery(
    stages: Iterable[StageRecord], *, current_source_version: str
) -> RunRecoveryPlan:
    """Plan recovery for an ordered run: resume point, reruns, invalidated approvals.

    Compares the source-manifest version each stage recorded against the run's
    current version. From the resume point onward outputs are regenerated or
    untrusted, so every approval gated at or after it is invalidated (rules 4 & 6).
    """
    if _blank(current_source_version):
        return RunRecoveryPlan(None, (), (), (), True, ("current source version is missing",), None)
    ordered = list(stages)
    if not ordered:
        return RunRecoveryPlan(
            None, (), (), (), True, ("no stages to recover",), current_source_version
        )

    decisions = [
        _decide_stage(
            stage,
            current_source_version=current_source_version,
            evaluate_input_hash=False,
            current_input_hash=None,
        )
        for stage in ordered
    ]
    first_stop = next(
        (i for i, d in enumerate(decisions) if d.action is not RecoveryDecision.RESUME),
        len(ordered),
    )
    invalidated = [
        approval
        for stage in ordered[first_stop:]
        if isinstance(stage, StageRecord)
        for approval in stage.approvals
    ]
    return RunRecoveryPlan(
        resume_point=decisions[first_stop].stage_key if first_stop < len(ordered) else None,
        stages_to_rerun=tuple(d.stage_key for d in decisions if d.action is RecoveryDecision.RERUN),
        stages_to_reconcile=tuple(
            d.stage_key for d in decisions if d.action is RecoveryDecision.RECONCILE
        ),
        invalidated_approvals=tuple(invalidated),
        blocked=any(d.action is RecoveryDecision.BLOCK for d in decisions),
        reasons=tuple(d.reason for d in decisions),
        current_source_version=current_source_version,
    )


def approval_remains_valid(plan: RunRecoveryPlan, approval_key: str) -> bool:
    """Rule 6: an invalidated approval must never satisfy a later gate."""
    return approval_key not in plan.invalidated_approvals


def format_recovery_plan(plan: RunRecoveryPlan, *, lang: str = "en") -> str:
    """A short operator/UI status line. `lang` is `vi` for Vietnamese, else English."""
    vi = lang == "vi"
    if plan.blocked:
        head = "Phục hồi bị chặn" if vi else "Recovery blocked"
        detail = "; ".join(plan.reasons) or ("không rõ nguyên nhân" if vi else "unknown reason")
        return f"{head}: {detail}"
    if plan.resume_point is None:
        return (
            "Không cần chạy lại; kết quả vẫn hiệu lực" if vi else "Nothing to re-run; results stand"
        )
    parts = [f"Tiếp tục từ {plan.resume_point}" if vi else f"Resume at {plan.resume_point}"]
    if plan.stages_to_rerun:
        parts.append(("chạy lại: " if vi else "rerun: ") + ", ".join(plan.stages_to_rerun))
    if plan.stages_to_reconcile:
        parts.append(("đối soát: " if vi else "reconcile: ") + ", ".join(plan.stages_to_reconcile))
    if plan.invalidated_approvals:
        parts.append(
            ("phê duyệt mất hiệu lực: " if vi else "invalid approvals: ")
            + ", ".join(plan.invalidated_approvals)
        )
    return " · ".join(parts)


__all__ = [
    "RecoveryDecision",
    "RunRecoveryPlan",
    "StageDecision",
    "StageRecord",
    "StageStatus",
    "approval_remains_valid",
    "format_recovery_plan",
    "plan_run_recovery",
    "plan_stage_recovery",
]
