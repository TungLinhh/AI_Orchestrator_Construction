"""An agent that has just been approved learns from the run it was approved for.

The loop the platform was missing: a piece of work is requested, an agent does
it, a person approves the result, and then **the agent reads what it actually
did and writes down how to do it better next time**. Without that last step the
approval ends the story — the same task next month gets the same work, because
nothing about the run survives into the next run.

Two properties make this a learning loop rather than a memory dump:

* **The lesson is a `SkillVersion`, bound to the agent, not a note.** A
  `SkillVersion` row is something the runtime actually loads: it is read on
  every run of that agent. A lesson written anywhere else is a lesson nobody
  reads.
* **It is proposed, not applied.** The agent *writes the draft*; a person
  approves it. An agent that rewrote its own instructions after a run has a
  model that can talk itself into anything over several iterations, and the
  approval is the only thing standing between "learned from evidence" and
  "decided it was right". The proposal carries the evidence it was drawn from,
  and the evidence is the run log, not the agent's memory of the run.

The log is the input rather than a summary of it, and deliberately: a summary
written at the time is the agent's own account of what it did, which is exactly
the account being questioned. `audit_logs.context` holds the tool sequence and
the refusals as the platform recorded them, including the ones the agent would
rather not mention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ai_orchestrator.domain.errors import PlatformError

#: Tool outcomes worth reading when composing a lesson. A run that did the right
#: thing for a reason nobody wrote down is still worth learning from; a run that
#: only ever refused is not a procedure, it is a misconfiguration.
REFUSAL_OUTCOMES = frozenset({"refused", "denied", "failure"})

#: Ceiling on how much log one lesson is drawn from. A lesson built from two
#: hundred rows is a document, and a document is not something an agent applies
#: on every run. Three is enough to see a shape and few enough to read.
LESSON_EVIDENCE_LIMIT = 3


class SkillWriter(Protocol):
    """The one thing this module needs from persistence.

    A protocol so the learning rules can be tested against a stub that counts
    what was asked for, rather than against a database whose state has to be
    reconstructed to make the test mean anything.
    """

    async def existing_instructions(self, skill_id: str) -> str: ...

    async def next_version(self, skill_id: str) -> int: ...

    async def write_version(
        self,
        *,
        skill_id: str,
        version: int,
        instructions: str,
        derived_from: dict[str, Any],
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class RunLesson:
    """What one approved run has to say about how to do it better.

    Built from the log, so a field here is a fact about what the platform
    recorded rather than a recollection.
    """

    task_title: str
    #: The agent's own closing sentence — the "what happened" half.
    summary: str
    #: The tool names in the order they ran, which is the shape of the approach.
    tools_used: tuple[str, ...]
    #: Why the platform refused anything, if it did. These are the most
    #: instructive rows in the log and the ones an agent is least likely to
    #: volunteer.
    refusals: tuple[str, ...]
    #: How many times the run was attempted. One is the easy case.
    attempts: int

    def as_evidence(self) -> dict[str, Any]:
        """The evidence block attached to the proposal.

        Kept to what a reviewer needs to check the lesson: what was attempted,
        what was used, what was refused. A reviewer who cannot see these cannot
        tell a well-drawn lesson from a plausible one, and this file's entire
        claim is that the difference matters.
        """
        return {
            "task_title": self.task_title,
            "summary": self.summary,
            "tools_used": list(self.tools_used),
            "refusals": list(self.refusals),
            "attempts": self.attempts,
        }


def compose_lesson(lesson: RunLesson, previous: str) -> str:
    """The skill text: what worked, what to avoid, and what to do first.

    Composed by code rather than by a model, deliberately, and this is the one
    place in the learning loop that is *not* a model call. Two reasons, and the
    second is the important one:

    1. It has to run every time a task is approved, and a model call per
       approval is a bill with a latency nobody asked for.
    2. The text is assembled from rows the platform holds. A model asked to
       "write what it learned" from a log it is also being shown is a model
       asked to be persuasive. The refusals go in verbatim and unedited, which
       is the part that makes the lesson worth reviewing.

    The previous instructions are carried forward rather than replaced, because
    a lesson that deletes what the agent already knew is not a revision. The
    lesson is appended under its own heading so a reviewer can see exactly what
    was added and remove it if the evidence does not support it.
    """
    parts: list[str] = []
    if previous.strip():
        parts.append(previous.strip())
    parts.append("")
    parts.append(f"## From the approved run: {lesson.task_title}")
    parts.append("")
    if lesson.tools_used:
        parts.append(
            "This was completed using, in order: "
            + ", ".join(lesson.tools_used)
            + ". Start from that sequence rather than from scratch."
        )
    if lesson.refusals:
        parts.append("")
        parts.append(
            "The platform refused part of this work, and these are the recorded reasons, unchanged:"
        )
        parts.extend(f"- {reason}" for reason in lesson.refusals)
        parts.append(
            "Do not retry the same action expecting a different answer. Either the "
            "authority was missing or the request was wrong; the log does not say "
            "which, so ask before acting."
        )
    if lesson.attempts > 1:
        parts.append("")
        parts.append(
            f"This took {lesson.attempts} attempts. The first one did not finish, so "
            "check the earliest steps first rather than assuming the obvious approach "
            "works."
        )
    parts.append("")
    parts.append(
        f"Outcome: {lesson.summary.strip() or 'the run completed without a written summary'}"
    )
    return "\n".join(parts).strip() + "\n"


def lesson_is_worth_writing(lesson: RunLesson) -> tuple[bool, str]:
    """Whether this run has anything to teach, and why not if it does not.

    The refusals that matter are stated plainly: a "lesson" distilled from a run
    with no tools, no summary and no refusal is an instruction to continue doing
    whatever it did, which for a run that did nothing is an instruction to
    continue doing nothing.
    """
    if not lesson.tools_used and not lesson.refusals and not lesson.summary.strip():
        return False, (
            "the run recorded no tools, no summary and no refusals; there is "
            "nothing here to learn from"
        )
    if not lesson.tools_used and not lesson.refusals:
        # A refusal can only exist because a tool was invoked and the gateway
        # refused it, so this branch is normally "no tools at all". The
        # `refusals` half is here so a log that recorded a refusal without a
        # completed call still counts as a lesson -- the reason is the useful
        # part, and losing it would repeat the attempt.
        return False, (
            "the run used no tools and recorded no refusals, so there is no "
            "approach to improve; what it shows is a capability the agent does "
            "not have, not a procedure to revise"
        )
    return True, ""


def skill_id_for(agent_id: str, task_title: str) -> str:
    """A stable skill id, so a repeated task updates one skill rather than adding.

    Keyed on the agent and a normalised title. Two runs of the same work produce
    the same id, and the second is a new version of the first — which is what
    "improving" means, and what a random id would destroy by making every run a
    separate skill nobody will ever read twice.
    """
    import hashlib
    import re

    normalised = re.sub(r"[^a-z0-9 ]+", " ", task_title.lower()).strip()
    normalised = re.sub(r"\s+", " ", normalised)[:60]
    # **26 hex characters, not 16.** `SkillId` is validated to at least 30
    # characters, so a 20-character id is not a valid skill id at all: it is
    # refused the moment the learned skill is bound to an agent, which is to say
    # the learning loop could never publish anything it wrote. Found by binding
    # one, not by reading the constraint — the id looked fine in every table it
    # had been written to, and nothing validated it until the runtime tried to
    # load it.
    digest = hashlib.sha256(f"{agent_id}|{normalised}".encode()).hexdigest()[:26]
    return f"skl_{digest}"


def build_skill_instruction(
    task_title: str,
    tool_names: list[str],
    summary: str,
    refusals: list[str],
    attempts: int,
) -> str:
    """Read the log and turn it into a lesson. The single call site for this.

    Kept as a function so the rules above can be tested without a database or a
    model, and so the one place the ordering of the rules is decided is visible.
    """
    lesson = RunLesson(
        task_title=task_title,
        summary=summary,
        tools_used=tuple(tool_names),
        refusals=tuple(refusals),
        attempts=attempts,
    )
    return compose_lesson(lesson, previous="")


def require_gateway(profile: str) -> str:
    """Which model profile a proposal must come from.

    `primary`, and the reason is worth stating: a lesson written by a small free
    model is a draft for a person to read, and a person is reading it. Sending
    it to the strongest available model is spending money on a document whose
    whole value is that a human can check it against the log.
    """
    if not profile:
        msg = "a proposal needs a model profile; an empty one would be silently replaced"
        raise PlatformError(msg, details={"field": "profile"})
    return profile
