# The gap between the schema and the product

Written after migration `0012` and `0013`, with the whole construction domain
migrated and tested. This is the state of the whole codebase, measured rather than
recalled, and the ordering it implies for what comes next.

Every number here was read out of the code or the running database. Where a claim
could not be measured it says so.

---

## 1. What exists

| Layer | State | Evidence |
|---|---|---|
| Substrate | Complete and exercised | 8 routers, 73 routes, task/delegation/approval/audit/proposal/memory/tools/MCP/SSE |
| Domain schema | 52 tables, migrated, RLS, tested | migrations `0007`–`0014`, 0 over-long identifiers, builds from an empty database |
| Pure domain rules | Gates, contracts, numbers, progress | `domain/gates.py`, `domain/contracts.py`, `domain/progress.py`, `ingest/numbers.py` |
| Ingest | Two readers, both proved on real files, neither writing | `ingest/sheets.py`, `ingest/reader.py`, `ingest/progress_reader.py`, `scripts/ingest_corpus.py` |
| Process spine | Seeded and verified | 6 Gates (G0–G5), 45 criteria, 28 SOPs, 6 autonomy policies, 8 DOA bands, 14 RACI rows |
| Corpus | ~1,540 bilingual documents | **3 projects across 7 name spellings**, none named "Hoabinh Group": Bãi Tràm Estates (3 spellings plus the typo `Bãi Tràn`), Melia Cam Ranh Bay Villa & Resort (2 spellings, one of them `Khu Biệt Thự & Nghỉ Dưỡng Melia Cam Ranh`), Lawrence Sting School 2; 43 sheets carry an identity block; 13 zones, 4 MEP packages; 306 sheets read, 199 with a header row |

## 2. The finding

**97 tables are declared in the persistence layer; 102 exist in the schema. 9 are
named in any source file outside persistence. 42 of the construction tables are
unreachable from the running product, and zero are exposed by the API.**

*(Measured 2026-09-27, after migration `0016`. The two counts differ because 32
construction tables are in the schema and seeded but have no ORM model — a known and
separately-tracked gap, not a discrepancy in the measurement.)*

Measured three ways, and all three agree:

**Statically.** Walking every table name declared in the six domain modules and
grepping for it outside `persistence/`:

```
  domain tables: 51
  named in any non-persistence source file: 9
    gate_definitions, gate_instances, gate_criteria, gate_conditions,
    gate_criterion_evaluations, gate_decisions   -> application/gate_operations.py
    sop_raci                                     -> domain/gates.py
    contracts                                    -> domain/contracts.py (pure rules)
    suppliers                                    -> tools/builtin.py
```

Six of the nine are the Gate spine, which was built *after* the domain precisely so
Gates could be tested against something. The remaining three are pure functions and
one tool definition. Not one is a read or a write.

**At runtime.** `application/gate_operations.py` is the only domain write path in the
codebase, and `grep -rn "gate_operations" src/ai_orchestrator/api/` returns nothing —
it is not mounted on any router. The eight mounted routers are all substrate:
organizations, agents, tasks, skills/tools, approvals, events/audit, runtime, stream.

**In the interface. RESOLVED in Phase 5.** The UI was a 44 KB single page with no
construction vocabulary, three regions and title
`Orchestrator`. Word frequency in the served HTML:

```
  gate 3   project 2
  contract 0   invoice 0   purchase 0   supplier 0
  requisition 0   quotation 0   budget 0   wbs 0
```

## 3. Why this happened, and why it is not a mistake

The schema work was the right thing to do first, and it was not gold-plating. Two
reasons, both of which survive the finding above.

**The dossier's hard part is the rules, and rules need somewhere to live.** Tập 1
§5.3's four "vùng cấm tuyệt đối", §4.1's "an agent is never Accountable", the
four Gate outcomes, the 15% market-variance threshold — these are check
constraints and state machines, and each one is only testable once the tables it
governs exist. Building the Gate spine second was the right order for the same
reason: a Gate with nothing to gate cannot be tested.

**The measurements that shaped the schema could only be made from the corpus.** The
finding that there are no material codes, that units are inconsistently cased
(`Bộ` 17 / `bộ` 7), that `Mã Hiệu` is an unlabelled drawing number, that the
checklist and item list are two blocks in one sheet — none of these is available
without opening 221 workbooks. Designing against them after the fact means
retrofitting a schema, which is strictly more expensive than designing against
them first.

