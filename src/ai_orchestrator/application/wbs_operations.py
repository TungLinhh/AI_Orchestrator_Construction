"""Writing the work breakdown, and refusing the rows the sheet cannot justify.

`ingest/wbs_reader.py` reads. This writes, following `project_operations.py` and
`progress_operations.py`: SQL is module constants, `written_on` is a required
parameter rather than a clock read, and every statement casts `:o`.

## The one thing this writer refuses, and why it is a refusal and not a skip

`ingest/wbs_reader` flags the sheet's first node `SUMMARY_ROW` when its window equals
the union of every activity's window. On the one real file that is `BOH`, and the proof
is exact: `(2019-03-13, 2019-09-20)` on both sides.

Writing it would produce a work breakdown whose first item is "the whole project" and
which contains every other item. So it is not written. It is **not** silently dropped
either — it comes back in the outcome with the reason attached, because "one node
refused, here is which and here is how we know" is a report and "11 nodes" is a number
somebody has to trust.

## A backwards window is a finding, not a refusal

Five nodes on the real file end the day before they begin. The corpus counts days
inclusively, so that is a typo in the file rather than a convention.

The first version of this writer **refused** them, alongside the summary row. The
end-to-end test over the real file is what showed that to be wrong: 12 nodes read,
6 written, 5 of them real work packages with activity groups under them, gone — and
their activities unplaced, because `wbs` has **no date columns** to put the bad dates
in anyway. Refusing accomplished nothing except losing structure.

So `BACKWARDS_WINDOW` is reported and the node is written. The finding says the file's
dates for it are wrong; the node says the work package exists. Both are true, and only
one of them is about this table.

`wbs` having no date columns is the reason this is even a question: a breakdown is a
structure, and a structure is not a schedule. The schedule lives in
`progress_snapshots`, where `wbs_id` links the two.

## Parent before child, and why the order is not incidental

`wbs.parent_id` is a self-referencing foreign key, so a zone cannot be written before
the system it hangs from. The writer therefore walks the tree breadth-first and writes
each node only after its parent, and it reports **every refusal in the whole tree**
rather than stopping at the first — a caller fixing one problem at a time is a slower
route to the same place.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.ingest.wbs_reader import Finding, Node, NodeLevel, Structure

_INSERT_NODE = """
INSERT INTO wbs (
    id, organization_id, project_id, code, name, parent_id, sequence, is_leaf,
    source, source_actor, created_at, updated_at
)
VALUES (
    :i, CAST(:o AS varchar(40)), CAST(:project AS varchar(40)), :code, :name,
    :parent, :sequence, :is_leaf, CAST(:source AS varchar(128)), :actor,
    CAST(:now AS timestamptz), CAST(:now AS timestamptz)
)
"""

#: The highest sequence a project already holds. Read immediately before **each**
#: insert, not once per tree.
#:
#: Capturing it once per tree is right for a tree and wrong for a pipeline: the corpus
#: holds 16 progress sheets and 48 sheets carrying a project header, and writing one
#: tree per sheet into one project means the numbering has to advance between trees
#: that a *different* call wrote. The first version read `count(*)` at the top of
#: `write_structure` and two trees were handed overlapping ranges, which the pipeline
#: found as `UniqueViolationError: ix_wbs_org_project_code` at `S016`.
#:
#: Read-per-insert rather than capture-and-increment because it is the version that
#: cannot be defeated by a caller interleaving two writes into the same project. It is
#: still not safe against *concurrent* writers: two transactions can read the same
#: maximum and collide. The `UNIQUE` index is what stops that, and the collision is
#: then a loud failure rather than a silent duplicate — which is the right way round
#: for a script, and would not be for a service.
_NEXT_SEQUENCE = """
SELECT coalesce(max(sequence), -1) + 1 FROM wbs
WHERE organization_id = CAST(:o AS varchar(40)) AND project_id = :project
"""

#: Attached by `source_row`, which is the sheet row both sides already carry.
#:
#: The first version matched on `line_label` alone, and that is wrong in a way the
#: numbers made obvious: `TĐ BOH.xlsx` numbers each system's activities from 1, so
#: every `line_label = '1'` in a report matched the same node, the last write won, and
#: the workspace reported 40 zones of which 36 had no readings and one had all of
#: them. `source_row` is unique within a report by construction — it is the same fact
#: the unique key on `progress_snapshots` is built on — so this cannot collide.
_LINK_ACTIVITY = """
UPDATE progress_snapshots
SET wbs_id = :node, updated_at = CAST(:now AS timestamptz)
WHERE organization_id = CAST(:o AS varchar(40)) AND report_ref = :report
  AND source_row = :source_row
