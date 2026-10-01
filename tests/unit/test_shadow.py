"""Shadow mode: did the model and the person agree, and may this deploy go live.

The dossier sets four weeks of parallel running at >=95% agreement as the precondition
for go-live. `domain/promotion.py` already *consumed* shadow evidence and
`agent_shadow_runs` already had the columns for it, so this file covers what was
missing: the decision about whether two answers are the same answer.

Three things here are the whole design, and each is a place where a plausible
implementation would have reported something false:

* **Normalising only one side.** Folding turned `ĐỒNG Ý` into `dồng ý`, which matched
  nothing in a table written `đồng ý`. The model said "đồng ý", the person said
  "approve", the two words differ by one character, and the run was recorded as a
  total disagreement. A normalisation that folds the answer but not the vocabulary
  reports spelling as disagreement.
* **A partial answer counting as agreement.** A model that answered two of three
  claims was refused rather than scored on the two it answered, because the rate would
  then be a score on questions nobody asked.
* **A rate reported before the time.** Five runs agreeing 100% reads as excellent and
  is one day of evidence. The span is checked first and named.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ai_orchestrator.domain.shadow import (
    GO_LIVE_MIN_AGREEMENT,
    GO_LIVE_MIN_WEEKS,
    Uncomparable,
    compare,
    normalise,
    readiness,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

HUMAN = {
    "verdicts": {
        "Minh Châu 3.600.000": "approve",
        "Văn phòng pháp chế 32.000.000": "conditional",
        "Kiên Phát 18.500.000": "reject",
    },
    "reason": "the lease contract is missing for the third claim",
}


class TestNormalisation:
    @pytest.mark.parametrize(
        "written",
        ["approve", "APPROVE", "Approve", "approved", "accept", "ĐỒNG Ý", "đồng ý", "phê duyệt"],
    )
    def test_the_words_for_agreeing_all_reach_one_word(self, written: str) -> None:
        assert normalise(written) == "approve"

    @pytest.mark.parametrize(
        "written",
        ["reject", "TỪ CHỐI", "từ chối", "refused", "không đồng ý"],
    )
    def test_the_words_for_refusing_all_reach_one_word(self, written: str) -> None:
        assert normalise(written) == "reject"

    @pytest.mark.parametrize(
        "written", ["conditional", "escalate", "chuyển lên", "trình sếp", "chưa quyết định"]
    )
    def test_the_words_for_escalating_all_reach_one_word(self, written: str) -> None:
        assert normalise(written) == "escalate"

    def test_both_written_number_forms_become_the_same_number(self) -> None:
        """The dossier writes `3.600.000`; tool output writes `3,600,000`.

        A shadow run that disagreed with itself over punctuation would be measuring
        the formatter, and the formatter is not what go-live is being decided on.
        """
        assert normalise("3.600.000") == normalise("3,600,000") == "3600000"

    def test_a_hedged_answer_is_not_reduced_to_its_first_word(self) -> None:
        """`approve` and `approve all three` are not the same answer.

        Reducing them together would make a conditional recommendation look like a
        clean one, and a conditional recommendation on a payment is exactly what the
        comparison exists to notice.
        """
        assert normalise("approve all three") != normalise("approve")
        assert normalise("approve all three") == "approve all three"

    def test_a_word_nobody_declared_is_left_alone(self) -> None:
        """Guessing at synonyms is how a comparison starts agreeing with itself.

        An unrecognised answer has to stay visible, so a new vocabulary can be seen
        rather than silently mapped onto an old one.
        """
        assert normalise("Wibble") == "wibble"

    def test_a_float_is_refused_rather_than_compared(self) -> None:
        with pytest.raises(Uncomparable, match="float"):
            normalise(3.6)

    def test_absent_and_empty_are_refused(self) -> None:
        with pytest.raises(Uncomparable):
            normalise(None)
        with pytest.raises(Uncomparable, match="empty"):
            normalise("   ")


class TestTheComparison:
    def test_an_identical_answer_agrees(self) -> None:
        result = compare(HUMAN, HUMAN)
        assert result.agreed
        assert result.matching == result.compared == 4
        assert result.divergence == ""

    def test_the_same_words_written_differently_agree(self) -> None:
        """The whole point of the vocabulary: one character is not a disagreement."""
        model = {
            "verdicts": {
                "Minh Châu 3.600.000": "ĐỒNG Ý",
                "Văn phòng pháp chế 32.000.000": "chuyển lên",
                "Kiên Phát 18.500.000": "TỪ CHỐI",
            },
            "reason": "the lease contract is missing for the third claim",
        }
        assert compare(model, HUMAN).agreed

    def test_one_wrong_answer_disagrees_and_says_which(self) -> None:
        """A rate with a reason. A disagreement nobody can act on is not a finding."""
        model = {
            "verdicts": {**HUMAN["verdicts"], "Văn phòng pháp chế 32.000.000": "approve"},
            "reason": HUMAN["reason"],
        }
        result = compare(model, HUMAN)
        assert not result.agreed
        assert result.matching == 3
        assert "Văn phòng pháp chế 32.000.000" in result.divergence
        assert "approve" in result.divergence and "escalate" in result.divergence

    def test_a_run_is_agreed_only_when_every_key_agrees(self) -> None:
        """Not a majority and not a mean.

        Right about two of three claims is wrong about a payment, and averaging it
        into 67% hides the only case a person can act on.
        """
        model = {
            "verdicts": {
                "Minh Châu 3.600.000": "reject",
                "Văn phòng pháp chế 32.000.000": "reject",
                "Kiên Phát 18.500.000": "approve",
            },
            "reason": "the lease contract is missing for the third claim",
        }
        result = compare(model, HUMAN)
        assert result.compared == 4
        assert result.matching == 1, "only the reason still matched, and one of three"
        assert not result.agreed

    def test_a_nested_answer_is_compared_by_path(self) -> None:
        result = compare({"verdicts": {"a": "approve"}}, {"verdicts": {"a": "approve"}})
        assert [k.key for k in result.keys] == ["verdicts.a"]

    def test_a_list_is_compared_by_position(self) -> None:
        result = compare({"v": ["approve", "reject"]}, {"v": ["approve", "reject"]})
        assert result.agreed
        assert [k.key for k in result.keys] == ["v[0]", "v[1]"]


class TestWhatIsRefusedRatherThanScored:
    def test_a_partial_answer_is_refused_not_scored_on_what_it_answered(self) -> None:
        """Two of three claims agreed is a score on questions nobody asked."""
        model = {
            "verdicts": {
                "Minh Châu 3.600.000": "approve",
                "Văn phòng pháp chế 32.000.000": "conditional",
            },
            "reason": "the lease contract is missing",
        }
        with pytest.raises(Uncomparable, match="same decisions"):
            compare(model, HUMAN)

    def test_an_empty_model_answer_is_refused(self) -> None:
        with pytest.raises(Uncomparable, match="no comparable values"):
            compare({}, HUMAN)

    def test_an_absent_answer_is_refused(self) -> None:
        with pytest.raises(Uncomparable, match="both"):
            compare(None, HUMAN)

    def test_the_refusal_names_what_each_side_was_missing(self) -> None:
        model = {"verdicts": {"a": "approve", "b": "approve"}}
        human = {"verdicts": {"a": "approve", "c": "approve"}}
        with pytest.raises(Uncomparable) as caught:
            compare(model, human)
        message = str(caught.value)
        assert "b" in message and "c" in message


class TestGoLiveReadiness:
    def _four_weeks_of_agreement(self, count: int = 40) -> list[tuple[datetime, bool]]:
        start = NOW - timedelta(weeks=5)
        return [(start + timedelta(days=i), True) for i in range(count)]

    def test_nothing_recorded_is_not_ready(self) -> None:
        report = readiness([], now=NOW)
        assert not report.ready
        assert any("no shadow runs" in m for m in report.missing)

    def test_a_pure_rate_does_not_substitute_for_the_time(self) -> None:
        """F101's shape: a statistic designed from a sample it cannot replace.

        Forty perfect runs inside one day is a 100% agreement rate and one day of
        parallel running. The dossier asks for four weeks, and the week gate is named
        separately so a dashboard cannot show the rate without it.
        """
        report = readiness(
            [(NOW - timedelta(hours=1), True)] * 40, now=NOW, required_weeks=GO_LIVE_MIN_WEEKS
        )
        assert report.agreement == 1.0
        assert not report.ready
        assert any("day(s) of parallel running" in m for m in report.missing)

    def test_four_weeks_of_agreement_is_ready(self) -> None:
        report = readiness(self._four_weeks_of_agreement(), now=NOW)
        assert report.ready, report.missing
        assert report.agreement >= GO_LIVE_MIN_AGREEMENT
        assert report.observed_days >= GO_LIVE_MIN_WEEKS * 7

    def test_a_rate_below_the_floor_blocks_regardless_of_time(self) -> None:
        start = NOW - timedelta(weeks=5)
        runs = [(start + timedelta(days=i), i % 8 != 0) for i in range(40)]
        report = readiness(runs, now=NOW)
        assert report.observed_days >= GO_LIVE_MIN_WEEKS * 7
        assert not report.ready
        assert any("not a rounding error" in m for m in report.missing)

    def test_the_go_live_thresholds_are_the_dossier_s_not_the_rollout_policy_s(self) -> None:
        """80% over 5 runs is a rollout gate; 95% over 4 weeks is go-live.

        Using the smaller numbers here would have turned a stated business
        precondition into a five-sample smoke test.
        """
        assert GO_LIVE_MIN_AGREEMENT == 0.95
        assert GO_LIVE_MIN_WEEKS == 4

    def test_the_last_week_is_reported_separately(self) -> None:
        """A single cumulative rate cannot show agreement holding or decaying."""
        start = NOW - timedelta(weeks=5)
        runs = [(start + timedelta(days=i), True) for i in range(28)]
        runs += [(NOW - timedelta(days=i), False) for i in range(6)]
        report = readiness(runs, now=NOW)
        assert report.runs == 34
        assert report.last_week_runs == 6
        assert report.last_week_agreements == 0
        assert report.agreement > report.last_week_agreement

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """Otherwise the span raises rather than answering, on a replay of history."""
        report = readiness([(datetime(2026, 9, 1), True)] * 30, now=datetime(2026, 10, 1))
        assert report.observed_days == 30

    def test_the_report_is_serialisable_for_the_api(self) -> None:
        report = readiness(self._four_weeks_of_agreement(), now=NOW).as_dict()
        assert report["ready"] is True
        assert report["required_agreement"] == 0.95
        assert isinstance(report["agreement"], float)
