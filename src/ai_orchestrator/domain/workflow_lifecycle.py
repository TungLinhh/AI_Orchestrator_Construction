"""Recovery rules for ordered controllers. External writes never replay implicitly."""

from enum import StrEnum

from ai_orchestrator.domain.business_workflow import WORKFLOWS


class WorkflowKind(StrEnum):
    BUSINESS = "business_workflow"
    AGENT = "agent_workflow"


class RecoveryAction(StrEnum):
    REPLAY = "replay"
    RECONCILE = "reconcile"


def recovery_action(kind: WorkflowKind, workflow: str, stage_key: str) -> RecoveryAction:
    if kind is WorkflowKind.AGENT:
        # BlueprintRuntime only returns artifacts. Review tasks are excluded by
        # the caller using the approved plan's step_ids.
        return RecoveryAction.REPLAY
    stage = next((s for s in WORKFLOWS.get(workflow, ()) if s.key == stage_key), None)
    if stage and stage.kind in {
        "model",
        "input",
        "mail_read",
        "onboard",
        "record",
        "match",
        "close",
    }:
        # Onboarding currently writes deterministic sandbox files or reads
        # supplied evidence. A future account-provisioning adapter must opt out.
        return RecoveryAction.REPLAY
    return RecoveryAction.RECONCILE