"""


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """One node's result, and the reason if it was refused."""

    code: str
    description: str
    level: NodeLevel
    row_no: int
    node_id: str | None = None
    refusals: tuple[str, ...] = field(default_factory=tuple)
    #: The reader's findings, carried through unchanged so a caller writing the tree
    #: learns what is wrong with the file without having to re-read it.
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    #: What those findings mean *here*. Separate from `refusals` because a reported
    #: problem and a refused write are different outcomes, and a report that merges
    #: them reports that a node was not written when it was.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def written(self) -> bool:
        return self.node_id is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "description": self.description,
            "level": self.level.value,
            "row_no": self.row_no,
            "node_id": self.node_id,
            "written": self.written,
            "refusals": list(self.refusals),
            "findings": [f.value for f in self.findings],
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class WbsOutcome:
    """The whole tree, every refusal, and the count of activities that found a home.

    `node_ids` is returned rather than kept private because the caller needs it to
    attach activities by hand when a node was refused -- and a refused node means its
    activities have no parent, which is a finding the caller has to see.
    """

    project_id: str
    outcomes: tuple[NodeOutcome, ...] = field(default_factory=tuple)
    #: `node_id` keyed by the reader's own id, so an activity's `parent_id` maps
    #: straight through.
    node_ids: dict[str, str] = field(default_factory=dict)
    activities_linked: int = 0
    activities_unplaced: int = 0

    @property
    def written(self) -> tuple[NodeOutcome, ...]:
        return tuple(o for o in self.outcomes if o.written)

    @property
    def refused(self) -> tuple[NodeOutcome, ...]:
        return tuple(o for o in self.outcomes if not o.written)

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "nodes_written": len(self.written),
            "nodes_refused": len(self.refused),
            "activities_linked": self.activities_linked,
            "activities_unplaced": self.activities_unplaced,
            "refusals": [o.as_dict() for o in self.refused],
        }


def _code_for(node: Node, sequence: int) -> str:
    """A code that is unique within the project and says what level it is.

    The reader's `label` is the sheet's own — `I`, `VI`, `A`, `2` — and it repeats
    across levels: `A` is both a system and a zone on the real file. So the level
    prefix is part of the code, and the sequence disambiguates the six systems that
    share one description.

    The sequence counts from the whole project rather than from one tree, because
    `uq_wbs_org_project_code` is per project. Numbering from 1 per tree is right for
    the first tree and wrong for the second.

    Deliberately not derived from the description: `I` through `VI` are all
    "Hệ thống cấp thoát nước", and a code made from the description would collide.
    """
    prefix = "S" if node.level is NodeLevel.SYSTEM else "Z"
    return f"{prefix}{sequence:03d}"


