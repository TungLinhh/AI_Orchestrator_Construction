# The remaining work, in the order it has to happen

Written after reading all 118 source files (40k lines) and 74 test files (26k lines).
Every number in this document was measured this session; the ones I could not measure
are marked as decisions rather than findings.

---

## 1. What is actually true right now

The product existed in three disconnected pieces, and the disconnect was the whole
problem.

| Piece | State | Reachable? |
|---|---|---|
| Construction domain | 6 projects · 240 WBS nodes · 2778 progress readings, ingested from the real corpus | **was not** |
| Role-shaped reads | `application/role_views.py` — CEO cockpit, PM workspace, HITL inbox, **18 tests** | **was not** |
| HTTP API | **72 operations, 64 paths** — substrate only | no construction path |
| UI | one 1046-line `index.html`: workflow tree, task list, event feed | no construction view, no charts |

**Not one of the 72 operations touched a project, a WBS, a progress reading, a contract,
a supplier, a purchase order, an RFQ, a tender, a zone or a gate.** The only path
containing any of that vocabulary was `/api/v1/tasks/{task_id}/delegate`, matching on
"gate" inside "delegate". The pipeline read a real corpus into a real database, the
reads answered real questions about it, the tests proved they did — and none of it could
be seen or operated by a person. A library with no consumer.

**Phase 5 has now closed that.** 7 construction operations, 28 endpoint tests, 22 page
tests, and a UI with the three role surfaces in it.

---

## 2. Phase 5 — done

### 5a. The construction API — `api/construction.py`

| Method | Path | Answers |
|---|---|---|
| GET | `/api/v1/portfolio` | CEO cockpit: projects, gates, value, agent posture |
| GET | `/api/v1/projects` | the project list, paginated |
| GET | `/api/v1/projects/{id}` | one project, with its node count |
| GET | `/api/v1/projects/{id}/workspace` | PM view: zones, lateness, unmeasured |
| GET | `/api/v1/projects/{id}/wbs` | the flat breakdown, with `parent_id` |
| GET | `/api/v1/projects/{id}/activity` | readings in `source_row` order |
| GET | `/api/v1/approvals/inbox` | the mobile HITL queue, oldest first |

Read-only, deliberately. An HTTP write path for construction would need the same refusal
vocabulary, provenance columns and review-as-an-agent-act questions the writers were
built with; until that exists, the honest answer is that this module cannot corrupt
anything.

Two new reads were needed underneath it — `wbs_operations.wbs_tree`,
`wbs_operations.node_by_id`, `progress_operations.readings_for_node` — and one new type,
`application/ports.SqlRunner`, because `api/deps.py` hands handlers an `AsyncSession`
and mypy was right to refuse to call it an `AsyncConnection`.

### 5b. The product UI, REBUILT

**Kept:** the substrate console. Working, tested capability, and deleting it to make room
for new capability would have been the wrong trade. It is one section now, and all 21 of
its tests still pass against the new page.

**Structure:** a persistent sidebar, a breadcrumb bar, and six sections — Dashboard ·
Projects · Approvals · Agents · Decisions · Console.

**The three complaints that prompted the rebuild, and what answered each:**

| Complaint | Answer |
|---|---|
| *"không thể thoát ra ngoài dễ dàng"* | The URL is the state. A `Back` control in the breadcrumb that is **never turned off** once you have drilled in, `Esc` bound on the document, and the list is never replaced by its detail. |
| Work could be seen but not handled | Approve / Ask / Reject in the queue. A **Stop** control per agent, which asks for a reason before sending, because the database refuses a kill without one. |
| No information hierarchy | Six sections, a nav that does not move, a real type and spacing scale, and semantic colour used for exactly four meanings. |

**The role switcher is now additive.** It sets CSS `order` — reorders what the dashboard
leads with, hides nothing, and leaves the sidebar identical. The previous version
toggled `hidden`, which is *why* the page felt like it was hiding things from you.

**The one design rule the stylesheet still exists to enforce:** an unmeasured work package
does not look like an on-time one. Hatched outline and **no bar**, against a solid green
bar. The corpus makes that the *normal* case — 100% of `TĐ BOH.xlsx`'s actual columns are
byte-identical to its planned ones — and a bar is a width and a width is a measurement.

### 5c. What is proved, and what is not

**Proved:** 7 endpoints, 28 tests, including cross-tenant 404s, the unmeasured
distinction surviving serialisation, and five that make the requests that found F123.
22 page tests, including that **every `$("id")` in the script resolves to an element in
the served document** and that the script **parses** under `node --check`.

