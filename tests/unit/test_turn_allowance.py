"""The turn's token allowance is a policy decision with a measurement behind it.

An earlier version of this file's subject was "keep the cap small". That was
measured, it was wrong, and re-deriving it costs two live provider calls, so the
numbers live here as an assertion instead of a comment nobody will re-check.

The finding, on the real seeded executive goal with the real roster and the real
three tools, varying *only* this number:

| allowance | finish      | reasoning tokens | first tool call        |
|-----------|-------------|------------------|------------------------|
| 2048      | tool_calls | 546              | `write_report`         |
| 8192      | tool_calls | 308              | `delegate_to_agent`    |

The model is a reasoning model. A tight cap does not make it cheaper or faster —
it removes the deliberation that produces a delegation, and the model answers the
goal itself instead. That is the single most important number in the runtime.

## And a second number, added after the first one truncated an answer

8192 is right for a *routing* turn and it is not enough for a turn that must emit a
declared object. Same model, same goal, and the procurement department's answer was cut
off mid-string — 15644 output tokens against an 8192 per-request ceiling, because
`qwen3`'s thinking is billed against `max_tokens`:

```
summary  1077 chars, no closing brace
  {"reason": "...tiêu chí quyết định: C (Công ty Toàn Cầu) 1.090.000.000 VND..."
ends    "...hoặc giá C ních lên bằng hoặc cao"
```

The answer was correct and the runtime threw it away. So a turn that declares required
keys gets 32768, measured against the 235929 the provider actually permits, and the
cost ceiling that governs spend is untouched by either number. F265.

The property that matters is unchanged and still holds for both: **a wider context
window must not widen the answer.**
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import _output_allowance
from ai_orchestrator.domain.budget import Money
from ai_orchestrator.domain.contracts import AgentContext, BudgetEnvelope
from ai_orchestrator.domain.enums import DataClassification

pytestmark = pytest.mark.unit

#: The measured delegation-producing allowance. Changing this without re-running
#: the measurement is how the platform gets a model that answers everything itself.
MEASURED_DELEGATING_ALLOWANCE = 8192

#: The cap that produced `write_report` instead of `delegate_to_agent`. Recorded so
#: the failure is reproducible rather than folklore.
MEASURED_SOLO_ALLOWANCE = 2048


def _context(max_tokens: int, *, contract: dict | None = None) -> AgentContext:
    task = None
    if contract is not None:
        task = SimpleNamespace(expected_output_schema=contract)
    return AgentContext.model_construct(
        budget=BudgetEnvelope(max_tokens=max_tokens, max_cost_usd=Money("1.00"), max_runtime_s=300),
        data_classification=DataClassification.INTERNAL,
        task=task,
    )


#: The measured allowance for a turn that must emit the keys it declared.
MEASURED_CONTRACT_ALLOWANCE = 32_768

CONTRACT = {"required": ["reason", "risk", "winner"]}


class TestTheAllowanceIsAMeasuredNumber:
    def test_it_is_the_allowance_that_produced_a_delegation(self) -> None:
        assert _output_allowance(_context(64_000)) == MEASURED_DELEGATING_ALLOWANCE

    def test_it_is_not_the_allowance_that_produced_solo_work(self) -> None:
        """The regression this guards is silent: the run succeeds either way.

        With the smaller cap the Executive answers the goal itself and every status
        in the system says `completed`. Nothing raises. Only the absence of
        `delegations` reveals it, which is why the demo prints that count.
        """
        assert _output_allowance(_context(64_000)) != MEASURED_SOLO_ALLOWANCE

    @pytest.mark.parametrize("max_tokens", [1_000, 8_000, 64_000, 512_000])
    def test_a_wider_context_does_not_widen_the_answer(self, max_tokens: int) -> None:
        """`max_tokens` is a context window, not permission to fill it.

        A 512k window at a fifth of the budget is how a turn once asked for 8192
        tokens of prose and then asked for 100k.
        """
        assert _output_allowance(_context(max_tokens)) == MEASURED_DELEGATING_ALLOWANCE


class TestAWorkerGetsRoomToAnswer:
    def test_a_declared_contract_gets_the_larger_allowance(self) -> None:
        assert _output_allowance(_context(64_000, contract=CONTRACT)) == MEASURED_CONTRACT_ALLOWANCE

    def test_the_larger_allowance_is_not_the_context_window(self) -> None:
        """The property the whole file exists for, applied to the second number.

        A 512k window is not permission to write 512k tokens, and the fix for the
        truncation must not have quietly become "ask for everything".
        """
        for window in (1_000, 8_000, 64_000, 512_000):
            got = _output_allowance(_context(window, contract=CONTRACT))
            assert got == MEASURED_CONTRACT_ALLOWANCE, f"{window}: got {got}"
            assert got < window or window < 64_000, (
                f"a {window}-token window was answered with {got} tokens"
            )

    def test_a_contract_without_required_keys_is_not_a_contract(self) -> None:
        """`{"produces": ...}` is documentation, and gets the routing allowance.

        The column is read as either `required` or `produces`; only `required` is a
        promise the gate enforces, so only it earns the larger budget.
        """
        assert _output_allowance(_context(64_000, contract={"field_meaning": {}})) == (
            MEASURED_DELEGATING_ALLOWANCE
        )

    def test_the_contract_allowance_is_within_what_the_provider_permits(self) -> None:
        """Measured at OpenRouter for `qwen/qwen3.8-27b:free`: 235929.

        Asserted so that raising this number past the provider's ceiling is a test
        failure rather than a run full of `token limit exceeded`.
        """
        assert MEASURED_CONTRACT_ALLOWANCE <= 235_929


class TestAContextWithNoTask:
    def test_a_context_without_a_task_gets_the_routing_allowance(self) -> None:
        """The shape the existing tests in this file build.

        Reading `context.task` unguarded raised `AttributeError` rather than
        returning the default, so a unit test that never needed a task could not be
        written — and the fallback is the *narrower* number, which is the right
        direction for something that cannot declare a contract.
        """
        assert _output_allowance(_context(64_000)) == MEASURED_DELEGATING_ALLOWANCE
