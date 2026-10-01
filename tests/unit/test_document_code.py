"""The document code is parsed, never concatenated.

## What these tests are for

`sop_definitions.code` is a flat string, and `sop_definitions` also carries `block` and
`department` beside it. So the code's information exists twice and nothing checked that the
two agree. `domain/document_code.py` is the single place the grammar lives, and these tests
are what make that claim checkable.

## The three properties, and why each is asserted separately

* **Every seeded code parses.** Not a sample — all twenty-eight, read from the database.
  A parser that works on the three codes in its own docstring and not on the twenty-eight
  in the seed is a parser for the docstring.
* **Every seeded code agrees with its columns.** `ONX-BO-HR-SOP-004` must carry
  `block = 'BO'` and `department = 'HR'`. This is the test that catches a hand-edited row,
  and it is the one that would have caught the information existing twice.
* **Parsing round-trips.** `parse(format(x)) == x` for every code, because a grammar that
  does not round-trip cannot be used to *make* a code and only to read one.

## A refusal is tested by its message, not by raising

Every negative test asserts the message mentions the **segment position and the offending
value**. A code is something a person types, and `invalid document code` on one is a support
ticket; `segment 3 (bộ phận) is 'H'` is an answer. The tests are what stop that message from
rotting.

## The department vocabulary is open, and that is asserted too

`HRS` parses. It is three uppercase letters, the rule is two or three, and eighteen codes
cannot establish a vocabulary — a closed list invented from them would refuse the nineteenth
department the day it arrives. `test_a_three_letter_department_is_accepted` exists so that
this is a decision on the record rather than an oversight: a closed set would be a schema
change and a migration, and nobody would make it by accident.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.document_code import PROJECT, Block, DocumentCode, DocumentKind

#: The twenty-eight codes as the dossier numbers them. Written out rather than derived, so
#: a change to the seed is a change to this list and a test failure, not a silent agreement.
DOSSIER_CODES: tuple[str, ...] = (
    "ONX-BO-FIN-SOP-001",
    "ONX-BO-FIN-SOP-002",
    "ONX-BO-FIN-SOP-003",
    "ONX-BO-HR-SOP-004",
    "ONX-BO-HR-SOP-005",
    "ONX-BO-IT-SOP-007",
    "ONX-BO-LEG-SOP-006",
    "ONX-FO-BD-SOP-001",
    "ONX-FO-BD-SOP-004",
    "ONX-FO-CR-SOP-005",
    "ONX-FO-TE-SOP-002",
    "ONX-FO-TE-SOP-003",
    "ONX-MO-DES-SOP-001",
    "ONX-MO-HSE-SOP-008",
    "ONX-MO-PM-SOP-002",
    "ONX-MO-PM-SOP-003",
    "ONX-MO-PM-SOP-004",
    "ONX-MO-PM-SOP-009",
    "ONX-MO-PM-SOP-010",
    "ONX-MO-PRC-SOP-005",
    "ONX-MO-PRC-SOP-006",
    "ONX-MO-QA-SOP-007",
    "ONX-PMO-GOV-SOP-001",
    "ONX-PMO-KNW-SOP-006",
    "ONX-PMO-REP-SOP-002",
    "ONX-PMO-RES-SOP-004",
    "ONX-PMO-RSK-SOP-003",
    "ONX-PMO-STD-SOP-005",
)


class TestTheGrammar:
    @pytest.mark.parametrize("code", DOSSIER_CODES)
    def test_every_dossier_code_parses(self, code: str) -> None:
        assert DocumentCode.parse(code).format() == code

    @pytest.mark.parametrize("code", DOSSIER_CODES)
    def test_parsing_round_trips(self, code: str) -> None:
        """`parse(format(x)) == x` — a grammar that does not round-trip cannot *make* codes."""
        parsed = DocumentCode.parse(code)
        assert DocumentCode.parse(parsed.format()) == parsed

    def test_make_and_parse_agree(self) -> None:
        built = DocumentCode.make(Block.BO, "HR", 4)
        assert built.format() == "ONX-BO-HR-SOP-004"
        assert built == DocumentCode.parse("ONX-BO-HR-SOP-004")

    def test_the_number_is_always_three_digits(self) -> None:
        """`7` and `007` are one document, and a code that formats as `7` is unparseable."""
        assert DocumentCode.make(Block.MO, "QA", 7).format() == "ONX-MO-QA-SOP-007"
        assert DocumentCode.make(Block.MO, "QA", 7).number == 7
        assert DocumentCode.make(Block.MO, "QA", 999).format() == "ONX-MO-QA-SOP-999"

    def test_make_uppercases_a_department_and_a_block(self) -> None:
        """The one place case is normalised, and it is at construction, not at parse.

        A code that parses case-insensitively has an identity that depends on the
        database's collation, so `parse` refuses lowercase. `make` normalises instead,
        because a caller with a department from a form should not have to uppercase it.
        """
        assert DocumentCode.make("bo", "hr", 4).format() == "ONX-BO-HR-SOP-004"

    def test_the_prefix_is_the_handle_a_block_owns(self) -> None:
        code = DocumentCode.parse("ONX-BO-HR-SOP-004")
        assert code.prefix == "ONX-BO-HR-SOP-"
        assert DocumentCode.parse("ONX-BO-HR-SOP-005").prefix == code.prefix
        assert DocumentCode.parse("ONX-MO-PM-SOP-002").prefix != code.prefix

    def test_next_number_refuses_at_the_end_of_the_scheme(self) -> None:
        assert DocumentCode.make(Block.BO, "HR", 998).next_number().number == 999
        with pytest.raises(ValueError, match="numbering decision"):
            DocumentCode.make(Block.BO, "HR", 999).next_number()

    def test_is_in_filters_by_block_and_department(self) -> None:
        code = DocumentCode.parse("ONX-BO-HR-SOP-004")
        assert code.is_in(block=Block.BO, department="HR")
        assert code.is_in(block="bo", department="hr"), "a filter normalises its input"
        assert not code.is_in(block=Block.MO)
        assert not code.is_in(department="IT")
        assert code.is_in(), "no filter matches everything"


class TestRefusals:
    @pytest.mark.parametrize(
        ("code", "segment", "value"),
        (
            ("onx-bo-hr-sop-004", 1, "onx"),
            ("ONX-BO-hr-sop-004", 3, "hr"),
            ("ONX-XX-HR-SOP-004", 2, "XX"),
            ("ONX-BO-H-SOP-004", 3, "H"),
            ("ONX-BO-HRSX-SOP-004", 3, "HRSX"),
            ("ONX-BO-HR-SOP-4", 5, "4"),
            ("ONX-BO-HR-SOP-0004", 5, "0004"),
        ),
    )
    def test_a_bad_segment_names_its_position_and_value(
        self, code: str, segment: int, value: str
    ) -> None:
        with pytest.raises(ValueError) as refusal:
            DocumentCode.parse(code)
        message = str(refusal.value)
        assert f"segment {segment}" in message, message
        assert repr(value) in message or f"'{value}'" in message, message

    def test_a_wrong_project_prefix_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match=r"project segment is 'ONX'"):
            DocumentCode.parse("ABC-BO-HR-SOP-001")

    def test_a_wrong_segment_count_is_refused_with_the_scheme(self) -> None:
        with pytest.raises(ValueError) as refusal:
            DocumentCode.parse("ONX-BO-HR-SOP")
        message = str(refusal.value)
        assert "4 segment(s)" in message, "the message states what it got, not what it wanted"
        for name in ("project", "khối (block)", "bộ phận (department)"):
            assert name in message, (
                f"the shape in the message misnames a segment: {name!r} is missing. The "
                f"first version split the name on a space and printed '[bộ]' where the "
                f"segment is 'bộ phận' -- a message that misnames a segment is worse "
                f"than a terse one, because the reader trusts it."
            )

    def test_an_unknown_kind_is_refused_with_the_vocabulary(self) -> None:
        with pytest.raises(ValueError, match="loại"):
            DocumentCode.parse("ONX-BO-HR-FOO-004")

    def test_a_number_outside_the_scheme_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"001\.\.999"):
            DocumentCode.make(Block.BO, "HR", 0)
        with pytest.raises(ValueError, match=r"001\.\.999"):
            DocumentCode.make(Block.BO, "HR", 1000)

    def test_a_three_letter_department_is_accepted(self) -> None:
        """A decision on the record: the department vocabulary is **open**.

        Eighteen codes cannot establish a closed set, and a closed list invented from them
        would refuse the nineteenth department the day it arrives. Two or three uppercase
        letters is the rule, and this test says so.
        """
        assert DocumentCode.make(Block.BO, "HRS", 1).format() == "ONX-BO-HRS-SOP-001"

    def test_whitespace_is_stripped_but_nothing_else_is(self) -> None:
        assert DocumentCode.parse("  ONX-BO-HR-SOP-004  ").format() == "ONX-BO-HR-SOP-004"
        with pytest.raises(ValueError):
            DocumentCode.parse("ONX - BO - HR - SOP - 004")


class TestTheVocabularies:
    def test_the_four_blocks_are_the_dossiers(self) -> None:
        assert [b.value for b in Block] == ["BO", "FO", "MO", "PMO"]

    def test_every_block_has_a_vietnamese_name(self) -> None:
        """A block a person reads needs a name they read. `Block.BO.name_vi` is not 'BO'."""
        for block in Block:
            assert block.name_vi and block.name_vi != block.value

    def test_the_kind_vocabulary_is_sop_only_and_says_so(self) -> None:
        assert [k.value for k in DocumentKind] == ["SOP"]
        assert DocumentKind.SOP.name_vi, "the one kind still needs a readable name"

    def test_the_project_prefix_is_fixed(self) -> None:
        assert PROJECT == "ONX"
        assert all(code.startswith("ONX-") for code in DOSSIER_CODES)


class TestTheSeededCatalogue:
    """The twenty-eight rows, read from the database rather than from this file.

    A parser tested only against its own docstring is a parser tested against three
    examples. The seed is the real corpus, and a code edited by hand in a spreadsheet is
    exactly what this catches.
    """

    async def test_every_seeded_code_parses_and_agrees_with_its_columns(self) -> None:
        from sqlalchemy import text

        from ai_orchestrator.persistence.session import Database

        db = Database.from_settings(use_admin_role=True)
        try:
            async with db.session() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT code, block, department, doc_type FROM sop_definitions "
                            " ORDER BY code"
                        )
                    )
                ).all()
        finally:
            await db.dispose()
        if not rows:
            pytest.skip("no SOPs seeded; run the process-spine seed")

        for code, block, department, doc_type in rows:
            parsed = DocumentCode.parse(str(code))
            assert parsed.block.value == block, (
                f"{code}: the code says {parsed.block.value!r} and the row says {block!r}"
            )
            assert parsed.department == department, (
                f"{code}: the code says {parsed.department!r} and the row says {department!r}"
            )
            assert parsed.kind.value == doc_type, (
                f"{code}: the code says {parsed.kind.value!r} and the row says {doc_type!r}"
            )

    async def test_the_seeded_codes_are_exactly_the_dossiers(self) -> None:
        """Both directions. A code the parser does not know, and a dossier code never seeded."""
        from sqlalchemy import text

        from ai_orchestrator.persistence.session import Database

        db = Database.from_settings(use_admin_role=True)
        try:
            async with db.session() as session:
                seeded = {
                    str(r[0])
                    for r in (await session.execute(text("SELECT code FROM sop_definitions"))).all()
                }
        finally:
            await db.dispose()
        if not seeded:
            pytest.skip("no SOPs seeded; run the process-spine seed")
        assert seeded == set(DOSSIER_CODES)