Plus `scripts/verify_page.mjs`, wired up as `make verify-page BASE=… ORG=…`. There is no
browser in this environment and no headless one, so it does the next best thing: it runs
the served document's JavaScript in Node against a small DOM shim and the **live API**,
then asserts on what it rendered.

    runtime:  script loaded without throwing · 5 fetches · none failed
    rendered: portfolio stats · 6 projects · 9 agents · above-ceiling 0
              40 zone bars — 36 unmeasured, 1 late
              288 of the progress rows marked unmeasured

**It found F125, which 18 unit tests had not.** `pm_workspace` returned the same
measurement two different ways and the two answers disagreed: `late` said a zone was 2
days behind, `zones` said it was on time. The page drew it green while its own legend
said one zone was late.

The fix took two attempts and the first was worse than the bug. Filtering the aggregate
on `actual_updated` was necessary and not sufficient, because
`max(actual_finish_on) - max(planned_finish_on)` are **two independent aggregates** that
need not come from the same row — and on `S028` they did not, yielding **-65 days** for
a zone 2 days late. The answer is a `LEFT JOIN LATERAL` over the newest measured reading
per node, so both operands are the same row, and `_LATE_ZONES` became a filter over that
same lateral rather than a second opinion.

**F126, found while writing the endpoint tests rather than by running them:** a node
from project B answered under project A's URL, because the check read
`node.get("project_id")` and `node_by_id` never projected `project_id` — so it compared
`None` and could not have fired. A predicate is only as good as the value it reads.

### How to check it yourself

    make page

One command: starts Postgres if needed, brings the API up on 8099 in its documented demo
mode, looks up a real tenant, **executes the page against it**, and prints the URL to
open. It needs neither NATS nor Temporal — the construction surfaces read Postgres and
nothing else, and `make dev` requires both brokers, so the obvious instruction leaves
anyone without them stuck at step one.

`docs/CHECK_THE_PAGE.md` is the full walkthrough, in Vietnamese, with the numbers to
expect. The one to look for by eye: **36 of the 40 zone bars must be hatched.** 40 green
bars would mean the page is reporting a project nobody measured as perfectly on schedule.

**Still not proved:** the page has never been *seen*. `verify-page` proves it computes
the right thing; it cannot tell you the layout is readable at 375px, that a colour
passes contrast, or that a bar does not overflow its track. That needs a human with a
window, and it remains the one open item in Phase 5.

---

## 2a. The test database is now bounded — `make test-fresh`

`test_procedure_promotion.py` failed at *setup* with
`Key (slug)=(tenant-7116ae6f) already exists`, which has nothing to do with promotions.
The `tenant` fixture creates one organization per test and **never deletes it**, and
nothing truncated between runs: the test database had **31,211 organizations** in it.

* `make reset-test-db` truncates every table in the schema — not `organizations` with
  `CASCADE`, which left 3518 projects and 6181 progress readings behind because not every
  construction table has a foreign key to `organizations`. The list comes from
  `information_schema`; `alembic_version` is excluded, because emptying a database must
  not un-migrate it.
* The fixture's slug is now `{label}-{pid}-{counter}`, so a collision is structurally
  impossible within a run rather than improbable. The row is also readable now.
* `make test-fresh` is reset-then-run. **2341 passed, 6:25** — down from 9:24, because
  the suite was spending its time on 31,000 rows it was not using.

## 3. Phase 2c — the composite-FK retrofit: **DONE**

**All 135 single-column foreign keys are composite. `pg_constraint` reports zero single-column
keys to a parent that is not `organization_id`.** Migrations `0020` (18 keys, the supply
chain), `0021`/`0022`/`0023` (117 keys, in three tranches of 61/14/42), and `0024` (a check
constraint, below). All three schemas are at `0024`, every one round-trips
upgrade → downgrade → upgrade, and the ORM model is generated from the schema and verified
idempotent by `scripts/sync_model.sh --verify`.

**The problem was never a data leak.** RLS is `FORCE`d on every tenant-scoped table and
every read-path isolation test passed before any of this existed. What RLS does not do is
check a *foreign key*, so a write could name a parent in another tenant and succeed. The
write was the only place the hole showed.

### What it cost, and what the cost bought

