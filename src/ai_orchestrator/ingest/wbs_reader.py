"""The work breakdown structure, read off a construction-progress sheet.

Every rule here was measured on `TĐ BOH.xlsx :: TĐ .BOH`, the one real progress
file, because the sheet's own labels are useless for this and a reader that trusted
them would produce a tree that looks plausible and is wrong.

## What the sheet actually is

    row 16   A    window 2019-03-13..2019-09-20   BOH
    row 18   I    window 2019-03-13..2019-09-08   Hệ thống cấp thoát nước
    row 24   II   window 2019-05-21..2019-05-20   Hệ thống cấp thoát nước
    row 26   III  window 2019-05-21..2019-05-20   Hệ thống cấp thoát nước
    row 31   IV   window 2019-06-30..2019-06-29   Hệ thống cấp thoát nước
    row 36   V    window 2019-07-15..2019-07-14   Hệ thống cấp thoát nước
    row 41   VI   window 2019-08-03..2019-08-02   Hệ thống cấp thoát nước
    row 46   2    window 2019-05-21..2019-09-20   Hệ thống thông gió và điều hòa
    row 47   A    no window                       Zone A
    row 69   B    no window                       Zone B
    row 91   C    no window                       Zone C
    row 113  D    no window                       Zone D
    ...     1..n  with `Số ngày`                   110 activities

## The label does not say what level a row is

`A` is row 16, a level-1 node, **and** row 47, a level-2 node. `I` through `VI` are
level 1. `2` is level 1. So the label shape holds for no consistent rule, which is why
this module does not use it.

**The discriminator is whether the node has a date window.** A node with a window is
level 1; a node without one is level 2. Measured: 8 nodes with windows, 4 without, and
the split is exactly the two levels.

## Activities attach at two different depths, and the sheet uses both

Rows 19-23 sit directly under node `I` — there is no level-2 node between them.
Rows 48-68 sit under `Zone A`. So the rule is "the most recent node of each level,
whichever is nearer", and a node at either level resets only its own level and below.

## Three findings, and none of them is resolved here

**The first node is the report's own summary row.** Its window is
`(2019-03-13, 2019-09-20)`, and the union of all 110 activities' windows is
`(2019-03-13, 2019-09-20)` — the same, exactly. That is a structural proof rather than
an assertion about a label, and it is what F96 recorded by eye for activities. A
`wbs` node called "the whole project", holding every other node, is not a work package.

**Four nodes finish before they start.** Rows 24, 26, 31, 36, 41 each end one day
*before* they begin. The corpus's own duration rule is an inclusive day count, so a
one-day item has `start == finish`; a node that ends before it starts is a typo in the
file, and its window is refused rather than normalised.

**Six level-1 nodes share one name.** `I` through `VI` are all
"Hệ thống cấp thoát nước", distinguishable only by their label and their window. That
is not a defect — six zones of one system is a real shape — but it means a WBS keyed on
description would collapse them into one node and lose 5 of 6.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ai_orchestrator.ingest.progress_reader import _is_rollup
from ai_orchestrator.ingest.sheets import SheetShape


#: One below the shallowest node. Level 1 is the top, level 2 sits under it, and an
#: activity is a leaf. Named rather than numbered so a finding can say "level 2" and a
#: reader can act on it.
class NodeLevel(StrEnum):
    SYSTEM = "system"
    ZONE = "zone"


class Finding(StrEnum):
    """What the reader noticed and did not resolve.

    Named because these are the output. A reader that silently repaired four nodes that
    finish before they start would be making four decisions nobody recorded.
    """

    #: A node whose window is exactly the union of every activity below it, so it is
    #: the report's total row rather than a work package.
    SUMMARY_ROW = "summary_row"
    #: `finish < start`. The corpus counts days inclusively, so this is a typo.
    BACKWARDS_WINDOW = "backwards_window"
    #: A node with no window, so it cannot be scheduled even though it is not the
    #: sheet's total. Refused rather than filled with its children, because a window
    #: invented as "the union of my children" is a fact about the file, not the plan.
    NO_WINDOW = "no_window"
    #: An activity row before any node, so there is nothing to attach it to.
    ORPHAN_ACTIVITY = "orphan_activity"
    #: The sheet has a node but no activities beneath it.
    EMPTY_NODE = "empty_node"


@dataclass(frozen=True, slots=True)
class Node:
    """One work package or zone, as the sheet states it."""

    node_id: str
    label: str
    description: str
    level: NodeLevel
    #: The row it came from, so a finding can point at a line in a file.
    row_no: int
    #: The system a zone belongs to. `None` for a system, which is the root of the
    #: subtree it opens. Needed because the sheet's level-2 rows are *not* nested in
    #: the text -- `Zone B` appears 22 rows after `Zone A`'s activities with nothing
    #: between them saying which system it is under.
    parent_id: str | None = None
    planned_start: dt.date | None = None
    planned_finish: dt.date | None = None
    findings: tuple[Finding, ...] = ()

    @property
    def is_schedulable(self) -> bool:
        """Whether this node has a window it can honestly claim.

        A node with no window is not unschedulable in reality — it just is not
        schedulable *from this file*, and the difference matters.
        """
        return (
            self.planned_start is not None
            and self.planned_finish is not None
            and Finding.NO_WINDOW not in self.findings
            and Finding.BACKWARDS_WINDOW not in self.findings
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "node_id": self.node_id,
            "label": self.label,
            "description": self.description,
            "level": self.level.value,
            "parent_id": self.parent_id,
            "row_no": self.row_no,
            "planned_start": self.planned_start.isoformat() if self.planned_start else None,
            "planned_finish": self.planned_finish.isoformat() if self.planned_finish else None,
            "findings": [f.value for f in self.findings],
        }


@dataclass(frozen=True, slots=True)
class Activity:
    """One schedulable line, attached to the node that contains it."""

    activity_id: str
    line_label: str
    description: str
    #: The row's position in the sheet, and the handle `wbs_operations` attaches a
    #: progress reading by. Positional because the corpus numbers each system's
    #: activities from 1, so `line_label` is not unique within a report — and because
    #: `progress_snapshots.source_row` is unique by construction.
    row_no: int
    #: `None` when the activity precedes every node, which the real file never does.
    parent_id: str | None = None
    parent_level: NodeLevel | None = None
    planned_start: dt.date | None = None
    planned_finish: dt.date | None = None
    planned_days: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "activity_id": self.activity_id,
            "line_label": self.line_label,
            "description": self.description,
            "row_no": self.row_no,
            "parent_id": self.parent_id,
            "parent_level": self.parent_level.value if self.parent_level else None,
            "planned_days": self.planned_days,
        }


@dataclass(frozen=True, slots=True)
class Structure:
    """A whole sheet's worth of structure, and what could not be read from it."""

    nodes: tuple[Node, ...] = field(default_factory=tuple)
    activities: tuple[Activity, ...] = field(default_factory=tuple)
    #: The union of every activity's window, which is what makes the summary row
    #: provable rather than merely suspected.
    overall_window: tuple[dt.date, dt.date] | None = None

    def children_of(self, node_id: str) -> tuple[Node, ...]:
        return tuple(n for n in self.nodes if n.parent_id == node_id)

    def activities_of(self, node_id: str) -> tuple[Activity, ...]:
        return tuple(a for a in self.activities if a.parent_id == node_id)

    @property
    def all_findings(self) -> tuple[Finding, ...]:
        found: set[Finding] = set()
        for node in self.nodes:
            found.update(node.findings)
        if any(a.parent_id is None for a in self.activities):
            found.add(Finding.ORPHAN_ACTIVITY)
        return tuple(sorted(found, key=lambda f: f.value))

    def as_dict(self) -> dict[str, object]:
        return {
            "nodes": len(self.nodes),
            "activities": len(self.activities),
            "levels": {
                level.value: sum(1 for n in self.nodes if n.level is level) for level in NodeLevel
            },
            "findings": [f.value for f in self.all_findings],
        }


