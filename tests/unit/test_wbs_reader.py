"""The work breakdown structure, read from a real progress sheet.

Every expectation is measured on `TĐ BOH.xlsx :: TĐ .BOH`. The numbers this file
asserts — 12 nodes, 8 systems, 4 zones, 110 activities, 5 backwards windows — are the
sheet's, and a reader that quietly produced a different count would be a reader
inventing structure the file does not have.

The central claim under test is that **the label does not say what level a row is**.
Row 16 is labelled `A` and is a system; row 47 is labelled `A` and is a zone. Any rule
keyed on the label shape produces a tree that looks right and is wrong, which is the
F96 shape one layer up.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ai_orchestrator.ingest.sheets import SheetKind, SheetShape, detect_sheet
from ai_orchestrator.ingest.wbs_reader import (
    Finding,
    NodeLevel,
    read_structure,
)

D = dt.date
CORPUS = (
    "/home/vutun/pmo_project/reference_sheets/2019.04.28 HBG-HBC-BCTT/TIẾN ĐỘ THI CÔNG/TĐ BOH.xlsx"
)
NEEDS_FILE = pytest.mark.skipif(
    not __import__("pathlib").Path(CORPUS).exists(), reason=f"{CORPUS} not present"
)


@pytest.fixture(scope="module")
def corpus_shape() -> SheetShape:
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(CORPUS, data_only=True)
    sheet = book[book.sheetnames[0]]
    rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
    book.close()
    return detect_sheet(rows).shapes[0]


@pytest.fixture(scope="module")
def corpus_rows() -> list[tuple[object, ...]]:
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(CORPUS, data_only=True)
    sheet = book[book.sheetnames[0]]
    rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
    book.close()
    return rows


@pytest.fixture(scope="module")
def structure(corpus_rows, corpus_shape):
    return read_structure(corpus_rows, corpus_shape)


# ------------------------------------------------------------- real structure --


@NEEDS_FILE
class TestTheRealSheet:
    def test_twelve_nodes_split_eight_systems_and_four_zones(self, structure) -> None:
        assert len(structure.nodes) == 12
        assert sum(1 for n in structure.nodes if n.level is NodeLevel.SYSTEM) == 8
        assert sum(1 for n in structure.nodes if n.level is NodeLevel.ZONE) == 4

    def test_one_hundred_and_ten_activities_with_no_orphans(self, structure) -> None:
        """Every activity found a node, and that is not guaranteed — see the synthetic
        sheet below, where one deliberately does not."""
        assert len(structure.activities) == 110
        orphans = [a for a in structure.activities if a.parent_id is None]
        assert orphans == [], f"{len(orphans)} activity/activities have no node"

    def test_the_level_split_is_by_window_not_by_label(self, structure) -> None:
        """The sheet labels a system `A` (row 16) and a zone `A` (row 47).

        So a reader keyed on the label would file one of them two levels too shallow,
        and the tree would look plausible.
        """
        # A list, not a dict keyed by label: two nodes share the label `A`, and
        # `{n.label: n for n in nodes}` silently keeps the last one, which is exactly
        # the kind of collapse this test exists to rule out.
        by_a = [n for n in structure.nodes if n.label == "A"]
        assert len(by_a) == 2
        assert {n.level for n in by_a} == {NodeLevel.SYSTEM, NodeLevel.ZONE}, (
            "the same label appears at both levels, which is why the label cannot decide the level"
        )

    def test_a_node_with_a_window_is_a_system_and_one_without_is_a_zone(self, structure) -> None:
        for node in structure.nodes:
            has_window = node.planned_start is not None and node.planned_finish is not None
            assert (node.level is NodeLevel.SYSTEM) == has_window, (
                f"{node.node_id} at row {node.row_no} breaks the window rule"
            )

    def test_all_four_zones_sit_under_the_same_system(self, structure) -> None:
        """The one system that precedes `Zone A` in the sheet is
        `Hệ thống thông gió và điều hòa không khí/HVAC`, and the sheet puts all four
        zones under it.

        The sheet never says so in words. It says it by ordering, and an unparented zone
        would be a root competing with the systems.
        """
        zones = [n for n in structure.nodes if n.level is NodeLevel.ZONE]
        assert len({z.parent_id for z in zones}) == 1
        # Look the parent up *by id*. Matching on `n.parent_id == zones[0].parent_id`
        # finds Zone A itself first, because Zone A is one of the nodes that points at
        # it -- a self-match that reads as a failure of the reader and is a failure of
        # the test.
        parent_id = zones[0].parent_id
        hvac = next(n for n in structure.nodes if n.node_id == parent_id)
        assert hvac.level is NodeLevel.SYSTEM
        assert "thông gió" in hvac.description

    def test_activities_attach_at_both_depths(self, structure) -> None:
        """22 directly under a system, 88 under a zone.

        The sheet uses both, so a reader that assumed one depth would either lose the
        22 or invent four zones for them.
        """
        by_level: dict[NodeLevel | None, int] = {}
        for activity in structure.activities:
            by_level[activity.parent_level] = by_level.get(activity.parent_level, 0) + 1
        assert by_level == {NodeLevel.SYSTEM: 22, NodeLevel.ZONE: 88}

    def test_the_first_node_is_provably_the_reports_own_total(self, structure) -> None:
        """`BOH`'s window equals the union of all 110 activities' windows, exactly.

        That is a proof rather than an assertion about a label, and it is what
        distinguishes a work package from the row that totals them.
        """
        first = structure.nodes[0]
        assert first.description == "BOH"
        assert Finding.SUMMARY_ROW in first.findings
        assert (first.planned_start, first.planned_finish) == structure.overall_window
        assert structure.overall_window == (D(2019, 3, 13), D(2019, 9, 20))

    def test_and_it_is_the_only_node_with_nothing_underneath(self, structure) -> None:
        """A second, independent signal agreeing with the first.

        Everything is a *sibling* of the total rather than a child, so the total has no
        children and no activities. Two unrelated observations landing on the same row
        is much stronger than either alone.
        """
        empty = [
            n
            for n in structure.nodes
            if not structure.activities_of(n.node_id) and not structure.children_of(n.node_id)
        ]
        assert [n.node_id for n in empty] == [structure.nodes[0].node_id]

    def test_five_nodes_finish_before_they_start(self, structure) -> None:
        """Rows 24, 26, 31, 36 and 41 — each ends one day *before* it begins.

        The corpus counts days inclusively, so a one-day item has `start == finish`.
        A node ending before it starts is a typo in the file, and the reader reports
        it rather than normalising a date it did not invent.
        """
        backwards = [n for n in structure.nodes if Finding.BACKWARDS_WINDOW in n.findings]
        assert len(backwards) == 5
        assert [n.label for n in backwards] == ["II", "III", "IV", "V", "VI"]
        for node in backwards:
            assert node.planned_finish < node.planned_start

    def test_a_backwards_node_is_not_schedulable(self, structure) -> None:
        assert not next(n for n in structure.nodes if n.label == "II").is_schedulable

    def test_a_zone_with_no_window_is_not_schedulable(self, structure) -> None:
        """It is not unschedulable in reality — just not from this file.

        Filling it with the union of its children would be a fact about the file
        presented as a fact about the plan.
        """
        zone = next(n for n in structure.nodes if n.description == "Zone A")
        assert Finding.NO_WINDOW in zone.findings
        assert not zone.is_schedulable
        assert zone.planned_start is None and zone.planned_finish is None

    def test_six_systems_share_one_description(self, structure) -> None:
        """`I` through `VI` are all "Hệ thống cấp thoát nước", one per zone.

        Not a defect — six zones of one system is a real shape — but it means a WBS
        keyed on description would collapse them and lose five of six.
        """
        shared = [n for n in structure.nodes if n.description.startswith("Hệ thống cấp thoát nước")]
        assert len(shared) == 6
        assert len({n.label for n in shared}) == 6

    def test_the_findings_are_reported_rather_than_repaired(self, structure) -> None:
        assert set(structure.all_findings) == {
            Finding.BACKWARDS_WINDOW,
            Finding.NO_WINDOW,
            Finding.SUMMARY_ROW,
        }

    def test_every_node_can_point_at_its_row(self, structure) -> None:
        """A finding has to be actionable, and an action needs a line number."""
        for node in structure.nodes:
            assert node.row_no >= 15, f"{node.node_id} at row {node.row_no} is a header row"


# ------------------------------------------------------------- synthetic cases --


def _shape() -> SheetShape:
    """A shape with the roles `read_structure` needs and nothing else.

    Hand-built rather than run through `detect_sheet` so a synthetic case cannot be
    made to pass by a detection quirk.
    """
    from ai_orchestrator.ingest.sheets import Column

    # `role` is a plain string drawn from `sheets.COLUMN_ROLES`, not an enum, so these
    # are the literal role names the reader looks up.
    return SheetShape(
        kind=SheetKind.CONSTRUCTION_PROGRESS,
        header_row=0,
        columns=(
            Column(index=0, label="Stt/No", role="line_no"),
            Column(index=1, label="Công việc thi công/Work", role="work_description"),
            Column(index=2, label="Ngày bắt đầu KH", role="planned_start_on"),
            Column(index=3, label="Ngày kết thúc KH", role="planned_finish_on"),
            Column(index=4, label="Số ngày KH", role="planned_duration_days"),
            Column(index=5, label="Số ngày TT", role="actual_duration_days"),
        ),
    )


def _row(
    label: object = None,
    work: str = "",
    start: object = None,
    finish: object = None,
    days: object = None,
) -> tuple[object, ...]:
    return (label, work, start, finish, days, days)


class TestTheRulesOnASheetWeControl:
    def test_an_activity_before_any_node_is_an_orphan(self) -> None:
        """Not guaranteed by the real file, so it has to be its own case.

        The activity is still returned — dropping it would lose a line of a schedule —
        but it has no parent and `ORPHAN_ACTIVITY` is reported.
        """
        st = read_structure([_row(label="1", work="Task", days=3)], _shape())
        assert len(st.activities) == 1
        assert st.activities[0].parent_id is None
        assert Finding.ORPHAN_ACTIVITY in st.all_findings

    def test_the_same_label_at_two_levels_does_not_decide_the_level(self) -> None:
        """The whole reason the window rule exists, on a sheet where it is unambiguous."""
        st = read_structure(
            [
                _row(label="A", work="System A", start=D(2019, 1, 1), finish=D(2019, 1, 9)),
                _row(label="1", work="Task", days=3),
                _row(label="A", work="Zone A"),
                _row(label="1", work="Task 2", days=2),
            ],
            _shape(),
        )
        assert len(st.nodes) == 2
        # Positional, because the two share a label and a dict would keep only one.
        assert st.nodes[0].label == st.nodes[1].label == "A"
        assert st.nodes[0].level is NodeLevel.SYSTEM, "it has a window"
        assert st.nodes[1].level is NodeLevel.ZONE, "it does not"

    def test_a_zone_closes_when_the_next_system_opens(self) -> None:
        """Otherwise everything after the first zone would land in it.

        This is the bug a naive "most recent zone" rule has, and the real file has four
        zones in a row with no system between them — so the real file cannot catch it.
        """
        st = read_structure(
            [
                _row(label="1", work="Sys 1", start=D(2019, 1, 1), finish=D(2019, 1, 9)),
                _row(label="A", work="Zone A"),
                _row(label="1", work="In A", days=1),
                _row(label="2", work="Sys 2", start=D(2019, 2, 1), finish=D(2019, 2, 9)),
                _row(label="1", work="After 2", days=1),
            ],
            _shape(),
        )
        assert st.activities[0].parent_level is NodeLevel.ZONE
        assert st.activities[1].parent_level is NodeLevel.SYSTEM, (
            "a new system must close the open zone, or every later activity is "
            "attributed to a zone that ended before it started"
        )

    def test_a_summary_row_needs_the_whole_sheet_to_be_provable(self) -> None:
        """Not provable from its own two cells.

        A window that covers everything is only suspicious once you know what
        everything is, which is why `_annotate` runs second.
        """
        rows = [
            _row(label="T", work="TOTAL", start=D(2019, 1, 1), finish=D(2019, 1, 5)),
            _row(label="1", work="Sys 1", start=D(2019, 1, 1), finish=D(2019, 1, 4)),
            _row(label="1", work="Task", start=D(2019, 1, 1), finish=D(2019, 1, 5), days=5),
        ]
        st = read_structure(rows, _shape())
        total = st.nodes[0]
        assert (total.planned_start, total.planned_finish) == st.overall_window
        assert Finding.SUMMARY_ROW in total.findings
        # And the system, which is a strict subset, is not flagged.
        assert Finding.SUMMARY_ROW not in st.nodes[1].findings

    def test_a_system_matching_the_union_by_accident_is_still_flagged(self) -> None:
        """The cost of the rule, stated: it is a window comparison, not a proof of intent.

        A system that genuinely spans the whole programme is indistinguishable from a
        total row on this evidence alone. Reporting it is the safe direction — the
        cost is a person reading one extra finding.
        """
        rows = [
            _row(label="S", work="Whole programme", start=D(2019, 1, 1), finish=D(2019, 1, 9)),
            _row(label="1", work="Task", start=D(2019, 1, 1), finish=D(2019, 1, 9), days=9),
        ]
        st = read_structure(rows, _shape())
        assert Finding.SUMMARY_ROW in st.nodes[0].findings

    def test_a_node_with_no_window_and_no_activities_is_still_reported(self) -> None:
        """An empty zone is a fact about the plan: nobody has scoped it yet."""
        st = read_structure(
            [
                _row(label="1", work="Sys", start=D(2019, 1, 1), finish=D(2019, 1, 4)),
                _row(label="A", work="Zone A"),
            ],
            _shape(),
        )
        zone = st.nodes[1]
        assert not structure_has_children(st, zone.node_id)
        assert Finding.NO_WINDOW in zone.findings

    def test_a_row_with_no_description_is_skipped_entirely(self) -> None:
        """Blank and separator rows carry no work, so they are not nodes."""
        st = read_structure(
            [
                _row(label="1", work="Sys", start=D(2019, 1, 1), finish=D(2019, 1, 4)),
                (None, None, None, None, None, None),
                (None, "   ", None, None, None, None),
                _row(label="1", work="Task", days=2),
            ],
            _shape(),
        )
        assert len(st.nodes) == 1
        assert len(st.activities) == 1

    def test_the_structure_serialises(self) -> None:
        st = read_structure(
            [
                _row(label="1", work="Sys", start=D(2019, 1, 1), finish=D(2019, 1, 4)),
                _row(label="1", work="Task", days=2),
            ],
            _shape(),
        )
        got = st.as_dict()
        assert got["nodes"] == 1 and got["activities"] == 1
        assert got["levels"] == {"system": 1, "zone": 0}
        assert got["findings"] == []


def structure_has_children(st, node_id: str) -> bool:
    return bool(st.children_of(node_id))
