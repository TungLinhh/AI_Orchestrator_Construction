"""A truncated answer is not an absent one.

**Measured on a real procurement run, 2026-10-03.** `qwen/qwen3.8-27b:free` is a
reasoning model and its thinking is billed against `max_tokens`. The procurement
department produced exactly the right answer — a ranked comparison by price, warranty
and delivery with the winner named — and it was cut off partway through the last string
value:

```
executions.output_tokens   15644
summary                    1077 chars, no closing brace
  {"reason": "...tiêu chí quyết định: C (Công ty Toàn Cầu) 1.090.000.000 VND..."
ends      "...hoặc giá C ních lên bằng hoặc cao"
```

`json.loads` refused it, `_declared_output` returned `None`, and the platform reported

```
failed | output_contract_unmet | this task said it would produce reason, risk, winner,
and produced nothing
```

on a task that had produced part of the answer. That is a **confident, specific and
wrong** statement about work that was done, and it is the one the whole product is
judged by. The review loop then rejected and reran the same work, which is why
`prior_repetitions` climbed 9 → 10 on the run that found this.

Three facts have to hold, and each is a separate failure:

1. the keys that **arrived** are recovered, so the answer is not discarded;
2. a value that was **cut** is marked as cut, so nobody reads a fragment as a finished
   answer;
3. the failure message says **the answer was truncated**, because that is a platform
   fault and sends a person to the right place — a department, or the runtime.

The test inputs below are the real summaries, not invented ones.
"""

from __future__ import annotations

import json

import pytest

from ai_orchestrator.agent_runtime.pydanticai_agent import (
    TruncatedValue,
    _completed_pairs,
    _declared_output,
)
from ai_orchestrator.domain.output_contract import describe_mismatch, missing_keys

CONTRACT = {"required": ["reason", "risk", "winner"]}

#: The real 1077-character answer, cut off inside the first value.
CUT_INSIDE_FIRST_VALUE = (
    '{\n  "reason": "So sánh theo đúng thứ tự ưu tiên tiêu chí (1) giá, (2) bảo hành, '
    "(3) thời gian giao hàng: (1) GIÁ — C (Công ty Toàn Cầu) 1.090.000.000 VND thấp nhất, "
    "rẻ hơn A (Cơ Điện Hải Phòng, 1.240.000.000 VND) 150 triệu và rẻ hơn B (Điện Lạnh "
    "Việt, 1.180.000.000 VND) 90 triệu. (2) BẢO HÀNH — A (3 năm) vượt trội, B và C bằng "
    "nhau (2 năm). (3) TIẾN ĐỘ GIAO — A (45 ngày) nhanh nhất, B (60 ngày), C (75 ngày). "
    "Kết luận: C rẻ nhất nhưng yếu về bảo hành và tiến độ, nên rủi ro vận hành cao hơn "
    "lợi ích tiền; nếu điều khoản gia cố bảo hành"
)

#: A well-formed answer, to prove the recovery did not start inventing things.
COMPLETE = json.dumps(
    {
        "reason": "C is lowest on price, A is best on warranty and lead time",
        "risk": "C carries a delivery risk",
        "winner": "A",
    }
)

#: Pretty-printed and truncated after one *complete* pair. The common shape.
CUT_AFTER_ONE_KEY = '{\n  "reason": "C is cheapest",\n  "risk": "C ships slowest'


class TestAWholeAnswerIsUnchanged:
    def test_a_complete_object_parses(self) -> None:
        assert _declared_output(COMPLETE, CONTRACT) == json.loads(COMPLETE)

    def test_a_complete_pretty_printed_object_parses(self) -> None:
        text = json.dumps(json.loads(COMPLETE), indent=2, ensure_ascii=False)
        assert _declared_output(text, CONTRACT) == json.loads(COMPLETE)


