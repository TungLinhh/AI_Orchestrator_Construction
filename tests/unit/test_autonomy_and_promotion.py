"""The two autonomy scales, and the gate before a behaviour change.

Tests for `domain/autonomy.py` and `domain/promotion.py`, organised around what could
go wrong that would be invisible afterwards.

The point of `autonomy.py` is to stop a string comparison doing the work. So most of
these tests are about the *spelling*: does an `L10` sort below `L2` if somebody adds a
tenth level, and does a `AutonomyLevel` cross to the dossier's form and back without
losing anything. Both answers are yes and no respectively, and the test is what keeps
them that way.

The point of `promotion.py` is that every refusal is reachable and names itself. A
gate with an unreachable branch is a branch nobody has reasoned about, and a gate that
reports only its first failure sends somebody round the loop once per problem.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.autonomy import (
    DOSSIER_LEVELS,
    LevelError,
    Policy,
    ceiling_for_all,
    may_act,
    parse_level,
    rank,
    to_dossier,
)
from ai_orchestrator.domain.enums import AutonomyLevel
from ai_orchestrator.domain.promotion import (
    Block,
    PromotionPolicy,
    VersionFacts,
    VersionState,
    evaluate,
)

# The six policies `make seed-process` writes from Tập 1 §5.3, including the two hard
# blocks. Written out here rather than read from the database so the rules are tested
# against the dossier rather than against whatever the seeder currently produces — the
# F101 lesson, applied to a fixture.
DOSSIER_POLICIES: dict[str, Policy] = {
    "hr_personnel_decision": Policy("hr_personnel_decision", "L2", False),
    "financial_commitment": Policy("financial_commitment", "L3", False),
    "safety_conclusion": Policy("safety_conclusion", "L1", True),
    "supplier_risk_flagged": Policy("supplier_risk_flagged", "L1", True),
    "contract_signature": Policy("contract_signature", "L3", False),
    "routine_classification": Policy("routine_classification", "L4", False),
}


class TestTheTwoScalesAgree:
    @pytest.mark.parametrize("level", list(AutonomyLevel))
    def test_every_level_crosses_to_the_dossier_and_back(self, level: AutonomyLevel) -> None:
        """The round trip, over the whole enum rather than the levels in use.

        A level added to `AutonomyLevel` with no dossier spelling would raise here
        instead of at the first write, which is the difference between a test failing
        and a migration failing.
        """
        assert to_dossier(level) == to_dossier(level)
        assert parse_level(to_dossier(level)) is level

    def test_the_forward_table_is_complete_in_both_directions(self) -> None:
        """The reverse table is built from the forward one, so it cannot disagree.

        Worth stating because a hand-written reverse table is the obvious way to write
        this and it would be tested only in whichever direction somebody remembered.
        """
        assert len(DOSSIER_LEVELS) == len(AutonomyLevel)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("L1", AutonomyLevel.L1_LOW_RISK_AUTONOMOUS),
            ("l3", AutonomyLevel.L3_HUMAN_APPROVAL),
            ("  L4  ", AutonomyLevel.L4_BOUNDED_AUTONOMOUS),
            ("l0", AutonomyLevel.L0_SUGGEST),
        ],
    )
    def test_case_and_whitespace_do_not_matter(self, code: str, expected: AutonomyLevel) -> None:
        """The two scales in the database differ in case, and both spellings exist.

        These values arrive in seeded SQL and in spreadsheets, not from a
        type-checked call site, so the convenience is load-bearing rather than
        cosmetic.
        """
        assert parse_level(code) is expected

    def test_l0_is_in_the_substrate_and_not_in_the_dossier(self) -> None:
        """Documented as a fact rather than smoothed over.

        `AutonomyLevel` has `L0_SUGGEST`; Tập 1's scale starts at L1. "Suggest and
        wait" is a real state and the dossier's Phase-1 arrangement *is* it, so it is
        accepted on input and ranks below everything rather than being rejected.
        """
        assert to_dossier(AutonomyLevel.L0_SUGGEST) == "L0"
        assert rank("L0") == 0
        assert rank("L1") > rank("L0")

    @pytest.mark.parametrize("junk", ["", "L5", "suggest", "L1a", "1", "l", "L01"])
    def test_something_that_is_not_a_level_is_refused_by_name(self, junk: str) -> None:
        with pytest.raises(LevelError) as caught:
            parse_level(junk)
        assert ", ".join(DOSSIER_LEVELS) in str(caught.value)


class TestRankIsPositionNotAlphabet:
    def test_two_digits_would_break_an_alphabetical_comparison(self) -> None:
        """The accident this module exists to prevent, demonstrated.

        `'L10' < 'L2'` is true as strings and false as autonomy. If someone ever
        graduates L1 into `L1a`/`L1b`, a version capped at `L2` would silently permit
        `L10`. Ranking by position makes that impossible to express.
        """
        assert "L10" < "L2", "the string comparison really does get this backwards"
        with pytest.raises(LevelError):
            rank("L10"), "so rank() refuses it rather than inventing a position"

    def test_rank_is_strictly_increasing(self) -> None:
        ranks = [rank(c) for c in DOSSIER_LEVELS]
        assert ranks == sorted(ranks)
        assert len(set(ranks)) == len(ranks)


class TestMayActAgainstTheDossierPolicies:
    def test_a_routine_action_is_permitted_at_its_ceiling(self) -> None:
        assert may_act("routine_classification", "L4", DOSSIER_POLICIES) is None

    def test_a_routine_action_is_permitted_below_its_ceiling(self) -> None:
        assert may_act("routine_classification", "L1", DOSSIER_POLICIES) is None

    def test_an_action_above_its_ceiling_is_refused(self) -> None:
        refusal = may_act("hr_personnel_decision", "L3", DOSSIER_POLICIES)
        assert refusal is not None
        assert refusal.rule_id == "autonomy.over_ceiling"
        assert "L2" in refusal.reason

    def test_a_contract_signature_needs_human_approval_not_just_review(self) -> None:
        """L3, per the dossier: *không bao giờ tự ký* -- never signs by itself.

        L3 is permitted, and it is permitted precisely because it means a human
        approves. The ceiling is the mechanism, not the prohibition, which is why the
        assertion that matters is that **L4 is refused**: a bounded-autonomous agent
        signing on its own is the thing the dossier forbids.
        """
        assert may_act("contract_signature", "L3", DOSSIER_POLICIES) is None
        refusal = may_act("contract_signature", "L4", DOSSIER_POLICIES)
        assert refusal is not None
        assert refusal.rule_id == "autonomy.over_ceiling"
        assert "L3" in refusal.reason, "the refusal says which level is allowed"


class TestTheHardBlockIsNotACeiling:
    """Tập 1 §5.3's *vùng cấm tuyệt đối*, and why a level comparison cannot express it."""

    @pytest.mark.parametrize("action_class", ["safety_conclusion", "supplier_risk_flagged"])
    @pytest.mark.parametrize("level", list(DOSSIER_LEVELS))
    def test_no_level_permits_a_hard_blocked_action(self, action_class: str, level: str) -> None:
        """All five levels, including L4.

        The seed data gives these a `max_level` of `L1`, which read as a ceiling says
        "an L1 agent may do this". Read as a flag it says "no agent may do this", and
        the second is what the dossier means. Checking the flag before comparing levels
        is what makes L4 refuse rather than pass.
        """
        refusal = may_act(action_class, level, DOSSIER_POLICIES)
        assert refusal is not None
        assert refusal.rule_id == "autonomy.hard_block"

    def test_a_hard_block_with_a_generous_ceiling_still_refuses(self) -> None:
        """A data-entry slip must not turn a prohibition into a setting.

        If someone edits `max_level` to `L4` and leaves `is_hard_block` true, the
        correct outcome is unchanged. A rule that only looked at the level would
        quietly allow the action.
        """
        generous = {"dangerous": Policy("dangerous", "L4", True)}
        assert may_act("dangerous", "L4", generous).rule_id == "autonomy.hard_block"  # type: ignore[union-attr]

    def test_the_refusal_says_absolute_prohibition(self) -> None:
        """The sentence is the output, and it has to survive being quoted upward."""
        refusal = may_act("safety_conclusion", "L4", DOSSIER_POLICIES)
        assert refusal is not None
        assert "absolute prohibition" in refusal.reason
        assert "Tập 1 §5.3" in refusal.reason


