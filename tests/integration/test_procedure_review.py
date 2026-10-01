"""The review job, and the approval it feeds.

The job's three properties are all about *not doing too much*: a sweep over every
agent in a large tenant is an unbounded number of model calls, a review that costs
more than the work it inspects has a negative return, and nobody notices until the
bill arrives. So the bounds are parameters with small defaults, and every skip is
recorded — a sweep that silently skips things looks exactly like one that found
nothing.

The approval half is one property: a decision covers the bytes that were read, and
nothing else. Everything in `test_approval_packet.py` about hashing exists so that
this holds.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.approval_packet import EvidenceRun
from ai_orchestrator.application.learning import ProposalGate
from ai_orchestrator.application.procedure_review import ProcedureReviewJob
from ai_orchestrator.application.procedures import ProcedureReader
from ai_orchestrator.application.proposal_approval import (
    PacketChangedAfterApproval,
    load_approved,
    submit_for_approval,
)
from ai_orchestrator.application.proposal_generation import ProposalGenerator
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.approvals.service import ApprovalService
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.models import Agent, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def seeded(tenant):
    org = (
        await tenant.session.execute(
            select(Organization).where(Organization.id == tenant.organization_id)
        )
    ).scalar_one()
    await seed(tenant.session, into=org)
    return tenant


class _ToolCallingRuntime(ScriptedRuntime):
    name = "tool-calling"

    async def execute(self, task, context, *, execute_tool=None, **kwargs):  # type: ignore[no-untyped-def]
        if execute_tool is not None:
            await execute_tool(tool_name="write_report", arguments={"title": "t", "body": "b"})
        return await super().execute(task, context, execute_tool=None, **kwargs)


async def _repeated(seeded, times: int) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]

    org_id = seeded.organization_id
    agent = (
        await seeded.session.execute(
            select(Agent).where(Agent.organization_id == org_id, Agent.name == "Sales Agent")
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, org_id)
    service = TaskExecutionService(seeded.session, org_id, runtime=_ToolCallingRuntime())
    ids: list[str] = []
    for i in range(times):
        task = await tasks.create(
            title="Repeat",
            goal=f"objective number {i}",
            task_type="analysis",
            requester_type="human",
        )
        await tasks.assign(task.id, agent.id)
        await service.execute_task(task.id, agent_id=agent.id)
        ids.append(str(task.id))
    return str(agent.id), ids


async def _evidence_lookup(seeded):  # type: ignore[no-untyped-def]
    """A lookup over the real trace, which is what the job would use in production."""

    async def lookup(agent_id: str, fingerprint: str):  # type: ignore[no-untyped-def]
        from ai_orchestrator.persistence.models import Task

        rows = (
            (
                await seeded.session.execute(
                    select(Task).where(
                        Task.organization_id == seeded.organization_id,
                        Task.owner_agent_id == agent_id,
                        Task.procedure_fingerprint == fingerprint,
                    )
                )
            )
            .scalars()
            .all()
        )
        evidence = tuple(
            EvidenceRun(
                task_id=str(r.id),
                outcome=str(r.status),
                summary=str(r.title or ""),
                tools=("write_report",),
            )
            for r in rows
        )
        return evidence, "scripted-1"

    return lookup


def _job(seeded, **overrides):  # type: ignore[no-untyped-def]
    from ai_orchestrator.models.gateway import ModelGateway

    reader = ProcedureReader(seeded.session, seeded.organization_id)
    defaults = {
        "reader": reader,
        "generator": ProposalGenerator(ModelGateway()),
        "gate": ProposalGate(reader),
        "threshold": 3,
        "max_agents": 2,
        "max_procedures_per_agent": 2,
    }
    defaults.update(overrides)
    return ProcedureReviewJob(**defaults)  # type: ignore[arg-type]


class TestTheJobIsBounded:
    async def test_it_examines_at_most_max_agents(self, seeded) -> None:
        agent_id, _ids = await _repeated(seeded, 4)
        job = _job(seeded, max_agents=1)
        result = await job.sweep(agent_ids=[agent_id, "agt_other", "agt_another"])
        assert result.agents_examined == 1, (
            f"examined {result.agents_examined} agents against a bound of 1"
        )

    async def test_every_skip_is_recorded(self, seeded) -> None:
        """A sweep that silently skips looks exactly like one that found nothing."""
        agent_id, _ids = await _repeated(seeded, 2)
        job = _job(seeded, threshold=3)
        result = await job.sweep(agent_ids=[agent_id], evidence_for=await _evidence_lookup(seeded))
        assert result.skipped_below_threshold, "a procedure below threshold was skipped silently"
        assert "needs 3" in result.skipped_below_threshold[0][1]

    async def test_the_summary_accounts_for_everything(self, seeded) -> None:
        agent_id, _ids = await _repeated(seeded, 2)
        job = _job(seeded, threshold=3)
        result = await job.sweep(agent_ids=[agent_id], evidence_for=await _evidence_lookup(seeded))
        text = result.summary()
        assert "1 agent(s) examined" in text
        assert "below threshold" in text


class TestTheJobIsIdempotent:
    async def test_a_procedure_already_proposed_about_is_skipped(self, seeded) -> None:
        agent_id, _ids = await _repeated(seeded, 4)
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        seen = await reader.seen_procedures(agent_id=agent_id)
        fingerprint = seen[0][0]
        job = _job(seeded)
        result = await job.sweep(agent_ids=[agent_id], already_proposed=[fingerprint])
        assert fingerprint[:8] in {f[:8] for f in result.already_proposed}

    async def test_running_twice_proposes_once(self, seeded) -> None:
        """The gate would catch a duplicate anyway — that is what gates are for —
        but a duplicate that reaches a human is one a human has to read."""
        agent_id, _ids = await _repeated(seeded, 4)
        job = _job(seeded)
        first = await job.sweep(agent_ids=[agent_id])
        reader = ProcedureReader(seeded.session, seeded.organization_id)
        seen = [f for f, _ in await reader.seen_procedures(agent_id=agent_id)]
        second = await job.sweep(agent_ids=[agent_id], already_proposed=seen)
        assert second.proposed == ()
        assert len(first.proposed) + len(second.proposed) <= 1


class TestTheApprovalBindsThePacket:
    async def _packet(self, seeded, **overrides):  # type: ignore[no-untyped-def]
        from ai_orchestrator.application.approval_packet import build_packet
        from ai_orchestrator.domain.learning import ChangeSet, ProcedureProposal

        fingerprint = "a" * 64
        cited = (
            "tsk_01m3d5hwxet3x61vjc1ffjyrzk",
            "tsk_01m3d5hwxet3x61vjc1ffjyrzm",
            "tsk_01m3d5hwxet3x61vjc1ffjyrzl",
        )
        proposal = ProcedureProposal(
            procedure_fingerprint=fingerprint,
            occurrences=3,
            evidence_task_ids=cited,
            change=ChangeSet(
                kind="patch",
                target="requisition",
                old_string="ask for the supplier",
                new_string="ask for the cost centre",
            ),
            why="every run was reworked",
            falsifier="revert if it stops",
            proposed_by="generator:another-model",
        )
        return build_packet(
            proposal,
            evidence=(
                EvidenceRun(
                    task_id=cited[0],
                    outcome="failed",
                    summary="the write was refused",
                    tools=("write_report",),
                    refusal_reason="ARTIFACT_NOT_WRITTEN",
                ),
            ),
            subject_model="a-model",
            **overrides,
        )

    async def test_an_approval_records_the_packet_hash(self, seeded) -> None:
        """The packet's own hash, so the re-check on the way out compares like with
        like.

        These are two different hashes over the same content — the approval
        service's adds `action_type`, `task_id` and the organization. Recording one
        and re-checking the other means every verification fails, which is
        indistinguishable from a platform that rejects every approval.
        """
        packet = await self._packet(seeded)
        service = ApprovalService(seeded.session, seeded.organization_id)
        submitted = await submit_for_approval(
            service,
            packet,
            organization_id=seeded.organization_id,
            requested_by="procedure-review",
        )
        await seeded.session.flush()
        assert submitted.packet_hash == packet.packet_hash()

    async def test_the_approval_row_binds_to_its_payload_independently(self, seeded) -> None:
        """A second, independent binding, so an edit in the database is caught too."""
        from sqlalchemy import select as _select

        from ai_orchestrator.persistence.models import Approval as ApprovalRow

        packet = await self._packet(seeded)
        service = ApprovalService(seeded.session, seeded.organization_id)
        submitted = await submit_for_approval(
            service,
            packet,
            organization_id=seeded.organization_id,
            requested_by="procedure-review",
        )
        await seeded.session.flush()
        row = (
            await seeded.session.execute(
                _select(ApprovalRow).where(ApprovalRow.id == submitted.approval_id)
            )
        ).scalar_one()
        # The row stores the payload and verifies against it, so the content is
        # recoverable and an edit is detectable.
        assert row.action_payload["falsifier"] == packet.proposal.falsifier
        with pytest.raises(Exception, match=r"(?i:hash|payload)"):
            await service.verify_payload(
                submitted.approval_id, {**packet.payload(), "why": "edited underneath"}
            )

    async def test_editing_the_packet_after_approval_invalidates_it(self, seeded) -> None:
        """The whole point. An approval of a summary of a change is not an approval
        of the change."""
        packet = await self._packet(seeded)
        service = ApprovalService(seeded.session, seeded.organization_id)
        submitted = await submit_for_approval(
            service,
            packet,
            organization_id=seeded.organization_id,
            requested_by="procedure-review",
        )
        from ai_orchestrator.application.approval_packet import ApprovalPacket

        # `model_copy` rather than `dataclasses.replace`: `ProcedureProposal` is a
        # frozen Pydantic model, and `replace` is for dataclasses.
        edited_proposal = packet.proposal.model_copy(
            update={"why": packet.proposal.why + " and also something else entirely"}
        )
        edited = ApprovalPacket(
            proposal=edited_proposal,
            evidence=packet.evidence,
            counter_examples=packet.counter_examples,
            subject_model=packet.subject_model,
        )
        with pytest.raises(PacketChangedAfterApproval, match="changed after it was approved"):
            await load_approved(service, submitted, edited)

    async def test_the_unedited_packet_still_verifies(self, seeded) -> None:
        packet = await self._packet(seeded)
        service = ApprovalService(seeded.session, seeded.organization_id)
        submitted = await submit_for_approval(
            service,
            packet,
            organization_id=seeded.organization_id,
            requested_by="procedure-review",
        )
        await seeded.session.flush()
        row = await load_approved(service, submitted, packet)
        assert row.action_type == "procedure.change"
        assert row.requested_by_type == str(ActorType.SYSTEM)
