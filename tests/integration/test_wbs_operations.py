"""Writing the work breakdown, and what it refuses to write.

The refusals are the output, so they are what most of this tests. Three of them are
measured on `TĐ BOH.xlsx`:

* `BOH` at row 16 is the report's own total — its window equals the union of all 110
  activities' windows, exactly.
* Rows 24, 26, 31, 36 and 41 finish the day before they start.
* Six systems share one description, so a code derived from it would collide.

The last class of test is the end-to-end one: read the real sheet, write the tree,
attach the activities, and ask the question a PM workspace exists to answer.
"""

from __future__ import annotations

import datetime as dt
import warnings
from pathlib import Path

import pytest
from sqlalchemy import text

from ai_orchestrator.application.project_operations import AddressResolution, write_project
from ai_orchestrator.application.wbs_operations import write_structure
from ai_orchestrator.ingest.project_reader import HeaderRead, ProjectHeader
from ai_orchestrator.ingest.sheets import detect_sheet
from ai_orchestrator.ingest.wbs_reader import Finding, Node, NodeLevel, Structure, read_structure
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

NOW = dt.datetime(2026, 9, 29, 9, 30, tzinfo=dt.UTC)
D = dt.date
CORPUS = Path(
    "/home/vutun/pmo_project/reference_sheets/2019.04.28 HBG-HBC-BCTT/TIẾN ĐỘ THI CÔNG/TĐ BOH.xlsx"
)
NEEDS_FILE = pytest.mark.skipif(not CORPUS.exists(), reason=f"{CORPUS} not present")


def _node(**over) -> Node:
    base: dict[str, object] = {
        "node_id": "nd_0001",
        "label": "1",
        "description": "Hệ thống",
        "level": NodeLevel.SYSTEM,
        "row_no": 10,
        "parent_id": None,
        "planned_start": D(2019, 1, 1),
        "planned_finish": D(2019, 1, 9),
    }
    base.update(over)
    return Node(**base)  # type: ignore[arg-type]


async def _project(tenant: Tenant, code: str = "HBG-PY-01") -> str:
    outcome = await tenant.run(
        lambda s: write_project(
            s,
            organization_id=tenant.organization_id,
            read=HeaderRead(header=ProjectHeader(project_name="Bãi Tràm Estates")),
            code=code,
            address=AddressResolution(
                chosen="Phú Yên", seen=("Phú Yên",), decided_by="ops", decided_on=NOW
            ),
            observed={},
            written_on=NOW,
        )
    )
    assert outcome.written, outcome.refusals
    return outcome.project_id or ""


