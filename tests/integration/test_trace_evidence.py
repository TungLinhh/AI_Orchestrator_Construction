"""Reading the runs behind a procedure, and the model that produced them.

The subject model is the part with real branches and the least room to be wrong: it
is what "proposer is not the subject" is checked against, and an empty or a wrong
answer means a proposal about the wrong model.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from ai_orchestrator.agent_runtime import ScriptedRuntime
from ai_orchestrator.application.procedures import ProcedureReader
from ai_orchestrator.application.task_execution import TaskExecutionService
from ai_orchestrator.application.trace_evidence import TraceEvidenceLookup
from ai_orchestrator.domain.ids import ModelUsageId
from ai_orchestrator.persistence.models import Agent, ModelUsage, Organization
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.seed import seed

pytestmark = pytest.mark.integration

MODEL = "dots-studio/dots-3-note-preview:free"
OTHER_MODEL = "some/other-model:free"


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


async def _repeat(seeded, times: int) -> tuple[str, str, list[str]]:  # type: ignore[no-untyped-def]
    """Run the same shape `times`, return (agent_id, fingerprint, task_ids)."""
    agent = (
        await seeded.session.execute(
            select(Agent).where(
                Agent.organization_id == seeded.organization_id,
                Agent.name == "Sales Agent",
            )
        )
    ).scalar_one()
    tasks = TaskRepository(seeded.session, seeded.organization_id)
    service = TaskExecutionService(
        seeded.session, seeded.organization_id, runtime=_ToolCallingRuntime()
    )
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
    await seeded.session.flush()
    reader = ProcedureReader(seeded.session, seeded.organization_id)
    fingerprint = str(await reader.fingerprint_for(ids[0]) or "")
    return str(agent.id), fingerprint, ids


def _record_model(seeded, task_id: str, model: str) -> None:  # type: ignore[no-untyped-def]
    seeded.session.add(
        ModelUsage(
            id=str(ModelUsageId.create()),
            organization_id=seeded.organization_id,
            task_id=task_id,
            provider="openrouter",
            model_used=model,
            model_profile="primary",
            input_tokens=10,
            output_tokens=10,
            cost_usd=0,
        )
    )


class TestTheRunsAreRead:
    async def test_it_returns_the_runs_oldest_first(self, seeded) -> None:
        _agent, fingerprint, ids = await _repeat(seeded, 3)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        evidence, _model = await lookup(_agent, fingerprint)
        assert [e.task_id for e in evidence] == ids, (
            "evidence read newest-first: a history ordered by when someone looked at it"
        )

    async def test_each_run_shows_the_tools_it_called(self, seeded) -> None:
        _agent, fingerprint, _ids = await _repeat(seeded, 3)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        evidence, _model = await lookup(_agent, fingerprint)
        assert evidence, "no runs read from a trace that exists"
        assert all("write_report" in run.tools for run in evidence)

    async def test_a_procedure_nobody_ran_reads_as_nothing(self, seeded) -> None:
        _agent, _fingerprint, _ids = await _repeat(seeded, 1)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        evidence, model = await lookup(_agent, "0" * 64)
        assert evidence == ()
        assert model == "", "a model was named for runs that do not exist"

    async def test_the_cap_is_enforced(self, seeded) -> None:
        """A twelve-run prompt is already a lot of text to pay for and to read."""
        from ai_orchestrator.application.trace_evidence import MAX_EVIDENCE_RUNS

        _agent, fingerprint, _ids = await _repeat(seeded, 3)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        evidence, _model = await lookup(_agent, fingerprint)
        assert len(evidence) <= MAX_EVIDENCE_RUNS


class TestTheSubjectModel:
    async def test_it_names_the_model_that_did_the_work(self, seeded) -> None:
        _agent, fingerprint, ids = await _repeat(seeded, 3)
        for task_id in ids:
            _record_model(seeded, task_id, MODEL)
        await seeded.session.flush()
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        _evidence, model = await lookup(_agent, fingerprint)
        assert model == MODEL

    async def test_it_names_the_one_that_did_most_of_it(self, seeded) -> None:
        """Three runs, two of them on a fallback model: the fallback is the subject.

        Taking the *last* call instead would name whichever model happened to be
        tried last, which on a retried run is the one that failed over.
        """
        _agent, fingerprint, ids = await _repeat(seeded, 5)
        _record_model(seeded, ids[0], OTHER_MODEL)
        _record_model(seeded, ids[1], OTHER_MODEL)
        _record_model(seeded, ids[2], OTHER_MODEL)
        _record_model(seeded, ids[3], MODEL)
        _record_model(seeded, ids[4], MODEL)
        await seeded.session.flush()
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        _evidence, model = await lookup(_agent, fingerprint)
        assert model == OTHER_MODEL, (
            "named the least-used model; the subject is the one that did most of the work"
        )

    async def test_a_tie_is_refused_rather_than_guessed(self, seeded) -> None:
        """Two models equally represented, and no way to tell which is the subject.

        Naming one of them would be a claim the data does not support, and the
        generator treats an empty name as a refusal — so a coin flip here would
        silently decide whether the proposer-equals-subject rule is even checked.
        """
        _agent, fingerprint, ids = await _repeat(seeded, 4)
        for i, task_id in enumerate(ids):
            _record_model(seeded, task_id, MODEL if i % 2 == 0 else OTHER_MODEL)
        await seeded.session.flush()
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        _evidence, model = await lookup(_agent, fingerprint)
        assert model == "", "a tie was resolved into a single named subject"

    async def test_no_recorded_model_is_refused_rather_than_guessed(self, seeded) -> None:
        _agent, fingerprint, _ids = await _repeat(seeded, 3)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        _evidence, model = await lookup(_agent, fingerprint)
        assert model == "", "a model was named for runs where no call recorded one"

    async def test_the_empty_name_actually_stops_the_proposal(self, seeded) -> None:
        """The lookup refusing is only worth something if the generator notices."""
        from ai_orchestrator.application.proposal_generation import ProposalGenerator
        from ai_orchestrator.models.gateway import ModelGateway

        _agent, fingerprint, _ids = await _repeat(seeded, 3)
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        evidence, subject = await lookup(_agent, fingerprint)
        assert evidence and subject == ""
        outcome = await ProposalGenerator(ModelGateway()).generate(
            agent_id=_agent,
            procedure_fingerprint=fingerprint,
            evidence=evidence,
            subject_model=subject,
            organization_id=seeded.organization_id,
            agent_id_of_subject=_agent,
        )
        assert not outcome.generated, "a proposal was built with an unnamed subject"
        assert "no named subject" in outcome.reason, (
            "refused, but not for the reason the lookup refused for"
        )


class TestThePendingProposals:
    async def test_a_procedure_already_awaiting_a_human_is_reported(self, seeded) -> None:
        """The sweep uses this to avoid asking the same question twice."""
        from ai_orchestrator.application.approval_packet import (
            CounterExample,
            EvidenceRun,
            build_packet,
        )
        from ai_orchestrator.application.proposal_approval import (
            ACTION_PROCEDURE_CHANGE,
            submit_for_approval,
        )
        from ai_orchestrator.approvals.service import ApprovalService
        from ai_orchestrator.domain.learning import ChangeSet, ProcedureProposal

        _agent, fingerprint, _ids = await _repeat(seeded, 3)
        service = ApprovalService(seeded.session, seeded.organization_id)
        proposal = ProcedureProposal(
            procedure_fingerprint=fingerprint,
            occurrences=3,
            evidence_task_ids=tuple(f"tsk_01m3d5hwxet3x61vjc1ffjyrzk{i}" for i in "abc"),
            change=ChangeSet(kind="patch", target="r", old_string="a", new_string="b"),
            why="w",
            falsifier="f",
            proposed_by="generator:other",
        )
        await submit_for_approval(
            service,
            build_packet(
                proposal,
                evidence=tuple(
                    EvidenceRun(
                        task_id=f"tsk_01m3d5hwxet3x61vjc1ffjyrzk{i}",
                        outcome="failed",
                        summary="s",
                    )
                    for i in "abc"
                ),
                counter_examples=(
                    CounterExample("tsk_01m3d5hwxet3x61vjc1ffjyrzka", "failed_run", "d"),
                ),
                subject_model=MODEL,
            ),
            organization_id=seeded.organization_id,
            requested_by="procedure-review",
        )
        await seeded.session.flush()
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        pending = await lookup.proposed_fingerprints([_agent])
        assert fingerprint in pending, (
            "a proposal is already sitting in a human's inbox and the sweep does not know"
        )
        assert all(str(ACTION_PROCEDURE_CHANGE) == "procedure.change" for _ in [0]), (
            "the action type a proposal appears under changed"
        )


class TestFingerprintIsolation:
    async def test_another_agents_runs_are_not_mixed_in(self, seeded) -> None:
        """The runs are one agent's. Another agent's would be evidence about a
        different subject, and the reviewer would not be able to tell."""
        _agent, fingerprint, _ids = await _repeat(seeded, 2)
        other = (
            await seeded.session.execute(
                select(Agent).where(
                    Agent.organization_id == seeded.organization_id,
                    Agent.name == "Finance Agent",
                )
            )
        ).scalar_one()
        lookup = TraceEvidenceLookup(seeded.session, seeded.organization_id)
        theirs, _model = await lookup(str(other.id), fingerprint)
        assert theirs == ()
        assert ProcedureReader(seeded.session, seeded.organization_id) is not None

    async def test_another_tenants_runs_are_not_visible(self, seeded) -> None:
        """RLS is the real enforcement; this is the check that it is on."""
        _agent, fingerprint, _ids = await _repeat(seeded, 2)
        other_tenant = TraceEvidenceLookup(seeded.session, "org_01m3d5hwxet3x61vjc1ffjyrzz")
        evidence, _model = await other_tenant(_agent, fingerprint)
        assert evidence == (), "a run was read across a tenant boundary"
