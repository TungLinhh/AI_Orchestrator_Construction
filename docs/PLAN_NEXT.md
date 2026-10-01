# Next phases

## Done in this tranche

**Phase 4b — the rules that use `0016`'s invariants.** Two pure modules and one writer.
`domain/autonomy.py` owns the mapping between the two autonomy scales that the database
already contained, and `domain/promotion.py` decides whether a shadow version may go
live. `application/procedure_operations.py` supplies the three missing moves: record a
shadow run, write a version citing its approval, promote. **The learning loop closes
end to end**, which is the thing `load_approved()`'s zero callers meant.

**Phase 2, redirected by measurement.** The plan was "role-shaped read services over 42
unreachable tables". Measuring first showed the premise was wrong:

| table | writers | dev rows |
|---|---|---|
| `projects`, `wbs_items`, `clients` | 0 | 0 |
| `contracts`, `suppliers`, `materials` | 0 | 0 |
| `rfqs`, `purchase_orders`, `goods_receipts` | 0 | 0 |
| `progress_snapshots` | 1 | 0 |
| `gate_instances` | 1 | 0 |

**Every construction table has zero rows and zero writers.** Read services over empty
tables return nothing and cannot be measured — the same position the tender agent is in
("zero tender documents"). So Phase 2's first step is not reads, it is *rows*.

`ingest/project_reader.py` is that step's first piece, and it found the project spine
sitting in files this repository already reads. Every construction sheet opens with
`Dự án:`, `Địa điểm/Address:`, `Gói thầu/Package:`, `Hạng Mục/Item:` — and the Phase 1
progress reader **discards it**, classifying the row as a roll-up because a roll-up has a
window and no duration.

## Still to do, in order

**Phase 2a — the project spine.** ~~`projects`~~ **done**. ~~`wbs`~~ **done** — reader,
writer, and migration `0017` joining `progress_snapshots` to it, which is what makes
"which zone is late?" answerable.

**Still open, and it is a decision rather than a code gap: `clients`.** `clients` needs
a `code` and a `name`; the header block carries project, address, package and item and
**no client at all**. Client identity has to come from somewhere other than a file.

Also measured along the way: `wbs_items` is a **priced BOQ line**, not the hierarchy.
The tree is `wbs`, self-referencing on `parent_id`, and it has **no date columns** —
a breakdown is a structure, and a structure is not a schedule.

The measured disagreements remain the design input:

- **3 projects**, not the "3 Hoabinh Group projects" the docs claimed. None of them is
  called Hoabinh Group: `BÃI TRÀM ESTATES`, `MELIA CAM RANH BAY VILLA & RESORT`,
  `LAWRENCE STING SCHOOL 2`.
- **10 address spellings for 2–3 real places.** Which is canonical is a business
  decision. The reader reports all ten verbatim and picks none; the writer must not
  either without a recorded decision.
- **2 packages are strict prefixes of a third** (`Cơ`, `Cơ điện` ⊂ `Cơ điện khách sạn
  và nhà phụ trợ`) — cells cut short by their column width. Writing `Cơ` would put a
  value in a column that cannot be told from a real one.

**Phase 2b — the read services.** **Done**, per Tầng 1: `application/role_views.py` with
the CEO cockpit, PM workspace and HITL inbox, plus `scripts/ingest_construction.py` to
put the corpus in. Both verified against 6 projects, 240 WBS nodes and 2778 progress
readings.

Still open under 2b: the 48 sheets each carry their **own** hierarchy, so the pipeline
writes 40 nodes per project rather than one reconciled tree. One WBS per project is the
correct shape and reconciling them is a decision about which sheet is authoritative.

**Phase 2c — the composite-FK gap.** The supply-chain tranche is the tractable one:
**9 tables, 20 foreign keys**, needing `uq_<parent>_org_id` on 8 parents
(`contracts`, `projects`, `rfqs`, `suppliers`, `quotations`, `materials`,
`purchase_orders`, `goods_receipts`) plus the two self-references (`wbs.parent_id`,
`zones.parent_zone_id`). `wbs` and `wbs_items` already have the supporting index from
`0017`. Measured with:

    for fk in table.foreign_key_constraints:
        len(fk.columns) == 1 and fk.elements[0].column.table.name != "organizations"

 Measured, not estimated: **135** single-column
foreign keys to a tenant-scoped table, across **75 tables** — 65 construction, 70
substrate. `PRODUCT_GAP.md` §8a has the list and the precise risk.

Not a data leak: RLS is `FORCE`d on all 99 tenant-scoped tables, so you cannot read the
row you are pointing at. It **is** a referential-integrity hole on write — the database
will accept a pointer into another tenant, and the child then looks orphaned with no
explanation, which is worse than a dangling pointer because a dangling one is visible.

Construction first (65 FKs), because those are the tables the product reads. A
135-FK migration in one file is a migration nobody can review. `0016` did this right on
its four new tables, so the new work does not add to the count.

**Phase 3 — document control.** One of Tập 1's five governing principles with nothing
behind it: `ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]` codes, a version table, a distribution
matrix, and an expiry distinct from `documents.retention_until`.

**Phase 4c — the AI Decision Log writer and the kill switch as an action.** The table
exists and the promotion service writes to it; nothing yet throws a kill switch or reads
the log back for an audit.

Then the 8 agents, in the dossier's own order.