So the sequencing was right. What was missing is the step that turns a schema into
a product, and it is a step rather than an oversight: there was no reason to build a
read path for tables whose contents were still being invented.

## 4. The gap that actually binds

Ordered by what unblocks the most, not by what is most satisfying to build.

### 4.1 Ingest — 221 spreadsheets, no reader

`src/ai_orchestrator/ingest/` contains exactly one file: `numbers.py`, 259 lines, a
strict Vietnamese number parser ported from `pmo_project_procore` after finding its
`toFloat` reading `12.500.000` as `12.5` (F74). **It has no caller.** The number
parser exists because the rest of the ingest was going to need it, and the rest was
never written.

The corpus, measured:

| Kind | Count | Note |
|---|---|---|
| `.xlsx` / `.xls` | 221 | machine-readable, cell-addressable |
| `.pdf` / `.doc` / `.docx` | 94 | need text extraction, not parsing |
| `.identifier` | 35 | Windows NTFS alternate data streams — **not documents, skip** |
| total | 367 files, 1.0 GB | two PDFs alone are 35 MB and 45 MB |

This is the highest-value next tranche and it is not close. It is what converts
"cannot be measured" into "measured" for the four things the corpus cannot currently
validate — the 3-way match, supplier due diligence, tender/BOQ, and retention
actually withheld — and it is the only way any of the 42 unreachable tables ever gets
a non-test row in it.

Two constraints the files impose, both found by measurement and both easy to get
wrong:

- **Sheet selection must be by content, not by name.** The corpus has `PL01`,
  `ĐXTT-NCC`, `CCXX`, `Bien ban giao hang, nghiem thu` and `Sheet1`. The same logical
  document appears under different sheet names across projects, and one file's first
  sheet is a contract header while its eleventh is the item list.
- **A 45 MB PDF is a drawing set, not text.** Treat the 94 documents as a separate
  problem from the 221 workbooks, with a size ceiling and an explicit
  `extraction_failed` outcome rather than a silent skip.

### 4.2 A domain read path — 42 tables, no way in

One application service and one router per area, not one per table. The rule that
makes this tractable: the API exposes *operations*, and an operation is a business
verb. "Raise a requisition", "record a receipt", "run the 3-way match" — not "list
`rfq_items`". The dossier's form catalogue (Tập 1 appendix C) is the natural list,
because the forms are already what people do.

`gate_operations.py` is the precedent to follow: pure rules in `domain/gates.py`,
writes in `application/gate_operations.py`, SQL as module constants. It is 339 lines
for six tables and is the only reason the Gate spine is testable against a database.

### 4.3 The eight agents — none exist

There is no agent registry, no `agent_definitions` table, and no role table. The
eight services are not started. `docs/CONSTRUCTION_DOMAIN.md` §8b already records
what has to be columns when they are built, and it is right to build them after
ingest: an agent that proposes a requisition reconciliation needs real requisitions
to have been reconciled, or its shadow-mode agreement rate is measured against
nothing.

### 4.4 The 24 remaining tables

Finance (7), assurance (13), remaining process (6). Sequenced as
`CONSTRUCTION_DOMAIN.md` §8 lists them. The finance tranche is the one that closes
the loop on the supply chain: `ONX-BO-FIN-SOP-002` is the 3-way match, and
`invoices`/`three_way_matches`/`payment_requests` are the tables that make the
requisition-to-receipt chain worth having.

## 4a. What the first ingest pass measured, and what it changed

`ingest/sheets.py` and `ingest/reader.py` are built and `scripts/ingest_corpus.py`
runs them over the whole corpus without writing. The measurement, on 2026-09-27:

```
  workbooks read        202      (12 unreadable: .xls, or protected)
  sheets                306
  tables found            5
  rows                   41
  clean rows             13
  rows with refusals     28      (all of them: quantity)
  sheets with no table  302
```

**Five tables out of 306 sheets.** Read naively that is a detector that does not
work. It is not, and the difference is the whole finding:

| Corpus root | Files | Sheets | Tables found |
|---|---|---|---|
| `procurement/input` | 9 | 18 | **5** |
| `pmo_project/reference_sheets` | 59 | 83 | 0 |
| `pmo_project_procore/backend/uploads` | 146 | 205 | 0 |

Every table found is in the procurement contract templates — the `PL01` price
appendix, the goods-receipt checklist and item blocks, the delivery checklist. The
other 288 sheets are a **different document family**, and the vocabulary was written
for the first one.

**The detection machinery works; the vocabulary is the gap.** Scanning those 288
sheets for *any* row with four or more label-shaped cells:

```
  sheets scanned             288
  sheets with a >=4-label row 199   (69%)
  ...of those, below row 9    159
```

So 199 sheets have a real header row, and 159 of them have it below row 9 — which is
why `MAX_HEADER_SCAN_ROWS` is 30 and why the deep scan is the load-bearing part of
`detect_sheet`. The detector locates them; `COLUMN_ROLES` does not describe them.

**The labels it is missing are the construction-progress family**, and they are
frequent enough to be a specification rather than a guess. Measured counts across
those 199 headers:

| Count | Label | Role it implies |
|---|---|---|
| 134 | `STT` | already mapped |
| 55 | `Mã Hiệu` | already mapped |
| 55 | `H/T` | unexplained; appears 55 times and is not mapped |
| 54 | `Khu vực thi công` | construction zone, distinct from `Khu vực lắp đặt` |
| 39 | `Hệ thống` | `PW`/`LV`/`WD`/`AC`/`FP` — the system code |
| 37 | `Tình trạng` | status |
| 35 | `Công việc thi công` | the work, not the material |
| 35 | `Hoàn thành`, `Hoàn thành tổng theo hạng mục` | completion percentage |
| 34 | `Chi tiết đầu việc theo HĐ` | task detail per contract |
| 32 | `Ngày bắt đầu KH`, `Ngày kết thúc KH` | **planned** start/end |
| 32 | `Ngày bắt đầu TT`, `Ngày kết thúc TT` | **actual** start/end |
| 32 | `Số ngày KH`, `Số ngày TT` | planned/actual duration |
| 32 | `Hạng mục theo hợp đồng` | the WBS item, not a material |
| 29 | `Tên bản vẽ` | drawing name |
| 28 | `Ngày dự kiến trình`, `Ngày thực tế trình` | planned/actual submission |
| 28 | `Phản hồi BQLDA` | the engineer's comment |
| 28 | `Tên vật tư` | **material name** |
| 26 | `Ngày yêu cầu vật tư từ CT` | material requested, by the client |
| 26 | `Ngày phóng vật tư đặt hàng` | procurement released the order |

**The `KH`/`TT` pairing is the find.** `KH` is *kế hoạch* (plan) and `TT` is *thực
tế* (actual), and the corpus pairs them column for column on the same row: planned
start, actual start, planned finish, actual finish, planned duration, actual
duration. That is the "is this late and by how much" question expressed as
**columns** — and it is not what `material_reconciliations` models, which is
milestones (requested, ordered, expected, delivered) read from the `VẬT TƯ` zone
sheets.

Both are right and they are not the same shape, and reconciling them is a decision
nobody has made. The next vocabulary tranche has to decide whether a planned/actual
duration pair is a `material_reconciliations` row, a `progress_snapshots` row, or
both — because building it as "just another step" would quietly lose the pairing,
which is the entire value of the sheet.

### What the pass proved about the reader

Of the 41 rows read, 13 were clean and **all 28 refusals were `quantity`**. The
three checklist tables are blank `Mẫu` templates — 32 merged cell ranges and no
data below the header — so 0 rows for them is correct rather than a failure. The 13
clean rows are the `PL01` price appendix, read with real Vietnamese prices
(`18.000.000` → `18000000`), real `bộ` casing, and zero refusals.

Two defects in the reader were found by this pass, and the synthetic tests could not
have found either:

**Every checklist kind read zero rows.** `read_table` tested for a `name` cell and
stopped at the first row without one — correct for a price schedule, and exactly
wrong for an inspection checklist, whose rows are named in `Nội dung kiểm tra`. Three
of the five tables found were affected. Fixed with `LABEL_COLUMN`, and the
sub-header case with it: the measured checklist carries
`Đạt | Không đạt | Không áp dụng` on its first data row, so "no label" had to mean
*skip* without breaking the section break that should *stop*.

