"""Source tasks run smooth on fixture providers: reject, repair, stay in budget.

Each case below is an observed live-model shape, replayed with a scripted
gateway so the suite spends nothing and touches no network:

* NVIDIA writes the tool call as *text* (`[[{"name": ...}]]`) with no tool
  call and `finish_reason="stop"`. The gateway/runtime must reject it -- never
  treat prose as a call -- and repair on the next turn on the SAME candidate;
  the candidate is excluded only after its second failure (a provider-side
  `finish_reason="error"` still excludes at once).
* a turn calls the wrong tool, or sends arguments that are not a JSON object.
* the artifact parses but the validator refuses it (wrong quote winner/total,
  invented citation, insubstantial summary). The same candidate is retried
  once with the rejection and the previous submission in the prompt, then
  excluded after the second failure.
* the 2026-10-15 live quote_comparison chain (no-tool-call, then a truncated
  turn, then a weak summary) still completes inside the five-turn budget.
* when every candidate is excluded while turns remain, the least-failed
  candidate is reopened once for a final repair; when every excluded candidate
  has already been reopened, the run fails fast naming the failures instead of
  surfacing the gateway's ModelUnavailable.
* the repair loop is bounded (five submissions), and transport retries are off
  inside it (`max_provider_retries=0`), so a bad turn costs one call, not a
  retry storm.
* a run that exceeds its token/cost budget stops with its usage recorded,
  instead of looping.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ai_orchestrator.agent_runtime.source_document import SourceDocumentRuntime
from ai_orchestrator.domain.budget import Money, TokenUsage
from ai_orchestrator.domain.contracts import Actor, AgentContext, AgentTask, BudgetEnvelope
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.ids import ExecutionId, OrganizationId, TaskId
from ai_orchestrator.domain.source_report import compare_quotes
from ai_orchestrator.models.gateway import ModelResponse

pytestmark = pytest.mark.unit

NVIDIA = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
DOTS = "openrouter/dots-studio/dots-3-note-preview:free"


def _review_sources() -> list[dict[str, Any]]:
    return [
        {
            "id": "src_review_one",
            "filename": "brief.txt",
            "segments": [
                {
                    "locator": "brief.txt#lines:1-2",
                    "text": "Can 12 van co chung chi. Chua dat hang, chua co phe duyet mua hang.",
                }
            ],
            "tables": [],
            "warnings": [],
        },
        {
            "id": "src_review_two",
            "filename": "note.txt",
            "segments": [
                {
                    "locator": "note.txt#lines:1-1",
                    "text": "Nha cung cap B thieu chung chi bat buoc theo yeu cau.",
                }
            ],
            "tables": [],
            "warnings": [],
        },
    ]


def _review_report() -> dict[str, Any]:
    return {
        "summary": "Hai nguon duoc ra soat, chi ro nha cung cap thieu chung chi.",
        "findings": [
            {
                "text": "Nhu cau 12 van co chung chi duoc ghi trong brief.",
                "source_id": "src_review_one",
                "locator": "brief.txt#lines:1-2",
                "quote": "Can 12 van co chung chi.",
            },
            {
                "text": "Nha cung cap B thieu chung chi.",
                "source_id": "src_review_two",
                "locator": "note.txt#lines:1-1",
                "quote": "Nha cung cap B thieu chung chi",
            },
        ],
        "limitations": ["Bao cao tren nguon duoc chon, can xac minh them."],
    }


def _quote_context() -> dict[str, Any]:
    rows = [
        {
            "quote_id": "A",
            "item": "Valve-1",
            "quantity": 12,
            "unit_price": 120000,
            "delivery": 80000,
            "certified": "true",
        },
        {
            "quote_id": "B",
            "item": "Valve-1",
            "quantity": 12,
            "unit_price": 110000,
            "delivery": 0,
            "certified": "false",
        },
        {
            "quote_id": "C",
            "item": "Valve-1",
            "quantity": 12,
            "unit_price": 130000,
            "delivery": 0,
            "certified": "true",
        },
    ]
    expected = compare_quotes([{"rows": rows}])
    assert expected == {"winner": "A", "total_vnd": 1520000}
    source = {
        "id": "src_quotes",
        "filename": "mock-quotes-1.csv",
        "segments": [
            {"locator": "rows:1", "text": "A,Valve-1,12,120000,80000,true"},
            {"locator": "rows:2", "text": "B,Valve-1,12,110000,0,false"},
            {"locator": "rows:3", "text": "C,Valve-1,12,130000,0,true"},
        ],
        "tables": [{"rows": rows}],
        "warnings": [],
    }
    return {
        "manifest": {"id": "smf_test", "sha256": "test", "sources": [], "version": 1},
        "sources": [source],
        "expected": expected,
        "classification": "internal",
    }


def _review_context() -> dict[str, Any]:
    return {
        "manifest": {"id": "smf_test", "sha256": "test", "sources": [], "version": 1},
        "sources": _review_sources(),
        "expected": None,
        "classification": "internal",
    }


class _ScriptedGateway:
    """Replays one response per turn and records every request for assertions."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []
        self.closed = False

    async def complete(self, request: Any) -> ModelResponse:
        self.requests.append(request)
        # The artifact loop owns repair, so a bad turn must cost exactly one
        # provider call: no nested transport retry inside the five-turn budget.
        assert request.tool_choice == "required"
        assert request.tools[0]["function"]["name"] == "submit_evidence"
        assert request.max_provider_retries == 0
        assert request.caller_validates_output is True
        return self._responses.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _call(arguments: Any, *, model: str = DOTS) -> ModelResponse:
    provider, _, name = model.partition("/")
    return ModelResponse(
        provider=provider or "openrouter",
        model_used=name or model,
        finish_reason="tool_calls",
        tool_calls=[
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "submit_evidence",
                    "arguments": arguments
                    if isinstance(arguments, str)
                    else json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
        usage=TokenUsage(input_tokens=50, output_tokens=50),
    )


def _context() -> AgentContext:
    org = OrganizationId.create()
    task = AgentTask(
        task_id=TaskId.create(),
        organization_id=org,
        execution_id=ExecutionId.create(),
        goal="Read the chosen files and report grounded findings. Do not send or order.",
    )
    return AgentContext(
        actor=Actor(id="agent-test", kind=ActorType.AGENT),
        task=task,
        system_instructions="You are a test agent.",
        organization_id=org,
        budget=BudgetEnvelope(max_tokens=20000, max_cost_usd=Money("1.00"), max_runtime_s=60),
    )


def _nvidia_text_shaped_prose() -> ModelResponse:
    # The observed NVIDIA failure: the tool call written as message text, with
    # no tool call attached and a plain "stop" finish reason.
    return ModelResponse(
        provider="openrouter",
        model_used="nvidia/nemotron-3-ultra-550b-a55b:free",
        finish_reason="stop",
        text='[[{"name": "submit_evidence", "parameters": {"summary": "picked A"}}]]',
        usage=TokenUsage(input_tokens=50, output_tokens=50),
    )


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(_nvidia_text_shaped_prose(), id="tool-call-as-text"),
        pytest.param(
            ModelResponse(
                provider="openrouter",
                model_used="nvidia/nemotron-3-ultra-550b-a55b:free",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "calculator", "arguments": '{"a": 1}'},
                    }
                ],
                usage=TokenUsage(input_tokens=50, output_tokens=50),
            ),
            id="wrong-tool",
        ),
        pytest.param(
            ModelResponse(
                provider="openrouter",
                model_used="nvidia/nemotron-3-ultra-550b-a55b:free",
                finish_reason="tool_calls",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "submit_evidence", "arguments": "not JSON"},
                    }
                ],
                usage=TokenUsage(input_tokens=50, output_tokens=50),
            ),
            id="malformed-arguments",
        ),
    ],
)
async def test_first_unusable_turn_is_repaired_on_the_same_candidate(bad: ModelResponse) -> None:
    gateway = _ScriptedGateway([bad, _call(_review_report())])
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    result = await runtime.execute(_context().task, _context(), record_usage=record)

    assert result.output["findings"][0]["source_id"] == "src_review_one"
    assert len(gateway.requests) == 2
    # One bad turn is a slip, not proof the candidate cannot do it: the repair
    # stays on the same candidate, so nothing is excluded yet.
    assert gateway.requests[1].excluded_candidates == frozenset()
    assert gateway.requests[1].max_provider_retries == 0
    # The rejection is recorded with its reason, not swallowed.
    assert len(ledger) == 2 and ledger[0].validation_error
    # The repair prompt carries the rejection and what the model did wrong.
    repair_prompt = gateway.requests[1].prompt
    assert "Previous submission was rejected" in repair_prompt
    if bad.tool_calls or bad.text:
        assert "PREVIOUS SUBMISSION:" in repair_prompt
    # The prompt tells the model to invoke the tool, not to write it as text.
    assert "never write the artifact or the tool call" in gateway.requests[0].system_instructions
    assert gateway.closed