class TestUnclassifiedIsNotPermitted:
    def test_an_unknown_action_class_is_refused(self) -> None:
        refusal = may_act("something_new", "L1", DOSSIER_POLICIES)
        assert refusal is not None
        assert refusal.rule_id == "autonomy.unclassified"

    def test_the_refusal_says_to_classify_it_first(self) -> None:
        """Not "denied" -- what to do about it is the useful part."""
        refusal = may_act("something_new", "L1", DOSSIER_POLICIES)
        assert refusal is not None
        assert "classify it first" in refusal.reason

    def test_an_unknown_level_is_refused_rather_than_ranked(self) -> None:
        refusal = may_act("routine_classification", "L9", DOSSIER_POLICIES)
        assert refusal is not None
        assert refusal.rule_id == "autonomy.unknown_level"


class TestCeilingForASet:
    def test_it_is_the_lowest_ceiling_in_the_set(self) -> None:
        assert ceiling_for_all(DOSSIER_POLICIES, ["routine_classification"]) == "L4"
        assert (
            ceiling_for_all(DOSSIER_POLICIES, ["routine_classification", "contract_signature"])
            == "L3"
        )
        assert (
            ceiling_for_all(
                DOSSIER_POLICIES,
                ["routine_classification", "hr_personnel_decision", "contract_signature"],
            )
            == "L2"
        )

    def test_one_hard_block_refuses_the_whole_set(self) -> None:
        """Not the lowest ceiling of the survivors.

        Capping the rest and proceeding is the tempting repair, and it turns a
        prohibition into a setting. A version concluding a labour-safety matter is the
        version the dossier says must never be automated, and capping it at L1 does not
        make automating it safe, it only makes it slower.
        """
        assert (
            ceiling_for_all(DOSSIER_POLICIES, ["routine_classification", "safety_conclusion"])
            is None
        )

    def test_one_unclassified_refuses_the_whole_set(self) -> None:
        assert (
            ceiling_for_all(DOSSIER_POLICIES, ["routine_classification", "unknown_thing"]) is None
        )

    def test_an_empty_set_has_no_ceiling(self) -> None:
        """Not `L4`. An empty set is not "nothing to worry about"; it is unclassified."""
        assert ceiling_for_all(DOSSIER_POLICIES, []) is None


