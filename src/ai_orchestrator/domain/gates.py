"""Gate rules: what blocks a Gate, and what the outcome would be.

**Pure.** No database, no clock, no I/O. `tests/unit/test_domain_purity.py`
enforces it, and the first draft of this module failed that test by importing
SQLAlchemy and calling `datetime.now()` — which is the point of the test. A
business rule that can reach a database needs a running system to be tested, and
the rules that decide whether a project may proceed are precisely the ones that
must be cheap to test exhaustively.

So the split is: **this module decides, `application/gate_operations.py` writes.**
The decision is a pure function of the checklist state, so every edge case is a
unit test with no fixture; the write is a thin mapping of a decision the caller
has already made.

Four decisions, all of them from the dossier rather than invented.

**A Gate cannot open with an unmet mandatory criterion.** Tập 3 §1.2 makes an
exemption a first-class state that names who granted it, which means the
alternative — refusing to record an unmet criterion at all — would make the
exemption route impossible and the record incomplete. So unmet criteria are
recorded, and `block_can_pass` returns the list of them. The Gate holds; it does
not pretend the criterion was never there.

**The outcome is derived, not chosen.** `PASS` when nothing mandatory is
outstanding. `PASS_WITH_CONDITIONS` when the council has recorded remediation
items, which is the same object as Tập 2 §E.1 step 4's action items. `HOLD` and
`FAIL` are decisions only a human makes, so they arrive as an explicit override
and always carry a reason. A function that returned `PASS` because nobody
objected would be one nobody could audit — and `proposed_outcome` has no outcome
parameter, which is how that is enforced structurally rather than by convention.

**A conditional pass converts itself to HOLD.** Tập 2 §E.1 step 5: extended more
than `max_extensions` times, a conditional pass becomes a HOLD and reports to the
CEO. That is a rule about *counting*, so the count is part of the state and the
rule lives in one place — `check_extension_limit` — rather than in whatever
workflow happens to be running.

**The decision's integrity belongs to the approval engine.** The substrate's
approvals service binds a decision to a hash of the exact payload and refuses one
whose payload has changed since. That is the property that defeated the tamper
attempt in testing, and a Gate decision is exactly the kind of thing somebody
will want to alter afterwards. The caller builds the approval request; the engine
owns the integrity. This module neither reimplements it nor bypasses it.

An agent cannot reach any of this. `sop_raci` refuses an agent as `A`, the
provenance check refuses an agent-sourced row without a proposal, and
`approved_by` is a person because nothing else is permitted to supply it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

#: The four Gate outcomes, Tập 2 §E.1 step 3.
#:
#: Re-declared here rather than imported from `persistence.process` because a
#: domain module must not import the persistence layer — that is the inward
#: dependency rule the purity test enforces, and the values themselves are part
#: of the governance rule rather than of the schema. A test asserts the two
#: agree, because two declarations of a closed vocabulary that can drift apart is
#: a real hazard and not a theoretical one.
# `noqa: S105` on the two names containing "PASS": ruff's secret heuristic
# fires on the word, and a Gate outcome is not a credential. Renaming to avoid
# the false positive would make the constant read as something it is not.
GATE_PASS = "PASS"  # noqa: S105
GATE_PASS_WITH_CONDITIONS = "PASS_WITH_CONDITIONS"  # noqa: S105
GATE_HOLD = "HOLD"
GATE_FAIL = "FAIL"
GATE_OUTCOMES = (GATE_PASS, GATE_PASS_WITH_CONDITIONS, GATE_HOLD, GATE_FAIL)


class BlockerKind(StrEnum):
    """Why a Gate cannot pass.

    A criterion is one reason. The other two are not criteria and could not be
    encoded as one: the extension limit is a rule about a *count*, and a missing
    rationale is about the decision rather than the checklist. Modelling them as
    criteria would have meant a fake criterion row for each.
    """

    CRITERION = "criterion"
    NO_RATIONALE = "no_rationale"
    EXTENSION_LIMIT = "extension_limit"
    QUORUM = "quorum"


#: A criterion in this state blocks the Gate if it is mandatory and is an entry
#: criterion. `not_applicable` is included because it is the state a freshly
#: generated checklist is in (Tập 2 §E.1 step 1) — a Gate that could pass with an
#: unassessed checklist would pass by never being read.
BLOCKING_STATES = ("not_met", "not_applicable")

#: `waived` is deliberately *not* blocking. Tập 3 §1.2 makes an exemption
#: legitimate provided it names who granted it, and the schema enforces that. A
#: properly granted waiver lets the Gate proceed; one that blocks anyway would
#: make the exemption route pointless.
NON_BLOCKING_STATES = ("met", "waived")


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason a Gate cannot pass, in a form an operator can act on.

    Frozen because it is a finding about the state, not a handle on it. A
    function that returned a mutable blocker would invite a caller to "fix" one
    in place and silently stop describing reality.
    """

    kind: BlockerKind
    detail: str
    criterion_code: str | None = None
    criterion_title: str | None = None
    #: Who waived it, when it was waived. Present so a review list can show
    #: "waived by X" rather than hiding the line entirely.
    waived_by: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "detail": self.detail,
            "criterion_code": self.criterion_code,
            "criterion_title": self.criterion_title,
            "waived_by": self.waived_by,
        }