F133 through F140 — eight entries — are the record of getting the ORM to agree with the
schema, and most of them are the same mistake wearing different clothes: **editing a
SQLAlchemy model as though it were a text file.** The sequence was a second
`__table_args__` (six indexes lost, no error), text-based merges (three class bodies
duplicated), an AST merge that kept the first copy (a column removed), and a second one
that kept the last (408 and 928 lines deleted). Every failure but the last imported
cleanly.

The two things that finally worked, and they are the transferable part:

* **`scripts/rebuild_model_table_args.py`** — replace each `__table_args__` with what the
  schema says. Duplicated blocks cannot be repaired by appending, and the schema is the
  authority. It does not import the models, because a script that repairs them cannot
  import them while they are broken.
* **`scripts/sync_model.sh --verify`** — run the pipeline twice and require the second pass
  to change nothing. Every one of those scripts was non-idempotent on first write, and a
  non-idempotent step shows up as a hash difference, which is far cheaper than a red test
  three files away.

**What was lost, stated plainly:** the hand-written comments explaining *why* a check
constraint exists. They are prose in a Python file; the schema does not store them. The
rules survive exactly.

### `0024`, which the model rebuild found

Rebuilding the model from the schema copied a real absence faithfully:
`ck_wbs_items_agent_row_needs_a_confidence` was on `bid_items` and `tender_requirements`
and missing from `wbs_items`. A model-extracted work-package line with no confidence cannot
be placed in the review queue, so it is invisible to the process meant to review it — the
same class of defect as a missing `proposal_id`. `TestAiAuthoredValuesAreTriageable` had
been passing because the *model* declared the constraint and the schema did not.

### `0021` has no discriminating test, and that is stated

`tests/integration/test_tenant_keys_per_tranche.py` has one cross-tenant test per tranche.
Downgrading to `0020` turns the `0022` and `0023` tests red. It does not turn the `0021`
one red, because RLS already refuses that relationship — the bare key is belt to an
existing braces. The test is kept and labelled; **61 composite keys in `0021` are currently
unproven**, and the way to close it is a `0021` relationship whose parent is not under RLS.
`organizations` is the one table that is not, by design.

## 3b. What Phase 2c originally proposed (superseded by the section above)

135 single-column foreign keys to a tenant-scoped table across 75 tables.

**Not a data leak.** RLS is `FORCE`d on all 99 tenant-scoped tables and the application
role is not `BYPASSRLS`. It is a **referential-integrity hole on write**: a row in tenant
A can name a parent id belonging to tenant B, and the insert succeeds.

The supply-chain tranche, measured:

    9 tables, 20 foreign keys
    needing uq_<parent>_org_id on 8 parents:
      contracts, projects, rfqs, suppliers, quotations,
      materials, purchase_orders, goods_receipts
    plus 2 self-references: wbs.parent_id, zones.parent_zone_id

`wbs` and `wbs_items` already carry the supporting index from `0017`. A 135-FK migration
in one file is unreviewable, so this is 4–5 migrations, each with its own `up → down → up`
round-trip from empty.

---

## 3c. The eight agents: **seeded, bounded, tested, and converged**

Tập 1's table 10, in the dossier's order, with each one's reason in
`domain/agent_register.py` and the invariants asserted in
`tests/unit/test_agent_register.py` (13 tests) and
`tests/integration/test_agent_register_seeded.py` (11 tests).

| Pri | Agent | Block | SOPs | Ceiling | Granted |
|---|---|---|---|---|---|
| P1 | HR | Back Office | 2 | L2 | L1 |
| P2 | Procurement | Middle Office | 2 | L3 | L1 |
| P2 | Knowledge | PMO | 1 | L4 | L1 |
| P3 | Finance | Back Office | 3 | L3 | L1 |
| P3 | Project Mgmt | Middle + PMO | 3 | L4 | L1 |
| P4 | Design/M&E | Middle Office | 1 | L4 | L1 |
| P4 | Sales/BD | Front Office | 2 | L3 | L1 |
| P4 | QA/QC-HSE | Middle Office | 2 | L4 | L1 |

Sixteen procedures, all `active` after 80 shadow runs, 66 decisions logged. Every agent
granted **L1**, none at its ceiling. Every refusal is written down: the two Tập 1 §5.3 hard
blocks are `must_refuse` entries, not permissions.

Four things this work measured rather than assumed, and all four are F143–F146:

* the dossier's action classes are **six**, in `autonomy_policies`, and my invented
  vocabulary was refused by the promotion gate;