# --------------------------------------------------------------------- promotion --


def _facts(**over: object) -> VersionFacts:
    """A version that should promote, so a test states only what blocks it."""
    base: dict[str, object] = {
        "version_id": "prv_1",
        "status": VersionState.SHADOW.value,
        "shadow_runs": 20,
        "shadow_agreements": 19,
        "autonomy_ceiling": "L2",
        "action_classes": ("hr_personnel_decision",),
    }
    base.update(over)
    return VersionFacts(**base)  # type: ignore[arg-type]


def _verdict(**over: object):
    return evaluate(_facts(**over), DOSSIER_POLICIES)


class TestAVersionThatShouldPromote:
    def test_it_does(self) -> None:
        verdict = _verdict()
        assert verdict.may_promote
        assert verdict.blocks == ()
        assert verdict.reasons == ()

    def test_it_reports_the_level_it_would_run_at(self) -> None:
        """So an operator can see the ceiling without reading the policy table."""
        assert _verdict().permitted_level == "L2"

    def test_exactly_95_percent_agreement_passes(self) -> None:
        """19/20 against an 80% floor, comfortably over."""
        assert _verdict(shadow_runs=20, shadow_agreements=19).may_promote

    def test_the_threshold_is_inclusive(self) -> None:
        """Exactly at the floor promotes; one run's worth below it does not.

        `>=` rather than `>`, tested on both sides, because an off-by-one here is an
        unexplained refusal somebody works around by lowering the floor.
        """
        assert _verdict(shadow_runs=100, shadow_agreements=80).may_promote
        assert not _verdict(shadow_runs=100, shadow_agreements=79).may_promote

    def test_exactly_the_minimum_run_count_passes(self) -> None:
        assert _verdict(shadow_runs=5, shadow_agreements=5).may_promote


