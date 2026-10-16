"""Behavioural tests for the pure checkpoint-recovery decision core.

Each test calls the module the way a caller would and asserts the observable
result (decision, reason, resume point, invalidated approvals, status line) —
not any internal helper. One negative case per rule, plus malformed input.
"""

from __future__ import annotations

from ai_orchestrator.domain.workflow_recovery import (
    RecoveryDecision,
    StageRecord,
    StageStatus,
    approval_remains_valid,
    format_recovery_plan,
    plan_run_recovery,
    plan_stage_recovery,
)


def stage(
    key,
    status=StageStatus.COMPLETED,
    *,
    source_version="v3",
    input_hash="h1",
    receipt=False,
    approvals=(),
):
    return StageRecord(
        key=key,
        status=status,
        source_version=source_version,
        input_hash=input_hash,
        has_external_write_receipt=receipt,
        approvals=tuple(approvals),
    )


# --- rule: completed against current source and inputs -> RESUME -------------


def test_completed_stage_with_matching_version_and_input_resumes():
    decision = plan_stage_recovery(
        stage("extract"), current_source_version="v3", current_input_hash="h1"
    )
    assert decision.action is RecoveryDecision.RESUME
    assert decision.stage_key == "extract"


def test_whole_run_resumes_when_source_is_unchanged():
    plan = plan_run_recovery(
        [stage("a", source_version="v3"), stage("b", source_version="v3")],
        current_source_version="v3",
    )
    assert plan.resume_point is None
    assert plan.stages_to_rerun == ()
    assert not plan.blocked
    assert plan.invalidated_approvals == ()


# --- rule: stale source version -> RERUN, reuse forbidden --------------------


def test_stale_source_version_forces_rerun_and_forbids_reuse():
    decision = plan_stage_recovery(
        stage("extract", source_version="v2"), current_source_version="v3", current_input_hash="h1"
    )
    assert decision.action is RecoveryDecision.RERUN
    assert "reuse forbidden" in decision.reason


# --- rule: consumed-input hash differs -> RERUN ------------------------------


def test_stale_input_hash_forces_rerun():
    decision = plan_stage_recovery(
        stage("extract"), current_source_version="v3", current_input_hash="h2"
    )
    assert decision.action is RecoveryDecision.RERUN
    assert "reuse forbidden" in decision.reason


# --- rule: external write receipt -> RECONCILE (never blind replay/reuse) ----


def test_external_write_receipt_reconciles_even_against_current_source():
    decision = plan_stage_recovery(
        stage("send_quote", receipt=True),
        current_source_version="v3",
        current_input_hash="h1",
    )
    assert decision.action is RecoveryDecision.RECONCILE


def test_interrupted_external_write_reconciles_not_reruns():
    decision = plan_stage_recovery(
        stage(
            "send_quote",
            status=StageStatus.RUNNING,
            source_version=None,
            input_hash=None,
            receipt=True,
        ),
        current_source_version="v3",
        current_input_hash="h1",
    )
    assert decision.action is RecoveryDecision.RECONCILE


# --- rule: not completed -> RERUN (has to run); unstarted is not "missing" ---


def test_non_completed_stage_reruns():
    for status in (StageStatus.PENDING, StageStatus.RUNNING, StageStatus.FAILED):
        decision = plan_stage_recovery(
            stage("step", status=status, source_version=None, input_hash=None),
            current_source_version="v3",
            current_input_hash="h1",
        )
        assert decision.action is RecoveryDecision.RERUN, status


# --- run-level: mid-run source change, resume point + invalidated approvals ---


def test_run_resumes_at_first_stale_stage_and_invalidates_downstream_approvals():
    plan = plan_run_recovery(
        [
            stage("intake", source_version="v3", approvals=["ap_intake"]),
            stage("draft", source_version="v2", approvals=["ap_draft"]),
            stage(
                "render", status=StageStatus.PENDING, source_version=None, approvals=["ap_render"]
            ),
        ],
        current_source_version="v3",
    )
    assert plan.resume_point == "draft"
    assert plan.stages_to_rerun == ("draft", "render")
    assert not plan.blocked
    # approvals at/after the resume point are invalidated; the resumable one survives.
    assert plan.invalidated_approvals == ("ap_draft", "ap_render")
    assert approval_remains_valid(plan, "ap_intake") is True
    assert approval_remains_valid(plan, "ap_draft") is False


