"""What a proposed change looks like, and what it must carry to be considered.

`SELF_IMPROVEMENT.md` §9 step 4. The doc's reason for doing this before anything
generates a proposal is the right one: a shape decided in advance cannot be
quietly bent to fit whatever the generator produced, and every rule below is a rule
a generator cannot talk its way past.

Three rules carry the design, and each has been violated somewhere in this
project's history, which is why they are validators rather than comments.

**`falsifier` is mandatory.** A change that cannot say what would disprove it is
not a hypothesis. Shipping proposals without one is how a self-improving system
gets steadily worse with a clean audit trail — every step defensible, the direction
never questioned.

**`patch` is the default kind and a patch is two strings.** Hermes' reason is the
reason here: only the diff costs tokens, and a reviewer who skims a four-hundred
line rewrite approves things they did not read. A patch shows the two lines that
moved. `create` is the exception, for a procedure that does not exist yet.

**Evidence is a list of task ids, not prose.** "Three of four runs were reworked"
is a claim; the three ids are the claim. A proposal that cannot be checked against
the records is a proposal asking to be believed.

The scan for prompt injection and credential exfiltration is deliberately *not*
here — that is step 5, and it is a function someone else supplies. Its absence is
handled by refusing, in `application.learning`, because a gate that is missing
should stop traffic rather than wave it through.
"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProposalKind = Literal["patch", "create", "memory"]

#: Refuse anything below this. A change proposed from one observation is a change
#: proposed from a coincidence, and one coincidence is what a broken week looks
#: like. Three is Hermes' threshold and the reason for it is not "three is a nice
#: number": it is the smallest number at which a coincidence and a pattern are
#: distinguishable at all.
MIN_OCCURRENCES = 3

#: Evidence must be concrete. A proposal citing zero runs is a proposal with no
#: observation behind it, which is the thing the repetition gate exists to prevent.
MIN_EVIDENCE = 1


class ChangeSet(BaseModel):
    """The change itself.

    Three kinds, because a self-improving system can want three different things
    and conflating them is how an agent ends up rewriting its own policy:

    * `patch` — a change to something that already exists. `old_string` must be
      findable in the target, so a patch against text that has drifted is refused
      rather than silently applied to the wrong place.
    * `create` — a procedure that does not exist yet. `content` is the whole thing.
    * `memory` — a fact about this organisation, not a procedure. "This company
      approves purchases over $500 with the CFO" belongs in memory and must never
      be able to become a change to how agents behave.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProposalKind = "patch"
    #: For `patch`: the exact text to find. Must be non-empty, and short enough to
    #: be a human-sized change rather than a rewrite in disguise.
    old_string: str = ""
    #: For `patch`: what replaces it.
    new_string: str = ""
    #: For `create`: the procedure's body. For `memory`: the statement.
    content: str = ""
    #: Which procedure or skill this touches. For `create`, the name being created.
    target: str = ""

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        """A change must say which kind it is, and carry that kind's parts.

        A `model_validator` rather than several `field_validator`s, because the
        rules span fields: "both strings or neither" cannot be expressed one field
        at a time, and splitting it produces two half-valid states.
        """
        if self.kind == "patch":
            if not self.old_string.strip():
                msg = "a patch must name the text it replaces"
                raise ValueError(msg)
            if not self.new_string.strip():
                msg = (
                    "a patch must say what replaces it; an empty new_string is a "
                    "deletion, not a patch"
                )
                raise ValueError(msg)
            if self.old_string == self.new_string:
                msg = "a patch whose old and new text are identical changes nothing"
                raise ValueError(msg)
            if not self.target.strip():
                msg = "a patch must say what it patches"
                raise ValueError(msg)
        elif self.kind == "create":
            if not self.content.strip():
                msg = "a create must carry the content it creates"
                raise ValueError(msg)
            if not self.target.strip():
                msg = "a create must name what it creates"
                raise ValueError(msg)
        elif self.kind == "memory":
            if not self.content.strip():
                msg = "a memory must state the fact"
                raise ValueError(msg)
            if self.old_string or self.new_string:
                msg = "a memory is a statement, not a patch; use kind='patch' to change text"
                raise ValueError(msg)
        return self

    @property
    def is_readable_as_a_diff(self) -> bool:
        """Whether a human can review this in one glance.

        The diff gate in §5. A `create` of something long cannot be reviewed as a
        diff, and the honest response is to say so rather than to pretend the
        concept applies.
        """
        if self.kind != "patch":
            return False
        return len(self.old_string) + len(self.new_string) <= _REVIEWABLE_CHARS