**A section heading was silently renumbered.** `I. Hồ sơ - tài liệu` is a category,
not an item, and `I` is not a number — so the fallback gave it `line_no = 1`,
colliding with the real item 1 immediately below it. `IngestRow.line_label` now
carries the sheet's own `STT` verbatim, and the integer is derived from it rather
than the other way round.

### What this changes about the ordering

The ingest tranche was recommended as step 1 because it makes the unmeasurable
measurable. That was right, and the first pass has paid for itself twice: it found
that five of the corpus's six roots are a document family the material master does
not describe, and it turned "extend the vocabulary" from a guess into a table of
measured label frequencies.

The revised next step is **not** "a bigger vocabulary". It is to decide what the
construction-progress family *is* — progress snapshots, task-level planned/actual
durations, or milestones — and then write the roles for that one answer. 199 sheets
with a known header row is a large prize, and it is not reachable by adding
fragments to a list.

## 4b. Migration `0014` — the KH/TT question, answered

`docs/PRODUCT_GAP.md` §4a left one question open: the corpus pairs `KH` (*kế hoạch*,
plan) against `TT` (*thực tế*, actual) column for column, and that pairing is neither
a milestone nor a duration in the sense `material_reconciliations` uses. It is now
answered, by building it.

**`progress_snapshots` — one table, planned and actual on the same row.** Not a
`basis` discriminator, because the sheet pairs them and the question the sheet exists
to answer is the difference between them; a discriminator turns a subtraction into a
self-join and stores the pairing less directly than the file does.

**It is not `material_reconciliations` and must not be merged with it.** That models
*milestones for materials* (requested, ordered, expected, delivered). This models
*durations for construction activities*. Different subject, different question,
different arithmetic. Putting "when did the client ask for it" and "how long did the
pipe-laying take" on one row and calling the result a timeline would be worse than
two tables.

### The one real file, and what it decided

`TĐ BOH.xlsx :: TĐ .BOH` is the only sheet in the corpus with a KH/TT pair, and every
column below came from reading it.

| Measured | Consequence |
|---|---|
| `Số ngày` is an **inclusive** day count, 5 for 5 | `ck_..._planned_days_match_dates` enforces `duration = finish - start + 1`. Exclusive would be off by one on every row and nothing would raise. |
| `% Hoàn thành` holds `0.65`, `0.8`, `0.9` — a **fraction** under a header reading "percentage" | `completion_ratio` is a ratio constrained to `0..1`; `domain/progress.completion_ratio` **refuses** above 1 rather than dividing by 100. |
| Every data row's actual dates are **identical** to its planned | `actual_updated`, because a zero variance is otherwise ambiguous between "on time" and "not recorded". |
| The hierarchy is three-level, and the outer rows **carry a window** | `row_is_an_observation` plus the reader's roll-up rule. |
| `HPNC KH` / `HPNC TT` are small integers with no established meaning | `handover_planned` / `handover_actual`, ambiguity recorded rather than a name invented. |

**The roll-up rows nearly got read as activities.** The first version of the reader
treated `A` / `BOH` and `I` / `Hệ thống cấp` as headings with nothing in them. They
are not empty — the `BOH` row spans `2019-03-13 → 2019-09-20`, the whole job:

    A  BOH   2019-03-13 -> 2019-09-20   Số ngày: (empty)

So "has no dates" does not identify a roll-up, and the second attempt used a
non-numeric `Stt` as the signal. That does not hold either: the corpus numbers its
system headings `A`, `I`, `1` and `2` interchangeably, and `2` is a system heading
three sheets above an activity also numbered `2`. The rule that works is a single
signal — **`Số ngày` is empty** — because every activity in the file is costed in
days and no roll-up is. It has a known cost, recorded rather than hidden: a row with
a window and no duration is either a roll-up or an uncosted activity, so it is filed
as a roll-up and **counted** in `skipped_sections`.

### The vocabulary, extended from measurement

14 new roles, every fragment taken from a header row that exists in the corpus, all
already in normalised form (`test_every_vocabulary_fragment_is_already_normalised`
enforces that). `Ngày kết thúc Kh` in the same row as `Ngày bắt đầu KH` is why
`normalise` lowercases before matching — the `Bộ`/`bộ` defect one level up.