class TestWhatArrivesIsRecovered:
    def test_a_value_cut_in_the_middle_is_not_thrown_away(self) -> None:
        """The whole point, and the failure that motivated it: `produced nothing`."""
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert "reason" in got, (
            f"a truncated answer was discarded entirely; recovered {sorted(got)}"
        )

    def test_a_key_that_never_started_is_not_invented(self) -> None:
        """`risk` and `winner` were never written, so they must not appear."""
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert set(got) == {"reason"}, sorted(got)

    def test_the_missing_keys_are_still_missing(self) -> None:
        """Recovery does not relax the contract. The task is still failed."""
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert missing_keys(got, CONTRACT) == ("risk", "winner")

    def test_a_complete_key_before_the_cut_is_kept_whole(self) -> None:
        got = _declared_output(CUT_AFTER_ONE_KEY, CONTRACT) or {}
        assert got["reason"] == "C is cheapest"
        assert "risk" in got, "a key that was written is not a key that was lost"


class TestACutValueIsMarked:
    def test_a_cut_value_says_so(self) -> None:
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert isinstance(got["reason"], TruncatedValue)
        assert getattr(got["reason"], "truncated", False) is True

    def test_a_finished_value_does_not_say_so(self) -> None:
        got = _declared_output(COMPLETE, CONTRACT) or {}
        assert not isinstance(got["reason"], TruncatedValue)

    def test_the_marker_is_still_a_string(self) -> None:
        """It has to survive `json.dumps` on the way into the database."""
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert json.loads(json.dumps(got))["reason"] == str(got["reason"])

    def test_the_fragment_is_the_text_that_was_written(self) -> None:
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert str(got["reason"]).startswith("So sánh theo đúng thứ tự")
        assert str(got["reason"]).endswith("gia cố bảo hành")


class TestTheMessageTellsTheTruth:
    """**A wrong message here costs more than the truncation.**

    "produced nothing" says the department did no work. It did work, the runtime lost
    the tail, and a reader who believes the message goes and looks at the department.
    """

    def test_a_truncated_answer_is_not_reported_as_nothing(self) -> None:
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        message = describe_mismatch(got, CONTRACT)
        assert "produced nothing" not in message, message
        assert "cut off" in message, message

    def test_it_names_the_runtime_as_the_fault(self) -> None:
        got = _declared_output(CUT_INSIDE_FIRST_VALUE, CONTRACT) or {}
        assert "runtime fault" in describe_mismatch(got, CONTRACT)

    def test_a_genuinely_empty_answer_still_says_nothing(self) -> None:
        """The other direction. Recovery must not excuse a department that said nothing."""
        message = describe_mismatch({}, CONTRACT)
        assert "produced nothing" in message, message
        assert "cut off" not in message, message

    def test_a_complete_but_incomplete_object_names_what_is_absent(self) -> None:
        got = _declared_output(CUT_AFTER_ONE_KEY, CONTRACT) or {}
        message = describe_mismatch(got, CONTRACT)
        assert "risk" in message and "winner" in message, message

    def test_a_plain_string_is_never_reported_as_truncated(self) -> None:
        assert "cut off" not in describe_mismatch({"reason": "plain text"}, CONTRACT)


class TestTheRecoveryItself:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('{"a": 1}', {"a": 1}),
            ('{"a": 1, "b": [1, 2]}', {"a": 1, "b": [1, 2]}),
            # **Pretty-printed.** The first version of this skipped the space after the
            # colon, so the cursor landed on whitespace and every pretty-printed reply
            # recovered nothing -- including, on the real run, the only input it existed
            # for.
            ('{\n  "a": 1\n}', {"a": 1}),
            ('{  "a"  :  "x"  }', {"a": "x"}),
            ('{"a": {"b": 2}}', {"a": {"b": 2}}),
            # An escaped quote inside a finished string must not end it early.
            (r'{"a": "he said \"hi\"", "b": 2}', {"a": 'he said "hi"', "b": 2}),
        ],
    )
    def test_it_reads_whole_values(self, text: str, expected: dict) -> None:
        assert _completed_pairs(text) == expected

    def test_a_reply_with_no_object_is_nothing(self) -> None:
        assert _completed_pairs("I could not complete this.") == {}

    def test_it_stops_at_the_cut_and_invents_nothing_after_it(self) -> None:
        got = _completed_pairs('{"a": "whole", "b": "cut here')
        assert got == {"a": "whole", "b": "cut here"}
        assert isinstance(got["b"], TruncatedValue)
        assert not isinstance(got["a"], TruncatedValue)
