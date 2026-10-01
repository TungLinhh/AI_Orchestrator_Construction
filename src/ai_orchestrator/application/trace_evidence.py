"""The runs behind a procedure, read from the trace that recorded them.

`SELF_IMPROVEMENT.md` §9. The review job takes an `EvidenceLookup` as a parameter
because the runs come from the audit trace and the job should not have to know how
to query it. That boundary only works if there is a real implementation of it, and
until this module existed the only one was a closure inside a test — which means
the production path was never exercised and the parameter was decoration.

Two things it has to get right, and both were wrong in the first draft:

**The subject model, honestly.** The question "which model did this work" has three
possible answers from the database: the one on the task's only call, the one on its
last call, and the one that answered *most often*. They differ, and picking the
last because it is easy produces a subject model that is whichever model happened to
be tried last. So the mode is taken, and a tie is a refusal rather than a coin flip —
a proposal whose subject is misnamed is a proposal about the wrong model.

**The tool sequence, in order.** A fingerprint says two runs match; it does not say
what they are. A reviewer needs the sequence, and it has to be the order the calls
were made, which is the audit sequence number.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.application.approval_packet import EvidenceRun
from ai_orchestrator.application.procedures import TOOL_INVOKE_ACTION
from ai_orchestrator.persistence.models import AuditLog, ModelUsage, Task

#: How many runs one proposal may cite. The gate requires three; a higher cap would
#: only ever produce a longer prompt and a reviewer with more to hold in their head.
MAX_EVIDENCE_RUNS = 12

#: The audit outcomes the platform actually writes, read from
#: `AuditLog.outcome` (whose column default is `success`) and the writers in
#: `task_execution`. Listed rather than treated as "anything else is a problem"
#: so a value added later shows up as unrecognised in a reviewer's packet instead
#: of being silently folded into one of these.
_CLEAN = "success"
_FAILED = "failure"
_BLOCKED = "blocked"


class TraceEvidenceLookup:
    """Reads the runs behind a fingerprint, and the model that produced them."""

    def __init__(self, session: AsyncSession, organization_id: str) -> None:
        self._session = session
        self._org = organization_id

    async def __call__(
        self, agent_id: str, procedure_fingerprint: str
    ) -> tuple[Sequence[EvidenceRun], str]:
        """The runs, oldest first, and the model that produced them.

        Oldest first on purpose. The evidence is a history, and a history read
        newest-first is a list ordered by how recently someone looked at it.
        """
        tasks = (
            (
                await self._session.execute(
                    sa.select(Task)
                    .where(
                        Task.organization_id == self._org,
                        Task.owner_agent_id == agent_id,
                        Task.procedure_fingerprint == procedure_fingerprint,
                    )
                    .order_by(Task.created_at)
                    .limit(MAX_EVIDENCE_RUNS)
                )
            )
            .scalars()
            .all()
        )
        if not tasks:
            return (), ""

        task_ids = [str(t.id) for t in tasks]
        traces, reasons = await self._traces(task_ids)
        models = await self._subject_model(task_ids)

        evidence = tuple(
            EvidenceRun(
                task_id=task_id,
                outcome=str(task.status),
                summary=_summary(task),
                tools=traces.get(task_id, ()),
                refusal_reason=reasons.get(task_id),
            )
            for task_id, task in zip(task_ids, tasks, strict=True)
        )
        return evidence, models

    async def _traces(
        self, task_ids: list[str]
    ) -> tuple[dict[str, tuple[str, ...]], dict[str, str]]:
        """The tool sequence per run, refusals included, and the first reason.

        The first version marked anything that was not `ok`, and the platform never
        writes `ok` — it writes `success`, `failure` or `blocked` (the column default
        is `success`). So every call in every run was marked as a refusal, and a
        reviewer would have been shown three runs of nine refused writes where
        nothing had been refused at all. Guessing an enum's spelling is the same
        mistake as guessing a session's liveness: the value has to be read.

        An outcome outside the known vocabulary is shown as unrecognised rather than
        folded into success, so a value added later is visible in the packet instead
        of quietly disappearing.
        """
        result = await self._session.execute(
            sa.select(
                AuditLog.task_id,
                AuditLog.resource_id,
                AuditLog.outcome,
                AuditLog.policy_reason,
            )
            .where(
                AuditLog.organization_id == self._org,
                AuditLog.task_id.in_(task_ids),
                AuditLog.action == TOOL_INVOKE_ACTION,
            )
            .order_by(AuditLog.sequence)
        )
        out: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        reasons: dict[str, str] = {}
        for row in result:
            task_id, tool, outcome = str(row.task_id), str(row.resource_id), str(row.outcome)
            # A refused call is part of the shape too. Leaving it out would make a
            # run where the platform stopped three writes look identical to one
            # where it stopped none, which is the opposite of what the reviewer
            # needs.
            if outcome == _CLEAN:
                out[task_id].append(tool)
                continue
            label = {_BLOCKED: "refused", _FAILED: "failed"}.get(
                outcome, f"unrecognised outcome {outcome!r}"
            )
            out[task_id].append(f"{tool}!{label}")
            # The reason, not the label: "the write was refused because the artifact
            # was not written" is something a reviewer can act on, and `refused` is
            # not.
            if row.policy_reason and task_id not in reasons:
                reasons[task_id] = str(row.policy_reason)
        return {task_id: tuple(tools) for task_id, tools in out.items()}, reasons

    async def _subject_model(self, task_ids: list[str]) -> str:
        """The model that answered most of these runs.

        Empty when no call recorded a model, and empty is the honest answer: the
        generator refuses to proceed without one rather than guessing, because
        "proposer is not the subject" cannot be checked against a blank.
        """
        rows = (
            await self._session.execute(
                sa.select(ModelUsage.model_used)
                .where(
                    ModelUsage.organization_id == self._org,
                    ModelUsage.task_id.in_(task_ids),
                )
                .order_by(ModelUsage.created_at)
            )
        ).scalars()
        counted = Counter(str(name) for name in rows if name)
        if not counted:
            return ""
        top = counted.most_common(2)
        # A tie is a refusal, not a coin flip: naming one of two equally-likely
        # models as "the" subject is a claim the data does not support, and a
        # proposal built on it is a proposal about the wrong model.
        if len(top) > 1 and top[0][1] == top[1][1]:
            return ""
        return top[0][0]

    async def proposed_fingerprints(self, agent_ids: Sequence[str]) -> set[str]:
        """Which of these agents' procedures already have a pending proposal.

        Read from the approval inbox rather than from a separate table, because the
        approval row *is* the record of what a human has been asked about. A second
        table tracking "what has been proposed" would be a second source of truth
        that can disagree with the first.
        """
        from ai_orchestrator.application.proposal_approval import ACTION_PROCEDURE_CHANGE
        from ai_orchestrator.persistence.models import Approval

        if not agent_ids:
            return set()
        result = await self._session.execute(
            sa.select(Approval.action_payload).where(
                Approval.organization_id == self._org,
                Approval.action_type == ACTION_PROCEDURE_CHANGE,
                Approval.status == "pending",
            )
        )
        return {
            str(row.action_payload.get("procedure", ""))
            for row in result
            if isinstance(row.action_payload, dict) and row.action_payload.get("procedure")
        }


def _summary(task: Task) -> str:
    """One line about the run, in the reviewer's terms.

    The goal rather than the title: the title is what somebody typed when creating
    the task, and the goal is what the agent was actually asked to do.
    """
    return str(task.goal or task.title or "")[:200]


__all__ = ["MAX_EVIDENCE_RUNS", "TraceEvidenceLookup"]
