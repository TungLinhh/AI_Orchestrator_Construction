"""From a packet to an approval, and from an approval to a decision that means one thing.

`SELF_IMPROVEMENT.md` §5 gate 5. The approval service already binds a decision to a
hash of `action_payload`, and the packet is the payload. So an approval covers this
diff, this evidence and this falsifier — and nothing else. A proposal edited after
approval changes the hash, and the decision stops applying to it. That mechanism was
built for outbound email and it is exactly right here.

The check is done twice, deliberately:

* **on the way in**, so a decision is recorded against the bytes that were actually
  read, and
* **on the way out**, so publishing re-verifies rather than trusting that the row it
  is holding still describes what it described.

Skipping the second is how an approval system becomes advisory: the payload is
edited in place, the id is unchanged, and the decision quietly starts applying to
something nobody read. `ApprovalService.verify_payload` exists for this and is the
only thing that catches it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_orchestrator.application.approval_packet import ApprovalPacket
from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
from ai_orchestrator.domain.enums import (
    ActorType,
    DataClassification,
    EffectClass,
    RiskLevel,
)
from ai_orchestrator.persistence.models import Approval

#: The action type a proposed procedure change appears under in the approval inbox.
#: Distinct from any tool action, so a reviewer can filter the inbox to changes the
#: system is proposing to itself.
ACTION_PROCEDURE_CHANGE = "procedure.change"


@dataclass(frozen=True, slots=True)
class SubmittedApproval:
    approval_id: str
    packet_hash: str


async def submit_for_approval(
    service: ApprovalService,
    packet: ApprovalPacket,
    *,
    organization_id: str,
    requested_by: str,
    ttl_seconds: int = 86_400,
) -> SubmittedApproval:
    """Put a packet in front of a human, bound to its own bytes.

    A day rather than an hour, because a proposal is not a message to be answered
    promptly — it is a piece of work to be read. An approval that expires while
    somebody is thinking about it produces a second proposal rather than a decision,
    and the second one competes with the first in the inbox.

    `required_approver_roles` is left at its default, which is `org_admin`. A change
    to how agents behave is not something an ordinary operator should be able to
    approve, and making that explicit here means a future caller who changes it has
    to change it *here*, in the place that exists to be reviewed.
    """
    request = ApprovalRequest(
        organization_id=organization_id,
        action_type=ACTION_PROCEDURE_CHANGE,
        action_payload=packet.payload(),
        requested_by=requested_by,
        requested_by_type=ActorType.SYSTEM,
        # A proposal changes what agents do, so it is modelled as a mutation of
        # internal state at elevated risk. The taxonomy is the platform's own: this
        # is not an outbound message and not a read.
        effect_class=EffectClass.MUTATE_INTERNAL,
        risk_level=RiskLevel.HIGH,
        reason=_reason(packet),
        classification=DataClassification.INTERNAL,
        ttl_seconds=ttl_seconds,
    )
    row = await service.create(request)
    # The *packet's* hash, not the request's. They are two independent hashes over
    # the same content with different canonical forms — the request's adds
    # `action_type`, `task_id` and the organization — and recording one while
    # re-checking the other means every verification fails and the check is
    # indistinguishable from a system that rejects all approvals.
    #
    # The request's own hash is still doing its job: the approval row binds to the
    # action payload independently, so an edit to `action_payload` in the database
    # is caught by `verify_payload` even if nothing re-checks the packet.
    return SubmittedApproval(approval_id=row.id, packet_hash=packet.packet_hash())


async def verify_still_valid(
    service: ApprovalService, submitted: SubmittedApproval, packet: ApprovalPacket
) -> None:
    """Re-verify before publishing. The step that keeps an approval from being advisory.

    Raises if the packet no longer hashes to what was approved, which is the case
    where a human approved one thing and the system is about to do another.
    """
    if packet.packet_hash() != submitted.packet_hash:
        msg = (
            "the packet changed after it was approved: the decision covers the bytes "
            "that were read, and those are not the bytes that are about to be applied"
        )
        raise PacketChangedAfterApproval(msg)
    await service.verify_payload(submitted.approval_id, packet.payload())


class PacketChangedAfterApproval(RuntimeError):
    """A packet was edited between approval and publication.

    Its own exception type rather than a generic one, because the recovery is
    specific: re-approve, do not retry. A caller that catches this and publishes
    anyway has not fixed anything.
    """


def _reason(packet: ApprovalPacket) -> str:
    """The one-line reason the inbox shows. Short on purpose."""
    return (
        f"proposed change to procedure {packet.proposal.procedure_fingerprint[:8]}, "
        f"{len(packet.evidence)} run(s) of evidence, "
        f"{len(packet.counter_examples)} counter-example(s)"
    )


async def load_approved(
    service: ApprovalService, submitted: SubmittedApproval, packet: ApprovalPacket
) -> Approval:
    """Fetch the approval and re-check it against the packet, in one step.

    A missing row is an error rather than a `None`. An approval that has been
    deleted is not an approval, and a caller that treated "no row" as consent would
    make deletion the fastest way to publish an unreviewed change.
    """
    row = await service.get(submitted.approval_id)
    await verify_still_valid(service, submitted, packet)
    return row


__all__ = [
    "ACTION_PROCEDURE_CHANGE",
    "PacketChangedAfterApproval",
    "SubmittedApproval",
    "load_approved",
    "submit_for_approval",
    "verify_still_valid",
]