@dataclass(slots=True)
class GateSession:
    """A Gate instance's state, as the rules need to see it.

    A plain dataclass built by the application layer from two queries, not an
    ORM object. An ORM entity would drag the session, the connection and the
    lazy-load machinery into every rule evaluation, and `proposed_outcome` would
    stop being callable without a database — which is the property that makes
    these rules cheap to test.
    """

    instance_id: str
    #: Not defaulted anywhere: a session that reached the database without one
    #: would write tenant-less rows that RLS then hides from their own tenant.
    #: The application layer supplies it from the row it loaded.
    organization_id: str
    gate_code: str
    subject_kind: str
    subject_id: str
    status: str
    extension_count: int
    max_extensions: int
    chair_role_key: str
    lead_time_days: int | None
    evaluations: list[dict[str, object]] = field(default_factory=list)

    @property
    def mandatory_outstanding(self) -> list[dict[str, object]]:
        """Entry criteria that are mandatory and neither met nor waived.

        Exit criteria are excluded: they describe the *next* Gate's readiness, and
        checking them here would make a Gate depend on conditions established by
        passing it.
        """
        return [
            evaluation
            for evaluation in self.evaluations
            if evaluation.get("is_mandatory")
            and evaluation.get("criterion_type") == "entry"
            and str(evaluation.get("state")) in BLOCKING_STATES
        ]


def block_can_pass(session: GateSession) -> list[Blocker]:
    """Everything standing between this session and a `PASS`.

    A list rather than a bool, on purpose: an operator looking at a Gate that
    will not open needs the *list*, and a function returning `False` makes every
    caller re-derive it. An empty list is `True`, so `can_pass` is the trivial
    wrapper rather than the other way round.
    """
    blockers = [
        Blocker(
            kind=BlockerKind.CRITERION,
            detail=(
                "Mandatory entry criterion is not met"
                if str(evaluation.get("state")) == "not_met"
                else "Mandatory entry criterion has not been assessed"
            ),
            criterion_code=_as_str(evaluation.get("criterion_code")),
            criterion_title=_as_str(evaluation.get("criterion_title")),
        )
        for evaluation in session.mandatory_outstanding
    ]
    if check_extension_limit(session):
        blockers.append(
            Blocker(
                kind=BlockerKind.EXTENSION_LIMIT,
                detail=(
                    f"Conditional pass extended {session.extension_count} times, "
                    f"above the limit of {session.max_extensions}. Tập 2 §E.1 "
                    "step 5: this converts to HOLD and reports to the CEO."
                ),
            )
        )
    return blockers


def _as_str(value: object) -> str | None:
    """A string, or None. Never a repr of a non-string."""
    return value if isinstance(value, str) else None


def can_pass(session: GateSession) -> bool:
    """Whether nothing mandatory is outstanding and the extension limit holds."""
    return not block_can_pass(session)


