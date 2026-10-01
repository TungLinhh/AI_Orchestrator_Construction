"""The model is told the schema, so it stops guessing at it.

Measured on a real free-tier model: 46 tool calls, 13 succeeded, 25 failed with
a database error, every call the same tool. It wrote SQL against a schema it had
never seen. Raising the ceiling would have bought forty more wrong guesses.

The fix is the schema, and these tests hold two things about it:

* it is **derived from the ORM**, not written out -- a hand-written table list is
  correct on the day it is written and silently wrong the day a column is added,
  and nothing would notice because nothing would change;
* it actually **reaches the model**, through the tool description the runtime
  renders as the function docstring. A schema assembled and discarded is a
  schema nobody reads.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.application.schema_brief import QUERYABLE_TABLES, schema_brief, schema_note


class TestTheBriefComesFromTheModels:
    def test_every_named_table_actually_exists(self) -> None:
        """A name in the list that the ORM does not have is a wrong list.

        Silently skipping it would hide the mistake; the brief renders it as
        visibly broken so it is noticed in a prompt review rather than by a model
        that tries to query it.
        """
        brief = schema_brief()
        assert "NOT IN THE SCHEMA" not in brief, (
            f"the list names a table the ORM does not have: {brief}"
        )

    def test_a_column_added_to_a_model_appears_without_editing_this_file(self) -> None:
        """The property that makes the brief worth having.

        Checked by looking at a table the models declare, not by adding one --
        a test that added a column to check the brief would need a migration.
        `tasks.attempt_count` is the witness: it is a real column on a real
        table, and it is in the brief because the ORM says so.
        """
        assert "attempt_count" in schema_brief()

    def test_a_table_not_in_the_list_is_absent(self) -> None:
        """The brief is a shortlist, and shortlists have to be short.

        There are a hundred tables in this schema. A brief listing all of them is
        a document, and a document in a tool description is a document every run
        pays for and nobody reads.
        """
        brief = schema_brief()
        assert "audit_logs" in brief, "the audit ledger answers 'what happened'"
        assert "procedure_repetitions" not in brief, (
            "a table nobody asked for is still a table nobody read"
        )


class TestTheBriefAnswersWhatWentWrong:
    def test_it_names_the_tenant_predicate_and_a_working_query(self) -> None:
        """The first failure was eight identical retries of one query.

        A refusal that says what is wrong and not what is right leaves a model
        exactly one option. The example has to be complete -- predicate included
        -- or it teaches the shape and not the rule.
        """
        note = schema_note()
        assert "organization_id" in note
        assert "SELECT" in note and "FROM tasks" in note

    def test_it_is_not_a_wall(self) -> None:
        """Bounded, because it rides in every prompt that has the tool.

        The bound is a judgement, and this test is what makes it a decision
        rather than a drift: `tasks` alone has thirty-seven columns, and printing
        every type made this 5,213 characters of mostly `VARCHAR(40)`.
        """
        note = schema_note()
        assert len(note) < 6_000, f"the schema brief has grown to {len(note)} characters"
        assert "VARCHAR(40)" not in note, (
            "the column type on an id column is noise repeated forty times; it was "
            "the difference between 5,213 characters and a readable list"
        )

    def test_a_type_that_changes_a_comparison_is_kept(self) -> None:
        """Dropping every type would be wrong for the columns where it matters.

        `cost_usd NUMERIC(18, 6)` and `status` are not interchangeable: a
        comparison against the wrong one is the second-most-common way a query
        fails, after a name that does not exist.
        """
        brief = schema_brief()
        assert "NUMERIC" in brief
        assert "INTEGER" in brief


class TestTheBriefReachesTheModel:
    def test_the_tool_definition_points_at_the_schema(self) -> None:
        """The registry description is where an agent looks for the rules.

        Not where the schema itself lives -- that is attached per run, in
        `_contract_for_binding`, so it can be rebuilt from the ORM on every
        process rather than frozen into a row at seed time.
        """
        from ai_orchestrator.tools.builtin import build_default_tools

        registry = build_default_tools()
        query_tool = next(t for t in registry.all() if t.name == "internal_database_query")
        assert "listed at the end of this description" in query_tool.description, (
            "the tool tells the model to read the schema that is appended next, "
            "and the two have drifted apart"
        )
        assert "You have not been shown a schema" not in query_tool.description, (
            "the description still says no schema is shown, which stopped being "
            "true the moment one was"
        )

    def test_other_tool_descriptions_are_left_alone(self) -> None:
        """Only the tool that queries has a schema worth carrying.

        `write_report` does not read the database. Attaching the brief to every
        tool would pay for it on every run that never queries anything.
        """
        from ai_orchestrator.tools.builtin import build_default_tools

        registry = build_default_tools()
        for tool in registry.all():
            if tool.name != "internal_database_query":
                assert "organization_id" not in tool.description, (
                    f"{tool.name} was given a schema it has no use for"
                )

    def test_the_contract_the_model_receives_carries_the_schema(self) -> None:
        """The last mile: from ORM metadata to the string the model reads.

        Asserted on the `ToolContract` rather than the registry definition,
        because those are different objects and the schema lives on the second.
        """
        assert "tasks:" in schema_note()
        assert schema_note().count("organization_id") >= 2, (
            "the brief and the worked example both have to carry the predicate; "
            "one without the other teaches the wrong half"
        )


@pytest.mark.parametrize("table", QUERYABLE_TABLES)
def test_each_listed_table_has_a_line(table: str) -> None:
    """One assertion per table, so adding a bad name fails by name."""
    assert f"{table}:" in schema_brief()
