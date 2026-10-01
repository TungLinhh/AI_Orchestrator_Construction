"""The review job: find procedures that repeated, and ask about them.

`SELF_IMPROVEMENT.md` §9 step 7. Deliberately the last piece, because it is the
easy one and the least valuable on its own: it decides *when* to look, not what to
change, and a scheduler that runs on a system that cannot yet change anything just
generates work.

Three properties, each of which is a way this could waste money:

**It is bounded.** A sweep over every agent in a large tenant is an unbounded
number of model calls. `max_agents` and `max_procedures_per_agent` are parameters
with defaults, and the default is small — a review that costs more than the work it
inspects has a negative return, and nobody notices until the bill arrives.

**It records what it decided not to do.** A procedure below the threshold is
skipped, and the skip is returned. A sweep that silently skips things looks exactly
like a sweep that found nothing.

**It is idempotent.** A procedure already proposed about in this cycle is skipped,
so running the job twice does not produce two proposals for the same runs. The
approval gate would catch the duplicate anyway — that is what gates are for — but a
duplicate that reaches a human is a duplicate a human has to read.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ai_orchestrator.application.approval_packet import ApprovalPacket, EvidenceRun
from ai_orchestrator.application.learning import ProposalGate
from ai_orchestrator.application.procedures import ProcedureReader
from ai_orchestrator.application.proposal_generation import ProposalGenerator
from ai_orchestrator.domain.learning import MIN_OCCURRENCES


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one sweep did, and what it chose not to do."""

    proposed: tuple[ApprovalPacket, ...] = ()
    skipped_below_threshold: tuple[tuple[str, str], ...] = ()
    already_proposed: tuple[str, ...] = ()
    no_lesson: tuple[tuple[str, str], ...] = ()
    refused_by_gate: tuple[tuple[str, str], ...] = ()
    agents_examined: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        return (
            f"{self.agents_examined} agent(s) examined: {len(self.proposed)} proposed, "
            f"{len(self.skipped_below_threshold)} below threshold, "
            f"{len(self.already_proposed)} already proposed about, "
            f"{len(self.no_lesson)} no lesson found, "
            f"{len(self.refused_by_gate)} refused by a gate"
        )


class ProcedureReviewJob:
    """Finds repeated procedures and asks about them. Bounded on purpose."""

    def __init__(
        self,
        *,
        reader: ProcedureReader,
        generator: ProposalGenerator,
        gate: ProposalGate,
        threshold: int = MIN_OCCURRENCES,
        max_agents: int = 5,
        max_procedures_per_agent: int = 2,
    ) -> None:
        self._reader = reader
        self._generator = generator
        self._gate = gate
        self._threshold = threshold
        self._max_agents = max_agents
        self._max_procedures = max_procedures_per_agent

    async def sweep(
        self,
        *,
        agent_ids: Sequence[str],
        already_proposed: Sequence[str] = (),
        evidence_for: EvidenceLookup | None = None,
    ) -> SweepResult:
        """One pass over the given agents.

        `evidence_for` supplies the runs behind a fingerprint. It is a parameter
        because the runs come from the audit trace, and the job should not have to
        know how to query it — but a job with no way to get evidence could only ever
        report counts, and a count is not a lesson.
        """
        seen_proposals = set(already_proposed)
        proposed: list[ApprovalPacket] = []
        below: list[tuple[str, str]] = []
        duplicates: list[str] = []
        no_lesson: list[tuple[str, str]] = []
        refused: list[tuple[str, str]] = []
        notes: list[str] = []
        examined = 0

        for agent_id in list(agent_ids)[: self._max_agents]:
            examined += 1
            shapes = await self._reader.seen_procedures(
                agent_id=agent_id, limit=self._max_procedures
            )
            for fingerprint, _count in shapes:
                if fingerprint in seen_proposals:
                    duplicates.append(fingerprint)
                    continue
                seen_proposals.add(fingerprint)

                count = await self._reader.repetition_count(
                    agent_id=agent_id, fingerprint=fingerprint
                )
                if count < self._threshold:
                    below.append((fingerprint, f"observed {count}, needs {self._threshold}"))
                    continue

                if evidence_for is None:
                    notes.append(
                        f"{fingerprint[:8]}: repeated {count} time(s) but no "
                        "evidence lookup was supplied, so it was not examined"
                    )
                    continue

                evidence, subject_model = await evidence_for(agent_id, fingerprint)
                if not evidence:
                    notes.append(f"{fingerprint[:8]}: repeated but no runs could be read")
                    continue

                outcome = await self._generator.generate(
                    agent_id=agent_id,
                    procedure_fingerprint=fingerprint,
                    evidence=evidence,
                    subject_model=subject_model,
                    organization_id=self._reader.organization_id,
                    gate=self._gate,
                    agent_id_of_subject=agent_id,
                )
                if outcome.packet is not None:
                    proposed.append(outcome.packet)
                elif outcome.rejections:
                    refused.append((fingerprint, outcome.reason))
                else:
                    no_lesson.append((fingerprint, outcome.reason))

        return SweepResult(
            proposed=tuple(proposed),
            skipped_below_threshold=tuple(below),
            already_proposed=tuple(duplicates),
            no_lesson=tuple(no_lesson),
            refused_by_gate=tuple(refused),
            agents_examined=examined,
            notes=tuple(notes),
        )


#: What `sweep` needs to know about a procedure: the runs behind it, and which
#: model did that work. A protocol rather than a callable so the job does not
#: depend on how the trace is queried — and so a caller can supply evidence from a
#: fixture in a test without a database.
class EvidenceLookup(Protocol):
    """How the review job obtains the runs behind a procedure."""

    async def __call__(
        self, agent_id: str, procedure_fingerprint: str
    ) -> tuple[Sequence[EvidenceRun], str]:
        """The runs, oldest first, and the model that produced them."""
        ...


__all__ = ["EvidenceLookup", "ProcedureReviewJob", "SweepResult"]
