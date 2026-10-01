"""The gates a proposal passes through, in order, and the one that stops everything.

`SELF_IMPROVEMENT.md` §5. This is gates 1 to 3:

1. **Repetition** — at least `MIN_OCCURRENCES`, counted by the platform from the
   database rather than read from the proposal. A proposer that writes its own
   score does not get to set its own threshold.
2. **Verification** — the runs behind it must have *worked*. A lesson extracted
   from a run that failed teaches the failure. A run that failed and was then
   corrected by a human is the one exception, and it is counted separately
   because "corrected by a human" is evidence about the agent and "failed" is
   evidence about the code.
3. **Danger scan** — prompt injection, credential exfiltration, hidden text.

**The scan is not implemented yet, and that is the point.** §9 lists it as step 5
and says it must exist before the first proposal. Rather than leave gate 3 as a
hole, the gate treats a missing scan as a *refusal*: `scan=None` refuses, and says
so. A gate that is absent must stop traffic. An earlier version of this project's
favourite pattern — a `getattr(x, "attr", True)` — defaulted the other way, and
reported twenty-one failures as twenty-one successes; `FAILED_APPROACHES.md` F42.

So the shape here is: step 4 makes the proposal typed and the gates executable, and
the one gate whose implementation is missing is the one that refuses. Step 5 then
supplies a scan function and nothing else changes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ai_orchestrator.application.procedures import ProcedureReader
from ai_orchestrator.application.quarantine import QuarantineSink
from ai_orchestrator.domain.learning import (
    MIN_OCCURRENCES,
    ProcedureProposal,
    ProposalRejection,
)
from ai_orchestrator.domain.scan import scan_proposal, verdict


class RunOutcome(StrEnum):
    """What happened to a run the proposal cites.

    The distinction between `FAILED` and `CORRECTED` is the one that matters: a
    failed run is evidence about the platform, and a run that failed and was then
    fixed by a person is evidence about the agent. Treating them the same would let
    a proposal be justified entirely by runs that broke.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    CORRECTED = "corrected"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Admitted, or a list of reasons. Never a bare boolean."""

    proposal: ProcedureProposal
    rejections: tuple[ProposalRejection, ...] = ()
    #: The platform's own count, not the proposal's. The proposer does not get to
    #: set this.
    observed_occurrences: int = 0

    @property
    def admitted(self) -> bool:
        return not self.rejections

    def reason(self) -> str:
        if self.admitted:
            return "admitted"
        return "; ".join(f"{r.gate}: {r.reason}" for r in self.rejections)


#: Retained for callers that want to run the scan without a session. The gate
#: itself no longer accepts a scan: a parameter a caller can pass is a parameter a
#: caller can pass wrongly, and a security check that can be skipped is not one.
Scan = Callable[[ProcedureProposal], str | None]


class ProposalGate:
    """Admits a proposal, or explains every gate it failed."""

    def __init__(
        self,
        reader: ProcedureReader,
        *,
        threshold: int = MIN_OCCURRENCES,
        run_outcomes: Callable[[str], RunOutcome] | None = None,
        quarantine: QuarantineSink | None = None,
    ) -> None:
        self._reader = reader
        self._threshold = threshold
        # How a cited run turned out. Defaults to `COMPLETED` so a caller that has
        # no record does not have to construct one — and defaults *optimistically*
        # on purpose, because a run the platform cannot classify is not a run it
        # can cite against a proposal, and inventing failures would make every
        # proposal unprovable.
        self._outcome_of = run_outcomes or (lambda _task_id: RunOutcome.COMPLETED)
        # Where a refusal is recorded. Optional so that a caller can evaluate a
        # proposal without persisting anything — but a caller that wants
        # quarantine to actually happen has to pass one, because the default is not
        # to write.
        self._quarantine = quarantine

    async def evaluate(
        self,
        proposal: ProcedureProposal,
        *,
        agent_id: str,
    ) -> GateResult:
        """Run every gate. All of them, even after one fails.

        A caller that stops at the first failure has to call again to find the rest,
        and a proposer that fixes one problem at a time is a slower way to reach the
        same place. The full list is also what a review packet needs.
        """
        rejections: list[ProposalRejection] = []

        observed = await self._reader.repetition_count(
            agent_id=agent_id,
            fingerprint=proposal.procedure_fingerprint,
            exclude_task_id=proposal.evidence_task_ids[0] if proposal.evidence_task_ids else None,
        )
        if observed < self._threshold:
            rejections.append(
                ProposalRejection(
                    gate="repetition",
                    reason=(
                        f"observed {observed} time(s), needs {self._threshold}; a change "
                        "proposed from fewer runs is proposed from a coincidence"
                    ),
                    detail={"observed": observed, "threshold": self._threshold},
                )
            )

        failed = [t for t in proposal.evidence_task_ids if self._outcome_of(t) is RunOutcome.FAILED]
        if failed:
            rejections.append(
                ProposalRejection(
                    gate="verification",
                    reason=(
                        f"{len(failed)} cited run(s) failed; a lesson drawn from a run "
                        "that broke teaches the failure"
                    ),
                    detail={"failed_runs": list(failed)},
                )
            )

        # Gate 3. Unconditional.
        #
        # It used to take a `scan` argument so a caller could supply one, and
        # `scan=None` refused. That was fail-closed and it was still a footgun: a
        # caller who passed a permissive lambda got a proposal admitted, and the
        # gate could not tell. There is no way to run this without a scan now, so
        # there is no state in which the check is skipped — which is a stronger
        # property than "skipped when absent", and one fewer thing to get wrong.
        findings = scan_proposal(proposal)
        if findings:
            if self._quarantine is not None:
                await self._quarantine.record(proposal, findings)
            rejections.append(
                ProposalRejection(
                    gate="danger_scan",
                    reason=verdict(findings) or "the danger scan found something",
                    detail={
                        "quarantined": self._quarantine is not None,
                        "findings": [f.as_row() for f in findings],
                    },
                )
            )

        return GateResult(
            proposal=proposal,
            rejections=tuple(rejections),
            observed_occurrences=observed,
        )


def evidence_from(task_ids: Sequence[str]) -> tuple[str, ...]:
    """The evidence list a proposer should use, in the form the type wants.

    Sorted and deduped, because a proposal's evidence is compared against the
    database and two lists in a different order are the same evidence.
    """
    return tuple(sorted({t for t in task_ids if t.strip()}))


__all__ = ["GateResult", "ProposalGate", "RunOutcome", "Scan", "evidence_from"]