`detect_sheet` now finds the family and classifies it as `construction_progress` on
the pair `planned_start_on` + `planned_finish_on`, which no other kind has. Over the
real file: **35 activities read, 11 roll-ups and blank rows skipped, zero refusals,
and every duration agreeing with its dates inclusively.**

### What is still not done

Nothing is written yet. `read_progress` returns `ProgressReading` objects and
`ProgressRead` reports what was skipped and refused; the step that turns them into
`progress_snapshots` rows does not exist, and that is deliberate — the same order the
supply tranche used, where the reader was proved against real files before any row
was allowed near the material master.

### One thing fixed here and still open everywhere else

`progress_snapshots.project_id` and `wbs_item_id` are composite foreign keys —
`(organization_id, <col>) REFERENCES <table>(organization_id, id)` — so a progress
row cannot point at another tenant's project. A bare `ForeignKey("projects.id")` is
satisfied by any project in the database, and that is not a leak (RLS stops the
read) but a **corrupt pointer**: a report scoped to one project that quietly collects
another tenant's activities. It was found by test, not by review — see
`TestPointersAreTenantScoped` and `FAILED_APPROACHES.md` F98.

**These are still bare, and that is a known open gap rather than a finished job:**

| Table | Column | Points at |
|---|---|---|
| `rfqs` | `project_id` | `projects.id` |
| `purchase_orders` | `project_id` | `projects.id` |
| `contracts` | `project_id`, `client_id` | `projects.id`, `clients.id` |
| `contract_variations`, `claims`, `milestones` | parent ids | various |

Closing it means adding `uq_<parent>_org_id` to each referenced table and switching
each child to a composite key — the same four-line-per-table change as `0014`, times
about a dozen. It is a migration tranche of its own, it touches only already-applied
migrations' tables, and it is worth doing before the finance tranche adds three more
pointers of the same shape. Recording it here rather than quietly leaving it is the
point: **every isolation test in this repository checks the read half of tenant
safety, and the pointer half was unchecked until this table.**

## 5. Two things that are still unverified, stated plainly

**The page computes correctly and has still never been seen.** This is now a narrower
gap than it was, and the narrowing is worth being precise about.

What is proved: `make verify-page BASE=… ORG=…` runs the served document's JavaScript
against the live API and checks what it rendered — 6 projects, 40 zone bars of which 36
are unmeasured and 1 is late, 288 progress rows marked unmeasured, 9 agents with none
above its ceiling. It found F125, a disagreement between two queries in `role_views`
that 18 unit tests had missed.

What is not proved: **anything about how it looks.** No browser has been connected to
this session and there is no headless one installed, so nobody — including me — has seen
this page rendered. `verify_page.mjs` proves the page computes the right thing; it
cannot tell you the layout is readable at 375px, that a colour passes contrast
measurement, or that a bar does not overflow its track. **That still needs a human with
a window, and it is the one open item in Phase 5.**

**A 45 MB PDF and a `.docx` have never been through any code path here.** The
**A 45 MB PDF and a `.docx` have never been through any code path here.** The
corpus survey read spreadsheets. The document-extraction half of §4.1 is unstarted
and therefore entirely unmeasured.

## 5a. Rebuilding the database takes two commands, and the second one does not say so

Found while recovering from F85. The dev schema was dropped and rebuilt from the
migrations, which is the only real test of a migration chain — and then:

```
$ python scripts/seed_process_spine.py
no organization matches 'autonomous-demo-company'
```

The message is accurate and useless. The organization is created by `make seed`,
which has to run first, and nothing in the error points at it. The recovery is:

```
make migrate      # or: alembic upgrade head
make seed         # creates the demo organization
python scripts/seed_process_spine.py
```

`make setup` chains `bootstrap`, `migrate` and `seed` but **not** the process-spine
seeder, so even the documented setup path leaves the Gate spine, the 28 SOPs and the
DOA matrix empty. That is a real gap in `make setup` and the cheapest fix available:
add the seeder to the `setup` target and make its "no organization matches" branch
name the command to run.