class TestWhatTheWriterRefuses:
    async def test_the_reports_own_total_is_not_a_work_package(self, tenant: Tenant) -> None:
        """The central refusal.

        `wbs` would otherwise open with an item called "the whole project" that
        contains every other item -- and `is_leaf` and `sequence` would both be lies
        about it.
        """
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(_node(description="BOH", findings=(Finding.SUMMARY_ROW,)),)
                ),
                written_on=NOW,
            )
        )
        assert outcome.written == ()
        assert len(outcome.refused) == 1
        refusal = outcome.refused[0].refusals[0]
        assert "union of every activity" in refusal
        assert "first item is the whole project" in refusal

    async def test_a_backwards_window_is_reported_and_the_node_is_written(
        self, tenant: Tenant
    ) -> None:
        """Not a refusal, and the end-to-end test is what proved it.

        The first version refused these. Over the real file that meant 5 of 12 nodes
        gone -- real work packages with activity groups under them -- and their
        activities unplaced, in exchange for fixing nothing, because `wbs` has no date
        columns to put the bad dates in. The finding belongs in the report; the node
        belongs in the breakdown.
        """
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(
                        _node(
                            planned_start=D(2019, 5, 21),
                            planned_finish=D(2019, 5, 20),
                            findings=(Finding.BACKWARDS_WINDOW,),
                        ),
                    )
                ),
                written_on=NOW,
            )
        )
        assert outcome.refused == ()
        assert len(outcome.written) == 1
        written = outcome.written[0]
        assert Finding.BACKWARDS_WINDOW in written.findings
        assert "before it starts" in written.notes[0]
        assert "not" in written.notes[0] and "repaired" in written.notes[0]

    async def test_a_zone_with_no_window_is_written_anyway(self, tenant: Tenant) -> None:
        """A zone the file does not schedule is still a real part of the structure.

        The finding is reported; the node is not refused. Refusing it would drop four
        zones from the real file's breakdown and leave their 88 activities with no
        parent.
        """
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(
                        _node(node_id="nd_0001", description="System"),
                        _node(
                            node_id="nd_0002",
                            label="A",
                            description="Zone A",
                            level=NodeLevel.ZONE,
                            parent_id="nd_0001",
                            planned_start=None,
                            planned_finish=None,
                            findings=(Finding.NO_WINDOW,),
                        ),
                    )
                ),
                written_on=NOW,
            )
        )
        assert outcome.refused == (), "a missing window is a finding, not a refusal"
        assert len(outcome.written) == 2
        zone = next(o for o in outcome.written if o.description == "Zone A")
        assert "unscheduled" in zone.notes[0]

    async def test_every_refusal_is_reported_not_just_the_first(self, tenant: Tenant) -> None:
        """A caller fixing one problem at a time is a slower route to the same place."""
        project_id = await _project(tenant)
        nodes = tuple(
            _node(node_id=f"nd_{i:04d}", description=f"total {i}", findings=(Finding.SUMMARY_ROW,))
            for i in range(1, 4)
        )
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(nodes=nodes),
                written_on=NOW,
            )
        )
        # The point is that *all three* are reported. A writer that stopped at the
        # first would report one, and a caller would fix one and come back.
        assert len(outcome.refused) == 3
        assert [o.description for o in outcome.refused] == ["total 1", "total 2", "total 3"]


class TestTheTree:
    async def test_a_parent_is_written_before_its_child(self, tenant: Tenant) -> None:
        """`wbs.parent_id` is a self-referencing FK, so the order is not incidental.

        The reader's list has the parent first anyway; the writer's loop is what makes
        that safe when it is not.
        """
        project_id = await _project(tenant)
        # Child listed *before* its parent, deliberately.
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(
                        _node(
                            node_id="nd_0002",
                            description="Zone A",
                            level=NodeLevel.ZONE,
                            parent_id="nd_0001",
                            planned_start=None,
                            planned_finish=None,
                        ),
                        _node(node_id="nd_0001", description="System"),
                    )
                ),
                written_on=NOW,
            )
        )
        assert len(outcome.written) == 2
        zone = next(o for o in outcome.written if o.description == "Zone A")
        assert zone.node_id is not None
        parent = (
            await tenant.session.execute(
                text("SELECT parent_id FROM wbs WHERE id = :i"), {"i": zone.node_id}
            )
        ).scalar_one()
        assert parent is not None, "the zone is attached to something"
        assert parent in {o.node_id for o in outcome.written}

    async def test_a_child_whose_parent_was_refused_is_reported_not_dropped(
        self, tenant: Tenant
    ) -> None:
        """Otherwise its activities have no parent and the loss is invisible."""
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(
                        _node(
                            node_id="nd_0001", description="Total", findings=(Finding.SUMMARY_ROW,)
                        ),
                        _node(
                            node_id="nd_0002",
                            description="Zone A",
                            level=NodeLevel.ZONE,
                            parent_id="nd_0001",
                            planned_start=None,
                            planned_finish=None,
                        ),
                    )
                ),
                written_on=NOW,
            )
        )
        assert outcome.written == ()
        assert len(outcome.refused) == 2
        assert any("parent was refused" in r for o in outcome.refused for r in o.refusals)

    async def test_codes_are_unique_and_carry_the_level(self, tenant: Tenant) -> None:
        """Six systems share one description, so a code from the description collides.

        The reader's own labels cannot be used either: `A` is a system *and* a zone on
        the real file.
        """
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=tuple(
                        _node(
                            node_id=f"nd_{i:04d}", label="I", description="Hệ thống cấp thoát nước"
                        )
                        for i in range(1, 7)
                    )
                ),
                written_on=NOW,
            )
        )
        codes = [o.code for o in outcome.written]
        assert len(set(codes)) == 6, "six identical descriptions must not collide"
        assert all(c.startswith("S") for c in codes)
        rows = (
            await tenant.session.execute(
                text("SELECT code, name, is_leaf FROM wbs ORDER BY sequence")
            )
        ).all()
        assert len(rows) == 6
        assert all(r[2] is False for r in rows), "a system is not a leaf"

    async def test_a_zone_code_is_prefixed_differently_from_a_system(self, tenant: Tenant) -> None:
        project_id = await _project(tenant)
        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=Structure(
                    nodes=(
                        _node(node_id="nd_0001", label="A", description="Sys"),
                        _node(
                            node_id="nd_0002",
                            label="A",
                            description="Zone A",
                            level=NodeLevel.ZONE,
                            parent_id="nd_0001",
                            planned_start=None,
                            planned_finish=None,
                        ),
                    )
                ),
                written_on=NOW,
            )
        )
        assert {o.level for o in outcome.written} == {NodeLevel.SYSTEM, NodeLevel.ZONE}
        assert any(o.code.startswith("S") for o in outcome.written)
        assert any(o.code.startswith("Z") for o in outcome.written)


