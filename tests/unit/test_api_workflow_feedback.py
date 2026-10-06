import pytest

from ai_orchestrator.api.business_workflows import (
    FeedbackAnswer,
    FeedbackConfirmation,
    FeedbackRequest,
    answer_feedback,
    confirm_feedback,
    feedback,
)
from ai_orchestrator.api.deps import ApiContext
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.security.auth import AuthorizationError, Principal


@pytest.mark.parametrize("kind", [ActorType.HUMAN, ActorType.AGENT])
@pytest.mark.parametrize(
    "endpoint,body",
    [
        (feedback, FeedbackRequest(message="Please revise the recruitment requirements")),
        (answer_feedback, FeedbackAnswer(revision=1, answers="Updated source information")),
        (confirm_feedback, FeedbackConfirmation(revision=1)),
    ],
)
async def test_only_privileged_humans_can_control_feedback_before_any_database_work(
    kind, endpoint, body
):
    actor = Actor(id="unauthorized", kind=kind)
    ctx = ApiContext(principal=Principal(actor), session=None, organization_id="unit", actor=actor)
    with pytest.raises(AuthorizationError):
        await endpoint("root", body, None, ctx)
