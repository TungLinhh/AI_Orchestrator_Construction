"""A 200 response with no usable answer must not stop real work."""

import asyncio

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import _RecordingGateway
from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import ModelUnavailable, PolicyDenied
from ai_orchestrator.models.gateway import (
    ModelCandidate,
    ModelGateway,
    ModelProfile,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from ai_orchestrator.models.profiles import default_profiles


class Replies(ModelProvider):
    name = "fixture"

    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.seen = []

    async def complete(self, candidate, request):
        self.seen.append((candidate.model, request.max_provider_retries))
        value = self.replies[candidate.model]
        if isinstance(value, BaseException):
            raise value
        if value == "slow":
            await asyncio.sleep(10)
        return value


def gateway(replies, **kwargs):
    provider = Replies(replies)
    profile = ModelProfile(
        name="test",
        candidates=tuple(ModelCandidate(provider="fixture", model=k) for k in replies),
        **kwargs,
    )
    return ModelGateway(providers={"fixture": provider}, profiles={"test": profile}), provider


@pytest.mark.parametrize(
    "bad",
    [
        ModelResponse(),
        ModelResponse(text="  \n"),
        ModelResponse(text="looks plausible", finish_reason="error"),
        ModelResponse(text="plausible but unfinished", finish_reason="length"),
        ModelResponse(tool_calls=[{"function": {"name": "invented", "arguments": "{}"}}]),
    ],
)
async def test_unusable_output_falls_through_with_evidence(bad):
    gw, provider = gateway({"first": bad, "second": ModelResponse(text="answer")})
    response = await gw.complete(ModelRequest(profile="test"))
    assert [v[0] for v in provider.seen] == ["first", "second"]
    assert response.model_used == "second"
    assert response.decision.routing_reason == "fallback"
    assert response.decision.rejected
    assert response.rejected_responses == [bad]


async def test_required_tool_plain_prose_falls_through():
    tool = {"type": "function", "function": {"name": "submit", "parameters": {}}}
    gw, _ = gateway(
        {
            "first": ModelResponse(text="I would submit it"),
            "second": ModelResponse(
                tool_calls=[{"function": {"name": "submit", "arguments": "{}"}}]
            ),
        }
    )
    response = await gw.complete(ModelRequest(profile="test", tools=[tool], tool_choice="required"))
    assert response.model_used == "second"


async def test_explicitly_disabled_tools_allow_a_text_answer():
    tool = {"type": "function", "function": {"name": "submit", "parameters": {}}}
    gw, _ = gateway({"first": ModelResponse(text="answer")})
    response = await gw.complete(ModelRequest(profile="test", tools=[tool], tool_choice="none"))
    assert response.text == "answer"


@pytest.mark.parametrize("arguments", ["not JSON", "[]", "null"])
async def test_malformed_tool_arguments_fall_through(arguments):
    tool = {"type": "function", "function": {"name": "submit", "parameters": {}}}
    gw, _ = gateway(
        {
            "first": ModelResponse(
                tool_calls=[{"function": {"name": "submit", "arguments": arguments}}]
            ),
            "second": ModelResponse(text="answer"),
        }
    )
    assert (await gw.complete(ModelRequest(profile="test", tools=[tool]))).model_used == "second"


async def test_candidate_deadline_includes_retry_time_and_advances():
    gw, provider = gateway(
        {"first": "slow", "second": ModelResponse(text="answer")}, max_latency_ms=10, max_retries=0
    )
    response = await asyncio.wait_for(gw.complete(ModelRequest(profile="test")), 1)
    assert response.model_used == "second"
    assert provider.seen == [("first", 0), ("second", 0)]
    assert "deadline" in response.decision.rejected[0]


async def test_policy_refusal_never_falls_through():
    gw, provider = gateway(
        {"first": PolicyDenied("refused"), "second": ModelResponse(text="answer")}
    )
    with pytest.raises(PolicyDenied):
        await gw.complete(ModelRequest(profile="test"))
    assert len(provider.seen) == 1


@pytest.mark.parametrize("second", [ModelResponse(text="answer"), ModelResponse()])
async def test_recorder_receives_rejected_usage_even_when_all_models_fail(second):
    bad = ModelResponse(
        usage=TokenUsage(input_tokens=10, output_tokens=20), cost_usd=Money("0.001")
    )
    gw, _ = gateway({"first": bad, "second": second})
    recorded = []

    async def record(response):
        recorded.append(response)

    wrapped = _RecordingGateway(gw, record)
    if second.text:
        await wrapped.complete(ModelRequest(profile="test"))
    else:
        with pytest.raises(ModelUnavailable):
            await wrapped.complete(ModelRequest(profile="test"))
    assert recorded == [bad, second]
    assert recorded[0].total_tokens == 30
    assert recorded[0].decision.model == "first"


async def test_failed_completion_cost_reduces_fallback_budget():
    gw, provider = gateway(
        {"first": ModelResponse(cost_usd=Money("0.5")), "second": ModelResponse(text="answer")}
    )
    with pytest.raises(ModelUnavailable, match="remaining budget"):
        await gw.complete(ModelRequest(profile="test", remaining_budget_usd=Money("0.1")))
    assert len(provider.seen) == 1


async def test_workflow_keeps_raw_output_for_its_own_repair_and_ledger():
    bad = ModelResponse(finish_reason="error")
    gw, provider = gateway({"first": bad, "second": ModelResponse(text="answer")})
    response = await gw.complete(ModelRequest(profile="test", caller_validates_output=True))
    assert response is bad
    assert len(provider.seen) == 1


async def test_structured_request_skips_models_without_strict_schema_support():
    profiles = default_profiles()
    assert [c.model for c in profiles["primary"].candidates[:2]] == [
        "dots-studio/dots-3-note-preview:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ]
    gw = ModelGateway(profiles=profiles)
    candidates, _ = gw._select_candidates(
        profiles["primary"],
        ModelRequest(
            profile="primary",
            output_schema={"type": "object"},
            data_classification=DataClassification.PUBLIC,
        ),
    )
    assert candidates[0].model == "dots-studio/dots-3-note-preview:free"


@pytest.mark.parametrize(
    "profile_ceiling,candidate_ceiling",
    [
        (DataClassification.PUBLIC, DataClassification.SECRET),
        (DataClassification.SECRET, DataClassification.PUBLIC),
    ],
)
async def test_neither_privacy_ceiling_can_widen_the_other(profile_ceiling, candidate_ceiling):
    provider = Replies({"first": ModelResponse(text="must never be reached")})
    profile = ModelProfile(
        name="test",
        max_classification=profile_ceiling,
        candidates=(
            ModelCandidate(provider="fixture", model="first", max_classification=candidate_ceiling),
        ),
    )
    gw = ModelGateway(providers={"fixture": provider}, profiles={"test": profile})
    with pytest.raises(ModelUnavailable, match="no permitted"):
        await gw.complete(
            ModelRequest(profile="test", data_classification=DataClassification.INTERNAL)
        )
    assert provider.seen == []


@pytest.mark.parametrize("finish_reason", ["refused", "content_filter"])
@pytest.mark.parametrize("caller_validates", [False, True])
async def test_refusal_finish_is_terminal_with_known_usage_recorded_once(
    finish_reason, caller_validates
):
    bad = ModelResponse(
        text="refused",
        finish_reason=finish_reason,
        usage=TokenUsage(input_tokens=12, output_tokens=3),
    )
    gw, provider = gateway({"first": bad, "second": ModelResponse(text="answer")})
    recorded = []

    async def record(response):
        recorded.append(response)

    with pytest.raises(PolicyDenied):
        await _RecordingGateway(gw, record).complete(
            ModelRequest(profile="test", caller_validates_output=caller_validates)
        )
    assert [name for name, _ in provider.seen] == ["first"]
    assert recorded == [bad]
    assert recorded[0].total_tokens == 15