#: Two hunks. Anything larger is a rewrite wearing a patch's clothes, and the whole
#: point of preferring patches is that a reviewer can see what moved.
_REVIEWABLE_CHARS = 2_000


class ProcedureProposal(BaseModel):
    """One proposed change, with the evidence and the falsifier that make it a
    hypothesis rather than a preference.

    Frozen and `extra="forbid"`, so a proposal cannot grow a field after the fact
    that a later reviewer would not know to check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The shape of work this is about. The hash, not a name: two runs with the
    #: same fingerprint are the same procedure, and that is the whole claim.
    procedure_fingerprint: str
    #: How many times it has happened, counted by the platform and not by the
    #: proposer. The gate compares this against `MIN_OCCURRENCES` itself rather
    #: than trusting the number written here.
    occurrences: int = 0
    #: Task ids. Checkable; "several recent runs" is not.
    evidence_task_ids: tuple[str, ...] = ()
    change: ChangeSet
    #: Why this change follows from those runs.
    why: str
    #: **What would show this change was wrong.** Mandatory.
    falsifier: str
    #: Which model proposed it. Recorded so a proposer can be compared with its own
    #: record later — the reason the platform insists a proposer is never the
    #: subject of its own proposal.
    proposed_by: str

    @field_validator("procedure_fingerprint")
    @classmethod
    def _fingerprint_shape(cls, value: str) -> str:
        """A fingerprint is a sha256 hex digest, and a typo in one is a silent
        mismatch: the proposal would be about a procedure that does not exist."""
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            msg = f"not a procedure fingerprint: {value!r}"
            raise ValueError(msg)
        return value

    @field_validator("falsifier", "why")
    @classmethod
    def _must_say_something(cls, value: str) -> str:
        """Neither field may be a placeholder.

        `"n/a"` and `"because"` are the shapes this actually takes, and both are
        worse than an empty string because they *look* answered.
        """
        stripped = value.strip()
        if not stripped:
            msg = "a proposal must say something here; an empty field is not an answer"
            raise ValueError(msg)
        if stripped.lower() in _PLACEHOLDERS:
            msg = f"this is a placeholder, not an answer: {stripped!r}"
            raise ValueError(msg)
        return stripped

    @field_validator("evidence_task_ids")
    @classmethod
    def _evidence_must_exist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < MIN_EVIDENCE:
            msg = f"a proposal must cite at least {MIN_EVIDENCE} run; none is not evidence"
            raise ValueError(msg)
        if any(not v.strip() for v in value):
            msg = "an evidence id is blank"
            raise ValueError(msg)
        # Sorted and deduped so two proposals about the same runs with the evidence
        # listed in a different order compare equal.
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _check_against_its_own_evidence(self) -> Self:
        """The occurrence count may not exceed the evidence.

        Not paranoia. A proposer that says "this happened nine times" and cites
        three runs is either wrong or lying, and both are the same problem: the
        number is the gate's input, so a number bigger than its own evidence is a
        number the gate must not read.
        """
        if self.occurrences < 0:
            msg = "occurrences cannot be negative"
            raise ValueError(msg)
        if self.occurrences > len(self.evidence_task_ids):
            msg = (
                f"claims {self.occurrences} occurrences but cites "
                f"{len(self.evidence_task_ids)} run(s); the count cannot exceed the evidence"
            )
            raise ValueError(msg)
        return self

    def cites(self, task_id: str) -> bool:
        return task_id in self.evidence_task_ids


#: The strings people write when they mean "I did not think about this". A
#: validator cannot tell a placeholder from a real answer, but it can tell these
#: three from either.
_PLACEHOLDERS = frozenset({"n/a", "na", "tbd", "todo", "because", "because.", "none", "-", "?"})


class ProposalRejection(BaseModel):
    """Why a proposal was not admitted. A reason, never a bare `False`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: str
    reason: str
    detail: dict[str, object] = Field(default_factory=dict)


__all__ = [
    "MIN_EVIDENCE",
    "MIN_OCCURRENCES",
    "ChangeSet",
    "ProcedureProposal",
    "ProposalKind",
    "ProposalRejection",
]