Worth noting what this cost to find. Both the spine counts and the seeder's own
success message are in the summary this document is written from, and both were
**false of the running database** until it was queried — `gate_definitions` had 0
rows, not 6. A recorded fact about a system goes stale the moment the system is
rebuilt, and the only thing that distinguishes the two is asking the database.

## 6. The order

Revised twice by measurement, which is the point of measuring.

**Where this stands after two tranches.** Step 1 is done for the two document
families the corpus can be read for: the procurement contract templates (§4a) and
the construction-progress sheets (§4b). Step 5 has its first table,
`progress_snapshots`. Both readers are proved against real files and **neither
writes**. What exists is 97 modelled tables, 42 construction tables of which no API can
reach. Migration `0016` added `procedures`, `procedure_versions`, `agent_shadow_runs`
and `ai_decision_log` — all four with no API path yet, which is the honest state of a
schema tranche that has not had its read services built.

1. **Close the loop: write what the readers read.** Two readers, both measured, both
   writing nothing. `plan_ingest` and `ProgressRead` are the reports;
   `progress_snapshots` and the supply tables are where the rows go. This is the
   smallest step on the list and the one that makes 42 tables non-empty — a table
   with rows nobody wrote by hand is a schema, not a system.
2. **Close the pointer gap** (§4b). The composite foreign keys from `0014`, applied
   to the ~12 tables whose parent ids are still bare. Same four-line change per
   table, and it should land *before* the finance tranche adds three more pointers of
   the same shape.
3. **Domain read path**, one service and router per area, following
   `gate_operations.py`. This is what turns the schema into a product, and it cannot
   be skipped: 42 unreachable tables and an 8-tab substrate UI is the whole gap in
   §2.
4. **Finance tranche** — invoices, 3-way match, payment requests. Closes the supply
   chain and is the dossier's own SOP, `ONX-BO-FIN-SOP-002`.
5. **The eight agents**, with shadow mode, kill-switch and the AI Decision Log as
   columns from the first migration rather than added later. After step 1, because an
   agent's shadow-mode agreement rate measured against an empty table is a number
   about nothing.
6. **The remaining 12 assurance tables**, scheduled against data arrival. Zero permit
   documents, no filled inspection record; building them now would repeat the mistake
   this document was written to prevent.
7. **The frontend**, once there is an API worth a screen.

## 8a. The composite-FK gap, measured exactly

Recorded before as "about a dozen". It is **135**, across **75 tables**: 65 in the
construction domain, 70 in the substrate the platform is built on. Measured by walking
`Base.metadata` for single-column foreign keys to a tenant-scoped table, where the child
itself carries `organization_id`:

    po_items.material_id          -> materials
    projects.client_id             -> clients
    purchase_orders.project_id     -> projects
    quotations.rfq_id              -> rfqs
    receipt_items.po_item_id       -> po_items
    tender_requirements.tender_id  -> tenders
    wbs_items.wbs_id               -> wbs
    zones.parent_zone_id           -> zones          (self-reference)
    ... 127 more

### What the risk actually is, stated precisely

**It is not a data leak.** RLS is installed and `FORCE`d on all 99 tenant-scoped tables,
so a row in tenant B is invisible to a session bound to tenant A — a bare FK does not
change that, because you cannot *read* the row you are pointing at.

**It is a referential-integrity hole on write.** The database will accept
`organization_id = 'A'` with `project_id` naming a project in tenant B. The row is
then unreadable — RLS hides the parent, so every join through that pointer returns
nothing, and the child looks orphaned with no explanation. The failure is a row that
exists, is enforced by the database, and can never be followed. That is worse than a
dangling pointer, because a dangling one is visible.

`0016` did this correctly on its four new tables (`procedure_versions`, `procedures`,
`agent_shadow_runs`, `ai_decision_log` — 7 composite FKs, 0 bare), so the new work does
not add to this. The retrofit is 75 tables and needs a `uq_<parent>_org_id` unique index
on every parent before any FK can be swapped.

### The order it should happen in

**Construction domain first** (65 FKs, 26 tables), because those are the tables the
product reads and the ones every new service will be written against. The substrate's 70
matter less while no product code holds a pointer across them, and a 135-FK migration
in one file is a migration nobody can review.

Before the finance tranche, not after: a finance table pointing at a contract in another
tenant is a much more interesting bug than a substrate table pointing at a skill in
another tenant.
