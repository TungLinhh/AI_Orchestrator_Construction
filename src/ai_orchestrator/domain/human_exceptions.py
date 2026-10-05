"""Why work reaches a person, from platform facts rather than model labels."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from ai_orchestrator.domain.enums import EffectClass


class WorkClass(StrEnum):
    ROUTINE = "routine"
    DOA_BAND = "doa_band"
    IRREVERSIBLE_EXTERNAL = "irreversible_external"
    FAILED_ESCALATION = "failed_escalation"
    AGENT_REQUEST = "agent_request"
    POLICY_EXCEPTION = "policy_exception"


def work_class(
    *,
    effect: str = "read",
    doa_subject: str = "",
    failed_escalation: bool = False,
    agent_requested: bool = False,
) -> WorkClass:
    if failed_escalation:
        return WorkClass.FAILED_ESCALATION
    if effect in {EffectClass.EXTERNAL_SEND.value, EffectClass.DESTRUCTIVE.value}:
        return WorkClass.IRREVERSIBLE_EXTERNAL
    if doa_subject:
        return WorkClass.DOA_BAND
    if agent_requested:
        return WorkClass.AGENT_REQUEST
    return WorkClass.ROUTINE


def approval_class(*, action_type: str, effect: str, payload: dict[str, Any]) -> WorkClass:
    result = work_class(
        effect=effect,
        doa_subject=str(payload.get("subject_kind") or ""),
        failed_escalation=action_type == "task.escalation.review",
        agent_requested=action_type == "task.continue",
    )
    return WorkClass.POLICY_EXCEPTION if result is WorkClass.ROUTINE else result


def pending_human_review(
    *,
    output: dict[str, Any],
    schema: dict[str, Any],
    approved_outputs: tuple[dict[str, Any], ...],
) -> tuple[str, dict[str, Any]] | None:
    """Keep only the current draft until its declared review is recorded for that exact value."""
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        return None
    stages = sorted(
        (definition["x-human-review-order"], key)
        for key, definition in properties.items()
        if isinstance(definition, dict)
        and type(definition.get("x-human-review-order")) is int
        and definition["x-human-review-order"] > 0
    )
    candidate = {}
    for approved in approved_outputs:
        candidate.update(approved)
    candidate.update(output)
    previous_review = -1
    for order, field in stages:
        value = candidate.get(field)
        if not value:
            # An absent draft cannot be approved. Completion still owes its contract.
            return None
        matching = next(
            (
                index
                for index, approved in enumerate(approved_outputs)
                if index > previous_review and approved.get(field) == value
            ),
            None,
        )
        if matching is not None:
            previous_review = matching
            continue
        draft = {key: candidate[key] for rank, key in stages if rank <= order and key in candidate}
        return (
            f"Human review required for {field}. "
            "Approve this exact draft before continuing to later steps.",
            draft,
        )
    return None