* an agent must not **hold** a hard-blocked class — it must refuse it, and moving the two
  blocked acts from permissions to refusals is what let all sixteen promote;
* `sop_definitions`, `autonomy_policies` and `roles` are tenant-scoped, so a new tenant
  cannot run the dossier's agents without them. `scripts/seed_reference_catalogue.py` and
  `--from-org` exist for that, and the second version of the copy script was itself
  non-idempotent;
* **a seeder that only adds cannot be re-run.** The development rows had carried stale
  ceilings for a while and no run could correct them, because an existing agent was skipped
  (F146). The seeder now *reconciles* the ceiling — which is a function of the action
  classes and the policy table, not a free choice — and reports each correction. The
  **grant** is deliberately untouched: L1 is a decision, and a run that finds a higher grant
  reports it rather than quietly lowering it. The falsifier is
  `test_a_stale_ceiling_is_corrected_not_left`, which corrupts a ceiling, re-seeds, and
  requires it corrected: a test that only runs a seeder against a clean tenant cannot see a
  seeder that does not converge.

**Resolved.** All eight development rows now read the derived ceilings, and a second run
writes 0 and reports 24 already present.

## 4. Phase 3 — document control: **DONE**

The only Tấp 1 governing principle with nothing behind it. It now has all four, and each
one is a parser or a table rather than a convention.

### The code is parsed, never concatenated

`domain/document_code.py`. `DocumentCode.parse` refuses a bad segment **by position and by
value** — `document code 'ONX-BO-H-SOP-001' segment 3 (bộ phận) is 'H'; expected
two or three uppercase letters` — because a code is something a person types and *invalid
document code* on one is a support ticket. `DocumentCode.make` is the only supported way to
**build** one, so the grammar lives in one place and nothing in the repository concatenates.

The grammar was **derived from the 28 seeded codes**, not assumed: all 28 split into exactly
five segments. The block and kind vocabularies are closed and checked; the **department is
open** at two-to-three uppercase letters, because 18 codes cannot establish a vocabulary and
a closed list invented from them would refuse the 19th department on the day it arrives.
`test_a_three_letter_department_is_accepted` puts that on the record, so it reads as a
decision rather than an oversight.

81 unit tests, including that **every** seeded code parses and agrees with its own
`block` / `department` / `doc_type` columns. That information existed twice on the row and
nothing checked the two copies matched.

### Schema — migration `0025`

| what | why it is separate |
|---|---|
| `documents.code` | the dossier number, parsed. Nullable, because `documents` also holds an uploaded attachment with no number, and inventing one would be a lie in the identifier column. |
| `document_versions` | the **content** lineage. `sop_versions` versions the *procedure*; a procedure can be revised without the file changing, and a typo fixed in the file with nothing else moving. |
| `document_distributions` | who must hold which revision, by what channel, and whether they acknowledged it. This is the table that turns *the SOP says* into something checkable. |
| `documents.expires_on` | **distinct from `retention_until`** — see below. |

### Expiry is not retention, and the permit is why

`expires_on` answers *is this still valid*; `retention_until` answers *how long must we keep
it*. A construction permit has a three-year life and a seven-year retention obligation, so it
expires long before it may be destroyed — and a schema with one date has to record the wrong
one. `ck_documents_expiry_precedes_retention` refuses an expiry that outlives the retention
deadline, because that combination is never a decision anybody made on purpose. The tests
build the permit case explicitly, because it is the case that proves the two columns are not
one column wearing two names.

### What this phase found

Writing it surfaced eleven more defects, **F148–F157**, and the two that would have shipped
matter more than the nine that were caught:

* **`document_versions` and `document_distributions` had no RLS** (F154).
  `relrowsecurity = false`, **zero** policies, while 95 sibling tables had `true`, `FORCE`,
  and one `tenant_isolation` policy each. Every other test passed, because a table everyone
  can read still enforces its own constraints. Both existing RLS checks were per-tranche
  against hand-kept name lists — the same gap the dossier names about itself in
  `test_construction_domain.py`. `TestEveryTenantTableIsIsolated` now derives the set from
  `Base.metadata`, so a new tenant table is covered the moment it is declared. Verified by
  dropping `FORCE` from one table and watching both it and the named test fail.
