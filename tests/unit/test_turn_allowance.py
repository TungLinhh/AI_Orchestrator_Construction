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
"""

from __future__ import annotations

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


def _context(max_tokens: int) -> AgentContext:
    return AgentContext.model_construct(
        budget=BudgetEnvelope(max_tokens=max_tokens, max_cost_usd=Money("1.00"), max_runtime_s=300),
        data_classification=DataClassification.INTERNAL,
    )


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
