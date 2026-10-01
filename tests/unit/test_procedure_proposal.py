"""A proposal must be falsifiable, evidenced, and reviewable — or it is not a proposal.

`SELF_IMPROVEMENT.md` §9 step 4 exists to make the shape explicit *before* anything
generates one, so every rule here is a rule a generator cannot talk its way past.
The tests are therefore mostly refusals, and each one is a way this has gone wrong
or would have:

* a `falsifier` of `"n/a"` — a field that looks answered and is not;
* an occurrence count larger than its own evidence — a number the gate would read;
* a patch whose old and new text are identical — a change that changes nothing;
* a `memory` carrying patch fields — a fact quietly becoming a behaviour change.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_orchestrator.domain.learning import (
    MIN_EVIDENCE,
    MIN_OCCURRENCES,
    ChangeSet,
    ProcedureProposal,
)

pytestmark = pytest.mark.unit

FINGERPRINT = "a" * 64
TASKS = ("tsk_01m3d5hwxet3x61vjc1ffjyrzk", "tsk_01m3d5hwxet3x61vjc1ffjyrzm")


def _patch(**overrides: object) -> ChangeSet:
    base: dict[str, object] = {
        "kind": "patch",
        "target": "purchase-requisition",
        "old_string": "Ask the requester for the supplier first.",
        "new_string": "Ask for the cost centre first.",
    }
    base.update(overrides)
    return ChangeSet(**base)  # type: ignore[arg-type]


def _proposal(**overrides: object) -> ProcedureProposal:
    base: dict[str, object] = {
        "procedure_fingerprint": FINGERPRINT,
        "occurrences": 2,
        "evidence_task_ids": TASKS,
        "change": _patch(),
        "why": "three of four runs were reworked because the supplier question came first",
        "falsifier": "if requisitions reworked for this reason stop appearing, revert",
        "proposed_by": "dots-studio/dots-3-note-preview:free",
    }
    base.update(overrides)
    return ProcedureProposal(**base)  # type: ignore[arg-type]


class TestTheFalsifierIsMandatory:
    def test_a_real_proposal_is_accepted(self) -> None:
        assert _proposal().falsifier

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_a_blank_falsifier_is_refused(self, blank: str) -> None:
        with pytest.raises(ValidationError, match="empty field is not an answer"):
            _proposal(falsifier=blank)

    @pytest.mark.parametrize("placeholder", ["n/a", "N/A", "tbd", "none", "because", "?"])
    def test_a_placeholder_falsifier_is_refused(self, placeholder: str) -> None:
        """Worse than blank, because it *looks* answered.

        A reviewer scanning a table of proposals sees a filled cell and moves on.
        """
        with pytest.raises(ValidationError, match="placeholder"):
            _proposal(falsifier=placeholder)

    def test_a_blank_why_is_refused_too(self) -> None:
        with pytest.raises(ValidationError, match="empty field is not an answer"):
            _proposal(why="")


class TestTheCountCannotOutrunItsEvidence:
    def test_a_count_matching_the_evidence_is_accepted(self) -> None:
        assert _proposal(occurrences=2).occurrences == 2

    def test_a_count_larger_than_the_evidence_is_refused(self) -> None:
        """The number is the gate's input, so a number bigger than its evidence is
        a number the gate must not read."""
        with pytest.raises(ValidationError, match="cannot exceed the evidence"):
            _proposal(occurrences=9)

    def test_no_evidence_at_all_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="at least 1 run"):
            _proposal(evidence_task_ids=(), occurrences=0)

    def test_evidence_must_cite_runs(self) -> None:
        assert MIN_EVIDENCE == 1

    def test_evidence_is_sorted_and_deduped(self) -> None:
        """Two proposals about the same runs must compare equal.

        Evidence is checked against the database; a list in a different order is
        the same evidence, and treating it as different would make a re-proposal
        look like new evidence.
        """
        shuffled = _proposal(evidence_task_ids=(TASKS[1], TASKS[0], TASKS[0]))
        assert shuffled.evidence_task_ids == tuple(sorted(TASKS))
        assert shuffled.cites(TASKS[0])


class TestAFingerprintIsAFingerprint:
    @pytest.mark.parametrize(
        "bad", ["", "abc", "A" * 64, "z" * 64, "a" * 63, "a" * 65, "not-a-hash"]
    )
    def test_a_malformed_fingerprint_is_refused(self, bad: str) -> None:
        """A typo in a fingerprint is a proposal about a procedure that does not exist."""
        with pytest.raises(ValidationError, match="not a procedure fingerprint"):
            _proposal(procedure_fingerprint=bad)


class TestPatchShape:
    def test_a_patch_needs_both_strings(self) -> None:
        with pytest.raises(ValidationError, match="must name the text it replaces"):
            _patch(old_string="")
        with pytest.raises(ValidationError, match="must say what replaces it"):
            _patch(new_string="")

    def test_a_patch_that_changes_nothing_is_refused(self) -> None:
        same = "Ask the requester for the supplier first."
        with pytest.raises(ValidationError, match="changes nothing"):
            _patch(old_string=same, new_string=same)

    def test_a_patch_must_say_what_it_patches(self) -> None:
        with pytest.raises(ValidationError, match="must say what it patches"):
            _patch(target="")

    def test_a_short_patch_is_reviewable(self) -> None:
        assert _patch().is_readable_as_a_diff

    def test_a_rewrite_disguised_as_a_patch_is_not_reviewable(self) -> None:
        """Only the diff costs tokens; a four-hundred-line "patch" is a rewrite with
        better manners, and the diff gate has to be able to say no."""
        big = _patch(old_string="x" * 1_500, new_string="y" * 1_500)
        assert not big.is_readable_as_a_diff


class TestCreateShape:
    def test_a_create_carries_its_content_and_a_name(self) -> None:
        change = ChangeSet(kind="create", target="vendor-onboarding", content="do these steps")
        assert change.content

    def test_a_create_with_no_content_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must carry the content"):
            ChangeSet(kind="create", target="vendor-onboarding", content="  ")

    def test_a_create_with_no_name_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="must name what it creates"):
            ChangeSet(kind="create", target="", content="do these steps")


class TestMemoryIsNotAChangeInDisguise:
    def test_a_memory_is_a_statement(self) -> None:
        change = ChangeSet(
            kind="memory", content="This company approves purchases over $500 with the CFO"
        )
        assert change.content

    def test_a_memory_carrying_patch_fields_is_refused(self) -> None:
        """A fact must never be able to become a change to how agents behave.

        The two are separated so a proposal about what the company *is* cannot be
        approved by whoever approves changes to how the company *behaves*.
        """
        with pytest.raises(ValidationError, match="not a patch"):
            ChangeSet(
                kind="memory",
                content="the CFO approves spend",
                old_string="a",
                new_string="b",
            )


class TestImmutability:
    def test_a_proposal_cannot_be_edited_after_construction(self) -> None:
        """It has to be reviewable as *this* thing.

        A proposal that can be edited in place after approval is a proposal nobody
        approved.
        """
        proposal = _proposal()
        with pytest.raises(ValidationError):
            proposal.occurrences = 99  # type: ignore[misc]

    def test_an_unknown_field_is_refused(self) -> None:
        """A field a reviewer would not know to check must not exist."""
        with pytest.raises(ValidationError):
            _proposal(approved_by="someone")  # type: ignore[call-arg]


class TestTheThresholdIsNamed:
    def test_three_is_the_threshold_and_it_is_a_constant(self) -> None:
        """Not a literal buried in a gate, so a proposal cannot be built against a
        threshold that does not exist."""
        assert MIN_OCCURRENCES == 3
