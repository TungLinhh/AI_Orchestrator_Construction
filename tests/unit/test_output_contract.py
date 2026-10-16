"""A declared output is a claim, and it is now checked.

`tasks.expected_output_schema` was stored, handed to the agent in its context, and read by
nothing: **113 tasks** on the development tenant carried one. The first stage of
`ONX-BO-HR-SOP-004` was run for real, declared `approved_headcount`, reached `completed` and
wrote `{proposal_count, scripted}` — so a process advanced on a stage that produced something
else, and the UI showed it green.

These tests assert the rule, and — as importantly — the two directions it must **not** fire in:
a task that declared nothing, and a task that is waiting rather than finished.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.output_contract import (
    describe_mismatch,
    missing_keys,
    required_keys,
)

pytestmark = pytest.mark.unit


class TestReadingTheDeclaration:
    def test_the_explicit_list(self) -> None:
        assert required_keys({"required": ["job_description", "rubric"]}) == (
            "job_description",
            "rubric",
        )

    def test_the_label_the_existing_rows_use(self) -> None:
        """**113 rows** already say `{"produces": "a,b"}`. They are enforced without a
        migration, because a migration would have had to rewrite rows to express something the
        field already said."""
        assert required_keys({"produces": "offer_letter"}) == ("offer_letter",)
        assert required_keys({"produces": "onboarding_pack, employment_contract"}) == (
            "onboarding_pack",
            "employment_contract",
        )

    def test_a_task_that_declared_nothing_has_no_contract(self) -> None:
        """Most tasks return whatever they return. Inventing a contract for them would be noise."""
        assert required_keys(None) == ()
        assert required_keys({}) == ()
        assert required_keys({"something_else": "documentation"}) == ()

    def test_a_blank_declaration_is_not_a_contract(self) -> None:
        """A seeder writing `"produces": ""` must not turn every task into a failing one."""
        assert required_keys({"produces": ""}) == ()
        assert required_keys({"required": []}) == ()


class TestTheCheck:
    def test_it_passes_when_the_output_has_what_it_said(self) -> None:
        schema = {"produces": "approved_headcount"}
        assert missing_keys({"approved_headcount": 1}, schema) == ()

    def test_it_fails_on_what_was_actually_measured(self) -> None:
        """The real pair: declared one thing, produced two others."""
        schema = {"produces": "approved_headcount"}
        assert missing_keys({"proposal_count": 0, "scripted": True}, schema) == (
            "approved_headcount",
        )

    def test_no_output_at_all_is_not_anything_produced(self) -> None:
        assert missing_keys(None, {"produces": "offer_letter"}) == ("offer_letter",)
        assert missing_keys({}, {"produces": "offer_letter"}) == ("offer_letter",)

    def test_a_task_with_no_contract_never_fails_on_this(self) -> None:
        """The expensive false positive: an ordinary task with output nobody declared."""
        assert missing_keys({"whatever": 1}, None) == ()
        assert missing_keys(None, None) == ()

    def test_extra_output_does_not_fail_it(self) -> None:
        """Producing *more* than promised is not a failure. Only missing is."""
        assert missing_keys({"a": 1, "b": 2, "c": 3}, {"produces": "a,b"}) == ()


class TestTheMessage:
    def test_it_names_both_halves(self) -> None:
        """A person has to be able to act on this, and "it did not do what it said" without
        saying which part is a message that gets a task retried without anybody looking."""
        said = describe_mismatch({"proposal_count": 0}, {"produces": "approved_headcount"})
        assert "approved_headcount" in said, "what was required"
        assert "proposal_count" in said, "what arrived"
        assert "nothing" in describe_mismatch(None, {"produces": "approved_headcount"})


class TestRepairFromText:
    """The answer exists but the parsed map lost it — read before failing.

    A department that did the work in prose, or whose tail was truncated, used
    to fail `output_contract_unmet` with `tasks.output` persisted as `{}`. The
    work was thrown away and the review loop re-derived it at full model cost.
    The repair fills only keys found under their exact declared name; anything
    still missing fails honestly.
    """

    def test_keys_found_in_the_summary_are_restored(self) -> None:
        from ai_orchestrator.domain.output_contract import repair_from_text

        schema = {"required": ["verdicts", "reason"]}
        repaired = repair_from_text(
            {},
            schema,
            'Kết luận: {"verdicts": "duyệt 2, từ chối 1", "reason": "thiếu ngày thu hồi"}',
        )
        assert repaired == {"verdicts": "duyệt 2, từ chối 1", "reason": "thiếu ngày thu hồi"}

    def test_a_key_never_mentioned_stays_missing(self) -> None:
        from ai_orchestrator.domain.output_contract import missing_keys, repair_from_text

        schema = {"required": ["verdicts", "reason"]}
        repaired = repair_from_text({}, schema, "Tôi đã xem xét và mọi thứ đều ổn.")
        assert missing_keys(repaired, schema) == ("verdicts", "reason")

    def test_present_keys_are_kept_and_only_the_gap_is_filled(self) -> None:
        from ai_orchestrator.domain.output_contract import repair_from_text

        schema = {"required": ["verdicts", "reason"]}
        repaired = repair_from_text(
            {"verdicts": "duyệt"},
            schema,
            'Báo cáo: {"reason": "đủ hồ sơ"}',
        )
        assert repaired == {"verdicts": "duyệt", "reason": "đủ hồ sơ"}

    def test_a_translated_key_is_not_repaired(self) -> None:
        """Keys are identifiers. `"phán_quyết"` is not `"verdicts"`, and
        guessing the mapping back is inventing — the rerun brief, which carries
        the verbatim key list, is what teaches the shape."""
        from ai_orchestrator.domain.output_contract import missing_keys, repair_from_text

        schema = {"required": ["verdicts"]}
        repaired = repair_from_text({}, schema, '{"phán_quyết": "duyệt"}')
        assert missing_keys(repaired, schema) == ("verdicts",)

    def test_non_object_values_are_recovered_verbatim(self) -> None:
        from ai_orchestrator.domain.output_contract import repair_from_text

        schema = {"required": ["score", "approved", "count"]}
        repaired = repair_from_text({}, schema, '{"score": 8.5, "approved": true, "count": 12}')
        assert repaired == {"score": 8.5, "approved": True, "count": 12}

    def test_empty_text_repairs_nothing(self) -> None:
        from ai_orchestrator.domain.output_contract import repair_from_text

        assert repair_from_text({}, {"required": ["a"]}, "") == {}


class TestScenarioContracts:
    def test_the_browser_catalogue_and_command_line_share_the_full_contract(self):
        from ai_orchestrator.application.scenarios import SCENARIOS, catalogue
        from ai_orchestrator.domain.output_contract import schema_from_fields

        payload = {item["key"]: item for item in catalogue()}
        for scenario in SCENARIOS:
            assert payload[scenario.key]["expected_output_schema"] == schema_from_fields(
                scenario.expected_output
            )
        for key, field in (("progress-report", "progress"), ("access-review", "backup_status")):
            schema = payload[key]["expected_output_schema"]
            assert schema["properties"][field]["x-source-summary"] is True
            assert (
                schema["field_meaning"][field]
                == payload[key]["expected_output"][field]["description"]
            )


class TestExplicitContractValidation:
    @pytest.mark.parametrize("value", [None, True, "3", 3.5])
    def test_required_keys_with_wrong_values_fail(self, value: object) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        schema = {"required": ["count"], "properties": {"count": {"type": "integer"}}}
        assert contract_violations({"count": value}, schema) == ("output.count violates type",)

    def test_nested_department_artifact_is_checked(self) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        schema = {
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["accepted"],
                        "properties": {"accepted": {"type": "boolean"}},
                        "additionalProperties": False,
                    },
                }
            },
        }
        assert contract_violations({"findings": [{"accepted": True}]}, schema) == ()
        assert contract_violations({"findings": [{"accepted": "yes"}]}, schema)
        assert contract_violations({"findings": [{}]}, schema)
        assert contract_violations({"findings": []}, schema)
        assert contract_violations({"findings": [{"accepted": True, "invented": 1}]}, schema)

    def test_legacy_contracts_keep_key_only_meaning(self) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        assert contract_violations({"count": None}, {"produces": "count"}) == ()
        assert contract_violations({"count": "anything"}, None) == ()
        assert contract_violations({}, {"produces": "count"})

    @pytest.mark.parametrize(
        "department", ["sales", "procurement", "qa", "design", "finance", "hr", "it"]
    )
    def test_generic_contract_is_independent_of_department(self, department: str) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        schema = {
            "required": ["department", "decision"],
            "properties": {
                "department": {"type": "string"},
                "decision": {"enum": ["review", "accept"]},
            },
        }
        assert contract_violations({"department": department, "decision": "review"}, schema) == ()
        assert contract_violations({"department": department, "decision": "invented"}, schema)

    def test_unchecked_reference_is_explicitly_rejected(self) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        assert "unresolved" in contract_violations({"value": 1}, {"$ref": "#/$defs/value"})[0]

    def test_nullable_type_and_numeric_bounds(self) -> None:
        from ai_orchestrator.domain.output_contract import contract_violations

        schema = {"properties": {"price": {"type": ["number", "null"], "minimum": 0}}}
        assert contract_violations({"price": None}, schema) == ()
        assert contract_violations({"price": 0}, schema) == ()
        assert contract_violations({"price": -1}, schema)
        assert contract_violations({"price": True}, schema)


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": None},
        {"properties": {"v": {"enum": None}}},
        {"properties": {"v": {"type": [{}]}}},
    ],
)
def test_malformed_stored_schema_rejects_without_crashing(schema):
    from ai_orchestrator.domain.output_contract import contract_violations

    assert "malformed schema" in contract_violations({"v": 1}, schema)[0]


def test_pydantic_blueprint_schema_remains_supported():
    from ai_orchestrator.domain.agent_blueprint import AgentBlueprint
    from ai_orchestrator.domain.output_contract import contract_violations
    from tests.unit.test_agent_blueprint import example_plan

    output = AgentBlueprint.model_validate(example_plan()).model_dump(mode="json")
    assert contract_violations(output, AgentBlueprint.model_json_schema()) == ()


def test_remote_schema_reference_is_rejected_without_retrieval():
    from ai_orchestrator.domain.output_contract import contract_violations

    errors = contract_violations({"v": 1}, {"$ref": "https://example.invalid/schema.json"})
    assert "external retrieval is disabled" in errors[0]