* **The dashboard's "Above ceiling" control read 8 when the truth was 0** (F152). The query
  said `granted_level <> autonomy_ceiling` under a label that says *above*, and both columns
  are `varchar` — a string comparison of a level hierarchy, on which `'L1' < 'L2'` is false.
  It alarmed on the **correct** state of the system, and raising an agent to its own ceiling
  is the desired action. Found by `make verify-page` and by no unit test at all, because
  `test_construction_ui.py` asserted only that the *label* was on the page — and a label
  test cannot see a wrong number.

The rest: the naming convention composing check names (fourth occurrence, F148), a generator
re-emitting what its caller already supplies (F149), Postgres truncating a 73-character
constraint name with a hash and reporting success (F150), a `CHECK` with its implication
backwards (F151), a typo'd constraint name (F152's sibling), and an `asyncio` loop rule
(F157). All in `docs/FAILED_APPROACHES.md`, and each now has an instrument — each proved by
planting the defect and watching it fire.

### Tests

`tests/integration/test_document_control.py`, 23 tests, deliberately **lopsided** rather than
exclusively negative. A constraint that refuses everything refuses every bad row too, so a
suite of only negative tests cannot tell a working check from a broken one — and that is
not hypothetical: `a_mandatory_unsent_row_is_not_a_row` had its implication backwards and
refused the *outstanding* obligation the matrix exists to show. It was caught by the one
positive test in the file (F151).

### In the product: the register, and the ability to act on it

Phase 3 is not finished because three tables exist. It is finished when a person can open
the page and **do** something, which took four more pieces:

| piece | what it is |
|---|---|
| `application/document_control.py` | `document_register`, `document_detail`, `issue_version`, `distribute`, `acknowledge` — the first behaviour-changing path into the construction domain |
| `api/documents.py` | six operations, registered as `documents_router` (89 operations total) |
| `scripts/seed_document_register.py` + `make seed-docs` | publishes the dossier's 28 SOPs as controlled documents: 28 + 28 + 28 rows, idempotent |
| the page | `Documents` in the sidebar, a register, a detail with the lineage and the matrix, and the two actions that change it |

**The first behaviour-changing path, and what it holds.** `api/construction.py` is read-only
by decision, and putting the writers in a separate router is what makes that claim
trustworthy — a reader of `construction.py` can believe it because the thing that breaks it
is not in it. Three rules the schema only *could* hold are held by the operations:

* **issuing a revision closes the previous one**, in the same transaction;
* **a re-send updates the obligation** rather than inserting a second row, so a retry
  cannot inflate the compliance figure;
* **an acknowledgement keeps the first reader and the first timestamp** — "when did they
  read it" is the only reason that column has a timestamp.

**Two things a person can now do that they could not before**: *send revision N to a role*
and *confirm you have read it*. The second is the demo: the seed leaves 28 obligations
outstanding rather than fabricating acknowledgements, and pressing the button moves the
number. `make verify-page` checks 58 things on the served page, including that the unread
tile **agrees with the API** — which is how the "Above ceiling" defect in §4 was caught,
and how a stale tile would be caught.

**The way out is on the page**, which was the complaint this answers: a breadcrumb, the
`Back` button, and `Esc`. And leaving a document releases its detail — the same rule the
project view already had, and one `verify-page` check that the documents view failed until
it was fixed (F161, F162).

Still not built, and the reason has not changed: no permit documents and no filled
inspection record exist in the corpus, so the form catalogue and the 12 remaining assurance
tables are scheduled **last**. Building them now would mean inventing the requirements.

---

## 5. Phase 4c — DONE

* `GET /api/v1/ai-decisions` — filter by agent, decision, time range. 21 tests.
* `GET /api/v1/agents/{id}/control` — what a person may do to that agent right now.
* `POST /api/v1/agents/{id}/kill` — **required reason**, and killing twice keeps the
  second reason rather than refusing. A refusal there is a dead end at exactly the moment
  somebody needs to act.
* `POST /api/v1/agents/{id}/revive` — **not** the inverse. It grants L1 rather than
  restoring the previous grant, because an agent stopped for a reason and running again
  has not had that grant re-justified.

Each writes an `ai_decision_log` row with `actor_type = 'system'` and `agent_id` naming
the **subject** — a row saying "agent X decided to kill agent X" would be a lie in the one
table whose purpose is to be believed.

**Migration `0019`.** `ck_ai_decision_log_decision_known` allowed five values —
`acted`, `proposed`, `refused`, `escalated`, `shadowed` — and refused my first attempt at
`agent.kill`. The constraint was right and I was wrong: `decision` is the *agent's own*
decision vocabulary, and a kill is a decision *about* an agent taken by the platform. The
migration adds `killed` and `revived`, and — the important half — extends
`refusal_is_explained` to cover both, because an unexplained kill and a kill whose reason
was lost are the same row and only one of them is a governance failure.

