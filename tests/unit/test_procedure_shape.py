"""What counts as the same procedure, and what does not.

The load-bearing test here is `test_how_many_times_a_tool_was_called_is_not_the_shape`.
It exists because the fingerprint originally included repetition counts, and four
runs of the same real task produced four different fingerprints — so the repetition
gate, which counts identical fingerprints, sat at 1 while the platform did the same
work four times running. Every other test in the suite passed throughout, because
scripted runtimes are perfectly repeatable and never vary the way a real model does.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.procedure import (
    FINGERPRINT_VERSION,
    Procedure,
    ToolStep,
    procedure_fingerprint,
    same_procedure,
)

pytestmark = pytest.mark.unit


def _calls(*names: str, arguments: dict[str, list[str]] | None = None) -> list[dict[str, object]]:
    """Audit rows in call order, as `ProcedureReader` would hand them over."""
    return [
        {
            "sequence": index + 1,
            "tool": name,
            "outcome": "success",
            "arguments": {key: "x" for key in (arguments or {}).get(name, [])},
        }
        for index, name in enumerate(names)
    ]


def _fp(*names: str, arguments: dict[str, list[str]] | None = None) -> str:
    return procedure_fingerprint(
        task_type="analysis", tool_calls=_calls(*names, arguments=arguments)
    )


class TestTheShapeIsTheProcedure:
    def test_the_same_sequence_is_the_same_procedure(self) -> None:
        assert _fp("a", "b", "c") == _fp("a", "b", "c")

    def test_a_different_sequence_is_a_different_procedure(self) -> None:
        assert _fp("a", "b") != _fp("b", "a")

    def test_a_different_task_type_is_a_different_procedure(self) -> None:
        """The same tool calls under a different kind of work are not the same work."""
        left = procedure_fingerprint(task_type="analysis", tool_calls=_calls("a", "b"))
        right = procedure_fingerprint(task_type="coordination", tool_calls=_calls("a", "b"))
        assert left != right


class TestHowManyTimesIsNotTheShape:
    """The defect this file exists for."""

    def test_how_many_times_a_tool_was_called_is_not_the_shape(self) -> None:
        """Four real runs of one quarterly report, measured, all the same procedure.

        `search x18 -> write` and `search x21 -> write` are the same shape of work. If
        these hash differently the repetition gate can never open on live traffic,
        because a model's search count varies every run and the gate only counts
        *identical* fingerprints.
        """
        assert _fp(*(["safe_web_search"] * 18), "write_report") == _fp(
            *(["safe_web_search"] * 21), "write_report"
        )

    def test_the_real_sequences_from_the_live_run_now_collide(self) -> None:
        """The sequences actually observed, copied rather than invented.

        Three runs of one quarterly report on the same model, minutes apart. Two of
        them searched, wrote, searched again and wrote again; the third searched
        eighteen times and never wrote at all, and it stays distinct — a run that
        produced nothing is genuinely a different shape of work, and merging it in
        would be the opposite error.

        Before the fix these two were three different fingerprints, which is why the
        gate never opened.
        """
        wrote_then_revisited = _fp(
            *(["safe_web_search"] * 15),
            "write_report",
            *(["safe_web_search"] * 5),
            *(["write_report"] * 3),
        )
        wrote_twice_up_front = _fp(
            *(["safe_web_search"] * 15),
            "write_report",
            "write_report",
            *(["safe_web_search"] * 5),
            "write_report",
        )
        assert wrote_then_revisited == wrote_twice_up_front, (
            "two runs of the same procedure still fingerprint differently"
        )

        searched_and_never_wrote = _fp(*(["safe_web_search"] * 18))
        assert searched_and_never_wrote != wrote_then_revisited, (
            "a run that produced nothing was merged with one that produced a report"
        )

    def test_the_number_of_repetitions_is_still_visible_in_the_trace(self) -> None:
        """Collapsing the count for the fingerprint must not lose it.

        The reviewer is shown the sequence with counts intact, and the counts are what
        make a proposal about "searched 21 times then wrote it up" different from one
        about "searched twice and gave up". The fingerprint answers "same shape?";
        only the trace answers "how much?".
        """
        many = Procedure(
            task_type="analysis",
            steps=tuple(ToolStep(name="safe_web_search") for _ in range(21)),
        )
        assert many.tool_names.count("safe_web_search") == 21
        assert len(many.shape()) == 1


class TestOnlyConsecutiveRepeatsCollapse:
    def test_going_back_after_writing_is_a_different_shape(self) -> None:
        """`search, write, search` and `search, search, write` are different work.

        Collapsing *all* repeats rather than consecutive ones would merge these, and a
        lesson drawn from the merged bucket would be a lesson about a procedure that
        does not exist.
        """
        assert _fp("search", "write_report", "search") != _fp("search", "search", "write_report")

    def test_three_runs_of_the_same_work_all_collapse_to_one_step(self) -> None:
        steps = tuple(ToolStep(name="search") for _ in range(5))
        assert len(Procedure(task_type="t", steps=steps).shape()) == 1

    def test_alternating_steps_do_not_collapse_at_all(self) -> None:
        steps = (ToolStep(name="a"), ToolStep(name="b"), ToolStep(name="a"))
        assert len(Procedure(task_type="t", steps=steps).shape()) == 3


class TestArgumentKeysNotValues:
    def test_different_values_are_the_same_procedure(self) -> None:
        left = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[
                {
                    "sequence": 1,
                    "tool": "write_report",
                    "outcome": "success",
                    "arguments": {"title": "Q1", "body": "5 laptops"},
                }
            ],
        )
        right = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[
                {
                    "sequence": 1,
                    "tool": "write_report",
                    "outcome": "success",
                    "arguments": {"title": "Q2", "body": "6 laptops and a printer"},
                }
            ],
        )
        assert left == right

    def test_different_argument_names_are_a_different_procedure(self) -> None:
        left = _fp("write_report", arguments={"write_report": ["title", "body"]})
        right = _fp("write_report", arguments={"write_report": ["title"]})
        assert left != right

    def test_argument_key_order_does_not_matter(self) -> None:
        left = _fp("write_report", arguments={"write_report": ["body", "title"]})
        right = _fp("write_report", arguments={"write_report": ["title", "body"]})
        assert left == right


class TestRefusals:
    def test_a_refused_call_is_distinguishable_from_a_successful_one(self) -> None:
        """A lesson about "tried and was told no" is not a lesson about "did it"."""
        ok = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[{"sequence": 1, "tool": "write_report", "outcome": "success"}],
        )
        refused = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[{"sequence": 1, "tool": "write_report", "outcome": "failure"}],
        )
        assert ok != refused

    def test_a_blocked_call_is_also_distinguishable(self) -> None:
        """`blocked` is the outcome a policy refusal writes, not `failure`.

        Reading only `failure` marked every policy-blocked call as an ordinary
        success, which is the same class of mistake as reading an enum's spelling from
        memory: the trace said `blocked` and the fingerprint did not notice.
        """
        blocked = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[{"sequence": 1, "tool": "write_report", "outcome": "blocked"}],
        )
        ok = procedure_fingerprint(
            task_type="analysis",
            tool_calls=[{"sequence": 1, "tool": "write_report", "outcome": "success"}],
        )
        assert blocked != ok, "a policy-blocked call fingerprints the same as a successful one"


class TestTheVersionTag:
    def test_the_version_is_part_of_the_hashed_text(self) -> None:
        """So a change of rules cannot be mistaken for a change of procedure."""
        procedure = Procedure(task_type="analysis", steps=(ToolStep(name="a"),))
        assert procedure.as_text().startswith(FINGERPRINT_VERSION)

    def test_the_version_is_v2(self) -> None:
        """Pinned, because changing the shape of a fingerprint orphans every stored
        value. A silent bump would leave old rows looking like procedures that simply
        never repeat."""
        assert FINGERPRINT_VERSION == "proc-v2"


class TestRowsAreOrderedBySequenceNotByArrival:
    def test_out_of_order_rows_hash_the_same_as_ordered_ones(self) -> None:
        ordered = _calls("a", "b", "c")
        shuffled = [ordered[2], ordered[0], ordered[1]]
        assert procedure_fingerprint(
            task_type="analysis", tool_calls=ordered
        ) == procedure_fingerprint(task_type="analysis", tool_calls=shuffled)


class TestDegenerateInput:
    def test_no_tool_calls_is_a_procedure_with_no_steps(self) -> None:
        """A real state — a run that answered without touching a tool — and distinct
        from a run of a different task type."""
        empty = _fp()
        assert empty != procedure_fingerprint(task_type="coordination", tool_calls=[])

    def test_rows_with_no_tool_name_are_ignored(self) -> None:
        with_junk = [
            *_calls("a", "b"),
            {"sequence": 9, "tool": "", "outcome": "success", "arguments": {}},
        ]
        assert procedure_fingerprint(task_type="analysis", tool_calls=with_junk) == _fp("a", "b")

    def test_a_missing_outcome_is_treated_as_a_call_not_a_refusal(self) -> None:
        """Absent data must not silently become a refusal, which would make every
        under-instrumented run look like a policy fight."""
        assert same_procedure(
            procedure_fingerprint(task_type="analysis", tool_calls=[{"sequence": 1, "tool": "a"}]),
            _fp("a"),
        )
