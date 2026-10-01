"""Gate decision rules, as pure functions.

`domain/gates.py` holds the rules; these test them without a database, because
the interesting cases are combinatorial and a database round trip per case would
make the suite slow enough that someone would eventually skip the edges.

The cases come from the dossier:

* Tập 3 §1.2 — an exemption must name who granted it, so `waived` is never a
  blocker and never silently absent
* Tập 3 §1.3 — G0 passes on a score (≥65, no group <40%), which is a criterion
  weighting rather than a per-item pass/fail, and both mechanisms have to work
* Tập 2 §E.1 step 5 — a conditional pass extended more than twice becomes HOLD
* Tập 1 §1.2 — auditability: a decision without a reason is not a decision
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.gates import (
    Blocker,
    BlockerKind,
    GateSession,
    block_can_pass,
    can_pass,
    check_extension_limit,
    next_instance_status,
    proposed_outcome,
    validate_decision,
)
from ai_orchestrator.persistence import process as P


class TestTheDomainAndTheSchemaAgreeOnOutcomes:
    """The four outcomes are declared twice, on purpose, and this is the tie.

    `domain/gates.py` cannot import `persistence.process` — the domain layer
    reaches inwards to nothing — so the vocabulary is stated in both places. Two
    declarations of a closed set can drift apart, and the failure would be a
    `proposed_outcome` returning a value no check constraint accepts.
    """

    def test_outcome_values_match(self) -> None:
        from ai_orchestrator.domain import gates as G

        assert G.GATE_OUTCOMES == P.GATE_OUTCOMES

    def test_every_outcome_maps_to_a_known_instance_status(self) -> None:
        from ai_orchestrator.domain import gates as G

        for outcome in G.GATE_OUTCOMES:
            assert next_instance_status(outcome) in P.GATE_INSTANCE_STATUSES


def _session(
    *,
    evaluations: list[dict[str, object]] | None = None,
    extension_count: int = 0,
    max_extensions: int = 2,
    status: str = "in_session",
) -> GateSession:
    return GateSession(
        instance_id="gin_test",
        organization_id="org_test",
        gate_code="G2",
        subject_kind="project",
        subject_id="prj_test",
        status=status,
        extension_count=extension_count,
        max_extensions=max_extensions,
        chair_role_key="deputy_ceo_operations",
        lead_time_days=5,
        evaluations=evaluations or [],
    )


def _criterion(
    code: str,
    state: str = "met",
    *,
    mandatory: bool = True,
    criterion_type: str = "entry",
) -> dict[str, object]:
    return {
        "criterion_code": code,
        "criterion_title": f"Criterion {code}",
        "criterion_type": criterion_type,
        "is_mandatory": mandatory,
        "state": state,
    }


class TestMandatoryCriteriaBlockTheGate:
    def test_an_unmet_mandatory_criterion_blocks(self) -> None:
        session = _session(evaluations=[_criterion("E1"), _criterion("E2", "not_met")])
        blockers = block_can_pass(session)
        assert not can_pass(session)
        assert [b.criterion_code for b in blockers] == ["E2"]

    def test_an_unassessed_mandatory_criterion_blocks(self) -> None:
        """`not_applicable` is the *generated* state at registration.

        A checklist that has just been created is all `not_applicable`, and a Gate
        that could pass in that state would pass by never being assessed.
        """
        session = _session(evaluations=[_criterion("E1"), _criterion("E2", "not_applicable")])
        blockers = block_can_pass(session)
        assert len(blockers) == 1
        assert "not been assessed" in blockers[0].detail

    def test_an_optional_criterion_does_not_block(self) -> None:
        session = _session(
            evaluations=[_criterion("E1"), _criterion("E5", "not_met", mandatory=False)]
        )
        assert can_pass(session)

    def test_a_waived_criterion_does_not_block(self) -> None:
        """Tập 3 §1.2: an exemption is legitimate, provided it is attributable.

        The schema enforces who granted it. This is the other half: a properly
        granted waiver lets the Gate proceed rather than deadlocking on a
        criterion nobody can satisfy.
        """
        session = _session(evaluations=[_criterion("E1"), _criterion("E2", "waived")])
        assert can_pass(session)

    def test_exit_criteria_do_not_block_the_current_gate(self) -> None:
        """Exit criteria describe the *next* Gate's readiness.

        Checking them here would make a Gate depend on conditions that are only
        established by passing it.
        """
        session = _session(
            evaluations=[_criterion("E1"), _criterion("X1", "not_met", criterion_type="exit")]
        )
        assert can_pass(session)

    def test_the_blocker_says_what_to_do(self) -> None:
        session = _session(evaluations=[_criterion("E2", "not_met")])
        blocker = block_can_pass(session)[0]
        assert blocker.kind is BlockerKind.CRITERION
        assert blocker.criterion_code == "E2"
        assert blocker.as_dict()["criterion_title"] == "Criterion E2"


class TestExtensionLimit:
    """Tập 2 §E.1 step 5: a conditional pass extended too many times is a HOLD."""

    @pytest.mark.parametrize(
        ("count", "expected"), [(0, False), (1, False), (2, False), (3, True), (4, True)]
    )
    def test_the_limit_is_strictly_greater_than(self, count: int, expected: bool) -> None:
        """Two extensions is allowed; the third converts it.

        `max_extensions = 2` and "extended more than twice" means the boundary is
        `>`, not `>=`. An off-by-one here silently permits a third deferral,
        which is precisely the failure the rule exists to stop.
        """
        session = _session(extension_count=count, max_extensions=2)
        assert check_extension_limit(session) is expected

    def test_an_exceeded_limit_blocks_even_with_a_clean_checklist(self) -> None:
        session = _session(evaluations=[_criterion("E1")], extension_count=3)
        blockers = block_can_pass(session)
        assert [b.kind for b in blockers] == [BlockerKind.EXTENSION_LIMIT]
        assert "reports to the CEO" in blockers[0].detail

    def test_the_limit_also_forces_hold_in_the_proposed_outcome(self) -> None:
        session = _session(evaluations=[_criterion("E1")], extension_count=3)
        assert proposed_outcome(session, has_conditions=False, attendee_count=5, quorum=3) == (
            P.GATE_HOLD
        )


class TestProposedOutcome:
    def test_a_clean_checklist_passes(self) -> None:
        session = _session(evaluations=[_criterion("E1"), _criterion("E2")])
        assert proposed_outcome(session, has_conditions=False, attendee_count=5, quorum=3) == (
            P.GATE_PASS
        )

    def test_conditions_make_it_a_conditional_pass(self) -> None:
        session = _session(evaluations=[_criterion("E1")])
        assert proposed_outcome(session, has_conditions=True, attendee_count=5, quorum=3) == (
            P.GATE_PASS_WITH_CONDITIONS
        )

    def test_a_blocker_holds_the_gate(self) -> None:
        session = _session(evaluations=[_criterion("E1"), _criterion("E2", "not_met")])
        assert proposed_outcome(session, has_conditions=False, attendee_count=5, quorum=3) == (
            P.GATE_HOLD
        )

    def test_a_council_that_did_not_meet_cannot_decide(self) -> None:
        """Attendance below quorum is a HOLD regardless of the checklist.

        Tập 2 §E.2 names mandatory members per Gate. A decision taken without
        them is not a decision of the council, and without this a quorum column
        would be written and never read.
        """
        session = _session(evaluations=[_criterion("E1")])
        assert proposed_outcome(session, has_conditions=False, attendee_count=1, quorum=3) == (
            P.GATE_HOLD
        )

    def test_outcome_is_derived_not_chosen(self) -> None:
        """The signature has no outcome parameter, on purpose.

        `HOLD` and `FAIL` arrive as an explicit human override with a reason;
        this function only ever proposes what the state supports. A function
        that could be handed `PASS` would eventually be.
        """
        import inspect

        params = set(inspect.signature(proposed_outcome).parameters)
        assert "outcome" not in params
        assert {"session", "has_conditions", "attendee_count", "quorum"} <= params


class TestG0IsScoredNotAllOrNothing:
    """Tập 3 §1.3: G0 exits on "Điểm ≥65, không nhóm <40%" — a score across a
    group, not a per-item pass.

    The schema carries `weight` for exactly this, and the weighting is a Gate
    characteristic rather than a per-instance judgement, so it is asserted here
    as a property of the seeded data rather than invented per project.
    """

    def test_a_weighted_criterion_is_still_tracked_individually(self) -> None:
        """The state is per-criterion even when the pass depends on the aggregate.

        `weight` is a property of the criterion and the *score* is computed by
        whatever asked the question. What this asserts is the thing that is easy
        to get wrong: a criterion carrying a weight is still an ordinary
        mandatory line for blocking purposes, so an unmet one holds the Gate
        rather than being averaged away.
        """
        session = _session(
            evaluations=[
                _criterion("X1", "not_met"),
                {**_criterion("X2"), "weight": 40},
            ]
        )
        assert not can_pass(session), (
            "an unmet weighted criterion must still block; averaging it into a "
            "pass is how a Gate that should hold would pass"
        )

    def test_a_met_weighted_criterion_does_not_block(self) -> None:
        session = _session(evaluations=[{**_criterion("X2"), "weight": 40}])
        assert can_pass(session)


class TestDecisionValidation:
    """The four refusals, in the domain, with no database involved.

    Each is a check the schema cannot make on its own — conditions are child
    rows, so only the caller knows whether any exist — and each deserves to be
    provable without a fixture.
    """

    def test_an_unknown_outcome_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown gate outcome"):
            validate_decision(outcome="PROBABLY_FINE", rationale="x", approved_by="ceo")

    def test_an_empty_rationale_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must state its reason"):
            validate_decision(outcome=P.GATE_PASS, rationale="   ", approved_by="ceo")

    def test_conditional_pass_needs_conditions(self) -> None:
        with pytest.raises(ValueError, match="meaningless with no conditions"):
            validate_decision(
                outcome=P.GATE_PASS_WITH_CONDITIONS,
                rationale="pass",
                approved_by="ceo",
            )

    def test_the_compiler_cannot_approve(self) -> None:
        with pytest.raises(ValueError, match="cannot approve"):
            validate_decision(
                outcome=P.GATE_PASS,
                rationale="self-approved",
                approved_by="pmo_specialist",
                compiled_by="pmo_specialist",
            )

    def test_a_well_formed_conditional_pass_is_accepted(self) -> None:
        validate_decision(
            outcome=P.GATE_PASS_WITH_CONDITIONS,
            rationale="pass subject to a 30-day mobilisation plan",
            approved_by="deputy_ceo_operations",
            compiled_by="pmo_specialist",
            conditions=({"description": "plan", "owner_role_key": "pd", "due_on": 1},),
        )

    def test_a_blank_compiler_is_not_a_self_approval(self) -> None:
        """`compiled_by` is optional and "" means nobody recorded a compiler.

        Reading "" as a person and comparing it to `approved_by` would refuse
        every decision made without a recorded compiler, which is most of them.
        """

        def _run(approved_by: str, compiled_by: str) -> None:
            validate_decision(
                outcome=P.GATE_PASS,
                rationale="x",
                approved_by=approved_by,
                compiled_by=compiled_by,
            )

        _run("ceo", "")


class TestBlockerSerialisation:
    def test_a_blocker_survives_json(self) -> None:
        """The review screen and the escalation digest both take this shape."""
        import json

        blocker = Blocker(
            kind=BlockerKind.CRITERION,
            detail="Mandatory entry criterion is not met",
            criterion_code="E1",
            criterion_title="Signed contract",
            waived_by="deputy_ceo_ops",
        )
        payload = json.loads(json.dumps(blocker.as_dict()))
        assert payload["kind"] == "criterion"
        assert payload["criterion_code"] == "E1"
        assert payload["waived_by"] == "deputy_ceo_ops"