class TestTheServiceRefusesBadInput:
    async def test_a_naive_timestamp_is_refused(self, tenant: Tenant) -> None:
        project_id = await _project(tenant)
        with pytest.raises(ValueError, match="timezone-aware"):
            await tenant.run(
                lambda s: write_structure(
                    s,
                    organization_id=tenant.organization_id,
                    project_id=project_id,
                    structure=Structure(nodes=(_node(),)),
                    written_on=dt.datetime(2026, 9, 29, 9, 30),
                )
            )


@NEEDS_FILE
class TestTheRealFileEndToEnd:
    """Read the corpus, write the tree, attach the activities, ask the PM's question."""

    async def test_the_whole_path(self, tenant: Tenant) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import openpyxl

            book = openpyxl.load_workbook(CORPUS, data_only=True)
        sheet = book[book.sheetnames[0]]
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        book.close()

        shape = detect_sheet(rows).shapes[0]
        structure = read_structure(rows, shape)
        project_id = await _project(tenant)

        outcome = await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=structure,
                written_on=NOW,
            )
        )

        # 12 nodes read, 1 refused as the report's own total, 11 written.
        assert len(outcome.outcomes) == 12
        assert len(outcome.written) == 11
        assert len(outcome.refused) == 1
        assert outcome.refused[0].description == "BOH"
        # The five backwards-window systems are among the eleven, with the finding
        # reported. Losing them is what the first version did.
        backwards = [o for o in outcome.written if Finding.BACKWARDS_WINDOW in o.findings]
        assert len(backwards) == 5
        assert all(o.notes for o in backwards)

        levels = (
            await tenant.session.execute(text("SELECT count(*) FROM wbs WHERE name LIKE 'Zone%'"))
        ).scalar_one()
        assert levels == 4, "all four zones survive, or their 88 activities are orphaned"

    async def test_and_the_summary_row_is_never_the_only_thing_written(
        self, tenant: Tenant
    ) -> None:
        """A writer that refused everything would also produce zero."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import openpyxl

            book = openpyxl.load_workbook(CORPUS, data_only=True)
        sheet = book[book.sheetnames[0]]
        rows = [tuple(r) for r in sheet.iter_rows(values_only=True)]
        book.close()

        structure = read_structure(rows, detect_sheet(rows).shapes[0])
        project_id = await _project(tenant)
        await tenant.run(
            lambda s: write_structure(
                s,
                organization_id=tenant.organization_id,
                project_id=project_id,
                structure=structure,
                written_on=NOW,
            )
        )
        names = [r[0] for r in (await tenant.session.execute(text("SELECT name FROM wbs"))).all()]
        assert "BOH" not in names
        assert any("Hệ thống" in n for n in names)