Also: the five check constraints `0016` put on `agents` existed in the database and **not**
in the ORM model, so an `alembic revision --autogenerate` would have dropped them.

---

## 6. The eight agents

The dossier's register order governs. Each is gated behind what now exists: procedures,
shadow runs, promotion, the kill switch, the decision log. **The framework is complete;
the agents are unbuilt.** P1 HR is the pilot every later one is measured against.

| Agent | Corpus support | Status |
|---|---|---|
| P1 HR | roster, org units, `sop_definitions` (28) | **buildable now** |
| Progress | 2778 readings ingested and now reachable | **buildable now** |
| Cost Analyst | 1 quotation | blocked on documents |
| Contract Intelligence | 1 signed contract | blocked on documents |
| Supplier DD | zero qualification files | **blocked** |
| Tender & BOQ | zero tender documents | **blocked** |

Two are buildable and four are blocked by an absence of source documents. That is a
finding, not a schedule.

---

## 7. What is not being built, and why

* **Auth, RBAC, SSO, MFA, encryption, secrets.** Excluded by the brief. Tenancy is real
  because RLS is what makes the multi-tenant read path testable at all — and
  `test_another_tenants_project_is_404_not_403` is the test that depends on it.
* **The integrations.** MISA, Odoo, CDE, MS Project, Primavera, SharePoint, OpenClaw.
  Nothing in the product depends on any of them.
* **`clients` needs a `code` and a `name`, and the corpus has neither.** The header block
  carries project, address, package and item — and no client at all. A decision for a
  human, not a reader.
* **The form catalogue and the 12 remaining assurance tables.** No permit documents and no
  filled inspection record exist in the corpus, so their requirements would have to be
  invented. Scheduled last, in section 4.
* **3-way match.** No PO, GRN or invoice exists as a structured record, so the check is
  buildable and unit-testable but not measurable against anything real.
* **The `DocumentKind` vocabulary is a closed enum.** A fifth `loại` is a code change
  plus a migration, which is right for a numbering scheme and wrong for a catalogue. A
  deliberate trade: `parse` stays a pure function with no I/O, which is what makes it
  testable at all, and a scheme that can be renamed by an operator is a scheme whose codes
  stop matching.

---

## 8. One WBS per project, not forty-eight

Open, and it is the largest known modelling problem left.

48 progress sheets each carry their **own** hierarchy, so the pipeline writes 40 nodes
per project rather than one reconciled tree. One WBS per project is the correct shape.
Reconciling them is a decision about which sheet is authoritative — not something to
pick silently, and not something a reader can decide.

---

## 9. The four questions

1. **What does this actually do?** 5a is 7 reads over code with 18 tests. 5b is a page
   over those 7.
2. **What is it actually called?** `ceo_cockpit`, `pm_workspace`, `hitl_inbox` — the
   dossier's three names, not "dashboard", "reports", "queue".
3. **What does the fixture contain that the real thing does not?** The view and API
   fixtures number `source_row` from a counter, so they *cannot* produce the duplicate
   rows `TĐ Hạ Tầng.xlsx` genuinely contains. A fixture that cannot reproduce the
   corpus's worst case will not catch the next identity bug.
4. **What is already in the schema?** 6 projects, 240 nodes, 2778 readings, all there
   since Phase 2b. 5a was a transport, not an ingest.

5. **Which eight agents?** Two lists have existed in this repository. `docs/THE_PATH.md` §1
   carries Tấp 1's table 10, which is what `domain/agent_register.py` implements and what
   is seeded. The earlier plan's list — Supplier DD, Tender & BOQ, Cost Analyst, Contract
   Intelligence — is a **different set of four**, and those four were the ones blocked on
   absent source documents. They are not the same agents and reconciling them is a decision
   about which set the dossier means, not a merge.

And the recurring defect, now four occurrences (F101, F116, F121, F122): *a measurement is a
fact about what you measured, not about the world.* F123 and F124 are the same shape one
level up — **anything asserted by reading a file is a claim about a file; anything asserted
by making a request is a claim about the system.** F152 is the same defect wearing a
dashboard: a tile labelled *above ceiling*, computed as *not equal to ceiling*, read 8 when
the database said 0 — and no unit test could see it, because the test asserted the label.