async def write_structure(
    conn: AsyncConnection,
    *,
    organization_id: str,
    project_id: str,
    structure: Structure,
    written_on: dt.datetime,
    report_ref: str | None = None,
    source: str = "import",
    source_actor: str = "ingest:wbs_reader",
) -> WbsOutcome:
    """Write the tree and, when `report_ref` is given, attach the activities to it.

    Two passes over the nodes. The first writes every node whose parent is either
    absent or already written; the second retries the ones that were blocked on a
    parent that was itself refused. That is a breadth-first walk expressed as a loop
    rather than as a recursive helper, because a recursive walk would need the whole
    tree in memory to order it and the refusal logic is easier to read as "try again
    once the parent exists".

    `report_ref` is the progress report the activities came from, and it is what
    makes the linking possible: `progress_snapshots` is keyed by
    `(report_ref, line_label)`, so a reader node id resolves to rows through that key
    rather than by guessing.
    """
    if written_on.tzinfo is None:
        raise ValueError(
            "written_on must be timezone-aware; `created_at` is timestamptz so a naive "
            "value raises TypeError from inside the arithmetic rather than here"
        )

    outcomes: list[NodeOutcome] = []
    node_ids: dict[str, str] = {}
    written_ids: set[str] = set()
    pending = list(structure.nodes)

    # `sequence` and the code both count from what the project already holds, not from
    # one. The first version numbered from 1 per tree, so writing a second progress
    # sheet's hierarchy into the same project collided on
    # `ix_wbs_org_project_code` at `S003` -- correct behaviour for one tree, wrong for
    # a project with forty-eight of them.
    async def next_sequence() -> int:
        got: int = (
            await conn.execute(text(_NEXT_SEQUENCE), {"o": organization_id, "project": project_id})
        ).scalar_one()
        return int(got)

    while pending:
        progressed = False
        still: list[Node] = []
        for node in pending:
            if node.parent_id is not None and node.parent_id not in written_ids:
                still.append(node)
                continue
            sequence = await next_sequence()
            outcome, refused = await _write_node(
                conn,
                organization_id=organization_id,
                project_id=project_id,
                node=node,
                sequence=sequence,
                parent_db_id=node_ids.get(node.parent_id or ""),
                written_on=written_on,
                source=source,
                source_actor=source_actor,
            )
            outcomes.append(outcome)
            if not refused and outcome.node_id is not None:
                node_ids[node.node_id] = outcome.node_id
                written_ids.add(node.node_id)
                progressed = True
        pending = still
        if not progressed:
            # Nothing left can advance, so every remaining node is blocked on a parent
            # that was refused. Reported rather than dropped: a caller who sees "3
            # nodes blocked" knows to look at the refusal that caused it.
            for node in pending:
                outcomes.append(
                    NodeOutcome(
                        code=_code_for(node, len(outcomes) + 1),
                        description=node.description,
                        level=node.level,
                        row_no=node.row_no,
                        refusals=(
                            "parent was refused, so this node has nowhere to hang; "
                            f"its parent is {node.parent_id}",
                        ),
                    )
                )
            break

    linked = unplaced = 0
    if report_ref is not None:
        linked, unplaced = await _link_activities(
            conn,
            organization_id=organization_id,
            structure=structure,
            node_ids=node_ids,
            report_ref=report_ref,
            written_on=written_on,
        )

    return WbsOutcome(
        project_id=project_id,
        outcomes=tuple(outcomes),
        node_ids=node_ids,
        activities_linked=linked,
        activities_unplaced=unplaced,
    )


async def _write_node(
    conn: AsyncConnection,
    *,
    organization_id: str,
    project_id: str,
    node: Node,
    sequence: int,
    parent_db_id: str | None,
    written_on: dt.datetime,
    source: str,
    source_actor: str,
) -> tuple[NodeOutcome, bool]:
    """One node. Returns `(outcome, refused)`.

    The refusals are the file's, not ours:

    * `SUMMARY_ROW` — a node whose window covers every activity is the report's
      total, not a work package, and writing it makes a breakdown whose first item is
      the whole project. **The only refusal.**

    `BACKWARDS_WINDOW` and `NO_WINDOW` are reported and the node is written. A window
    that ends before it starts is a fact about the file's dates, and `wbs` has no date
    columns to write it to -- refusing the node would lose real structure and fix
    nothing. A zone the file does not schedule is still a zone.
    """
    findings = list(node.findings)
    code = _code_for(node, sequence)
    refusals: list[str] = []

    if Finding.SUMMARY_ROW in node.findings:
        refusals.append(
            f"row {node.row_no}: window {node.planned_start}..{node.planned_finish} is "
            f"exactly the union of every activity below it, so this is the report's "
            f"own total rather than a work package. Writing it would make a work "
            f"breakdown whose first item is the whole project"
        )
    if refusals:
        return NodeOutcome(
            code=code,
            description=node.description,
            level=node.level,
            row_no=node.row_no,
            refusals=tuple(refusals),
        ), True

    node_id = f"wbs_{new_ulid()}"
    # The reported-but-not-refused findings ride along on the outcome, so a caller
    # writing the tree learns that five of its nodes have bad dates in the source file
    # without the tree being damaged by the discovery.
    reported: list[str] = []
    if Finding.BACKWARDS_WINDOW in findings:
        reported.append(
            f"row {node.row_no}: window ends {node.planned_finish} before it starts "
            f"{node.planned_start}. The corpus counts days inclusively, so a one-day "
            f"item has start == finish; this is a typo in the file, reported and not "
            f"repaired. The node is written because `wbs` holds no dates -- the dates "
            f"live in progress_snapshots"
        )
    if Finding.NO_WINDOW in findings:
        reported.append(
            f"row {node.row_no}: no window in the file, so the node is written "
            f"unscheduled rather than given an invented one"
        )

    await conn.execute(
        text(_INSERT_NODE),
        {
            "i": node_id,
            "o": organization_id,
            "project": project_id,
            "code": code,
            "name": node.description,
            "parent": parent_db_id,
            "sequence": sequence,
            "is_leaf": False,
            "source": source,
            "actor": source_actor,
            "now": written_on,
        },
    )
    return NodeOutcome(
        code=code,
        description=node.description,
        level=node.level,
        row_no=node.row_no,
        node_id=node_id,
        findings=tuple(findings),
        notes=tuple(reported),
    ), False


