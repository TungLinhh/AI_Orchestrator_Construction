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
