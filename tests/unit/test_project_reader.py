"""Reading project identity off a construction sheet.

Every expectation here is taken from the corpus rather than invented, because the first
version of the reader reported **1 project out of 59 workbooks** and every test would
have passed. The numbers below are what `read_corpus` measures:

    25 sheets of 83 carry an identity block, across 59 workbooks
    2 projects          'BÃI TRÀM ESTATES'  'MELIA CAM RANH BAY VILLA & RESORT'
    9 address spellings for 2 real places
    5 package spellings, 2 of them strict prefixes of another

The reader's job is to report those disagreements rather than resolve them. Which
spelling of an address is canonical is a business decision, and a reader that picks
one has made it silently.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.ingest.project_reader import (
    HeaderRead,
    ProjectHeader,
    Refusal,
    comparable,
    read_project_header,
    strip_accents,
    survey_project_headers,
)

# Rows 5-8 of `2019.04.28 HBG-HBC-BCTT/TIẾN ĐỘ THI CÔNG/TĐ BOH.xlsx`, verbatim.
REAL_ROWS: list[tuple[object, ...]] = [
    (None, None, None, None, None, None),
    (None, None, None, None, None, None),
    (None, None, None, None, None, None),
    (None, None, None, ",", None, None),
    (None, None, None, "Dự án: Bãi Tràm Estates", None, None),
    (None, None, None, "Địa điểm/Address: Xã Xuân Cảnh, Thị xã Sông Cầu, Tỉnh Phú Yên", None, None),
    (None, None, None, "Gói thầu/Package: Cơ ", None, None),
    (None, None, None, "Hạng Mục/Item: MEP", None, None),
]


class TestTheRealHeaderBlock:
    def test_it_reads_all_four_fields(self) -> None:
        header = read_project_header(REAL_ROWS).header
        assert header.project_name == "Bãi Tràm Estates"
        assert header.address == "Xã Xuân Cảnh, Thị xã Sông Cầu, Tỉnh Phú Yên"
        assert header.package == "Cơ"
        assert header.item == "MEP"

    def test_the_values_are_verbatim(self) -> None:
        """Not normalised, not title-cased, not accent-stripped.

        A reader that tidies is a reader that has made a decision, and the tidied
        value cannot be checked against the file. `comparable` exists for
        *comparing*; the stored value stays as written.
        """
        assert read_project_header(REAL_ROWS).header.project_name == "Bãi Tràm Estates"

    def test_a_trailing_space_is_stripped_but_the_word_is_not_truncated(self) -> None:
        """`'Cơ '` is `'Cơ'` — a trailing space, not a missing word.

        The corpus also contains `'Cơ điện'`, so `Cơ` is *possibly* a cut-short cell.
        That is a corpus-level finding and `survey_project_headers` is what reports
        it; the per-file reader has no business deciding, because it cannot see the
        other 58 workbooks.
        """
        assert read_project_header(REAL_ROWS).header.package == "Cơ"

    def test_it_records_which_label_each_value_came_from(self) -> None:
        """So a bilingual sheet can be audited without re-opening it."""
        labels = read_project_header(REAL_ROWS).header.labels
        assert labels["project_name"] == "Dự án"
        assert labels["item"] == "Hạng Mục"

    def test_a_read_with_a_name_is_acceptable(self) -> None:
        assert read_project_header(REAL_ROWS).accepted


class TestTheLabelIsResolvedThroughTheSameFoldingAsTheLookup:
    """The bug this file exists partly to prevent.

    `_FIELDS` was keyed by the accented spelling while the lookup folded the label
    first, so all 71 of the corpus's header labels matched the pattern and then failed
    the dictionary — and the survey reported 1 project instead of 2 with no error
    anywhere.
    """

    @pytest.mark.parametrize(
        ("label", "field_name"),
        [
            ("DỰ ÁN", "project_name"),
            ("dự án", "project_name"),
            ("Dự án", "project_name"),
            ("ĐỊA ĐIỂM", "address"),
            ("Địa điểm", "address"),
            ("Address", "address"),
            ("GÓI THẦU", "package"),
            ("Gói thầu", "package"),
            ("Package", "package"),
            ("HẠNG MỤC", "item"),
            ("Hạng Mục", "item"),
            ("Item", "item"),
            ("HỢP ĐỒNG", "contract"),
            ("Contract", "contract"),
        ],
    )
    def test_every_spelling_of_every_label_resolves(self, label: str, field_name: str) -> None:
        read = read_project_header([(f"{label}: some value",)])
        assert getattr(read.header, field_name) == "some value", (
            f"{label!r} matched the pattern but did not resolve to a field"
        )

    def test_all_caps_resolves_and_lowercase_resolves_to_the_same_field(self) -> None:
        """The corpus uses both, on the same job."""
        upper = read_project_header([("DỰ ÁN: Bãi Tràm Estates",)]).header.project_name
        lower = read_project_header([("Dự án: Bãi Tràm Estates",)]).header.project_name
        assert upper == lower

    def test_the_fullwidth_colon_is_accepted(self) -> None:
        """U+FF1A, on some sheets. An invisible character that has to be matched.

        Written as an escape so the source file stays ASCII and the character is
        unmistakably deliberate -- a fullwidth colon typed literally into a Python
        string is indistinguishable from a typo until somebody's editor reflows it.
        """
        read = read_project_header([("Dự án\uff1aBãi Tràm Estates",)])
        assert read.header.project_name == "Bãi Tràm Estates"


class TestFolding:
    @pytest.mark.parametrize(
        ("raw", "folded"),
        [
            ("Bãi Tràm", "Bai Tram"),
            ("SÔNG CẦU", "SONG CAU"),
            ("HÒA THẠNH", "HOA THANH"),
            # `Đ` does not decompose under NFD or NFKD, so it survives both and would
            # defeat every comparison built on them (F91).
            ("ĐỊA", "DIA"),
            ("địa", "dia"),
            ("Địa điểm", "Dia diem"),
        ],
    )
    def test_vietnamese_marks_fold_including_de_dia_bar(self, raw: str, folded: str) -> None:
        assert strip_accents(raw) == folded

    def test_comparable_collapses_case_and_whitespace(self) -> None:
        """The corpus varies all three and none of them is a difference anybody means."""
        assert comparable("HÒA THẠNH,  XUÂN CẢNH") == comparable("Hòa Thanh, Xuân Cảnh")
        assert comparable("  MEP  ") == comparable("mep")

    def test_comparable_distinguishes_different_places(self) -> None:
        """Folding must not be so aggressive that two sites merge."""
        assert comparable("Phú Yên") != comparable("Khánh Hòa")


class TestWhatIsRefused:
    def test_an_empty_sheet_is_refused_by_being_empty(self) -> None:
        read = read_project_header([(None, None), ("some text",), (1, 2)])
        assert read.header.is_empty
        assert not read.accepted, "no project name means no identifiable job"

    def test_a_label_with_no_value_is_refused_by_name(self) -> None:
        read = read_project_header([("Dự án:",), ("Địa điểm: x")])
        assert ("project_name", "", Refusal.EMPTY) in read.refusals
        assert not read.accepted

    def test_a_sheet_with_a_name_and_a_doubtful_value_is_still_acceptable(self) -> None:
        """The name is the only thing that identifies the job.

        Refusing the whole sheet over a bad address would throw away a project
        identity that is perfectly good, and a project row is worth having with a
        flagged address rather than not worth having at all.
        """
        read = read_project_header([("Dự án: Bãi Tràm Estates",), ("Gói thầu:",)])
        assert read.accepted
        assert ("package", "", Refusal.EMPTY) in read.refusals

    def test_a_sheet_without_a_name_is_not_acceptable_even_with_an_address(
        self,
    ) -> None:
        read = read_project_header([("Địa điểm: Phú Yên",)])
        assert not read.accepted

    def test_the_scan_window_excludes_a_later_unrelated_match(self) -> None:
        """`HEADER_SCAN_ROWS` is 14 for a reason.

        A materials table lower down carries an `Item:` column, and widening the
        window to reach it would file a material code as the job's item.
        """
        rows = [("Dự án: Bãi Tràm Estates",)] + [(None,)] * 20 + [("Item: PEN-042",)]
        assert read_project_header(rows).header.item == ""

    def test_the_first_occurrence_wins(self) -> None:
        """The block is repeated on some sheets.

        The topmost one is the header proper; a value repeated in the scan window is
        a footer echoed upwards, not a second opinion.
        """
        rows = [("Dự án: Bãi Tràm Estates",), ("Dự án: Something Else",)]
        assert read_project_header(rows).header.project_name == "Bãi Tràm Estates"


class TestTheCorpusSurvey:
    """The disagreements, reported rather than resolved."""

    def _read(self, name: str = "", **over: str) -> HeaderRead:
        return HeaderRead(header=ProjectHeader(**over), source=name)

    def test_a_field_with_one_spelling_does_not_disagree(self) -> None:
        survey = survey_project_headers([self._read(project_name="BÃI TRÀM ESTATES")])
        finding = survey.findings[0]
        assert not finding.disagrees
        assert finding.values == ("BÃI TRÀM ESTATES",)

    def test_a_prefix_of_another_value_is_flagged_as_possibly_truncated(self) -> None:
        """`'Cơ'` is a strict prefix of `'Cơ điện'`, which is what a cell cut short
        by its column width looks like.

        And the corpus contains **two** such values, not one: `Cơ` and `Cơ điện`, both
        prefixes of `Cơ điện khách sạn và nhà phụ trợ`. A detector that caught only the
        first would look like it worked.
        """
        survey = survey_project_headers(
            [
                self._read(package="Cơ"),
                self._read(package="Cơ điện"),
                self._read(package="Cơ điện khách sạn và nhà phụ trợ"),
            ]
        )
        finding = next(f for f in survey.findings if f.field_name == "package")
        assert set(finding.prefixes) == {"Cơ", "Cơ điện"}

    def test_nine_spellings_of_two_places_are_all_reported(self) -> None:
        """Every spelling, verbatim, so a person can choose with the alternatives in
        front of them rather than from memory.

        The reader's job is to make the decision *possible*, not to make it.
        """
        spellings = [
            "HOA THANH, XUAN CANH, SONG CAU, PHU YEN PROVINCE, VIETNAM",
            "HÒA THẠNH, XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN",
            "Hòa Thanh, Xuân Cảnh, Sông Cầu , Phú Yên",
            "KHU D8b, BẮC BÁN ĐẢO CAM RANH, CAM LÂM, KHÁNH HÒA",
            "Khu D8b, Bắc Bán Đảo Cam Ranh, Cam Hải Đông, Cam Lâm,  Khánh Hòa, Việt Nam",
            "Thôn Hòa Thạnh, Xã Xuân Cảnh, Huyện Sông Cầu, Tình Phú Yên",
            "XUÂN CẢNH, SÔNG CẦU, PHÚ YÊN",
            "Xuân Cảnh, Sông cầu, Phú Yên",
            "xã Xuân Cảnh, huyện Sông Cầu, tỉnh Phú Yên",
        ]
        survey = survey_project_headers(
            [self._read(address=s, project_name="x") for s in spellings]
        )
        finding = next(f for f in survey.findings if f.field_name == "address")
        assert len(finding.values) == 9
        assert finding.disagrees
        assert finding.prefixes == (), "no address is a prefix of another"

    def test_two_projects_are_two_projects(self) -> None:
        """The corpus says two. It has never said three, and nothing is called
        'Hoabinh Group' in any of the 25 sheets that carry a name."""
        survey = survey_project_headers(
            [
                self._read(project_name="BÃI TRÀM ESTATES"),
                self._read(project_name="MELIA CAM RANH BAY VILLA & RESORT"),
            ]
        )
        finding = next(f for f in survey.findings if f.field_name == "project_name")
        assert finding.values == ("BÃI TRÀM ESTATES", "MELIA CAM RANH BAY VILLA & RESORT")

    def test_a_corpus_with_nothing_in_it_reports_nothing(self) -> None:
        survey = survey_project_headers([])
        assert survey.sheets_read == 0
        assert survey.findings == ()

    def test_the_survey_counts_sheets_and_sheets_with_identity(self) -> None:
        """The two numbers differ, and the difference is the finding.

        25 of 83 sheets carry an identity block; a survey reporting only the second
        number would hide that 58 sheets were looked at and had nothing to say.
        """
        survey = survey_project_headers(
            [self._read(project_name="BÃI TRÀM ESTATES"), self._read(), self._read()]
        )
        assert survey.sheets_read == 3
        assert survey.sheets_with_identity == 1
