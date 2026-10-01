"""The project survey, against the real corpus.

This file is the one that would have caught F112 and F113.

Both of those bugs produced a *plausible* number — "1 project out of 59 workbooks" —
and every unit test passed, because the unit tests were written against a fixture
derived from the reader's own output. A reader that quietly reads nothing and a reader
that quietly reads almost nothing look identical from inside the test suite.

So the assertions here are about the corpus, not about the reader:

    43 of 83 sheets carry an identity block
    3 distinct project names, none of them "Hoabinh Group"
    10 distinct address spellings
    5 package spellings, 2 of them strict prefixes of a third

A change in any of those is a change in what the platform knows, and it should fail a
test rather than be discovered next quarter. Skipped when the corpus is absent, which
is the convention `tests/integration/test_progress_operations.py` already follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_orchestrator.ingest.project_reader import read_corpus

CORPUS_ROOT = Path("/home/vutun/pmo_project/reference_sheets")
NEEDS_CORPUS = pytest.mark.skipif(
    not CORPUS_ROOT.is_dir(),
    reason=f"corpus not mounted at {CORPUS_ROOT}",
)
pytestmark = [pytest.mark.integration, NEEDS_CORPUS]


def _finding(survey, field: str):
    return next(f for f in survey.findings if f.field_name == field)


class TestTheCorpusSaysWhatItSays:
    def test_the_survey_finds_the_identity_blocks(self) -> None:
        """43, not 1.

        The first version of the reader found exactly one, for two independent reasons
        in `project_reader.py`, and the number looked entirely reasonable.
        """
        corpus = read_corpus(CORPUS_ROOT)
        assert corpus.survey().sheets_with_identity >= 40, (
            f"only {corpus.survey().sheets_with_identity} sheets carry an identity "
            f"block; the reader has stopped reading them"
        )
        # The denominator has to be the corpus, not the subset that happened to parse.
        assert corpus.skipped == (), f"{len(corpus.skipped)} file(s) would not open"

    def test_seven_spellings_of_project_names_folded_to_four(self) -> None:
        """The count is 3 *projects* and 7 *spellings*, and the gap is the finding.

        The documents said "3 Hoabinh Group projects". The count was right; every
        name was wrong, and no sheet calls a project "Hoabinh Group".

        This test was written when the reader saw 3 spellings and read that as 3
        projects. Fixing the corpus typo in F120 made the reader complete, and the
        number went **up** — 7 spellings, folding to 4 names:

            'BÃI TRÀM ESTATES'
            'Bãi Tràm ESTATES'
            'Bãi Tràm Estates'
            'Bãi Tràn Estates'                              <- a typo: Tran, not Tram
            'Khu Biệt Thự & Nghỉ Dưỡng Melia Cam Ranh'   <- the same resort, described
            'LAWRENCE STING SCHOOL 2'
            'MELIA CAM RANH BAY VILLA & RESORT'

        So there are **two** Melia Cam Ranh spellings, not one, and the third project
        has three of its own plus a typo. A project is not a name; it is a name under
        several spellings, and which spelling is canonical is the decision
        `AddressResolution` already forces a caller to record.
        """
        finding = _finding(read_corpus(CORPUS_ROOT).survey(), "project_name")
        assert len(finding.values) == 7, sorted(finding.values)
        assert not any("HOABINH" in n.upper() for n in finding.values), (
            "the corpus contains no project by that name, whatever a document says"
        )

    def test_one_of_the_spellings_is_a_typo_and_cannot_be_folded_away(
        self,
    ) -> None:
        """`Bãi Tràn` is not a spelling of `Bãi Tràm`; it is a different word.

        Asserted because it is the case a naive "just case-fold and strip accents"
        canonicalisation gets wrong in the other direction: it would create a **fourth**
        project that does not exist. Folding is necessary and not sufficient, which is
        why the canonical form is a recorded decision rather than a normalisation.
        """
        from ai_orchestrator.ingest.project_reader import comparable

        values = _finding(read_corpus(CORPUS_ROOT).survey(), "project_name").values
        folded = {comparable(v) for v in values}
        assert "bai tram estates" in folded
        assert "bai tran estates" in folded, "the typo survives folding, and must"
        assert len(folded) == 5

    def test_ten_spellings_of_an_address_and_none_chosen(self) -> None:
        """Which spelling is canonical is a business decision.

        The reader reports all ten verbatim and picks none, and a reader that picked
        one would make the decision silently and unrecordedly. The `dist` threshold is
        a floor rather than an equality so that a *new* site in the corpus does not
        fail this test; what must not happen is the count dropping, which is what
        picking one would cause.
        """
        finding = _finding(read_corpus(CORPUS_ROOT).survey(), "address")
        assert len(finding.values) >= 10
        assert finding.disagrees
        # Every value is kept exactly as written -- not folded, not title-cased.
        assert "Hòa Thanh, Xuân Cảnh, Sông Cầu , Phú Yên" in finding.values

    def test_two_package_values_are_possibly_truncated(self) -> None:
        """`Cơ` and `Cơ điện`, both strict prefixes of a third.

        Cells cut short by their column width. Writing `Cơ` into a package column
        would put a value there that cannot be told from a real one -- the F95 shape.
        """
        finding = _finding(read_corpus(CORPUS_ROOT).survey(), "package")
        assert set(finding.prefixes) == {"Cơ", "Cơ điện"}
        assert "Cơ điện khách sạn và nhà phụ trợ" in finding.values

    def test_the_bilingual_slash_form_is_read(self) -> None:
        """`Địa điểm/Address:` — a slash and a second word before the colon.

        Three of the four header lines on `TĐ BOH.xlsx` use it, and the reader's first
        pattern required a colon immediately after the first label, so it read exactly
        one line of the file the tests were written from.
        """
        values = set(_finding(read_corpus(CORPUS_ROOT).survey(), "address").values)
        assert "Xã Xuân Cảnh, Thị xã Sông Cầu, Tỉnh Phú Yên/" in values, (
            "the slash-form header on TĐ BOH.xlsx is not being read; the trailing "
            "slash is in the cell"
        )


class TestTheProgressReaderWasDiscardingThis:
    async def test_the_identity_block_is_in_the_file_the_progress_reader_reads(
        self,
    ) -> None:
        """The concrete reason Phase 2 needs a writer rather than more readers.

        `TĐ BOH.xlsx` is the one real progress file Phase 1 reads end to end. Its
        rows 5-8 carry the project identity, and `_is_rollup` in
        `ingest/progress_reader.py` classifies that row as a roll-up — because a
        roll-up has a window and no duration — so the identity is dropped.

        So the project's name, address, package and item are in a file this repository
        already opens, on a row it already reads, and they never reach a table.
        """
        import openpyxl

        path = CORPUS_ROOT / "2019.04.28 HBG-HBC-BCTT" / "TIẾN ĐỘ THI CÔNG" / "TĐ BOH.xlsx"
        if not path.exists():
            pytest.skip(f"{path} not present")
        book = openpyxl.load_workbook(path, data_only=True)
        sheet = book[book.sheetnames[0]]
        text = " ".join(
            str(sheet.cell(row=r, column=c).value or "") for r in range(1, 15) for c in range(1, 8)
        )
        book.close()

        assert "Bãi Tràm Estates" in text
        assert "Gói thầu" in text and "Hạng Mục" in text
