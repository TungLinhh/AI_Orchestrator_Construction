"""The packet a human reads, and the hash their decision binds to.

Three things are asserted here that a summary would not give a reviewer: the diff
is a diff, the counter-examples are actually in the packet, and the hash covers
every field. The last one is the security property — an approval that binds to
anything less than the whole packet is an approval of a summary of it.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.application.approval_packet import (
    ApprovalPacket,
    CounterExample,
    EvidenceRun,
    build_packet,
    render_diff,
)
from ai_orchestrator.domain.learning import ChangeSet, ProcedureProposal

pytestmark = pytest.mark.unit

FINGERPRINT = "e" * 64
TASKS = tuple(f"tsk_01m3d5hwxet3x61vjc1ffjyrz{letter}" for letter in "abcdef")


def _runs(count: int = 3, outcome: str = "failed") -> tuple[EvidenceRun, ...]:
    return tuple(
        EvidenceRun(
            task_id=task,
            outcome=outcome,
            summary="the write was refused and the run produced nothing",
            tools=("write_report",),
            refusal_reason="ARTIFACT_NOT_WRITTEN" if outcome == "failed" else None,
        )
        for task in TASKS[:count]
    )


def _proposal(**change_overrides: object) -> ProcedureProposal:
    fields: dict[str, object] = {
        "kind": "patch",
        "target": "requisition",
        "old_string": "ask the requester for the supplier",
        "new_string": "ask for the cost centre first",
    }
    fields.update(change_overrides)
    change = ChangeSet(**fields)  # type: ignore[arg-type]
    return ProcedureProposal(
        procedure_fingerprint=FINGERPRINT,
        occurrences=3,
        evidence_task_ids=TASKS[:3],
        change=change,
        why="every run was reworked because the supplier question came first",
        falsifier="if reworks for this reason stop appearing, revert",
        proposed_by="generator:some-other-model",
    )


class TestTheDiff:
    def test_a_patch_renders_as_two_lines_a_reviewer_can_see(self) -> None:
        text = render_diff(_proposal())
        assert "-ask the requester for the supplier" in text
        assert "+ask for the cost centre first" in text

    def test_a_create_is_not_rendered_as_if_it_had_an_old_side(self) -> None:
        """A new procedure has no old text, and printing one would be a lie about
        what the change does."""
        text = render_diff(_proposal(kind="create", content="do these steps"))
        assert text.startswith("[create]")
        assert "--- a/" not in text


class TestThePacketHasEverything:
    def test_the_counter_examples_are_in_the_rendered_packet(self) -> None:
        """The runs that do not support the change. A packet with only supporting
        evidence is a sales pitch."""
        packet = build_packet(
            _proposal(),
            evidence=_runs(),
            counter_examples=(
                CounterExample(task_id=TASKS[0], kind="failed_run", detail="the write was refused"),
            ),
        )
        assert "Counter-examples" in packet.rendered
        assert "the write was refused" in packet.rendered

    def test_the_falsifier_is_in_the_packet(self) -> None:
        """It is the reason the change is worth approving at all."""
        packet = build_packet(_proposal(), evidence=_runs())
        assert "What would show this is wrong" in packet.rendered
        assert "revert" in packet.rendered

    def test_the_tools_are_shown_not_just_the_hash(self) -> None:
        """A hash tells you two runs match. It does not tell you what they are."""
        packet = build_packet(_proposal(), evidence=_runs())
        assert "write_report" in packet.rendered


class TestTheWarnings:
    def test_no_counter_examples_is_called_out(self) -> None:
        packet = build_packet(_proposal(), evidence=_runs())
        assert any("counter-example" in w for w in packet.warnings)

    def test_a_change_too_large_for_a_diff_is_called_out(self) -> None:
        packet = build_packet(_proposal(new_string="y" * 2_000), evidence=_runs())
        assert any("not reviewable as a diff" in w for w in packet.warnings)

    def test_the_proposer_being_the_subject_is_called_out(self) -> None:
        """A subject proposing its own correction is an echo, and the packet says so
        where a reviewer will see it."""
        proposal = ProcedureProposal(
            procedure_fingerprint=FINGERPRINT,
            occurrences=3,
            evidence_task_ids=TASKS[:3],
            change=_proposal().change,
            why="the order was wrong",
            falsifier="revert if it recurs",
            proposed_by="dots-studio/dots-3-note-preview:free",
        )
        packet = build_packet(
            proposal, evidence=_runs(), subject_model="dots-studio/dots-3-note-preview:free"
        )
        assert any("echo" in w for w in packet.warnings)

    def test_a_complete_packet_warns_about_nothing(self) -> None:
        packet = build_packet(
            _proposal(),
            evidence=_runs(),
            counter_examples=(CounterExample(TASKS[0], "failed_run", "refused"),),
            subject_model="a-different-model",
        )
        assert packet.warnings == ()


class TestTheHashCoversTheWholePacket:
    def _packet(self, **kwargs: object) -> ApprovalPacket:
        # `setdefault` rather than a fixed default: several tests below vary the
        # evidence, and a hard-coded default alongside a caller-supplied one is a
        # duplicate argument.
        kwargs.setdefault("evidence", _runs())
        return build_packet(_proposal(), **kwargs)  # type: ignore[arg-type]

    def test_the_same_packet_hashes_the_same(self) -> None:
        assert self._packet().packet_hash() == self._packet().packet_hash()

    def test_a_different_diff_hashes_differently(self) -> None:
        """The property that makes the approval mean something: changing the change
        invalidates the decision about it."""
        changed = build_packet(
            _proposal(new_string="ask the supplier first, then the cost centre"),
            evidence=_runs(),
        )
        assert changed.packet_hash() != self._packet().packet_hash()

    def test_a_different_falsifier_hashes_differently(self) -> None:
        base = self._packet()
        other = build_packet(
            ProcedureProposal(
                procedure_fingerprint=FINGERPRINT,
                occurrences=3,
                evidence_task_ids=TASKS[:3],
                change=_proposal().change,
                why=_proposal().why,
                falsifier="a completely different falsifier",
                proposed_by="generator:some-other-model",
            ),
            evidence=_runs(),
        )
        assert other.packet_hash() != base.packet_hash()

    def test_different_evidence_hashes_differently(self) -> None:
        assert self._packet().packet_hash() != self._packet(evidence=_runs(2)).packet_hash()

    def test_a_different_subject_hashes_differently(self) -> None:
        assert (
            self._packet().packet_hash() != self._packet(subject_model="someone-else").packet_hash()
        )

    def test_the_hash_does_not_depend_on_field_order(self) -> None:
        """A hash over insertion order would make the approval depend on Python's
        iteration order, which is not a security property."""
        packet = self._packet()
        shuffled = ApprovalPacket(
            proposal=packet.proposal,
            counter_examples=packet.counter_examples,
            evidence=packet.evidence,
            subject_model=packet.subject_model,
        )
        assert shuffled.packet_hash() == packet.packet_hash()