def test_invalidated_approval_never_satisfies_a_later_gate():
    plan = plan_run_recovery(
        [stage("draft", source_version="v2", approvals=["gate_approval"])],
        current_source_version="v3",
    )
    assert approval_remains_valid(plan, "gate_approval") is False


def test_external_write_in_run_reconciles_and_invalidates_downstream_gates():
    plan = plan_run_recovery(
        [
            stage("post", source_version="v3", receipt=True, approvals=["ap_post"]),
            stage("log", source_version="v3", approvals=["ap_log"]),
        ],
        current_source_version="v3",
    )
    assert plan.stages_to_reconcile == ("post",)
    assert plan.stages_to_rerun == ()
    # even the downstream RESUMEd stage's gate is invalid until reconciliation
    assert set(plan.invalidated_approvals) == {"ap_post", "ap_log"}


# --- rule: missing / unknown data -> explicit BLOCK, never a silent default --


def test_non_stagerecord_input_blocks():
    assert (
        plan_stage_recovery(
            {"key": "x"}, current_source_version="v3", current_input_hash="h1"
        ).action
        is RecoveryDecision.BLOCK
    )


def test_blank_stage_key_blocks():
    decision = plan_stage_recovery(stage(""), current_source_version="v3", current_input_hash="h1")
    assert decision.action is RecoveryDecision.BLOCK


def test_unknown_status_type_blocks():
    decision = plan_stage_recovery(
        stage("x", status="completed"), current_source_version="v3", current_input_hash="h1"
    )
    assert decision.action is RecoveryDecision.BLOCK


def test_completed_stage_missing_source_version_blocks():
    decision = plan_stage_recovery(
        stage("x", source_version=None), current_source_version="v3", current_input_hash="h1"
    )
    assert decision.action is RecoveryDecision.BLOCK
    assert "source version" in decision.reason


def test_completed_stage_missing_input_hash_blocks():
    decision = plan_stage_recovery(
        stage("x", input_hash=None), current_source_version="v3", current_input_hash="h1"
    )
    assert decision.action is RecoveryDecision.BLOCK
    assert "input hash" in decision.reason


def test_missing_current_input_hash_blocks():
    decision = plan_stage_recovery(stage("x"), current_source_version="v3", current_input_hash=None)
    assert decision.action is RecoveryDecision.BLOCK


def test_missing_current_source_version_blocks_the_stage():
    decision = plan_stage_recovery(stage("x"), current_source_version=None, current_input_hash="h1")
    assert decision.action is RecoveryDecision.BLOCK


def test_run_with_missing_current_source_version_is_blocked():
    plan = plan_run_recovery([stage("a")], current_source_version=None)
    assert plan.blocked is True
    assert plan.reasons == ("current source version is missing",)


def test_empty_run_is_blocked_not_silently_clean():
    plan = plan_run_recovery([], current_source_version="v3")
    assert plan.blocked is True


def test_malformed_stage_in_run_blocks_without_crashing():
    plan = plan_run_recovery([stage("ok", source_version="v3"), None], current_source_version="v3")
    assert plan.blocked is True


# --- format_recovery_plan: short Vietnamese + English status lines ------------


def test_format_reports_resume_and_reruns_in_both_languages():
    plan = plan_run_recovery(
        [
            stage("draft", source_version="v2", approvals=["ap"]),
            stage("render", status=StageStatus.PENDING, source_version=None),
        ],
        current_source_version="v3",
    )
    en = format_recovery_plan(plan, lang="en")
    vi = format_recovery_plan(plan, lang="vi")
    assert "Resume at draft" in en
    assert "rerun: draft, render" in en
    assert "invalid approvals: ap" in en
    assert "Tiếp tục từ draft" in vi
    assert "chạy lại: draft, render" in vi


def test_format_reports_blocked_plan():
    plan = plan_run_recovery([stage("a")], current_source_version=None)
    assert "Recovery blocked" in format_recovery_plan(plan)
    assert "Phục hồi bị chặn" in format_recovery_plan(plan, lang="vi")


def test_format_reports_nothing_to_rerun():
    plan = plan_run_recovery([stage("a", source_version="v3")], current_source_version="v3")
    assert "Nothing to re-run" in format_recovery_plan(plan)
    assert "Không cần chạy lại" in format_recovery_plan(plan, lang="vi")
