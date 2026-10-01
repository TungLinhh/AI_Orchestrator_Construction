"""A run that a person approved has to change how the next run is done.

The loop the platform was missing: request, work, approval, and then nothing.
The same task next month produced the same work, because nothing about the run
survived into the next one. The approval ended the story instead of being the
point at which the story improved.

What these tests hold in place:

* the lesson comes from the **log**, not from the agent's account of the run —
  which matters because the agent's account is the thing being questioned;
* it lands somewhere the runtime **reads** — a `SkillVersion` bound to the
  agent — because a lesson in any other table is a lesson nobody reads;
* it is a **proposal**, not an applied change, and the evidence travels with it
  so a reviewer can check the lesson rather than take it on trust;
* a run with nothing to teach says so, rather than producing an instruction to
  keep doing whatever it did.

The last one is the guard against a learning loop that manufactures confidence.
An agent that rewrites its instructions from every run eventually rewrites them
into whatever it already wanted to do, and the only thing that stops it is a
refusal to write a lesson when there is no lesson.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.application.skill_learning import (
    LESSON_EVIDENCE_LIMIT,
    RunLesson,
    compose_lesson,
    lesson_is_worth_writing,
    skill_id_for,
)


def _lesson(**overrides) -> RunLesson:  # type: ignore[no-untyped-def]
    base = {
        "task_title": "price the HVAC package",
        "summary": "quoted 12% over budget; split into two lots",
        "tools_used": ("delegate_to_agent", "write_report"),
        "refusals": (),
        "attempts": 1,
    }
    return RunLesson(**{**base, **overrides})


class TestTheLessonRecordsWhatHappened:
    def test_the_tool_sequence_is_kept_in_order(self) -> None:
        """The shape of the approach, not a hash of it.

        A fingerprint says two runs matched; it does not say what they were. A
        lesson that cannot name the sequence cannot teach it.
        """
        text = compose_lesson(_lesson(), previous="")
        assert text.index("delegate_to_agent") < text.index("write_report")

    def test_a_refusal_is_carried_verbatim(self) -> None:
        """The most instructive row in the log, and the one least likely offered.

        Quoted unchanged, so a reviewer can match it against the audit log. A
        paraphrased refusal is a refusal nobody can check.
        """
        reason = "tool 'send_award_email' has an external effect and simulation mode forbids it"
        text = compose_lesson(_lesson(refusals=(reason,)), previous="")
        assert reason in text, "the recorded reason must survive into the lesson unchanged"

    def test_a_refusal_tells_the_agent_what_not_to_retry(self) -> None:
        """Knowing what failed is only half of it; the instruction has to follow.

        A lesson that lists a refusal and stops is trivia. The useful form tells
        the agent what to do instead, and refuses to guess which of the two
        causes applied because the log genuinely does not say.
        """
        text = compose_lesson(_lesson(refusals=("authority missing",)), previous="")
        assert "Do not retry" in text

    def test_repeated_attempts_are_visible(self) -> None:
        """The first attempt did not work, and that is worth knowing up front.

        A run that took three attempts and a run that took one produce the same
        "outcome" line. Only one of them should tell the next run to check its
        earliest steps.
        """
        text = compose_lesson(_lesson(attempts=3), previous="")
        assert "3 attempts" in text
        assert "earliest steps" in text

    def test_a_first_time_success_does_not_invent_difficulty(self) -> None:
        """One attempt is not a success worth explaining away.

        Writing "this took 1 attempt, check your earliest steps" on every run
        would train the agent to distrust a method that worked.
        """
        text = compose_lesson(_lesson(attempts=1), previous="")
        assert "attempts" not in text


class TestTheLessonDoesNotEraseWhatWasKnown:
    def test_existing_instructions_are_carried_forward(self) -> None:
        """A lesson that replaces what the agent knew is not a revision.

        The previous instructions stay, and the lesson is appended under its own
        heading — so a reviewer sees exactly what was added and can remove it if
        the evidence does not hold.
        """
        previous = "Always check the tender register before pricing."
        text = compose_lesson(_lesson(), previous=previous)
        assert previous in text
        assert "## From the approved run:" in text, (
            "the lesson needs its own heading, or it cannot be reviewed or removed on its own"
        )


class TestNothingToLearnIsSaid:
    def test_a_run_with_no_tools_has_no_procedure_to_improve(self) -> None:
        """The important refusal.

        A run with no tools is not a procedure done badly; it is a capability
        the agent does not have. Writing a "lesson" from it produces an
        instruction to keep doing what it did, which is nothing.
        """
        ok, why = lesson_is_worth_writing(_lesson(tools_used=(), refusals=(), summary=""))
        assert not ok
        assert "no tools" in why

    def test_a_refused_tool_call_is_instructive(self) -> None:
        """A refused *tool call* is the most instructive row in the log.

        The agent tried something and the platform said no, with a reason. The
        lesson is that reason, and suppressing it would repeat the attempt.

        Note the shape: a refusal exists because a tool was invoked and the
        gateway refused it, so a run carrying refusals always has at least one
        tool. "A run with refusals but no tools" is not a case that occurs, and
        the rule below reads that way because it is checked on the count rather
        than on the flag — which is what makes it a guard rather than a
        description.
        """
        ok, why = lesson_is_worth_writing(_lesson(refusals=("no such department",), summary=""))
        assert ok, f"a recorded refusal is a lesson: {why}"

    def test_a_completed_run_with_a_summary_is_worth_recording(self) -> None:
        ok, _ = lesson_is_worth_writing(_lesson())
        assert ok

    def test_a_run_with_nothing_at_all_is_refused(self) -> None:
        ok, why = lesson_is_worth_writing(_lesson(tools_used=(), refusals=(), summary="   "))
        assert not ok
        assert why


class TestEvidenceIsAvailableForReview:
    def test_the_evidence_carries_what_a_reviewer_needs(self) -> None:
        """The whole claim is that a person can check the lesson.

        A reviewer who cannot see the sequence and the refusals is being asked
        to approve a claim on its own authority, which is the outcome this file
        exists to prevent.
        """
        evidence = _lesson(refusals=("one",), attempts=2).as_evidence()
        for key in ("task_title", "summary", "tools_used", "refusals", "attempts"):
            assert key in evidence, f"a reviewer cannot check the lesson without {key}"
        assert evidence["refusals"] == ["one"]

    def test_the_evidence_limit_is_small_enough_to_read(self) -> None:
        """A lesson drawn from hundreds of rows is a document, not a procedure.

        Three is enough to see a shape. The cap being small is the point: a
        lesson nobody reads does not improve anything.
        """
        assert LESSON_EVIDENCE_LIMIT <= 5


class TestTheSameWorkUpdatesOneSkill:
    def test_two_runs_of_the_same_task_share_a_skill(self) -> None:
        """Otherwise every run is a new skill nobody opens twice.

        "Improving" means a second version of the first thing, which is what
        makes the next run better rather than merely different.
        """
        a = skill_id_for("agt_01abc", "Price the HVAC package")
        b = skill_id_for("agt_01abc", "price the hvac package!")
        assert a == b, "the same work must land on the same skill, whatever the spelling"

    def test_different_agents_do_not_share_a_skill(self) -> None:
        """A lesson is a habit of one agent, not of the organisation.

        Merging them would let one agent's mistake become another's instruction,
        which is how a bad lesson spreads instead of being corrected.
        """
        assert skill_id_for("agt_01abc", "Price the HVAC") != skill_id_for(
            "agt_01xyz", "Price the HVAC"
        )

    def test_different_work_gets_different_skills(self) -> None:
        a = skill_id_for("agt_01abc", "Price the HVAC package")
        b = skill_id_for("agt_01abc", "Draft the interview score sheet")
        assert a != b


class TestTheComposedTextIsCheckable:
    def test_it_ends_with_the_outcome(self) -> None:
        """The last line is what happened, so a reader's eye lands on the fact."""
        text = compose_lesson(_lesson(), previous="")
        assert text.strip().splitlines()[-1].startswith("Outcome:")

    def test_a_run_with_no_summary_says_so_rather_than_leaving_a_gap(self) -> None:
        """An empty outcome line reads as a truncated document.

        Said plainly, it reads as what it is: the run finished and wrote nothing
        down, which is itself worth knowing.
        """
        text = compose_lesson(_lesson(summary=""), previous="")
        assert "without a written summary" in text


@pytest.mark.parametrize("tools", [(), ("a",), ("a", "b", "c", "d", "e")])
def test_composing_never_raises_on_any_input(tools: tuple[str, ...]) -> None:
    """The lesson path runs on every approval, so it cannot be allowed to raise.

    A raise here would be raised inside a decision the person already made, and
    the outcome would be a run that completed and then failed to be learned from
    — with the failure surfacing as a 500 on a button that worked.
    """
    text = compose_lesson(_lesson(tools_used=tools), previous="")
    assert text.strip()
