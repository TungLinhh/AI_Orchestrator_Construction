"""A provider's content filter is a refusal, and must read like one.

The third `headcount-request` run delegated correctly and then died at the child
with the entire provider response body escaped into the task's `last_error` — a
500-word JSON blob with no status, no code, and no indication whether a retry
could ever help. The child was drafting a job description.

A content filter is categorically different from a rate limit or a transport
error, and the platform's error taxonomy has a category for it (`POLICY_DENIED`).
Falling through to the generic handler is what made the message unreadable.
"""

from __future__ import annotations

import httpx
import pytest

from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.errors import PolicyDenied
from ai_orchestrator.models.gateway import ModelCandidate, ModelPricing
from ai_orchestrator.models.providers import OpenAICompatibleProvider, _content_filter_refusal

pytestmark = pytest.mark.unit

CANDIDATE = ModelCandidate(
    provider="openrouter",
    model="dots-studio/dots-3-note-preview:free",
    pricing=ModelPricing(input_per_mtok=Money("0"), output_per_mtok=Money("0")),
    max_classification=DataClassification.PUBLIC,
)


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "openrouter",
        api_key="k",
        base_url="https://example.invalid/v1",
        timeout_s=1.0,
        max_retries=0,
    )


def _request():  # type: ignore[no-untyped-def]
    """A real `ModelRequest`, so the payload under test is the real payload."""
    from ai_orchestrator.models.gateway import ModelRequest

    return ModelRequest(
        profile="primary",
        system_instructions="",
        prompt="draft a job description",
        organization_id="org_01m3d5hwxet3x61vjc1ffjyrzh",
    )


def _client(body: dict[str, object], status: int = 200) -> httpx.AsyncClient:
    """An intercepting client, so no network and no real provider.

    `base_url` matters: the provider posts to the relative path
    `/chat/completions`, and without a base URL that path has no host and the
    transport raises `unknown url type` rather than reaching the handler.
    """
    return httpx.AsyncClient(
        base_url="https://example.invalid/v1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body, request=request)
        ),
    )


class TestDetectingTheRefusal:
    def test_an_error_code_is_recognised(self) -> None:
        assert _content_filter_refusal({"error": {"code": "content_filter"}}) == "content_filter"

    def test_an_error_message_is_recognised(self) -> None:
        """The variant that actually happened, which carried no code."""
        body = {"error": {"message": "Content filter triggered., body: [...]"}}
        assert _content_filter_refusal(body) == "content_filter"

    def test_a_finish_reason_is_recognised(self) -> None:
        body = {"choices": [{"finish_reason": "content_filter", "message": {}}]}
        assert _content_filter_refusal(body) == "content_filter"

    def test_a_normal_response_is_not_a_refusal(self) -> None:
        body = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}
        assert _content_filter_refusal(body) is None

    def test_an_empty_choices_list_is_not_a_refusal(self) -> None:
        assert _content_filter_refusal({"choices": []}) is None
        assert _content_filter_refusal({}) is None


class TestTheRefusalIsClassified:
    async def test_it_raises_policy_denied_not_a_generic_fault(self) -> None:
        provider = _provider()
        body = {"error": {"message": "Content filter triggered., body: [{...}]"}}
        provider._client = _client(body)
        with pytest.raises(PolicyDenied) as caught:
            await provider.complete(CANDIDATE, _request())
        await provider._client.aclose()
        assert "content filter" in str(caught.value)

    async def test_the_message_does_not_contain_the_provider_body(self) -> None:
        """The defect. A 500-word escaped blob is not a diagnostic.

        The body goes to `details` where it can be inspected deliberately, rather
        than into a task row an operator has to read to find out what happened.
        """
        provider = _provider()
        body = {"error": {"message": "Content filter triggered., body: [{...}]"}}
        provider._client = _client(body)
        with pytest.raises(PolicyDenied) as caught:
            await provider.complete(CANDIDATE, _request())
        await provider._client.aclose()
        message = str(caught.value)
        assert len(message) < 200, f"the message is {len(message)} characters long: {message}"
        assert "usage" not in message, "the provider body leaked into the message"

    async def test_the_body_is_kept_in_details_for_diagnosis(self) -> None:
        """Truncated on purpose: a diagnostic must not become a payload."""
        provider = _provider()
        body = {"error": {"message": "Content filter triggered."}, "model": "dots-3"}
        provider._client = _client(body)
        with pytest.raises(PolicyDenied) as caught:
            await provider.complete(CANDIDATE, _request())
        await provider._client.aclose()
        # `provider/model`, which is what identifies a candidate elsewhere.
        assert caught.value.details["model"] == "openrouter/dots-studio/dots-3-note-preview:free"
        assert caught.value.details["body"]["error"]["message"] == "Content filter triggered."
        # Only the two fields a diagnosis needs, so a provider cannot inflate a
        # task row by returning a megabyte of echo.
        assert set(caught.value.details["body"]) <= {"error", "model"}, (
            f"details carried more than it should: {caught.value.details['body']}"
        )

    async def test_a_normal_completion_is_untouched(self) -> None:
        """A guard against over-triggering: a refusal detector that fires on
        ordinary responses would turn every run into a policy denial."""
        provider = _provider()
        body = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hello"},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ],
            "model": "dots-3",
        }
        provider._client = _client(body)
        response = await provider.complete(CANDIDATE, _request())
        await provider._client.aclose()
        assert response.text == "hello"
