"""Quarantine: the place a dangerous proposal goes, and the reason it is a dead end.

`SELF_IMPROVEMENT.md` §5 gate 3 and §8. The important property is negative: **the
proposed text is never written.** The row records what was proposed, by whom, and
what tripped the scan, and nothing else. That is the difference between this and
Hermes, which leaves a quarantined skill on disk and hides it from the index — a
model of the system that works, and a model an operator dashboard will eventually
surface.

The text is not lost, only not *here*. The cited runs are, and a proposal is a
function of the runs it came from, so a human who needs to see it can reconstruct
it. Making that a decision someone takes beats storing it where a query can reach
it.

A second property, and the one that is easy to get wrong: **quarantine is not the
only outcome, and the safe one is not the default.** `QuarantineSink.record` is
called only when the scan found something. A caller that never calls it has simply
not quarantined anything, which is different from having quarantined it quietly.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.learning import ProcedureProposal
from ai_orchestrator.domain.scan import Finding
from ai_orchestrator.persistence.models import QuarantinedProposal
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)


class QuarantineSink:
    """Writes a verdict, never a payload.

    Takes the session rather than opening its own, because a quarantine record
    belongs in the same transaction as the refusal it belongs to. A record
    committed separately from the refusal could survive the refusal being rolled
    back, or vanish with it, and either way the audit trail stops matching what
    happened.
    """

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def record(
        self, proposal: ProcedureProposal, findings: list[Finding]
    ) -> QuarantinedProposal:
        """Record a refusal. Called only when the scan found something.

        The `findings` argument is required rather than optional precisely so that
        a caller cannot record a quarantine with no reason: a quarantine with no
        stated reason is indistinguishable from a bug, and the next person to see
        it will assume the worst — that the scanner fired on nothing.
        """
        if not findings:
            msg = "a quarantine must carry at least one finding; an unexplained refusal is a bug"
            raise ValueError(msg)
        row = QuarantinedProposal(
            organization_id=self._org,
            procedure_fingerprint=proposal.procedure_fingerprint,
            proposed_by=proposal.proposed_by,
            findings=[f.as_row() for f in findings],
            evidence_task_ids=list(proposal.evidence_task_ids),
        )
        self._session.add(row)
        # Deliberately no flush. A flush from inside a refusal handler is the
        # nested-flush error that killed several live runs (F50), and this is called
        # from exactly that kind of place.
        logger.warning(
            "proposal.quarantined",
            procedure=proposal.procedure_fingerprint[:8],
            proposed_by=proposal.proposed_by,
            findings=len(findings),
            classes=sorted({str(f.danger) for f in findings}),
            fields=sorted({f.field for f in findings}),
            note="the proposed text was not stored; reconstruct it from the cited runs",
        )
        return row

    async def recent(self, limit: int = 50) -> list[QuarantinedProposal]:
        """For an operator: what has been quarantined, and why.

        The only read path, and it exists so an operator can see the refusals
        happening. It returns the rows as they are — verdicts, not proposals — so
        there is no path from here back to the text.
        """
        result = await self._session.execute(
            sa.select(QuarantinedProposal)
            .where(QuarantinedProposal.organization_id == self._org)
            .order_by(QuarantinedProposal.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


def explain(row: QuarantinedProposal) -> str:
    """A one-line summary for a log or a CLI.

    Built from the findings rather than from a stored summary column, so it cannot
    disagree with them.
    """
    findings = row.findings if isinstance(row.findings, list) else json.loads(row.findings or "[]")
    classes = sorted({str(f.get("danger")) for f in findings})
    fields = sorted({str(f.get("field")) for f in findings})
    return (
        f"{row.procedure_fingerprint[:8]} by {row.proposed_by}: "
        f"{len(findings)} finding(s) [{', '.join(classes)}] in {', '.join(fields)}"
    )


__all__ = ["QuarantineSink", "explain"]
