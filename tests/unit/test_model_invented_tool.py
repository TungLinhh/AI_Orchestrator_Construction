"""What the platform says when the model asks for a tool that does not exist.

F70. A free model emitted a tool call whose name was a fragment of a Python repr —
`invoke name="delegate_to_agent`. PydanticAI could not resolve it, the retry counter
for an unresolvable name is 0, and it raised:

```
Tool 'invoke name="delegate_to_agent' exceeded max retries count of 0
```

which is **true and misleading**. The number is right — there is no budget for a tool
that does not exist — and the conclusion a reader draws from it, "the retry
configuration is wrong", is not. It sent me to the tool-wiring layer, where I
convincingly diagnosed a bug that did not exist.

The wiring is fine, and there is a test for that here too: F67's entry asked for a
test asserting the *effective* retry count on a *registered* tool, and that is the
assertion that would have caught my wrong diagnosis in one run.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import (
    _classify_model_failure,
    _make_tool_fn,
    _tool_name_the_model_invented,
)
from ai_orchestrator.domain.ids import ToolId
from ai_orchestrator.tools.registry import ToolContract, ToolRisk

pytestmark = pytest.mark.unit

#: The real message, from a real free-tier model. Verbatim, because the value of this
#: whole file is that it matches what actually arrived.
REAL = (
    "Tool 'invoke name=\"delegate_to_agent' exceeded max retries count of 0. "
    "Consider raising the retry limit, or see the docs on tool retries: "
    "https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/#tool-retries"
)


class _Ctx:
    """Just the two things the classifier reads."""

    def __init__(self, *names: str) -> None:
        self.authorized_tools = [type("C", (), {"name": n})() for n in names]


class TestWhatTheOperatorIsTold:
    """The decision, not just the classifier.

    The first version of this fix tested `_tool_name_the_model_invented` thoroughly
    and wired it into a handler that could have ignored it — and every test passed
    with the handler's call removed. A test on a helper proves the helper works; it
    does not prove anything is *done* with the answer.
    """

    def test_the_real_message_is_reported_as_an_invented_tool(self) -> None:
        code, summary = _classify_model_failure(REAL, _Ctx("write_report"))
        assert code == "model_invented_tool"
        assert "was not offered" in summary

    def test_the_summary_says_what_the_agent_was_offered(self) -> None:
        """The actionable half. "The model asked for a tool that does not exist" is a
        dead end; being shown the four tools it *could* have asked for is a next step.
        """
        _, summary = _classify_model_failure(REAL, _Ctx("write_report", "calculator"))
        assert "write_report" in summary
        assert "calculator" in summary

    def test_it_does_not_blame_the_retry_budget(self) -> None:
        """The specific harm of F70. The number in the framework's message is true;
        repeating it is what sends an operator to the wrong configuration."""
        _, summary = _classify_model_failure(REAL, _Ctx("write_report"))
        assert "retries" not in summary.lower(), (
            "the summary repeats the retry framing that caused the misdiagnosis"
        )

    def test_a_provider_rate_limit_is_not_blamed_on_a_tool(self) -> None:
        """A real earlier fault: a 429 was reported as a tool problem, and it sent an
        operator to the tool registry for a rate limit."""
        code, summary = _classify_model_failure("Provider returned error 429", _Ctx("a"))
        assert code == "model_run_aborted"
        assert "429" in summary

    def test_an_unrelated_failure_is_quoted_verbatim(self) -> None:
        detail = "The model produced invalid JSON"
        code, summary = _classify_model_failure(detail, _Ctx("a"))
        assert code == "model_run_aborted"
        assert summary == detail

    def test_a_real_tool_exhausting_its_budget_still_reads_as_a_tool_fault(self) -> None:
        """Different fault, different remedy, so it keeps its own code. The retry
        budget on a real tool is 2, and a model that fills it has a schema problem."""
        code, _ = _classify_model_failure(
            "Tool 'write_report' exceeded max retries count of 2.", _Ctx("write_report")
        )
        assert code == "tool_not_granted"

    def test_tool_not_granted_is_still_reachable(self) -> None:
        """The code the invented-tool branch displaced.

        It was the *only* code for this case before, reached through a substring test
        for the word "tool". Widening the classification without checking the narrower
        one still fires would have turned it into dead code that a reader would assume
        is covered because it is named in a docstring.
        """
        code, summary = _classify_model_failure(
            "the model called a tool it was not permitted to use",
            _Ctx("write_report"),
        )
        assert code == "tool_not_granted"
        # Quoted verbatim: the framework's message is the diagnosis for a case we
        # cannot classify more precisely, and paraphrasing it is how a provider rate
        # limit ended up described as a tool problem.
        assert summary == "the model called a tool it was not permitted to use"


class TestTheRealMessageIsRecognised:
    def test_the_name_is_recovered_from_the_real_message(self) -> None:
        assert _tool_name_the_model_invented(REAL, _Ctx("write_report")) == (
            'invoke name="delegate_to_agent'
        )

    def test_it_is_not_mistaken_for_a_tool_we_offered(self) -> None:
        """The live case: the agent *was* offered `delegate_to_agent`, and the model
        asked for something else wearing its name. A substring check would have said
        "offered" and reported nothing — which is the failure mode of every field-name
        guess in this project."""
        assert _tool_name_the_model_invented(REAL, _Ctx("delegate_to_agent")) is not None, (
            "a malformed name containing a real tool's name was treated as that tool"
        )

    def test_a_genuinely_offered_tool_is_not_reported(self) -> None:
        detail = "Tool 'write_report' exceeded max retries count of 2."
        assert _tool_name_the_model_invented(detail, _Ctx("write_report")) is None


class TestOtherFailuresAreNotThisOne:
    @pytest.mark.parametrize(
        "detail",
        [
            "Provider returned error 429: rate limited",
            "Tool 'write_report' exceeded max retries count of 2.",
            "The model produced invalid JSON",
            "",
            "exceeded max retries count of 0",
            "Tool exceeded max retries count of 0",
        ],
    )
    def test_unrelated_failures_are_left_alone(self, detail: str) -> None:
        """A provider token limit arrives here too, and an earlier version of this
        handler reported it as a tool problem — sending an operator to the tool
        registry for a rate limit. Narrow claims only."""
        assert _tool_name_the_model_invented(detail, _Ctx("write_report")) is None

    def test_a_permitted_but_unused_tool_is_not_an_invention(self) -> None:
        detail = "Tool 'safe_web_search' exceeded max retries count of 1."
        assert _tool_name_the_model_invented(detail, _Ctx("safe_web_search")) is None


class TestTheRetryBudgetIsActuallyApplied:
    """The check F67's entry asked for, and the one that disproved my wrong diagnosis.

    Not "the constant is 2" — a test reading the constant passes whatever the wiring
    does. This registers real tools through the real builder and reads the count back
    off the registered object, which is the only thing that can catch a `Tool` being
    built correctly and then dropped.
    """

    def _contract(self, name: str) -> ToolContract:
        return ToolContract(
            # A real `ToolId`, not a readable stand-in: the branded ids validate
            # length, and a test double that cannot be constructed teaches nothing
            # about the code path it is exercising.
            tool_id=str(ToolId.create()),
            name=name,
            description=f"the {name} tool",
            input_schema={
                "type": "object",
                "properties": {"objective": {"type": "string"}},
                "required": ["objective"],
                "additionalProperties": False,
            },
            risk=ToolRisk.READ_ONLY,
        )

    async def _noop(self, **_kwargs: object) -> str:
        return "ok"

    async def test_a_registered_tool_carries_the_retry_budget(self) -> None:
        from pydantic_ai.toolsets.function import FunctionToolset

        from ai_orchestrator.agent_runtime.pydanticai_agent import _VALIDATION_RETRIES

        toolset = FunctionToolset()
        for name in ("delegate_to_agent", "write_report"):
            tool = _make_tool_fn(self._contract(name), self._noop)
            assert tool is not None
            toolset.add_tool(tool)

        for name in ("delegate_to_agent", "write_report"):
            registered = toolset.tools[name]
            assert registered.max_retries == _VALIDATION_RETRIES, (
                f"{name} registered with max_retries={registered.max_retries}; the "
                "budget was built and then lost on the way into the toolset"
            )

    async def test_a_registered_tool_keeps_its_name(self) -> None:
        """The live failure named a tool as a repr fragment. If a name is ever
        synthesised instead of taken from the contract, this is what notices."""
        from pydantic_ai.toolsets.function import FunctionToolset

        toolset = FunctionToolset()
        tool = _make_tool_fn(self._contract("delegate_to_agent"), self._noop)
        assert tool is not None
        toolset.add_tool(tool)
        assert "delegate_to_agent" in toolset.tools, (
            f"registered under {list(toolset.tools)!r} instead of its contract name"
        )
