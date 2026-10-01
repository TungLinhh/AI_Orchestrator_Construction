"""The approval packet: what a human is asked to read, and what their decision binds to.

`SELF_IMPROVEMENT.md` §5 gate 4 and 5, §9 step 6. A summary is not a review. What
a person needs in front of them is the *diff*, the runs that motivated it, and the
runs that did not — because the second list is the one that makes the first
believable, and a packet with only supporting evidence is a sales pitch.

Three things are assembled here, and each was left out of the first draft:

**The diff, rendered as one.** `ChangeSet` is two strings; a reviewer needs a
`-`/`+` pair they can read in one look. The renderer refuses anything that is not
reviewable as a diff, rather than producing a four-hundred-line block and calling it
a patch.

**The counter-examples.** The runs of the same shape that *succeeded*, or that were
refused, or that the change would make worse. Without them every proposal argues
only its own case, and a reviewer cannot tell a pattern from a coincidence.

**A hash of the whole thing.** The approval service already binds a decision to
`payload_hash`, and this packet is the payload. So a decision covers this diff and
nothing else: the packet cannot be edited after approval without the hash changing
and the decision ceasing to apply. That mechanism already existed; this supplies it
with something worth protecting.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from ai_orchestrator.domain.learning import ProcedureProposal


@dataclass(frozen=True, slots=True)
class EvidenceRun:
    """One run, in the terms a reviewer needs and nothing more.

    `outcome` and the refusal reason, because "it failed, and the platform said
    the write was refused" is actionable and "it failed" is not.
    """

    task_id: str
    outcome: str
    summary: str
    #: The tool sequence, so the reviewer can see the shape rather than trust the
    #: fingerprint. A hash tells you two runs match; it does not tell you what
    #: they *are*.
    tools: tuple[str, ...] = ()
    refusal_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CounterExample:
    """A run that does not support the change.

    Two kinds, and they are not the same. `clean_run` is the same shape of work
    that went fine — evidence the pattern is not universal. `refused` is the same
    shape where the platform stopped it, which is the strongest possible argument
    that the procedure is not actually working.
    """

    task_id: str
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class ApprovalPacket:
    """Everything a reviewer reads, and everything their decision covers."""

    proposal: ProcedureProposal
    evidence: tuple[EvidenceRun, ...] = ()
    counter_examples: tuple[CounterExample, ...] = ()
    #: The model that proposed this. Recorded separately from the model that did
    #: the work, so a reviewer can see whether the proposer is the same model
    #: judging its own behaviour.
    subject_model: str = ""
    #: Rendered once, hashed once, and the hash is what the approval binds to.
    rendered: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def payload(self) -> dict[str, object]:
        """The canonical payload the approval decision is bound to.

        Sorted and stringified, so the same packet always produces the same bytes.
        A hash over a dict's *insertion order* would make the approval depend on
        Python's iteration order, which is not a security property.
        """
        return {
            "procedure": self.proposal.procedure_fingerprint,
            "change": self.proposal.change.model_dump(mode="json"),
            "why": self.proposal.why,
            "falsifier": self.proposal.falsifier,
            "proposed_by": self.proposal.proposed_by,
            "subject_model": self.subject_model,
            "evidence": [
                {"task_id": r.task_id, "outcome": r.outcome, "tools": list(r.tools)}
                for r in self.evidence
            ],
            "counter_examples": [
                {"task_id": c.task_id, "kind": c.kind, "detail": c.detail}
                for c in self.counter_examples
            ],
        }

    def packet_hash(self) -> str:
        import hashlib

        canonical = json.dumps(self.payload(), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_diff(proposal: ProcedureProposal) -> str:
    """The change as a reviewer reads it.

    Refuses to render a `create` as a diff. A new procedure has no old text, and
    printing one as if it did would be a lie about what the change does — the
    whole reason patches are preferred over rewrites is that a diff can be checked.
    """
    change = proposal.change
    if change.kind != "patch":
        return f"[{change.kind}] {change.target}\n\n{change.content or '(no inline content)'}"
    return (
        f"--- a/{change.target}\n"
        f"+++ b/{change.target}\n"
        f"@@ procedure change @@\n"
        f"-{change.old_string}\n"
        f"+{change.new_string}\n"
    )


def build_packet(
    proposal: ProcedureProposal,
    *,
    evidence: Sequence[EvidenceRun] = (),
    counter_examples: Sequence[CounterExample] = (),
    subject_model: str = "",
) -> ApprovalPacket:
    """Assemble the packet, and say plainly what is wrong with it.

    The warnings are not decoration. A packet with no counter-examples, or a change
    too large to read as a diff, or a proposer that is also the subject, is a
    packet a reviewer should not be shown as though it were complete — and the
    reviewer is the last control this system has, so telling them what is missing
    is the platform's job rather than theirs to discover.
    """
    warnings: list[str] = []
    if not counter_examples:
        warnings.append(
            "no counter-examples: nothing here shows the change would not have "
            "made things worse, which is what the evidence is for"
        )
    if not proposal.change.is_readable_as_a_diff:
        warnings.append(
            "the change is not reviewable as a diff: it is too large to approve by "
            "reading two lines, and should be split"
        )
    if subject_model and subject_model == proposal.proposed_by:
        warnings.append(
            "the proposer is the model whose behaviour is being changed; a subject "
            "proposing its own correction is an echo, not a lesson"
        )
    if not evidence:
        warnings.append("no runs are attached, so the claim is unevidenced on its face")

    evidence_lines = [
        f"  {r.task_id}  {r.outcome:<10} {' > '.join(r.tools) or '(no tools)'}"
        + (f"  [{r.refusal_reason}]" if r.refusal_reason else "")
        for r in evidence
    ] or ["  (none)"]
    counter_lines = [f"  {c.task_id}  {c.kind}: {c.detail}" for c in counter_examples] or [
        "  (none)"
    ]

    rendered = "\n".join(
        [
            "# Proposed change",
            "",
            f"procedure : {proposal.procedure_fingerprint[:12]} "
            f"({proposal.occurrences} cited run(s))",
            f"proposed by: {proposal.proposed_by}",
            f"subject    : {subject_model or 'unknown'}",
            "",
            render_diff(proposal),
            "",
            "## Why",
            proposal.why,
            "",
            "## What would show this is wrong",
            proposal.falsifier,
            "",
            "## Evidence",
            *evidence_lines,
            "",
            "## Counter-examples",
            *counter_lines,
            "",
            *(
                ["## Read this before approving", *(f"  - {w}" for w in warnings)]
                if warnings
                else []
            ),
        ]
    )
    return ApprovalPacket(
        proposal=proposal,
        evidence=tuple(evidence),
        counter_examples=tuple(counter_examples),
        subject_model=subject_model,
        rendered=rendered,
        warnings=tuple(warnings),
    )


__all__ = [
    "ApprovalPacket",
    "CounterExample",
    "EvidenceRun",
    "build_packet",
    "render_diff",
]