class TestWhatBlocksARefusal:
    def test_too_few_runs(self) -> None:
        verdict = _verdict(shadow_runs=4, shadow_agreements=4)
        assert verdict.blocks == (Block.NOT_ENOUGH_RUNS,)
        assert not verdict.may_promote

    def test_the_run_count_is_checked_before_the_rate(self) -> None:
        """3 out of 3 is 100% and means nothing, and it must not read as excellent.

        This is the guard against a statistic designed from a fixture: a rate over too
        few samples will beat any floor, so the count has to be its own gate.
        """
        verdict = _verdict(shadow_runs=3, shadow_agreements=3)
        assert verdict.blocks == (Block.NOT_ENOUGH_RUNS,)
        assert "computed over too few samples" in verdict.reasons[0]

    def test_agreement_too_low(self) -> None:
        verdict = _verdict(shadow_runs=20, shadow_agreements=10)
        assert verdict.blocks == (Block.AGREEMENT_TOO_LOW,)
        assert "50%" in verdict.reasons[0]

    def test_an_author_whose_switch_is_thrown(self) -> None:
        verdict = _verdict(author_killed=True)
        assert verdict.blocks == (Block.AUTHOR_KILLED,)
        assert "Not deleted" in verdict.reasons[0], (
            "it has to say the version is kept, or somebody will delete it"
        )

    def test_a_rejected_version_cannot_come_back(self) -> None:
        verdict = _verdict(status=VersionState.REJECTED.value)
        assert verdict.blocks == (Block.NOT_IN_SHADOW,)
        assert "a bug, not a promotion" in verdict.reasons[0]

    def test_an_already_active_version_is_not_re_promoted(self) -> None:
        """It would only rewrite `promoted_at` and make the change look later."""
        verdict = _verdict(status=VersionState.ACTIVE.value)
        assert verdict.blocks == (Block.ALREADY_PROMOTED,)

    def test_a_version_naming_no_action_class(self) -> None:
        """A procedure that has not been classified cannot be shown to be permitted."""
        verdict = _verdict(action_classes=())
        assert verdict.blocks == (Block.UNCLASSIFIED_ACTION,)

    def test_a_version_claiming_more_than_its_actions_permit(self) -> None:
        """`hr_personnel_decision` permits L2; a version claiming L4 is refused.

        Silently lowering it to L2 would promote a document nobody approved at the
        level it was written for.
        """
        verdict = _verdict(autonomy_ceiling="L4")
        assert verdict.blocks == (Block.AUTONOMY_REFUSED,)
        assert "at most L2" in verdict.reasons[0]

    def test_a_version_claiming_less_than_its_actions_permit_is_fine(self) -> None:
        """A stricter claim is always allowed, and is the safe direction."""
        assert _verdict(autonomy_ceiling="L1").may_promote

    def test_a_touching_a_hard_blocked_class(self) -> None:
        verdict = _verdict(
            action_classes=("routine_classification", "safety_conclusion"),
            autonomy_ceiling="L4",
        )
        assert Block.AUTONOMY_REFUSED in verdict.blocks
        assert any("absolute prohibition" in r for r in verdict.reasons)
        assert verdict.permitted_level is None, (
            "there is no level to offer, and offering L1 would be the same as saying "
            "automating it is fine if you go slowly"
        )

    def test_a_touching_an_unclassified_class(self) -> None:
        verdict = _verdict(action_classes=("hr_personnel_decision", "mystery_thing"))
        assert Block.UNCLASSIFIED_ACTION in verdict.blocks
        assert any("not in the autonomy policy table" in r for r in verdict.reasons)


class TestEveryReasonIsReported:
    def test_all_the_failing_gates_appear_at_once(self) -> None:
        """A person fixing one problem at a time is a slower route to the same place.

        Same argument as `ProposalGate.evaluate`: the caller would otherwise have to
        ask again to find the rest, and each asking is a round trip.
        """
        verdict = _verdict(
            status=VersionState.REJECTED.value,
            author_killed=True,
            autonomy_ceiling="L4",
            shadow_runs=2,
            shadow_agreements=1,
        )
        assert set(verdict.blocks) == {
            Block.NOT_IN_SHADOW,
            Block.AUTHOR_KILLED,
            Block.AUTONOMY_REFUSED,
            Block.NOT_ENOUGH_RUNS,
        }
        assert len(verdict.reasons) == len(verdict.blocks)

    def test_the_verdict_is_serialisable(self) -> None:
        """Because it goes into a report and an API response."""
        got = _verdict(author_killed=True).as_dict()
        assert got["may_promote"] is False
        assert got["blocks"] == ["author_killed"]
        assert isinstance(got["reasons"], list)


class TestTheThresholdPolicy:
    def test_an_agreement_rate_outside_zero_to_one_is_refused(self) -> None:
        """At the boundary, where the cause is.

        A rate of `80` or `-0.1` makes every comparison below trivially pass or fail,
        and the failure then looks like bad data rather than bad configuration.
        """
        for bad in (1.5, -0.1, 80.0):
            with pytest.raises(ValueError, match=r"fraction in 0\.\.1"):
                PromotionPolicy(min_agreement=bad)

    def test_a_tighter_floor_blocks_what_a_looser_one_allows(self) -> None:
        """So the number is load-bearing rather than decoration."""
        facts = _facts(shadow_runs=100, shadow_agreements=85)
        assert evaluate(facts, DOSSIER_POLICIES).may_promote
        strict = PromotionPolicy(min_agreement=0.90)
        assert not evaluate(facts, DOSSIER_POLICIES, policy=strict).may_promote

    def test_a_higher_min_runs_blocks_what_a_lower_one_allows(self) -> None:
        facts = _facts(shadow_runs=8, shadow_agreements=8)
        assert evaluate(facts, DOSSIER_POLICIES).may_promote
        strict = PromotionPolicy(min_runs=10)
        assert not evaluate(facts, DOSSIER_POLICIES, policy=strict).may_promote