def _cell(row: tuple[object, ...], shape: SheetShape, role: str) -> object:
    index = next((c.index for c in shape.columns if c.role == role), None)
    return row[index] if index is not None and index < len(row) else None


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _as_date(value: object) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def read_structure(rows: list[tuple[object, ...]], shape: SheetShape) -> Structure:
    """Derive the work breakdown from a progress sheet. Pure, per-file.

    Two passes, and the order matters. The first collects every node and every
    activity with its row number. The second decides which nodes are the sheet's own
    total, because that question is only answerable once every activity's window is
    known — a summary row is *defined* by covering its siblings.

    Re-walking the rows rather than reusing `read_progress`, which throws the roll-up
    rows away and keeps only the last heading as a flat `section_label`. A tree needs
    the nodes, their windows and their order, and a flat string is not any of those.
    The node/activity *test* is reused, though: `_is_rollup` is imported rather than
    copied, so the two readers cannot disagree about which row is which.
    """
    nodes: list[Node] = []
    activities: list[Activity] = []
    current_system: Node | None = None
    current_zone: Node | None = None
    counter = 0

    for row_no in range(shape.header_row, len(rows)):
        row = rows[row_no]
        description = _text(_cell(row, shape, "work_description"))
        if not description:
            continue
        planned_days = _cell(row, shape, "planned_duration_days")
        actual_days = _cell(row, shape, "actual_duration_days")
        label = _text(_cell(row, shape, "line_no"))
        start = _as_date(_cell(row, shape, "planned_start_on"))
        finish = _as_date(_cell(row, shape, "planned_finish_on"))

        if _is_rollup(planned_days, actual_days):
            counter += 1
            # A window makes it a system, no window makes it a zone. Measured, not
            # inferred from the label -- see the module docstring.
            level = NodeLevel.SYSTEM if (start and finish) else NodeLevel.ZONE
            node = Node(
                node_id=f"nd_{counter:04d}",
                label=label,
                description=description,
                level=level,
                row_no=row_no,
                # A zone belongs to the system that most recently preceded it. The
                # sheet does not say so in words, but the ordering does, and an
                # unparented zone would be a root competing with the systems.
                parent_id=current_system.node_id
                if (level is NodeLevel.ZONE and current_system)
                else None,
                planned_start=start,
                planned_finish=finish,
                findings=() if (start and finish) else (Finding.NO_WINDOW,),
            )
            nodes.append(node)
            if level is NodeLevel.SYSTEM:
                current_system, current_zone = node, None
            else:
                current_zone = node
            continue

        counter += 1
        parent = current_zone or current_system
        activities.append(
            Activity(
                activity_id=f"ac_{counter:04d}",
                line_label=label,
                description=description,
                row_no=row_no,
                parent_id=parent.node_id if parent else None,
                parent_level=parent.level if parent else None,
                planned_start=start,
                planned_finish=finish,
                planned_days=int(planned_days) if isinstance(planned_days, (int, float)) else None,
            )
        )

    overall = _union_window(activities)
    nodes = _annotate(nodes, overall)
    return Structure(nodes=tuple(nodes), activities=tuple(activities), overall_window=overall)


