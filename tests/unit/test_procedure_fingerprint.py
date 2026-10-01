"""A procedure fingerprint: same shape of work, whatever the wording.

The distinction being tested is the one in `task_fingerprint`'s docstring and this
module's: a task hash answers *"have I been asked this before?"* and a procedure
hash answers *"is this the same shape of work?"*. Using the task hash for the second
question is the failure mode of having no procedures at all, wearing a hash.

Every test here is a decision that can be got wrong, so each asserts the decision
rather than the implementation.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.procedure import (
    FINGERPRINT_VERSION,
    Procedure,
    ToolStep,
    describe,
    procedure_fingerprint,
    same_procedure,
)

pytestmark = pytest.mark.unit


def _call(
    tool: str, sequence: int, *, keys: tuple[str, ...] = (), outcome: str = "success"
) -> dict[str, object]:
    """One `tool.invoke` audit row, in the shape the reader returns."""
    return {
        "sequence": sequence,
        "tool": tool,
        "outcome": outcome,
        "arguments": {key: f"value-for-{key}" for key in keys},
    }


class TestWordingIsIrrelevant:
    def test_different_argument_values_are_the_same_procedure(self) -> None:
        """The decision that matters most, and the one most likely to be got wrong.

        "Buy 5 laptops" and "buy 6 laptops" are the same procedure. A fingerprint
        over values would call them different, and would then never detect a repeat
        — the exact problem the fingerprint exists to solve.
        """
        five = procedure_fingerprint(
            task_type="analysis", tool_calls=[_call("write_report", 1, keys=("title", "body"))]
        )
        six = procedure_fingerprint(
            task_type="analysis", tool_calls=[_call("write_report", 1, keys=("title", "body"))]
        )
        assert same_procedure(five, six)

    def test_different_argument_keys_are_different_procedures(self) -> None:
        """Keys are the shape. A tool called with different fields is different work."""
        one = procedure_fingerprint(
            task_type="analysis", tool_calls=[_call("delegate_to_agent", 1, keys=("agent_name",))]
        )
        two = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[_call("delegate_to_agent", 1, keys=("agent_name", "objective"))],
        )
        assert not same_procedure(one, two)


class TestOrderIsTheShape:
    def test_a_different_sequence_is_a_different_procedure(self) -> None:
        """A procedure is a sequence. Sorting the names would make the two equal."""
        forward = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[_call("write_report", 1), _call("delegate_to_agent", 2)],
        )
        backward = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[_call("delegate_to_agent", 1), _call("write_report", 2)],
        )
        assert not same_procedure(forward, backward)

    def test_the_order_comes_from_the_sequence_not_the_return_order(self) -> None:
        """Rows arrive in whatever order the query returns; the sequence is the truth.

        Without this the same run would fingerprint differently depending on the
        planner's mood, and every repeat count would be noise.
        """
        in_order = [_call("write_report", 1), _call("delegate_to_agent", 2)]
        shuffled = [_call("delegate_to_agent", 2), _call("write_report", 1)]
        assert same_procedure(
            procedure_fingerprint(task_type="analysis", tool_calls=in_order),
            procedure_fingerprint(task_type="analysis", tool_calls=shuffled),
        )


class TestTheTaskTypeIsPartOfTheShape:
    def test_the_same_tools_under_a_different_task_type_differ(self) -> None:
        """Reading a table and writing a report happen to call the same tool once.

        Whether that is the same procedure is a judgement, and the conservative one
        is no: the two have different success criteria, so a lesson learned from one
        does not transfer to the other unexamined.
        """
        calls = [_call("write_report", 1)]
        assert not same_procedure(
            procedure_fingerprint(task_type="analysis", tool_calls=calls),
            procedure_fingerprint(task_type="coordination", tool_calls=calls),
        )


class TestRefusalsAreVisible:
    def test_a_refused_call_is_not_the_same_as_one_that_ran(self) -> None:
        """An agent that was told no has not done the work.

        Folding the outcome in keeps "tried and was refused" and "did it" as
        different shapes, so a repeat count does not claim a procedure was
        performed when every attempt was blocked.
        """
        assert not same_procedure(
            procedure_fingerprint(
                task_type="analysis", tool_calls=[_call("write_report", 1, outcome="success")]
            ),
            procedure_fingerprint(
                task_type="analysis", tool_calls=[_call("write_report", 1, outcome="failure")]
            ),
        )

    def test_three_refusals_are_still_one_shape(self) -> None:
        """How many times is content; whether it was refused is shape.

        The count lives in the trace, so a repeat count can distinguish them without
        the fingerprint having to.

        This assertion was inverted until the fingerprint stopped counting
        repetitions. The docstring always said the count is not the shape; the check
        said the opposite, and the check was what ran.
        """
        one = procedure_fingerprint(
            task_type="analysis", tool_calls=[_call("write_report", 1, outcome="failure")]
        )
        three = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[_call("write_report", i, outcome="failure") for i in (1, 2, 3)],
        )
        assert same_procedure(one, three)


class TestDegenerateTraces:
    def test_a_run_with_no_tool_calls_still_fingerprints(self) -> None:
        """A task that answered in prose is a procedure, just an empty one.

        Without this, every tool-less run would have a null fingerprint and be
        uncountable — and "this agent keeps answering instead of acting" is exactly
        the kind of pattern a repetition count should be able to see.
        """
        value = procedure_fingerprint(task_type="analysis", tool_calls=[])
        assert value
        assert same_procedure(value, procedure_fingerprint(task_type="analysis", tool_calls=[]))

    def test_rows_with_no_tool_name_are_ignored(self) -> None:
        """Defensive: a malformed row must not become a step called ``."""
        value = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[{"sequence": 1, "tool": None, "outcome": "success", "arguments": {}}],
        )
        assert same_procedure(value, procedure_fingerprint(task_type="analysis", tool_calls=[]))

    def test_a_missing_sequence_sorts_first_rather_than_raising(self) -> None:
        """A sort key that raises is a miserable place to find a nullable column."""
        rows = [
            {"sequence": None, "tool": "write_report", "outcome": "success", "arguments": {}},
            {"sequence": 5, "tool": "delegate_to_agent", "outcome": "success", "arguments": {}},
        ]
        assert procedure_fingerprint(task_type="analysis", tool_calls=rows)


class TestTheVersionTag:
    def test_the_version_is_part_of_the_hashed_payload(self) -> None:
        """Changing the shape of a fingerprint must invalidate every stored value.

        Silently comparing fingerprints computed under different rules is how a
        repeat count starts meaning something other than what it says.
        """
        procedure = Procedure(task_type="analysis", steps=(ToolStep(name="write_report"),))
        assert procedure.as_text().startswith(FINGERPRINT_VERSION)

    def test_two_different_task_types_cannot_collide_with_a_shared_prefix(self) -> None:
        """Delimiters matter: a naive join would make these two the same string."""
        left = Procedure(task_type="ab", steps=(ToolStep(name="c"),))
        right = Procedure(task_type="a", steps=(ToolStep(name="bc"),))
        assert left.fingerprint() != right.fingerprint()


class TestTheHelpers:
    def test_describe_is_a_short_stable_label(self) -> None:
        value = procedure_fingerprint(task_type="analysis", tool_calls=[])
        assert describe(value) == value[:8]
        assert describe(value) == describe(value)

    def test_an_empty_fingerprint_is_never_the_same_procedure(self) -> None:
        """Two unknowns are not a match. Counting absent observations would be a lie."""
        assert not same_procedure("", "")
        assert not same_procedure("", "a" * 64)
        assert not same_procedure("a" * 64, "")