async def _link_activities(
    conn: AsyncConnection,
    *,
    organization_id: str,
    structure: Structure,
    node_ids: dict[str, str],
    report_ref: str,
    written_on: dt.datetime,
) -> tuple[int, int]:
    """Point each activity's readings at the WBS node that contains it.

    `unplaced` counts the activities whose node was refused or absent. That number is
    the one worth watching: it is how much of a project's schedule is in the file but
    not in the structure, and it grows silently if the refusals are ever ignored.
    """
    linked = unplaced = 0
    for activity in structure.activities:
        if activity.parent_id is None:
            unplaced += 1
            continue
        db_id = node_ids.get(activity.parent_id)
        if db_id is None:
            unplaced += 1
            continue
        result = await conn.execute(
            text(_LINK_ACTIVITY),
            {
                "o": organization_id,
                "node": db_id,
                "report": report_ref,
                "source_row": activity.row_no,
                "now": written_on,
            },
        )
        if result.rowcount:
            linked += result.rowcount
        else:
            # The reader found an activity the report does not contain. A mismatch
            # between two readers of the same file, and counting it separately is the
            # only way it will be noticed.
            unplaced += 1
    return linked, unplaced


__all__ = [
    "NodeOutcome",
    "WbsOutcome",
    "node_by_id",
    "wbs_tree",
    "write_structure",
]


# --- reading the breakdown back -----------------------------------------------

_SELECT_NODES = """
SELECT w.id, w.code, w.name, w.parent_id, w.sequence, w.is_leaf
FROM wbs w
WHERE w.organization_id = CAST(:o AS varchar(40)) AND w.project_id = :project
ORDER BY w.sequence
"""

#: `project_id` is in the projection on purpose. `api/construction.py` uses it to refuse
#: `GET /projects/{A}/activity?wbs_id=<a node of B>` -- a node that exists but answers
#: a question about a different project, which is a 200 full of somebody else's
#: schedule. The first version of this query did not return the column, so the
#: endpoint's check silently compared `None` and never fired.
_SELECT_ONE_NODE = """
SELECT w.id, w.code, w.name, w.project_id, w.parent_id, w.sequence, w.is_leaf
FROM wbs w
WHERE w.organization_id = CAST(:o AS varchar(40)) AND w.id = :node
"""


async def wbs_tree(
    conn: ReadConnection, *, organization_id: str, project_id: str
) -> list[dict[str, object]]:
    """Every node in a project, flat, in sequence order.

    **Flat, not nested**, and that is a decision about the transport rather than about
    the shape. A nested tree is friendlier to render and hostile to page: a project
    whose tree is 3 levels deep has no natural "first 50 nodes", so every consumer ends
    up flattening it themselves, differently. Flat with `parent_id` is the shape the
    table has, it paginates, and assembling the hierarchy is a client's business and
    not the database's.

    Ordered by `sequence` rather than by name, because `sequence` is the order the
    sheets list the work in and a PM reads a breakdown in the order it was built.
    """
    rows = (
        (await conn.execute(text(_SELECT_NODES), {"o": organization_id, "project": project_id}))
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def node_by_id(
    conn: ReadConnection, *, organization_id: str, node_id: str
) -> dict[str, object] | None:
    """One node, or `None`.

    `None` rather than an exception because "no such node" and "no such node *in this
    tenant*" are the same answer to a caller, and distinguishing them would leak the
    existence of another tenant's row — which is a thing this system does not do and
    the composite foreign keys in Phase 2c exist to make impossible on write.
    """
    row = (
        (await conn.execute(text(_SELECT_ONE_NODE), {"o": organization_id, "node": node_id}))
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None