def _union_window(activities: Sequence[Activity]) -> tuple[dt.date, dt.date] | None:
    starts = [a.planned_start for a in activities if a.planned_start]
    finishes = [a.planned_finish for a in activities if a.planned_finish]
    if not starts or not finishes:
        return None
    return (min(starts), max(finishes))


def _annotate(nodes: list[Node], overall: tuple[dt.date, dt.date] | None) -> list[Node]:
    """Add the findings only the whole sheet can justify.

    A node that finishes before it starts is wrong in isolation, checkable from its
    own two cells. A node whose window covers every activity is wrong only in
    company, because "covers everything" is not a defect until you know what
    "everything" is.
    """
    out: list[Node] = []
    for node in nodes:
        findings = set(node.findings)
        if node.planned_start and node.planned_finish and node.planned_finish < node.planned_start:
            findings.add(Finding.BACKWARDS_WINDOW)
        if (
            overall is not None
            and node.level is NodeLevel.SYSTEM
            and node.planned_start == overall[0]
            and node.planned_finish == overall[1]
        ):
            findings.add(Finding.SUMMARY_ROW)
        out.append(
            Node(
                node_id=node.node_id,
                parent_id=node.parent_id,
                label=node.label,
                description=node.description,
                level=node.level,
                row_no=node.row_no,
                planned_start=node.planned_start,
                planned_finish=node.planned_finish,
                findings=tuple(sorted(findings, key=lambda f: f.value)),
            )
        )
    return out


__all__ = [
    "Activity",
    "Finding",
    "Node",
    "NodeLevel",
    "Structure",
    "read_structure",
]
