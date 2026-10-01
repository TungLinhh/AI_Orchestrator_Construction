"""The declared output contract has to reach the model, and come back.

Two halves of one fault, both found by running a real free model against a
three-tier pipeline and watching the department fail:

    failed | output_contract_unmet | this task said it would produce reason,
    verdicts, and produced nothing

1. **The contract was never stated.** `expected_output_schema` appeared nowhere in
   the runtime. The agent was asked a question in prose and then judged for
   answering in a shape it had never been told about.
2. **The contract was never read back.** `AgentResult` was built with `summary=`
   and no `output=`, so `result.output` was always `None` -- every task with a
   declared contract was *unsatisfiable by a real model*. `ScriptedRuntime` sets
   `output`, which is why 2914 tests passed and no real model could finish a
   department task.

A contract that is enforced on a field the producer never writes to is not a
contract. Both halves are tested here, because either one alone reproduces the
failure.
"""

from __future__ import annotations

import json

from ai_orchestrator.agent_runtime.pydanticai_agent import _declared_output, _user_prompt

CONTRACT = {
    "required": ["verdicts", "reason"],
    "field_meaning": {
        "verdicts": "mỗi khoản: duyệt / duyệt có điều kiện / từ chối",
        "reason": "lý do theo chính sách",
    },
}


class _Task:
    def __init__(self, schema=None, goal="làm việc này", inputs=None):  # type: ignore[no-untyped-def]
        self.goal = goal
        self.input = inputs or {}
        self.expected_output_schema = schema


class _Context:
    def __init__(self, schema=None, goal="làm việc này", inputs=None):  # type: ignore[no-untyped-def]
        self.task = _Task(schema, goal, inputs)


class TestThePromptStatesTheContract:
    def test_the_required_keys_are_named(self) -> None:
        """The half that was missing entirely."""
        prompt = _user_prompt(None, _Context(CONTRACT))
        assert "MUST BE A JSON OBJECT" in prompt
        assert "verdicts" in prompt
        assert "reason" in prompt

    def test_the_meaning_travels_with_the_key(self) -> None:
        """A key name alone is not an instruction. `verdicts` means nothing; what to
        put in it does."""
        prompt = _user_prompt(None, _Context(CONTRACT))
        assert "mỗi khoản: duyệt / duyệt có điều kiện / từ chối" in prompt

    def test_it_says_what_not_to_do(self) -> None:
        """A model told "JSON" and nothing else will often explain its JSON. The
        failure this replaces was a model that explained and produced nothing."""
        prompt = _user_prompt(None, _Context(CONTRACT))
        assert "nothing else" in prompt
        assert "missing key is a failed run" in prompt

    def test_no_contract_means_no_json_instruction(self) -> None:
        """A task that promised nothing keys should not be told to emit JSON."""
        prompt = _user_prompt(None, _Context(None))
        assert "MUST BE A JSON OBJECT" not in prompt

    def test_a_contract_without_required_keys_is_not_invented(self) -> None:
        """`{"produces": "x"}` and `{}` promise no key list, so there is nothing to
        name. Guessing keys here would produce a contract the author never wrote."""
        assert "MUST BE A JSON OBJECT" not in _user_prompt(None, _Context({"produces": "x"}))
        assert "MUST BE A JSON OBJECT" not in _user_prompt(None, _Context({}))


class TestTheAnswerIsReadBack:
    def test_a_bare_json_object(self) -> None:
        reply = json.dumps({"verdicts": "khoản 1 duyệt", "reason": "dưới ngưỡng 5.000.000"})
        assert _declared_output(reply, CONTRACT) == {
            "verdicts": "khoản 1 duyệt",
            "reason": "dưới ngưỡng 5.000.000",
        }

    def test_a_fenced_block(self) -> None:
        """What a chat model actually sends when told to answer with JSON."""
        reply = (
            "Đây là kết quả:\n\n```json\n"
            + json.dumps({"verdicts": "duyệt", "reason": "hợp lệ"}, ensure_ascii=False)
            + "\n```\n"
        )
        assert _declared_output(reply, CONTRACT) == {"verdicts": "duyệt", "reason": "hợp lệ"}

    def test_json_with_a_sentence_around_it(self) -> None:
        """Also what models do. The tolerant order exists for these, not for
        tidiness."""
        reply = 'Kết luận: {"verdicts": "từ chối", "reason": "thiếu hợp đồng"} — như trên.'
        assert _declared_output(reply, CONTRACT) == {
            "verdicts": "từ chối",
            "reason": "thiếu hợp đồng",
        }

    def test_prose_with_no_json_is_none(self) -> None:
        """And `None` is the honest answer. The contract check then fails the task
        and says the work was not produced, rather than the platform inventing keys
        to satisfy itself."""
        reply = "Khoản thứ nhất thì duyệt, khoản thứ hai thì trình giám đốc."
        assert _declared_output(reply, CONTRACT) is None

    def test_a_json_array_is_not_an_object(self) -> None:
        """Right data, wrong shape. Coercing a list into keys would be inventing a
        contract."""
        assert _declared_output('["a", "b"]', CONTRACT) is None

    def test_no_contract_means_no_parsing(self) -> None:
        """Nothing promised, nothing extracted -- so the task keeps its prose
        summary and is not judged on a shape."""
        assert _declared_output('{"anything": 1}', None) is None
        assert _declared_output('{"anything": 1}', {"required": []}) is None

    def test_values_keep_their_type(self) -> None:
        """A number stays a number. Coercing everything to text would make
        `approved_headcount: 3` into `"3"` and lose the distinction a reviewer
        checks."""
        parsed = _declared_output(
            json.dumps({"headcount": 3, "verdict": "duyệt", "ok": True}),
            {"required": ["headcount", "verdict", "ok"]},
        )
        assert parsed == {"headcount": 3, "verdict": "duyệt", "ok": True}
        assert isinstance(parsed["headcount"], int)  # type: ignore[index]
        assert parsed["ok"] is True  # type: ignore[index]
