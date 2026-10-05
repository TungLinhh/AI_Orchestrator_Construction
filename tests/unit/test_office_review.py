"""The office's review of a department's work, as a decision a manager makes.

Pure. Every rule about what "good enough" means lives in `domain.review`; this
file is the arithmetic of turning a verdict into an action, with no I/O and no
model, so the loop that sends work back is testable without a database, a
provider, or a network.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.review import (
    DEFAULT_MAX_ATTEMPTS,
    MIN_TEXT_LENGTH,
    ReviewVerdict,
    assess_output,
    required_keys,
    should_rerun,
)


class TestTheContract:
    def test_both_contract_shapes_are_read(self) -> None:
        """Two shapes are live in this codebase and reading one is how a stage ran
        for months judged against nothing."""
        assert required_keys({"required": ["a", "b"]}) == ("a", "b")
        assert required_keys({"produces": "approved_headcount"}) == ("approved_headcount",)
        assert required_keys({"produces": ["x", "y"]}) == ("x", "y")
        assert required_keys(None) == ()
        assert required_keys("not a schema") == ()

    def test_an_empty_contract_promises_nothing(self) -> None:
        """A task that declared nothing is not thereby unjudgeable."""
        verdict = assess_output(output={"note": "the tender was awarded on price"}, attempt=1)
        assert verdict.ok


class TestWhatPasses:
    def test_an_output_meeting_its_contract_passes(self) -> None:
        verdict = assess_output(
            output={
                "winner": "Báo giá B, cheaper by 60 million and two years of warranty",
                "reason": "Price ranks first, and the warranty difference does not close the gap",
            },
            expected_output_schema={"required": ["winner", "reason"]},
        )
        assert verdict.ok, verdict.findings
        assert all(check.passed for check in verdict.checks)

    def test_a_bare_number_is_a_real_answer_to_how_many(self) -> None:
        """Rejected only as the *sole* output, which the presence check catches."""
        verdict = assess_output(
            output={"approved_headcount": 3},
            expected_output_schema={"produces": "approved_headcount"},
        )
        assert verdict.ok, verdict.findings

    def test_prose_is_acceptable_when_nothing_specific_was_promised(self) -> None:
        verdict = assess_output(output="The three invoices reconcile against the delivery notes.")
        assert verdict.ok


class TestWhatFails:
    def test_a_missing_promised_key_fails_and_names_itself(self) -> None:
        verdict = assess_output(
            output={"reason": "It is over the limit and needs the director"},
            expected_output_schema={"required": ["verdicts", "reason"]},
        )
        assert not verdict.ok
        assert any("verdicts" in finding for finding in verdict.findings)

    def test_a_placeholder_fails(self) -> None:
        """The failure that looks most like success.

        `[draft] verdicts` satisfies every presence check ever written. It is
        exactly what this project's own scenario harness produced, and it was
        marked `completed`.
        """
        verdict = assess_output(
            output={"verdicts": "[draft] verdicts — for Chấm nhận 3 khoản chi"},
            expected_output_schema={"required": ["verdicts"]},
        )
        assert not verdict.ok
        assert any("placeholder" in finding for finding in verdict.findings)

    @pytest.mark.parametrize("value", ["TODO", "tbd", "N/A", "chưa xác định", "...", "{{x}}"])
    def test_the_common_placeholders_all_fail(self, value: str) -> None:
        assert not assess_output(output={"verdict": value}).ok

    def test_a_thin_answer_fails(self) -> None:
        verdict = assess_output(output={"reason": "ok"})
        assert not verdict.ok
        assert any("too thin" in finding for finding in verdict.findings)

    def test_no_output_fails(self) -> None:
        assert not assess_output(output=None).ok
        assert not assess_output(output="").ok

    def test_a_paragraph_cannot_meet_a_key_contract(self) -> None:
        verdict = assess_output(
            output="I looked at it and it seems fine to me honestly",
            expected_output_schema={"required": ["verdicts", "reason"]},
        )
        assert not verdict.ok

    def test_extra_keys_are_judged_too(self) -> None:
        """The executive reads the padding first, so padding is not free."""
        verdict = assess_output(
            output={"reason": "A long enough reason about the invoice", "note": "TBD"}
        )
        assert not verdict.ok
        assert any("note" in finding for finding in verdict.findings)


class TestAnEmptyAnswerIsNotAnAnswer:
    """The false acceptance, found on a real run.

    A free model produced `{}` for a task that promised `verdicts` and `reason`,
    and the office accepted it **three times**, because the contract it was given
    was a description map -- `{"verdicts": "mỗi khoản: ...", "reason": "..."}` --
    rather than a list of keys. With no promised keys the checks had nothing to
    look at, a loop over nothing passed, and the pipeline reported the work done.
    """

    def test_an_empty_mapping_fails_even_with_no_contract(self) -> None:
        """`{}` and `None` are the same answer. Only one of them was being caught."""
        assert not assess_output(output={}).ok
        assert not assess_output(output={}, expected_output_schema=None).ok

    def test_an_empty_mapping_fails_with_a_contract(self) -> None:
        verdict = assess_output(
            output={}, expected_output_schema={"required": ["verdicts", "reason"]}
        )
        assert not verdict.ok
        assert any("nothing at all" in f for f in verdict.findings)

    def test_a_description_map_is_not_a_contract(self) -> None:
        """The shape that let it through, pinned so it cannot come back.

        `{"verdicts": "mừi khoản..."}` promises nothing: `required_keys` reads
        `required` and `produces`, and this map has neither.
        """
        described = {"verdicts": "mỗi khoản: duyệt / duyệt có điều kiện / từ chối"}
        assert required_keys(described) == ()
        # With a description map and an empty output, the verdict must still fail --
        # and it now fails on the empty output rather than on the shape.
        assert not assess_output(output={}, expected_output_schema=described).ok

    def test_the_real_contract_shape_is_understood(self) -> None:
        verdict = assess_output(
            output={},
            expected_output_schema={"required": ["verdicts"], "field_meaning": {}},
        )
        assert not verdict.ok
        assert required_keys({"required": ["verdicts"], "field_meaning": {}}) == ("verdicts",)

    def test_a_populated_answer_still_passes_with_that_shape(self) -> None:
        """The fix must not reject everything."""
        assert assess_output(
            output={
                "verdicts": "Khoản 1 duyệt; khoản 2 trình GĐH; khoản 3 từ chối",
                "reason": "Ngưỡng 5.000.000 và 20.000.000 VND theo chính sách",
            },
            expected_output_schema={
                "required": ["verdicts", "reason"],
                "field_meaning": {"verdicts": "…", "reason": "…"},
            },
        ).ok


class TestARestatementIsNotAnAnswer:
    """The answer that is the question again.

    Every other check here passes it: the keys are present, the values are long
    enough, and none of them is a placeholder. Measured on the scenario catalogue,
    three plausible-but-empty answers got through before this check existed:

        PASS  lặp lại câu hỏi
        PASS  copy lại goal

    An agent handed a brief can return the brief, and it will be forwarded upward
    as a finding. The check needs the brief itself, so it is optional -- omitting
    `source_text` costs this check and nothing else.
    """

    BRIEF = (
        "Chuẩn bị hồ sơ thanh toán giai đoạn 2 cho dự án Bãi Tràm Estates. "
        "Hãy giao phần đối chiếu số liệu PO-GRN cho bộ phận Mua sắm."
    )

    def test_the_brief_returned_under_the_promised_keys_fails(self) -> None:
        verdict = assess_output(
            output={"verdicts": self.BRIEF, "reason": self.BRIEF},
            expected_output_schema={"required": ["verdicts", "reason"]},
            source_text=self.BRIEF,
        )
        assert not verdict.ok
        assert any("repeats the task" in f for f in verdict.findings)

    def test_a_restatement_inside_a_list_fails_too(self) -> None:
        """The echo is judged on the whole value, not per element.

        Asked for `verdicts`, an agent will return a list of dicts whose fields
        restate the brief one at a time. Checking each string separately would miss
        that every one of them is the ask again.
        """
        verdict = assess_output(
            output={
                "verdicts": [{"claim": "Bãi Tràm Estates"}, {"claim": "giai đoạn 2"}],
                "reason": "Bãi Tràm Estates giai đoạn 2",
            },
            expected_output_schema={"required": ["verdicts", "reason"]},
            source_text=self.BRIEF,
        )
        assert not verdict.ok

    def test_a_real_answer_passes(self) -> None:
        verdict = assess_output(
            output={
                "verdicts": [
                    {"claim": "Minh Châu 3.600.000", "verdict": "approve"},
                    {"claim": "Văn phòng pháp chế 32.000.000", "verdict": "conditional"},
                ],
                "reason": "Khoản 32tr vượt hạn mức 20tr nên chuyển CEO quyết định.",
            },
            expected_output_schema={"required": ["verdicts", "reason"]},
            source_text=self.BRIEF,
        )
        assert verdict.ok, verdict.findings

    def test_an_answer_that_agrees_on_every_item_is_not_an_echo(self) -> None:
        """Every verdict "approve" is a *possible real answer*, and must pass.

        Written because the tempting version of this check -- require the answers to
        disagree -- would have been wrong, and would have rejected a department that
        genuinely found nothing to object to. The brief of this test is to catch
        restatement, not to demand disagreement.
        """
        verdict = assess_output(
            output={
                "verdicts": [{"claim": "khoản một", "verdict": "approve"}] * 3,
                "reason": "Cả ba khoản đều dưới hạn mức 5.000.000 nên không cần trình duyệt.",
            },
            expected_output_schema={"required": ["verdicts", "reason"]},
            source_text=self.BRIEF,
        )
        assert verdict.ok, verdict.findings

    def test_a_short_value_sharing_a_word_with_the_brief_is_not_an_echo(self) -> None:
        """Overlap means nothing on a value too short to carry a finding."""
        verdict = assess_output(
            output={"verdicts": "đã đối chiếu", "reason": "PO và GRN khớp hoàn toàn."},
            expected_output_schema={"required": ["verdicts", "reason"]},
            source_text=self.BRIEF,
        )
        assert verdict.ok, verdict.findings

    def test_without_the_brief_only_this_check_is_lost(self) -> None:
        """`source_text` is optional, and omitting it must not reject anything."""
        verdict = assess_output(
            output={"verdicts": self.BRIEF, "reason": self.BRIEF},
            expected_output_schema={"required": ["verdicts", "reason"]},
        )
        assert verdict.ok, "omitting the brief must not turn into a rejection"


class TestTheRerunBound:
    def test_a_rejection_with_attempts_left_is_sent_back(self) -> None:
        verdict = ReviewVerdict(ok=False, findings=("verdicts is a placeholder",))
        again, why = should_rerun(verdict, attempt=1, max_attempts=2)
        assert again
        assert "attempt 2" in why

    def test_the_last_attempt_escalates_instead_of_asking_again(self) -> None:
        """A manager who rejects forever is not managing either."""
        verdict = ReviewVerdict(ok=False, findings=("still wrong",))
        again, why = should_rerun(verdict, attempt=2, max_attempts=2)
        assert not again
        assert "escalating" in why

    def test_a_pass_is_never_rerun_however_many_attempts(self) -> None:
        again, why = should_rerun(ReviewVerdict(ok=True), attempt=9, max_attempts=2)
        assert not again
        assert "passed" in why

    def test_a_second_attempt_is_judged_as_strictly_as_the_first(self) -> None:
        """Otherwise a department learns that retrying is enough."""
        bad = {"verdicts": "[draft] verdicts"}
        schema = {"required": ["verdicts"]}
        assert not assess_output(output=bad, expected_output_schema=schema, attempt=1).ok
        assert not assess_output(output=bad, expected_output_schema=schema, attempt=2).ok

    def test_the_default_bound_is_two(self) -> None:
        assert DEFAULT_MAX_ATTEMPTS == 2


class TestTheVerdictIsReadable:
    def test_a_rejection_is_written_for_the_department_that_must_act_on_it(self) -> None:
        """`as_text` becomes the rerun brief, so it has to be actionable."""
        verdict = assess_output(
            output={"reason": "ok"},
            expected_output_schema={"required": ["verdicts", "reason"]},
        )
        text = verdict.as_text()
        assert text.startswith("Rejected.")
        assert "verdicts" in text
        assert "verdict" not in text.lower() or "verdicts" in text

    def test_an_acceptance_says_so_and_adds_nothing(self) -> None:
        assert assess_output(output={"a": "a long enough answer here"}).as_text() == (
            "Accepted: every check passed."
        )


class TestTheThreshold:
    def test_the_minimum_length_is_documented_by_being_used(self) -> None:
        """A test that pins the boundary, so raising it is a deliberate act.

        The filler is a letter that is not a placeholder marker. `"x" * 12` was the
        first choice and it failed, correctly: `xxx` is in the marker list, so a run
        that pads to length with `x` is rejected as a placeholder. That is a real
        property of the check and it is worth stating rather than working around.
        """
        at = "a" * MIN_TEXT_LENGTH
        assert assess_output(output={"reason": at}).ok
        assert not assess_output(output={"reason": at[:-1]}).ok

    def test_padding_to_length_with_marker_characters_is_still_rejected(self) -> None:
        """Length is not substance, and a marker is a marker at any length."""
        padded = "xxx" * 8
        assert len(padded) >= MIN_TEXT_LENGTH
        assert not assess_output(output={"reason": padded}).ok


class TestWorkThatWasDoneInTheWrongShape:
    """A long report is not "nothing produced", and the office must not say it is.

    Measured on a real free model against a real pipeline: 40.270 input tokens,
    6.713 output, 13 tool calls, and a full structured report in the execution
    summary -- while `tasks.output` was `{}`, because the model wrote prose instead
    of the contracted object.

    The office's finding said *"it produced nothing at all"*. That is false, and it
    is expensive: a rerun brief built from that finding tells a department which has
    already done the work that it did nothing, so it re-derives it. Measured five
    completed runs of the same work, each one told it had produced nothing.
    """

    REPORT = (
        "# KẾT QUẢ CHỐT KHOẢN CHI\n\n## 1. Khoản thứ nhất\n3.600.000, dưới hạn mức, "
        "đề nghị duyệt.\n\n## 2. Khoản thứ hai\n32.000.000, vượt hạn mức, chuyển CEO."
    )

    def test_prose_with_no_contract_fields_is_still_rejected(self) -> None:
        """The verdict must not move. A paragraph does not satisfy a key contract."""
        verdict = assess_output(
            output={},
            expected_output_schema={"required": ["verdicts", "reason"]},
            reported_text=self.REPORT,
        )
        assert not verdict.ok

    def test_the_finding_says_the_work_was_done_and_only_the_shape_was_wrong(self) -> None:
        verdict = assess_output(
            output={},
            expected_output_schema={"required": ["verdicts", "reason"]},
            reported_text=self.REPORT,
        )
        finding = " ".join(verdict.findings)
        assert "produced nothing at all" not in finding, (
            "the office told a department it did nothing when it wrote a report"
        )
        assert "DID produce an answer" in finding
        assert "not in the shape" in finding

    def test_the_finding_tells_it_to_reformat_rather_than_re_derive(self) -> None:
        """The instruction is the fix. Reformatting converges; re-deriving does not."""
        finding = " ".join(
            assess_output(
                output={},
                expected_output_schema={"required": ["verdicts", "reason"]},
                reported_text=self.REPORT,
            ).findings
        )
        assert "Do the analysis again only as far as you need to" in finding

    def test_a_genuinely_empty_run_still_says_nothing_was_produced(self) -> None:
        """The original finding is correct when it is true, and must survive."""
        verdict = assess_output(
            output={},
            expected_output_schema={"required": ["verdicts", "reason"]},
        )
        finding = " ".join(verdict.findings)
        assert "produced nothing at all" in finding

    def test_a_trivial_summary_does_not_claim_there_was_work(self) -> None:
        """A two-character summary is not a report, and must not be credited as one."""
        verdict = assess_output(
            output={},
            expected_output_schema={"required": ["verdicts", "reason"]},
            reported_text="ok",
        )
        assert "produced nothing at all" in " ".join(verdict.findings)


class TestTheRerunBriefCarriesTheShape:
    """The retry is told the keys, not just the complaint.

    Findings say what was wrong with the answer; without the key list the
    department fixes the tone and fails the same contract again. Keys ride
    verbatim — identifiers, never translated.
    """

    def _brief(self, schema: dict | None) -> str:
        from types import SimpleNamespace

        from ai_orchestrator.application.work_review import _rerun_goal
        from ai_orchestrator.domain.review import ReviewVerdict

        child = SimpleNamespace(
            goal="Chấm 3 khoản chi",
            input={"attempt": 1},
            expected_output_schema=schema,
        )
        verdict = ReviewVerdict(ok=False, findings=("thiếu key reason",))
        return _rerun_goal(child, verdict, "văn xuôi lần trước")

    def test_the_required_keys_are_listed_verbatim(self) -> None:
        brief = self._brief({"required": ["verdicts", "reason"]})
        assert "verdicts" in brief and "reason" in brief
        assert "ĐỊNH DẠNG BẮT BUỘC" in brief

    def test_no_contract_means_no_shape_block(self) -> None:
        brief = self._brief(None)
        assert "ĐỊNH DẠNG BẮT BUỘC" not in brief
        assert "thiếu key reason" in brief
