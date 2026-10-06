from ai_orchestrator.domain.workflow_lifecycle import RecoveryAction, WorkflowKind, recovery_action


def test_external_mail_and_unknown_actions_require_reconciliation():
    for workflow, stage in (
        ("mep_hiring", "test_mail"),
        ("unknown", "brief"),
        ("procurement", "new_write"),
    ):
        assert recovery_action(WorkflowKind.BUSINESS, workflow, stage) is RecoveryAction.RECONCILE


def test_artifact_model_and_read_only_intake_can_replay():
    for workflow, stage in (
        ("mep_hiring", "jd"),
        ("mep_hiring", "cv_intake"),
        ("procurement", "three_way_match"),
    ):
        assert recovery_action(WorkflowKind.BUSINESS, workflow, stage) is RecoveryAction.REPLAY


def test_blueprint_artifacts_can_replay():
    assert recovery_action(WorkflowKind.AGENT, "", "step_1") is RecoveryAction.REPLAY
