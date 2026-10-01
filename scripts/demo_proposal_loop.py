"""Does the system notice a repeated procedure, and can a human say yes to a change?

`SELF_IMPROVEMENT.md` §9 steps 6 and 7, plus the generator. This runs the whole loop
against the live database and the live model, and prints what actually happened at
each stage — including the stages where the right answer is "no".

    uv run python scripts/demo_proposal_loop.py --runs 3

The three things it exists to show, in order:

1. **The repetition gate opens on real traffic.** Everything before this was verified
   with a scripted runtime. Here three real runs of the same shape are executed, and
   the gate is asked whether that is a pattern or a coincidence.
2. **A proposal is written, with its diff, its evidence and its counter-examples.**
   Produced by a *different* model from the one whose behaviour it would change.
3. **A human's decision binds to the exact bytes they read.** Shown by editing the
   packet after approval and watching the platform refuse to publish it.

The last section deliberately breaks something. A demonstration that only ever shows
the happy path is a demonstration of nothing, because the interesting question is
what the platform refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.agent_runtime import PydanticAIRuntime
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
from ai_orchestrator.application.trace_evidence import TraceEvidenceLookup
from ai_orchestrator.approvals.service import ApprovalService
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.ids import OrganizationId
from ai_orchestrator.models.gateway import ModelGateway
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import build_providers_from_settings
from ai_orchestrator.persistence.models import Agent, ModelUsage, Organization, User
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database
from ai_orchestrator.telemetry.logging import configure_logging

GOAL = (
    "Lên báo cáo tổng hợp chi phí quý cho Marketing, gồm chi phí phần mềm, "
    "chi phí sự kiện và chi phí nhân sự. Cần số liệu từ cả ba phòng ban."
)


def _gateway() -> ModelGateway:
    gw = ModelGateway()
    for provider in build_providers_from_settings(get_settings()).values():
        gw.register_provider(provider)
    for profile in default_profiles().values():
        gw.register_profile(profile)
    return gw


def _rule(char: str = "-") -> str:
    return char * 72


def _wrap(text: str, *, indent: int = 6, width: int = 64) -> list[str]:
    """A reason, indented and wrapped.

    Gate reasons are written for a log, and a log line that runs off the edge of a
    terminal gets read as a different, shorter reason than the one that was recorded.
    """
    import textwrap

    return textwrap.wrap(
        text or "(no reason recorded)",
        width=width,
        initial_indent=" " * indent,
        subsequent_indent=" " * indent,
    )


async def main(runs: int, profile: str, agent_name: str) -> int:
    settings = get_settings()
    configure_logging(settings)
    db = Database.from_settings()

    async with db.session() as lookup:
        org = (
            await lookup.execute(
                select(Organization).where(Organization.slug == settings.seed_organization_slug)
            )
        ).scalar_one_or_none()
        if org is None:
            slug = settings.seed_organization_slug
            print(f"no organisation with slug {slug!r}; run `make seed --reset`")
            await db.dispose()
            return 1
        org_id = str(org.id)

    async with db.tenant_session(org_id) as session:
        agents = {str(a.name): str(a.id) for a in (await session.execute(select(Agent))).scalars()}

        # `--agent auto` picks the agent that has actually done something repeatedly,
        # which is the only one worth reviewing. Defaulting to a fixed name reports
        # "below threshold" about an agent with one run while a genuine three-fold
        # repeat sits in another agent's row, unreviewed — which reads as "no
        # lessons found" and is really "asked the wrong question".
        if agent_name == "auto":
            reader_now = ProcedureReader(session, org_id)
            best: tuple[int, str, str] | None = None
            for name, identifier in agents.items():
                for fingerprint, count in await reader_now.seen_procedures(
                    agent_id=identifier, limit=25
                ):
                    if best is None or count > best[0]:
                        best = (count, name, fingerprint)
            if best is None:
                print("no agent has left a trace yet; run with --runs 3 first")
                await db.dispose()
                return 1
            agent_name = best[1]
            print(f"picked agent : {agent_name}  (most repeated: {best[2][:12]}, {best[0]})")
        elif agent_name not in agents:
            print(f"no agent named {agent_name!r}; have: {', '.join(sorted(agents))}, or 'auto'")
            await db.dispose()
            return 1
        agent_id = agents[agent_name]

        print(f"organisation : {settings.seed_organization_slug}")
        print(f"agent        : {agent_name}")
        print(f"work profile : {profile}")
        print(f"runs         : {runs or 'none — reviewing the trace as it stands'}\n")

        # --- 1. real runs of the same shape -----------------------------
        if runs:
            print(_rule())
            print("1. RUN THE SAME SHAPE OF WORK, THROUGH THE REAL MODEL")
            print(_rule())

        tasks = TaskRepository(session, org_id)
        service = TaskExecutionService(session, org_id, runtime=PydanticAIRuntime())
        task_ids: list[str] = []
        for i in range(runs):
            task = await tasks.create(
                title=f"Quarterly cost report ({i + 1}/{runs})",
                goal=f"{GOAL}\n\n[run {uuid.uuid4().hex[:8]}]",
                task_type="analysis",
                requester_type="human",
            )
            await tasks.assign(task.id, agent_id)
            outcome = await service.execute_task(task.id, agent_id=agent_id)
            task_ids.append(str(task.id))
            used = (
                (
                    await session.execute(
                        select(ModelUsage.model_used)
                        .where(ModelUsage.task_id == str(task.id))
                        .order_by(ModelUsage.created_at)
                    )
                )
                .scalars()
                .all()
            )
            print(
                f"  run {i + 1}: {outcome.status.value:<10} "
                f"model={used[-1] if used else '(none recorded)'}"
            )
            # The reason, when there is one. A bare `failed` costs an hour: the turn
            # budget and a provider timeout look identical from the status alone, and
            # the first version of this script printed only the status — so the first
            # live run looked like a platform defect and was actually the platform
            # correctly stopping a model that had searched 33 times.
            if outcome.status.value != "completed" and outcome.summary:
                print(f"          {outcome.summary[:150]}")
        await session.flush()

        reader = ProcedureReader(session, org_id)

        # The procedure to review is the one this agent has done *most often*, not
        # the one the last run happened to produce. Taking the last run's fingerprint
        # reviews a procedure chosen at random, and with a free model that varies per
        # run it is usually a one-off — so the sweep would report "below threshold"
        # while a genuine three-fold repeat sat in the same table, unreviewed.
        seen = await reader.seen_procedures(agent_id=agent_id, limit=25)
        if not seen:
            print("\n  this agent has left no trace, so there is nothing to review.")
            await db.dispose()
            return 1
        fingerprint = max(seen, key=lambda pair: pair[1])[0]
        count = await reader.repetition_count(agent_id=agent_id, fingerprint=fingerprint)
        print(f"\n  procedure : {fingerprint[:12]}  (the most repeated of {len(seen)})")
        print(f"  repeated  : {count} time(s) before the one being recorded")
        if count + 1 >= 3:
            print("  the repetition gate is OPEN for this procedure")

        # --- 2. the sweep ------------------------------------------------
        print()
        print(_rule())
        print("2. THE REVIEW JOB LOOKS AT IT")
        print(_rule())

        lookup_evidence = TraceEvidenceLookup(session, org_id)
        job = ProcedureReviewJob(
            reader=reader,
            generator=ProposalGenerator(_gateway(), profile="proposer"),
            gate=ProposalGate(reader),
            threshold=3,
            max_agents=1,
            max_procedures_per_agent=1,
        )
        already = await lookup_evidence.proposed_fingerprints([agent_id])
        result = await job.sweep(
            agent_ids=[agent_id], already_proposed=sorted(already), evidence_for=lookup_evidence
        )
        print(f"  {result.summary()}")
        for fingerprint_short, why in result.skipped_below_threshold:
            print(f"    below threshold: {fingerprint_short[:8]}  {why}")
        for fingerprint_short, why in result.refused_by_gate:
            print(f"    refused by a gate: {fingerprint_short[:8]}")
            for line in _wrap(why, indent=6):
                print(line)
        for fingerprint_short, why in result.no_lesson:
            print(f"    no lesson found: {fingerprint_short[:8]}")
            for line in _wrap(why, indent=6):
                print(line)
        for note in result.notes:
            for line in _wrap(f"note: {note}", indent=4):
                print(line)

        if not result.proposed:
            print("\n  no proposal reached a human, and the reason is above.")
            if result.refused_by_gate:
                print("  That is a gate refusing, not a shortage of runs. The generator")
                print("  did ask a second model, and what came back did not survive.")
                print("  Adding runs will not change that; the proposal itself has to.")
            elif result.already_proposed:
                print("  This procedure is already sitting in someone's inbox. The sweep")
                print("  will not ask the same question twice.")
            else:
                print("  A lesson from too few runs is a coincidence, and the platform")
                print("  will not invent one. Re-run with --runs 3 or more.")
            await db.dispose()
            return 0

        packet = result.proposed[0]

        # --- 3. the packet ----------------------------------------------
        print()
        print(_rule())
        print("3. THE PACKET A HUMAN WOULD READ")
        print(_rule())
        print()
        print(packet.rendered)
        print()
        print(f"  packet hash : {packet.packet_hash()}")

        # --- 4. approval -------------------------------------------------
        print()
        print(_rule())
        print("4. A HUMAN APPROVES IT, AND THE DECISION BINDS TO THOSE BYTES")
        print(_rule())

        approvals = ApprovalService(session, org_id)
        submitted = await submit_for_approval(
            approvals,
            packet,
            organization_id=org_id,
            requested_by="procedure-review",
        )
        await session.flush()
        print(f"  approval id : {submitted.approval_id}")
        print(f"  bound to    : {submitted.packet_hash}")

        # A real seeded user, not an invented id. `decide` refuses a non-human
        # approver and refuses the requester approving their own request; the
        # `org_admin` role requirement is what the *inbox* filters on, so the honest
        # demonstration is to approve as somebody who exists in this tenant.
        admin = (
            (
                await session.execute(
                    select(User).where(User.organization_id == org_id).order_by(User.created_at)
                )
            )
            .scalars()
            .first()
        )
        if admin is None:
            print("  no user in this tenant to approve with; run `make seed --reset`")
            await db.dispose()
            return 1
        await approvals.decide(
            approval_id=submitted.approval_id,
            approver=Actor(
                id=str(admin.id),
                kind=ActorType.HUMAN,
                display_name=str(admin.display_name or admin.email),
                organization_id=OrganizationId(org_id),
                is_privileged_human=True,
            ),
            approve=True,
            note="read the diff, the evidence and the counter-examples",
        )
        await session.flush()

        row = await load_approved(approvals, submitted, packet)
        print(f"  decision    : {row.status}")
        print("  re-verified : the packet still hashes to what was approved")

        # --- 5. the part that matters -----------------------------------
        print()
        print(_rule())
        print("5. AND NOW EDIT IT AFTERWARDS")
        print(_rule())

        # `change=`, not a top-level `new_string`.
        #
        # `model_copy(update={...})` does not validate its keys: it sets whatever it is
        # given, and a key that is not a field is simply added. The first version of
        # this tamper did `update={"new_string": ...}` on the *proposal*, which has no
        # such field — so the edit landed on a new attribute, the packet was
        # unchanged, nothing was refused, and the demo printed
        # "refused: NO -- the approval was advisory, which is a bug" about a check
        # that had never been exercised. A demonstration that fails to demonstrate is
        # worse than no demonstration.
        changed_change = packet.proposal.change.model_copy(
            update={"new_string": packet.proposal.change.new_string + "\n# and quietly more"}
        )
        edited_proposal = packet.proposal.model_copy(update={"change": changed_change})
        assert edited_proposal.change.new_string != packet.proposal.change.new_string, (
            "the tamper did not change anything, so the check below proves nothing"
        )
        from ai_orchestrator.application.approval_packet import ApprovalPacket

        tampered = ApprovalPacket(
            proposal=edited_proposal,
            evidence=packet.evidence,
            counter_examples=packet.counter_examples,
            subject_model=packet.subject_model,
        )
        print(f"  original    : {packet.proposal.change.new_string!r}")
        print(f"  tampered    : {tampered.proposal.change.new_string!r}")
        try:
            await load_approved(approvals, submitted, tampered)
        except PacketChangedAfterApproval as exc:
            print(f"  refused     : {exc}")
        else:
            print("  refused     : NO -- the approval was advisory, which is a bug")
            await db.dispose()
            return 1

        print()
        print(_rule("="))
        print("The loop is closed: repetition -> proposal -> review -> hash-bound approval.")
        print("What is NOT closed: publishing the change, and reverting it if the falsifier")
        print("fires. Those are §9 steps 8 and 9, and they are not built.")
        print(_rule("="))
        await db.dispose()
        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs",
        type=int,
        default=0,
        help="run the work N more times first; 0 reviews the trace as it stands. "
        "Four runs of a free model take about sixteen minutes, and the thing under "
        "test here is the review job rather than the model.",
    )
    parser.add_argument("--profile", default="primary", help="profile the *work* uses")
    parser.add_argument(
        "--agent",
        default="auto",
        help="which agent to review; 'auto' picks the one with the most repeated "
        "procedure, which is the only one worth reviewing",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.runs, args.profile, args.agent)))