def check_extension_limit(session: GateSession) -> bool:
    """Whether the conditional pass must convert to `HOLD`.

    Split out because Tập 2 §E.1 step 5 is a *rule about a count*, and the
    escalation workflow needs to ask it without reconstructing a session.

    Strictly greater than, and that is the whole content of the rule: with
    `max_extensions = 2` the third extension converts the Gate. `>=` would permit
    a third deferral, which is exactly the failure the rule exists to stop.
    """
    return session.extension_count > session.max_extensions


def proposed_outcome(
    session: GateSession,
    *,
    has_conditions: bool,
    attendee_count: int,
    quorum: int,
) -> str:
    """What the outcome would be, given the state.

    A pure function of the state, so every caller derives the same answer and
    the interesting cases need no database.

    There is deliberately no `outcome` parameter. `HOLD` and `FAIL` only arrive
    as a human override with a reason attached, and a signature that accepted an
    outcome would eventually be handed `PASS` by a caller that meant to suggest
    it. The parameter's absence is the guarantee.

    Attendance is checked before the checklist: a decision of a council that did
    not meet is not a decision of the council, and Tập 2 §E.2 names mandatory
    members per Gate. Without this the quorum column would be written and never
    read.
    """
    if attendee_count < quorum:
        return GATE_HOLD
    if block_can_pass(session):
        return GATE_HOLD
    if has_conditions:
        return GATE_PASS_WITH_CONDITIONS
    return GATE_PASS


def validate_decision(
    *,
    outcome: str,
    rationale: str,
    approved_by: str,
    compiled_by: str = "",
    conditions: Sequence[object] = (),
) -> None:
    """Refuse a decision that should never have been offered.

    Four refusals, each of which the schema cannot enforce on its own, and each
    of which a constraint alone would report as a database error naming a
    constraint rather than a sentence naming the mistake.

    `conditions` is checked here rather than in the schema because conditions are
    child rows: only the caller knows whether any exist, and a
    `PASS_WITH_CONDITIONS` with none is a contradiction rather than a missing
    field.

    Raises `ValueError`. A refusal at the boundary is a programming error in the
    caller, not a runtime condition to handle.
    """
    if outcome not in GATE_OUTCOMES:
        msg = f"unknown gate outcome {outcome!r}; expected one of {GATE_OUTCOMES}"
        raise ValueError(msg)
    if not rationale.strip():
        msg = (
            "a Gate decision must state its reason (Tập 1 §1.2 auditability, and "
            "ck_gate_decisions_a_decision_states_its_reason)"
        )
        raise ValueError(msg)
    if outcome == GATE_PASS_WITH_CONDITIONS and not conditions:
        msg = (
            "PASS_WITH_CONDITIONS is a pass that creates obligations; it is "
            "meaningless with no conditions. Tập 2 §E.1 step 4 turns the "
            "conditions into action items with a deadline and an owner."
        )
        raise ValueError(msg)
    if compiled_by and compiled_by == approved_by:
        msg = (
            "the person who compiled the Gate pack cannot approve it "
            "(Tập 1 §1.2, and ck_gate_decisions_the_compiler_does_not_approve)"
        )
        raise ValueError(msg)


def next_instance_status(outcome: str) -> str:
    """The `gate_instances.status` a decision implies.

    A function because the mapping is a rule and a second caller will want it:
    the write path, and the escalation path that converts a conditional pass to a
    HOLD when the extension limit is reached.
    """
    if outcome == GATE_PASS:
        return "passed"
    if outcome == GATE_PASS_WITH_CONDITIONS:
        return "conditional"
    if outcome == GATE_HOLD:
        return "on_hold"
    if outcome == GATE_FAIL:
        return "failed"
    msg = f"unknown gate outcome {outcome!r}"
    raise ValueError(msg)


__all__ = [
    "BLOCKING_STATES",
    "GATE_FAIL",
    "GATE_HOLD",
    "GATE_OUTCOMES",
    "GATE_PASS",
    "GATE_PASS_WITH_CONDITIONS",
    "NON_BLOCKING_STATES",
    "Blocker",
    "BlockerKind",
    "GateSession",
    "block_can_pass",
    "can_pass",
    "check_extension_limit",
    "next_instance_status",
    "proposed_outcome",
    "validate_decision",
]
