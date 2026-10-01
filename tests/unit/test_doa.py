"""Who may approve an amount, decided from the bands alone.

Eight DOA bands were seeded and nothing ever asked them. These tests exist because
the interesting decisions are not the lookup -- they are the edges, the gaps, and the
reading of an autonomy label, and each of those was decided differently in each place
it was decided.

The one that would have cost real money: `L3_HUMAN_APPROVAL` sorts *after*
`L2_PARENT_REVIEW` and before `L4_BOUNDED_AUTONOMOUS`, so an ordinal reading says L4
outranks L3 and lets an agent holding the human-approval posture settle money with no
human. L3 is the level where a *human* signs. These tests pin that.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest

from ai_orchestrator.domain.doa import (
    AGENT_MAY_ACT,
    DoaBand,
    DoaRefusal,
    bands_of,
    resolve,
)

# The seeded matrix, exactly as `seed_process_spine.py` writes it.
SEEDED = (
    DoaBand("DOA-01", "payment", Decimal(0), Decimal("100000000"), "procurement_lead"),
    DoaBand("DOA-02", "payment", Decimal("100000000"), Decimal("1000000000"), "finance_manager"),
    DoaBand("DOA-03", "payment", Decimal("1000000000"), Decimal("10000000000"), "chief_accountant"),
    DoaBand("DOA-04", "payment", Decimal("10000000000"), None, "cfo"),
    DoaBand("DOA-PO-01", "purchase_order", Decimal(0), Decimal("500000000"), "procurement_lead"),
    DoaBand("DOA-PO-02", "purchase_order", Decimal("500000000"), None, "deputy_ceo_operations"),
    DoaBand("DOA-CT-01", "contract", Decimal(0), Decimal("50000000000"), "ceo"),
    DoaBand("DOA-CT-02", "contract", Decimal("50000000000"), None, "board"),
)


class TestTheBandEdges:
    def test_the_lower_bound_is_inclusive(self) -> None:
        assert resolve(SEEDED, "payment", "100000000").band.code == "DOA-02"

    def test_the_upper_bound_is_exclusive(self) -> None:
        """Otherwise an amount sits in two bands and the answer depends on row order.

        `payment` has `0..100.000.000` and `100.000.000..1.000.000.000`. With an
        inclusive upper bound, 100.000.000 is in both, and whichever the database
        returned first would be the answer -- so the same payment could be signed by
        the procurement lead on one day and the finance manager on another.
        """
        assert resolve(SEEDED, "payment", "99999999").band.code == "DOA-01"
        assert resolve(SEEDED, "payment", "100000001").band.code == "DOA-02"

    def test_the_bands_partition_without_gap_or_overlap(self) -> None:
        """Every amount from zero upward lands in exactly one band."""
        payments = bands_of("payment", SEEDED)
        for lo, hi in pairwise(payments):
            assert lo.max_amount == hi.min_amount, (
                f"{lo.code} ends at {lo.max_amount} but {hi.code} starts at {hi.min_amount}"
            )

    def test_the_highest_band_is_unbounded(self) -> None:
        decision = resolve(SEEDED, "payment", "99000000000000")
        assert decision.band.code == "DOA-04"
        assert decision.approver_role_key == "cfo"

    def test_the_order_rows_arrive_in_does_not_change_the_answer(self) -> None:
        assert resolve(tuple(reversed(SEEDED)), "payment", "100000000").band.code == "DOA-02"


class TestTheAmountMustBeMoney:
    def test_a_float_is_refused_rather_than_coerced(self) -> None:
        """`Decimal(0.1)` from a float is not 0.1, and a band edge is not a rounding.

        An amount arriving from JSON is a float by default, so this is the ordinary
        path and not a corner case. Coercing it would let an amount miss the
        100.000.000 edge by a fraction of a dong -- in a control.
        """
        with pytest.raises(DoaRefusal, match="float"):
            resolve(SEEDED, "payment", 100000.0)

    def test_the_projects_own_format_is_read_as_it_is_written(self) -> None:
        """The first claim in the scenario catalogue is written `3.600.000`.

        `Decimal("3.600.000")` raises, so before the locale was stated this was not
        read as 3.6 dong -- it was refused as "not a number", and so was every other
        amount in the dossier's format. A matrix that cannot read its own figures is
        a matrix nobody consults.
        """
        assert resolve(SEEDED, "payment", "3.600.000").amount == Decimal("3600000")

    def test_the_locale_is_declared_and_not_guessed(self) -> None:
        assert resolve(SEEDED, "payment", "3,600,000", locale="en_US") == resolve(
            SEEDED, "payment", "3600000"
        )

    def test_reading_one_locale_as_the_other_is_refused_not_reinterpreted(self) -> None:
        """A control that guesses picks an authority band on a coin flip."""
        with pytest.raises(DoaRefusal, match="not a number in vi_VN"):
            resolve(SEEDED, "payment", "3,600,000")

    def test_a_malformed_group_is_refused(self) -> None:
        """A trailing group that is not three digits is a typo, not a number.

        Reading `3.60.0000` as `360.0000` would put a payment in the wrong band for a
        reason that looks like arithmetic.
        """
        with pytest.raises(DoaRefusal):
            resolve(SEEDED, "payment", "3.60.0000")

    def test_a_fractional_amount_is_read(self) -> None:
        decision = resolve(SEEDED, "payment", "3.600.000,50")
        assert decision.amount == Decimal("3600000.50")

    def test_nonsense_is_refused_by_name(self) -> None:
        with pytest.raises(DoaRefusal, match="not a number"):
            resolve(SEEDED, "payment", "a lot")

    def test_a_negative_amount_is_refused(self) -> None:
        with pytest.raises(DoaRefusal, match="negative"):
            resolve(SEEDED, "payment", "-1")

    def test_a_boolean_is_not_an_amount(self) -> None:
        with pytest.raises(DoaRefusal, match="boolean"):
            resolve(SEEDED, "payment", True)


class TestTheTwoRefusals:
    def test_a_subject_with_no_bands_is_refused(self) -> None:
        """A matrix with a hole must stop, not hand the hole to a neighbour."""
        with pytest.raises(DoaRefusal, match="no DOA band covers subject kind"):
            resolve(SEEDED, "salary", "50000000")

    def test_an_amount_past_every_ceiling_is_refused(self) -> None:
        capped = (DoaBand("LOW", "payment", Decimal(0), Decimal("1000000"), "lead"),)
        with pytest.raises(DoaRefusal, match="above every payment band"):
            resolve(capped, "payment", "5000000")

    def test_a_gap_between_bands_is_refused_and_says_so(self) -> None:
        gapped = (
            DoaBand("LOW", "payment", Decimal(0), Decimal("1000000"), "lead"),
            DoaBand("HIGH", "payment", Decimal("5000000"), None, "cfo"),
        )
        with pytest.raises(DoaRefusal, match="gap between"):
            resolve(gapped, "payment", "2000000")

    def test_the_refusal_is_raised_and_not_returned(self) -> None:
        """A caller that swallows it becomes the bug this module prevents.

        Asserted through the type: `DoaRefusal` is an `Exception`, so there is no
        return path on which a caller has to remember to check anything.
        """
        assert issubclass(DoaRefusal, Exception)


class TestWhatAnAgentMayDoAlone:
    """The levels are a taxonomy, not a ladder, and the difference is the whole file.

    `L3_HUMAN_APPROVAL` is the level where a human signs. Read ordinally, it looks
    like the third rung of four and `L4` looks like the top, so an agent holding L4
    outranks one holding L3 -- and then the human-approval posture is the one that
    looks *less* autonomous, which is exactly backwards for money.
    """

    LOOSE = (DoaBand("LOOSE", "payment", Decimal(0), None, "lead", max_agent_autonomy="L4"),)

    @pytest.mark.parametrize("level", sorted(AGENT_MAY_ACT))
    def test_a_level_that_permits_action_may_act_within_a_permissive_band(self, level: str) -> None:
        assert resolve(self.LOOSE, "payment", "1000", agent_autonomy=level).may_act_alone

    def test_the_human_approval_level_may_never_act_alone(self) -> None:
        """Even inside a band that permits an agent to act.

        L3 means a human is in the loop by definition. An L3 agent that "may act
        alone" would be a policy that reads as approval and behaves as autonomy.
        """
        decision = resolve(self.LOOSE, "payment", "1000", agent_autonomy="L3")
        assert not decision.may_act_alone
        assert "human" in decision.reason

    def test_suggest_only_may_not_act(self) -> None:
        assert not resolve(self.LOOSE, "payment", "1000", agent_autonomy="L0").may_act_alone

    def test_a_band_capped_at_human_approval_is_closed_to_every_agent(self) -> None:
        """The seeded matrix caps every band at L3, so a human signs for everything.

        This is the conservative reading and it is the one the seeded data supports.
        Before it, the inert string `L3` was compared with `<=` against the agent's
        level and L4 beat it, so every seeded band was effectively open and the matrix
        did nothing at all -- which is the state this project shipped.
        """
        for level in ("L0", "L1", "L2", "L3", "L4"):
            decision = resolve(SEEDED, "payment", "3600000", agent_autonomy=level)
            assert not decision.may_act_alone, (
                f"the seeded matrix let {level} settle a payment alone"
            )

    def test_no_level_means_no_agency(self) -> None:
        decision = resolve(SEEDED, "payment", "3600000")
        assert not decision.may_act_alone
        assert "no autonomy level" in decision.reason

    def test_an_unknown_level_is_refused_by_name(self) -> None:
        with pytest.raises(DoaRefusal, match="unknown autonomy level"):
            resolve(SEEDED, "payment", "1000", agent_autonomy="L9")

    def test_both_spellings_of_a_level_are_understood(self) -> None:
        """The matrix stores `L3`; the agents carry `l3_human_approval`."""
        short = resolve(SEEDED, "payment", "1000", agent_autonomy="L3")
        long = resolve(SEEDED, "payment", "1000", agent_autonomy="l3_human_approval")
        assert short.may_act_alone == long.may_act_alone
        assert short.band.code == long.band.code


class TestWhatItSaysToThePersonAsking:
    def test_the_reason_names_the_band_the_role_and_the_amount(self) -> None:
        reason = resolve(SEEDED, "payment", "30000000000", agent_autonomy="L4").reason
        assert "DOA-04" in reason
        assert "cfo" in reason
        assert "30000000000" in reason

    def test_the_three_real_expenses_land_in_the_expected_bands(self) -> None:
        """The claims from the verified run, resolved against the matrix."""
        assert resolve(SEEDED, "payment", "3600000").approver_role_key == "procurement_lead"
        assert resolve(SEEDED, "payment", "32000000").approver_role_key == "procurement_lead"
        # 18.5tr is in the same band as 3.6tr -- which is why the *document* is what
        # rejects it, not the amount. The DOA and the contract are different controls.
        assert resolve(SEEDED, "payment", "18500000").approver_role_key == "procurement_lead"

    def test_a_purchase_order_and_a_payment_use_different_bands(self) -> None:
        """Same amount, different subject, different signer.

        Which is the reason the matrix is keyed on `subject_kind` and not on amount
        alone: 600.000.000 is the finance manager's as a payment and the deputy CEO's
        as a purchase order.
        """
        assert resolve(SEEDED, "payment", "600000000").approver_role_key == "finance_manager"
        assert (
            resolve(SEEDED, "purchase_order", "600000000").approver_role_key
            == "deputy_ceo_operations"
        )

    def test_a_large_contract_reaches_the_board(self) -> None:
        assert resolve(SEEDED, "contract", "60000000000").approver_role_key == "board"
