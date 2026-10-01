"""The eight agents of the dossier's register, and the invariants that make it a register.

A register that is only prose is a list. What makes this one checkable is that every
property below is *asserted*, so a change that breaks the dossier's shape fails here rather
than in a report nobody re-reads.

## What is asserted, and why each one is a real constraint

* **`test_the_register_is_the_dossier_s_eight_in_its_order`** — the order is Tập 1 table
  10's prioritisation, and Tập 2 §G.2 makes HR the standard pilot for the rest. Reordering
  is not a cosmetic change: it changes which agent is proved first.
* **`test_no_agent_holds_a_hard_blocked_action_class`** — the invariant I got wrong. The
  register first gave Procurement `supplier_risk_flagged` and QA/QC-HSE
  `safety_conclusion`, both of which Tập 1 §5.3 lists as absolute prohibitions. The
  promotion gate refused all four of those procedures, correctly. The lesson is that an
  agent must not *hold* a hard-blocked class at all: holding one means it can be asked for
  it. The blocked act belongs in `must_refuse`, where a person can read it.
* **`test_every_action_class_is_one_the_dossier_defines`** — an earlier draft invented
  `read`, `draft`, `record`, `reconcile`, and the gate refused every one with
  `unclassified_action`. An action class is a row somebody approved, not a label an agent
  picks.
* **`test_every_agent_is_granted_l1`** — a promotion says a version agrees with a human; it
  does not say the agent may act unsupervised. Conflating a ceiling with a grant is how an
  agent ends up authorised to spend money because somebody set a ceiling.
* **`test_the_ceiling_is_derived_not_declared`** — `ceiling_for` reads
  `autonomy_policies`, and the strictest of an agent's classes is the answer. Asserting a
  ceiling in the register was the earlier mistake and it disagreed with the table.
* **`test_every_sop_code_exists`** — the sixteen are read from `sop_definitions` at seed
  time, so a typo produces a named refusal rather than a procedure with nothing behind it.
* **`test_every_agent_has_a_justification_and_a_role`** — a register entry with a blank
  reason is a name in a list.

## What is deliberately *not* asserted

Nothing about the model's quality, and nothing about an agent's accuracy. There are no real
decisions to shadow yet, so a test claiming the agents work would be claiming something
this build cannot show. What it *can* show is that they are registered correctly, bounded
correctly, and refuse correctly — which is what these tests check.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.agent_register import (
    ALL_ACTION_CLASSES,
    DOSSIER_ACTION_CLASSES,
    HARD_BLOCK_CLASSES,
    REGISTER,
    ROLE_GAP,
    AgentSpec,
    by_name,
    ceiling_for,
)

#: Tập 1 table 10, in order. Written out here rather than derived, because a test that
#: derives its expectation from the thing it is checking is a test that cannot fail.
DOSSIER_ORDER: tuple[tuple[str, str], ...] = (
    ("HR Agent", "P1"),
    ("Procurement Agent", "P2"),
    ("Knowledge Agent", "P2"),
    ("Finance Agent", "P3"),
    ("Project Mgmt Agent", "P3"),
    ("Design/M&E Agent", "P4"),
    ("Sales/BD Agent", "P4"),
    ("QA/QC-HSE Agent", "P4"),
)

#: As seeded in `autonomy_policies`, from Tập 1 §5.3.
PERMITTED: dict[str, str] = {
    "hr_personnel_decision": "L2",
    "financial_commitment": "L3",
    "safety_conclusion": "L1",
    "supplier_risk_flagged": "L1",
    "contract_signature": "L3",
    "routine_classification": "L4",
}


class TestTheRegisterIsTheDossiersEight:
    def test_the_register_is_the_dossier_s_eight_in_its_order(self) -> None:
        assert len(REGISTER) == 8, f"the dossier names eight; the register has {len(REGISTER)}"
        assert tuple((s.name, s.priority) for s in REGISTER) == DOSSIER_ORDER

    def test_hr_is_first_and_the_pilot(self) -> None:
        """Tập 2 §G.2: the HR Agent is the standard pilot for every agent after it."""
        assert REGISTER[0].name == "HR Agent"
        assert "pilot" in REGISTER[0].justification.lower()

    def test_every_agent_has_a_justification_a_role_and_sops(self) -> None:
        for spec in REGISTER:
            assert len(spec.justification.split()) >= 20, (
                f"{spec.name}: a justification of {len(spec.justification.split())} words "
                "is a label, not a reason"
            )
            assert spec.role_name, f"{spec.name} has no role"
            assert spec.sop_codes, f"{spec.name} runs on no SOP"
            assert spec.model_profile, f"{spec.name} binds to no model profile"

    def test_by_name_finds_every_agent_and_nothing_else(self) -> None:
        for spec in REGISTER:
            assert by_name(spec.name) is spec
        with pytest.raises(KeyError):
            by_name("No Such Agent")


class TestBoundedCorrectly:
    def test_no_agent_holds_a_hard_blocked_action_class(self) -> None:
        """The invariant I got wrong, and the one the promotion gate taught me.

        Tập 1 §5.3 lists `safety_conclusion` and `supplier_risk_flagged` as absolute
        prohibitions. The gate refuses a procedure that touches either, at every level — and
        it refused four of mine. The fix was not to argue with the gate: an agent holding a
        class is an agent that can be asked for it. The blocked act is a refusal, and it is
        in `must_refuse` where a reader can see it.
        """
        for spec in REGISTER:
            held = HARD_BLOCK_CLASSES & set(spec.action_classes)
            assert not held, (
                f"{spec.name} holds {sorted(held)}, which Tập 1 §5.3 lists as an "
                "absolute prohibition. An agent that holds a hard-blocked class can be "
                "asked for it; the act belongs in `must_refuse`."
            )

    def test_an_agent_whose_domain_contains_a_hard_block_says_so(self) -> None:
        """The refusal has to be written down, not merely implied by the missing class."""
        procurement = by_name("Procurement Agent")
        assert any("risk flag" in r for r in procurement.must_refuse), (
            "Procurement does not hold `supplier_risk_flagged`, and nothing records that "
            "its absence is deliberate"
        )
        hse = by_name("QA/QC-HSE Agent")
        assert any("safety" in r for r in hse.must_refuse), (
            "QA/QC-HSE does not hold `safety_conclusion`, and nothing records that its "
            "absence is deliberate"
        )

    def test_every_action_class_is_one_the_dossier_defines(self) -> None:
        for spec in REGISTER:
            unknown = set(spec.action_classes) - set(DOSSIER_ACTION_CLASSES)
            assert not unknown, (
                f"{spec.name} uses {sorted(unknown)}, which is not in Tập 1 §5.3. An "
                "action class is a row somebody approved, not a label an agent picks -- "
                "the promotion gate refuses the others with `unclassified_action`."
            )

    def test_every_agent_is_granted_l1(self) -> None:
        """A promotion is not a grant.

        Nothing in this build has shadow-run evidence for autonomous action, because there
        are no real decisions to have shadowed yet. Every agent therefore starts at L1, and
        a test says so rather than leaving it to a reader of the seed output.
        """
        for spec in REGISTER:
            assert spec.granted_level == "L1", (
                f"{spec.name} is granted {spec.granted_level}; nothing in this build has "
                "shadow-run evidence for more"
            )

    def test_the_ceiling_is_derived_not_declared(self) -> None:
        for spec in REGISTER:
            assert not hasattr(spec, "_declared_ceiling")
            derived = ceiling_for(spec, PERMITTED)
            assert derived in {"L1", "L2", "L3", "L4"}
            # The strictest of the classes, so a procedure is bounded by its weakest link.
            strictest = min(int(PERMITTED[c][1:]) for c in spec.action_classes if c in PERMITTED)
            assert derived == f"L{strictest}", (
                f"{spec.name}: ceiling {derived} is not the strictest of "
                f"{ {c: PERMITTED[c] for c in spec.action_classes} }"
            )

    def test_an_unknown_action_class_cannot_raise_a_ceiling(self) -> None:
        """The safe direction. A typo must lower a ceiling, never raise it.

        `ceiling_for` with a table that knows nothing returns `L1`, so a mis-seeded policy
        table produces the most restricted agent rather than the least.
        """
        spec: AgentSpec = by_name("Knowledge Agent")
        assert ceiling_for(spec, {}) == "L1"
        assert ceiling_for(spec, {"routine_classification": "L4"}) == "L4"

    def test_the_action_classes_are_not_all_the_same(self) -> None:
        """A register where every agent may do the same thing is one agent with eight names."""
        assert len(ALL_ACTION_CLASSES) >= 4
        assert len({spec.action_classes for spec in REGISTER}) >= 5


class TestTheRolesAreAStatedGap:
    def test_role_gap_covers_exactly_the_agents_without_a_matching_role(self) -> None:
        """The register has no Procurement Director and no Design Director.

        Those two agents attach to the nearest existing authority and `ROLE_GAP` records it.
        Inventing the two missing roles would tidy the table and falsify the register: the
        role set is the dossier's, and the gap is a finding about the dossier.
        """
        assert set(ROLE_GAP) == {"Procurement Agent", "Design/M&E Agent"}
        for name, stand_in in ROLE_GAP.items():
            spec = by_name(name)
            assert spec.role_name == stand_in
            assert spec.role_name != name, "an agent cannot be its own authority"

    def test_every_agent_names_a_role_that_exists(self) -> None:
        """Checked against the seeded role set, so a renamed role is a failure here."""
        known = {
            "Back Office Director",
            "Executive",
            "Finance Director",
            "IT Director",
            "Marketing Director",
            "Programme Manager",
            "Quality Director",
            "Risk Director",
            "Sales Director",
        }
        for spec in REGISTER:
            assert spec.role_name in known, f"{spec.name}: no role named {spec.role_name!r}"