async def test_second_unusable_turn_excludes_the_candidate() -> None:
    gateway = _ScriptedGateway(
        [_nvidia_text_shaped_prose(), _nvidia_text_shaped_prose(), _call(_review_report())]
    )
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    result = await runtime.execute(_context().task, _context(), record_usage=record)

    assert result.output["findings"][0]["source_id"] == "src_review_one"
    assert len(gateway.requests) == 3
    # First failure repairs in place; only the second failure excludes.
    assert gateway.requests[1].excluded_candidates == frozenset()
    assert NVIDIA in gateway.requests[2].excluded_candidates
    assert len(ledger) == 3 and all(response.validation_error for response in ledger[:2])
    assert gateway.closed


async def test_wrong_quote_total_is_retried_on_the_same_candidate_with_evidence() -> None:
    quote_sources = _quote_context()["sources"]
    assert quote_sources  # the helper above documents the deterministic rule
    wrong_report = {
        "summary": "Bao gia B re nhat theo don gia va duoc chon nham.",
        "findings": [
            {
                "text": "Bao gia B co don gia thap nhat.",
                "source_id": "src_quotes",
                "locator": "rows:2",
                "quote": "B,Valve-1,12,110000,0,false",
            },
            {
                "text": "Bao gia A dat chung chi.",
                "source_id": "src_quotes",
                "locator": "rows:1",
                "quote": "A,Valve-1,12,120000,80000,true",
            },
            {
                "text": "Bao gia C dat chung chi.",
                "source_id": "src_quotes",
                "locator": "rows:3",
                "quote": "C,Valve-1,12,130000,0,true",
            },
        ],
        "limitations": ["Can xac minh them."],
        # B is uncertified, so the deterministic rule refuses it: the model must
        # copy winner A / total 1520000 instead of computing its own answer.
        "winner": "B",
        "total_vnd": 1320000,
    }
    right_report = dict(wrong_report, winner="A", total_vnd=1520000)
    gateway = _ScriptedGateway([_call(wrong_report), _call(right_report)])
    runtime = SourceDocumentRuntime(gateway, _quote_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    result = await runtime.execute(_context().task, _context(), record_usage=record)

    assert result.output["winner"] == "A" and result.output["total_vnd"] == 1520000
    # A semantic refusal is retried on the same candidate first: one bad turn
    # is a slip, not proof the candidate cannot do it.
    assert len(gateway.requests) == 2
    assert gateway.requests[1].excluded_candidates == frozenset()
    assert "winner" in (ledger[0].validation_error or "").lower()
    assert '"winner": "B"' in gateway.requests[1].prompt


def _dots_truncated() -> ModelResponse:
    # The observed DOTS failure in the live chain: the turn was cut off, so no
    # artifact could be parsed. The first truncation doubles the output limit
    # instead of excluding the candidate.
    return ModelResponse(
        provider="openrouter",
        model_used="dots-studio/dots-3-note-preview:free",
        finish_reason="length",
        text="truncated artifact, no tool call completed",
        usage=TokenUsage(input_tokens=50, output_tokens=50),
    )


def _dots_text_shaped_prose() -> ModelResponse:
    # Same no-tool-call shape as the NVIDIA prose failure, but served by the
    # fallback candidate so per-candidate failure accounting stays observable.
    return ModelResponse(
        provider="openrouter",
        model_used="dots-studio/dots-3-note-preview:free",
        finish_reason="stop",
        text='[[{"name": "submit_evidence", "parameters": {"summary": "picked A"}}]]',
        usage=TokenUsage(input_tokens=50, output_tokens=50),
    )


def _quote_report(summary: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "findings": [
            {
                "text": "Bao gia A dat chung chi va co tong thap nhat.",
                "source_id": "src_quotes",
                "locator": "rows:1",
                "quote": "A,Valve-1,12,120000,80000,true",
            }
        ],
        "limitations": ["Can xac minh them truoc khi dat hang."],
        "winner": "A",
        "total_vnd": 1520000,
    }


_GOOD_QUOTE_SUMMARY = (
    "Nha cung cap A thang: tong 1520000 VND thap nhat trong so cac bao gia "
    "dat chung chi; B bi loai vi thieu chung chi bat buoc, C dat hon."
)


async def test_live_quote_chain_recovers_within_five_turns() -> None:
    """Replays the 2026-10-15 mock quote_comparison probe shape to completion.

    Turn 1 (NVIDIA) writes no tool call, turn 2 (DOTS) is truncated, turn 3
    (DOTS) submits all five fields but a weak summary the validator refuses.
    Under the old rule both candidates were excluded by turn 3 and turns 4-5
    died on a gateway ModelUnavailable; now only a second failure excludes, so
    the run still completes on turn 4. The scripted gateway replays the
    live response order regardless of routing; what the test locks is the
    exclusion and repair accounting per turn.
    """
    gateway = _ScriptedGateway(
        [
            _nvidia_text_shaped_prose(),
            _dots_truncated(),
            _call(_quote_report("Chon A.")),
            _call(_quote_report(_GOOD_QUOTE_SUMMARY)),
        ]
    )
    runtime = SourceDocumentRuntime(gateway, _quote_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    result = await runtime.execute(_context().task, _context(), record_usage=record)

    assert result.output["winner"] == "A" and result.output["total_vnd"] == 1520000
    assert len(gateway.requests) == 4
    assert gateway.requests[1].excluded_candidates == frozenset()
    assert gateway.requests[2].excluded_candidates == frozenset()
    assert gateway.requests[2].max_output_tokens == gateway.requests[1].max_output_tokens * 2
    # The weak summary is DOTS's second failure, so turn 4 excludes DOTS and
    # the repair goes to the other candidate -- still inside the budget.
    assert gateway.requests[3].excluded_candidates == frozenset({DOTS})
    assert "Previous submission was rejected" in gateway.requests[3].prompt
    assert "Chon A." in gateway.requests[3].prompt
    assert all(response.validation_error for response in ledger[:3])
    assert gateway.closed


class _ExcludingGateway:
    """Mimics ModelGateway routing so the reopen behaviour is observable offline.

    Serves scripted responses per candidate key in profile order and raises
    GatewayUnavailable with the real exclusion detail shape once every
    candidate is excluded, exactly like ModelGateway.complete does.
    """

    NVIDIA_KEY = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
    DOTS_KEY = "openrouter/dots-studio/dots-3-note-preview:free"

    def __init__(
        self, scripted: dict[str, list[ModelResponse]], order: list[str] | None = None
    ) -> None:
        self._scripted = {key: list(responses) for key, responses in scripted.items()}
        self._order = order or [self.NVIDIA_KEY, self.DOTS_KEY]
        self.requests: list[Any] = []
        self.closed = False

    async def complete(self, request: Any) -> ModelResponse:
        from ai_orchestrator.models.gateway import GatewayUnavailable

        self.requests.append(request)
        assert request.tool_choice == "required"
        assert request.tools[0]["function"]["name"] == "submit_evidence"
        assert request.max_provider_retries == 0
        assert request.caller_validates_output is True
        excluded = set(request.excluded_candidates)
        available = [key for key in self._order if key not in excluded]
        if not available:
            errors = [f"{key}: excluded after artifact verification failure" for key in self._order]
            raise GatewayUnavailable(
                "no usable model for profile 'test': " + "; ".join(errors),
                details={
                    "profile": "test",
                    "errors": errors,
                    "unservable": [],
                    "considered": list(self._order),
                },
                cause=None,
                responses=(),
            )
        return self._scripted[available[0]].pop(0)

    async def aclose(self) -> None:
        self.closed = True


async def test_all_excluded_reopens_least_failed_candidate_within_budget() -> None:
    gateway = _ExcludingGateway(
        {
            _ExcludingGateway.NVIDIA_KEY: [
                _nvidia_text_shaped_prose(),
                _nvidia_text_shaped_prose(),
                _call(_review_report()),
            ],
            _ExcludingGateway.DOTS_KEY: [
                _dots_text_shaped_prose(),
                _dots_text_shaped_prose(),
            ],
        }
    )
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    result = await runtime.execute(_context().task, _context(), record_usage=record)

    assert result.output["findings"][0]["source_id"] == "src_review_one"
    # Four failed turns exclude both candidates; the fifth turn reopens the
    # least-failed one (tied 2-2, NVIDIA failed first) inside the same turn
    # instead of dying on the gateway error.
    assert len(gateway.requests) == 6
    assert gateway.requests[5].excluded_candidates == frozenset({_ExcludingGateway.DOTS_KEY})
    assert len(ledger) == 5 and all(response.validation_error for response in ledger[:4])
    assert gateway.closed


async def test_reopen_spent_fails_fast_with_named_failures() -> None:
    gateway = _ExcludingGateway(
        {
            _ExcludingGateway.NVIDIA_KEY: [_nvidia_text_shaped_prose() for _ in range(3)],
        },
        order=[_ExcludingGateway.NVIDIA_KEY],
    )
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    with pytest.raises(ValueError, match="every model candidate was excluded") as excinfo:
        await runtime.execute(_context().task, _context(), record_usage=record)
    # Turns 1-2 fail, turn 3 reopens the only candidate and fails again, turn 4
    # reports the named failures at once instead of a cryptic ModelUnavailable.
    assert _ExcludingGateway.NVIDIA_KEY in str(excinfo.value)
    assert len(gateway.requests) == 5
    assert len(ledger) == 3 and all(response.validation_error for response in ledger)
    assert gateway.closed


async def test_repair_loop_is_bounded_at_five_submissions() -> None:
    gateway = _ScriptedGateway([_nvidia_text_shaped_prose() for _ in range(5)])
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    with pytest.raises(ValueError, match="failed verification after five submissions"):
        await runtime.execute(_context().task, _context(), record_usage=record)
    assert len(gateway.requests) == 5
    assert len(ledger) == 5 and all(response.validation_error for response in ledger)


async def test_budget_exhaustion_stops_without_another_turn() -> None:
    heavy = _call(_review_report())
    heavy.usage = TokenUsage(input_tokens=9000, output_tokens=9000, reasoning_tokens=9000)
    gateway = _ScriptedGateway([heavy])
    runtime = SourceDocumentRuntime(gateway, _review_context())
    ledger: list[ModelResponse] = []

    async def record(response: ModelResponse) -> None:
        ledger.append(response)

    org = OrganizationId.create()
    task = AgentTask(
        task_id=TaskId.create(),
        organization_id=org,
        execution_id=ExecutionId.create(),
        goal="Read the chosen files and report grounded findings. Do not send or order.",
    )
    context = AgentContext(
        actor=Actor(id="agent-test", kind=ActorType.AGENT),
        task=task,
        system_instructions="You are a test agent.",
        organization_id=org,
        budget=BudgetEnvelope(max_tokens=10, max_cost_usd=Money("1.00"), max_runtime_s=60),
    )
    with pytest.raises(ValueError, match="token/cost budget"):
        await runtime.execute(task, context, record_usage=record)
    assert len(gateway.requests) == 1
    assert len(ledger) == 1
    assert ledger[0].validation_error == "Workflow token/cost budget exceeded"


async def test_gateway_level_text_shaped_call_falls_through_to_the_fallback() -> None:
    from ai_orchestrator.models.gateway import (
        ModelCandidate,
        ModelGateway,
        ModelProfile,
        ModelProvider,
        ModelRequest,
    )

    class _Fixture(ModelProvider):
        name = "fixture"

        def __init__(self) -> None:
            super().__init__()
            self.seen: list[str] = []

        async def complete(self, candidate: ModelCandidate, request: ModelRequest) -> ModelResponse:
            self.seen.append(candidate.model)
            if candidate.model == "nvidia-text":
                return _nvidia_text_shaped_prose()
            return _call(_review_report())

    provider = _Fixture()
    gateway = ModelGateway(
        providers={"fixture": provider},
        profiles={
            "test": ModelProfile(
                name="test",
                candidates=(
                    ModelCandidate(provider="fixture", model="nvidia-text"),
                    ModelCandidate(provider="fixture", model="dots-tools"),
                ),
            )
        },
    )
    tool = {"type": "function", "function": {"name": "submit_evidence", "parameters": {}}}
    response = await gateway.complete(
        ModelRequest(profile="test", tools=[tool], tool_choice="required")
    )
    assert provider.seen == ["nvidia-text", "dots-tools"]
    assert response.decision is not None and response.decision.routing_reason == "fallback"
    assert len(response.rejected_responses) == 1
    assert response.rejected_responses[0].validation_error == "required tool call missing"
