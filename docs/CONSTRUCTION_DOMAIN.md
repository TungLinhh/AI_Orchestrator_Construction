# Construction domain — the data model and why

`O-Nexus` is a construction and MEP/EPC operating platform: opportunity, bid,
contract, execution, handover, closeout. This file records the domain model, the
decisions behind it, and the corpus it is being built against. The 18-section
plan that motivated it lives in the conversation; this is the part that has to
survive contact with the code.

Status: **five tranches landed and proven.** Forty-one tables across migrations `0007`
through `0011`, applied forward and backward against a live database, plus the first
ingest unit, the Gate workflow, and the material master. The governance spine is
seeded from the O-Nexus dossier: 6 Gates, 45 criteria, 28 SOPs, the four forbidden
zones, a DOA matrix, and the Gate session as a versioned procedure.

`SUPPLY_CHAIN_CORPUS.md` records what was measured in the material spreadsheets and
which three findings changed the schema.

Read `ARCHITECTURE_DECISIONS.md` alongside this: it records where the dossier and
this implementation disagree, which of them won, and why.

---

## 1. The two substrates, and what was taken from each

There are two bodies of working code on this machine, and they are complementary
rather than competing.

| | `ai_orchestrator` | `pmo_project_procore` |
|---|---|---|
| Language | Python 3.14 | Node 22 |
| Size | 1,050 tests | 24,024 LOC, 72 tables, 70+ migrations |
| Has | Proposal → falsify → approve → revert loop, delegation, Temporal, RLS, model gateway, agent runtime | Construction domain: zones, WBS, work items, schedule baselines, daily manpower/material/safety, submittals, shop drawings, RFAs, QA inspections, payments, pillar gates |
| Lacks | All construction domain | All AI agent spine |

**Decision: Python-first.** `ai_orchestrator` is the base. The approval engine's
hash-bound decisions, the proposal quarantine, the falsifier and the audit log
are the hard, differentiating and already-proven parts; rebuilding them in Node
would throw away the most valuable code in the repository. The construction
domain is re-expressed in SQLAlchemy, and the eight tables in the port source
whose whole purpose was authentication or ERP integration are deleted rather
than ported (`auth_refresh_tokens`, `auth_revoked_jti`, `sso_configs`,
`pdpl_consents`, `pdpl_requests`, `erp_profiles`, `erp_push_log`, password
columns).

The Excel ingest is the one piece that must be ported rather than rewritten: it
already parses this exact corpus and already handles the two traps below.

## 2. The corpus

~1,540 documents, real and bilingual, from three Hoabinh Group projects (Bãi
Tràm Estates, Làng Việt Kiều Quốc Tế, Melia, plus a social-housing project),
13 zones (BOH, BPV, BSN, BUT, CLU, GEN, HPV, INF, KID, LOB&SPA, RES VILLAS,
VN RES) and 4 MEP packages (HVAC, PLB, ELE, PCCC). Real document-control
numbering throughout: RFA, MAS, MAA, MDS, MCR, PL01/PL02 price appendices,
HDKT, TBGH, DNTT, BBBG.

Where it lives: `pmo_project/reference_sheets/2019.04.28 HBG-HBC-BCTT/`,
`procurement/input/`, and the two prior apps' `backend/uploads/`.

### Two traps in the data, both already found

**The first sheet is often an empty scaffold.** ~179 workbooks open on a sheet
named `foxz` containing one cell, with the real content on sheet 2. Any
classifier that reads sheet 1 classifies nothing. The ingest selects by content,
not by position.

**Vietnamese grouped numbers.** Money arrives as `1.234,5` — a dot for
thousands and a comma for the decimal separator, the inverse of English. The
prior implementation's stated policy is to *refuse* rather than guess, and that
is the right call: guessing wrong is how a final account fails to reconcile. The
port keeps that behaviour.

### What the corpus does not contain

| Missing | Consequence |
|---|---|
| Tender packages (`hồ sơ mời thầu`) — **zero** | The Tender & BOQ agent cannot be validated |
| Quotations — 1 | Cost Analyst cannot be validated |
| Signed contracts with full appendices — 1 | Contract Intelligence cannot be validated |
| Supplier incorporation/tax/bank evidence — zero | Supplier Due Diligence cannot be validated |
| Permits (`giấy phép`) — zero | HSE workspace has no source documents |
| Real site photos — zero | Site Vision has nothing to look at |

Six of the eight AI services are therefore **built but unvalidated**, and are
documented as such rather than shipped as working. A request list for the
missing material was handed to the user as eighteen numbered items.

## 3. The one rule that shapes the schema

**An AI service never writes a business row.** It emits a proposal, which passes
quarantine, a falsifier, and a hash-bound approval before anything is committed.
The machinery already exists and is proven; it is reused unchanged.

So every business row carries:

```
source        'human' | 'agent_proposal' | 'import' | 'system'
source_actor  free text — a person, a service, or 'migration:0007'
proposal_id   the proposal that authorised the write, or NULL
```

with a check constraint making the claim and its evidence agree:

```sql
CHECK (source IN ('human','agent_proposal','import','system'))
CHECK ((source = 'agent_proposal') = (proposal_id IS NOT NULL))
```

Both directions matter. A row claiming agent provenance with no proposal is
unauditable. A row citing a proposal while claiming to be human-entered is a
model write laundered past the approval engine, and that is the one an audit
would miss.

The alternative — trusting the calling code to set `source` — makes the entire
audit story a convention. This makes it a constraint.

## 4. Tables

Ten in this tranche. The remaining ~60 are designed in the plan and not yet
written.

| Table | Holds | Notable |
|---|---|---|
| `clients` | The party a contract is signed with | `tax_code` is text; leading zeros are significant |
| `client_contacts` | Named people at a client | Approvals are addressed to people |
| `projects` | A delivery engagement | `contract_value` is the award envelope, not derived from `contracts` |
| `project_roles` | Who holds a role on a project | **The table DOA routing resolves against.** A department is a queue; a project role is a name with a fallback chain |
| `project_phases` | Mobilisation → handover | Coarse calendar, not the contractual milestone set |
| `units_dictionary` | Controlled units of measure | Natural composite key, no surrogate |
| `zones` | Physical areas of a site | The corpus is organised by zone, so it is a first-class dimension |
| `wbs` | Scope tree | Structure, not schedule; stable once priced |
| `wbs_items` | Priced lines | `quantity` and `unit_code` are separate columns |
| `milestones` | Dated events | `kind` separates contractual from internal: one is a claim, the other is a conversation |

Migration `0007`. Everything upstream of a signed contract is tranche 2.

## 4a. Commercial front end — migration `0008`

| Table | Holds | Notable |
|---|---|---|
| `opportunities` | A prospect, before any tender or bid | `expected_value` is an opinion and lives here, not on the bid |
| `tenders` | The invitation to bid | `opportunity_id` nullable — public tenders arrive with no prior relationship |
| `tender_documents` | One file inside a package | The classifier's `role` label and the operator's `extraction_status` queue |
| `tender_requirements` | One requirement read out of a package by an agent | **The first AI-authored table.** See below |
| `bids` | Our answer | `margin_pct` stored explicitly, not derived, so re-pricing does not rewrite history |
| `bid_items` | Priced lines in a bid | Separate from `wbs_items`; copied across on award |

### `tender_requirements` and the triage problem

The first table an AI service writes, and the first place where **"how wrong is
this and who says so"** matters more than "is this right".

A tender has a hundred requirements. A human checks forty in an afternoon. The
ones that will disqualify the bid are the ones that did not look strange — so
three columns are structural rather than optional:

* `source_page` and `source_span` — where the answer came from, so a reviewer
  compares it against the sentence the model read rather than their memory of
  the document. `source_span` is clipped text, deliberately **not** a hash: a
  hash would make the comparison impossible.
* `extraction_confidence` — **mandatory for a model-written row**, by check
  constraint. The review queue is ordered by it, so a row without one cannot be
  placed in that queue: it looks recorded and will never be read. The reverse is
  deliberately not required, because a human-typed requirement has no model
  confidence and inventing one would poison the number the review sorts by.

**A rejected extraction is kept**, `status='rejected'` with `review_note`, not
deleted. A deleted wrong answer is a wrong answer the next run produces again,
because nothing survived to correct it. This is the input the correction loop
consumes, and the same loop that fixes a defective procedure.

### `bids.margin_pct` is allowed far below -100

A bid priced at a -140% margin is a number the gate must be able to **store** in
order to refuse it. Clamping the column would hide exactly the bids that should
never be submitted, and the clamp is invisible in the data — it looks like a
number somebody computed.

### Design decisions worth defending

**`units_dictionary` has a composite primary key `(organization_id, code)` and
no surrogate id.** This was forced by a database error, not chosen for
elegance. The first draft gave it a ULID and had `wbs_items` reference `code`;
Postgres refused, because the only unique index on `code` was
`(organization_id, code)` and a foreign key needs a single-column target. Making
the key composite fixed the error and closed a hole at the same time —
`wbs_items` now carries a composite foreign key over
`(organization_id, unit_code)`, so a line cannot point at another tenant's unit.
RLS stops the read; the FK stops the pointer, which is the direction that
corrupts an estimate.

**`organization_id` has no server default anywhere.** A row that acquires its
tenant by default is a row whose tenant is whatever the connection happened to
be bound to. Omitting it makes the RLS `WITH CHECK` fail, and the error names
row-level security. This was learned by making the mistake while writing a test.

**Money and quantities are `NUMERIC`.** Never float. `0.1 + 0.2` has to equal
`0.3` in a quantity take-off.

**`project_roles.person_ref` is not a foreign key.** The platform deliberately
has no authentication and therefore no authoritative user table. Inventing one
here would put a broken referential promise in the approval path.

**`milestones.gate_code` is not a foreign key** to the Gate spine, which arrives
in a later tranche. A forward reference would make the migration
non-reversible on its own.

## 4b. Contracts, variations, claims, suppliers — migration `0009`

| Table | Holds | Notable |
|---|---|---|
| `suppliers` | Companies we buy from or subcontract to | `category` not a boolean; `approved` requires current papers |
| `contracts` | A signed agreement, client or supplier side | Exactly one counterparty, enforced |
| `contract_milestones` | Dated contractual obligations | An amount only on a payment milestone |
| `contract_variations` | Scope, price or time changes | Claimed *and* approved, for value and time |
| `contract_claims` | Claims for time or money | `notified_at` is the field that wins or loses it |
| `claim_events` | The claim chronology | Append-only |
| `contract_clauses` | Clauses read out by an agent | Mandatory confidence; `contracts` is what was *agreed* |

### The variation gap is the money

`value_claimed` and `value_approved` are both stored, and so are `days_claimed` and
`days_approved`. **The difference between them is the money.** A variation instructed
in writing and valued at nothing is a receivable that appears in no report unless
somebody is looking for it, and it is the most common way margin disappears on a
project. `instructed-but-unvalued` is one indexed query, and there is a test that
proves the gap is visible.

This is also why there is **no `change_orders` table**. A variation and a change
order are the same object before and after pricing; two tables guarantee a period
where the instruction is in one and the priced form is in the other, with nothing
joining them.

### A status that means something

Four checks turn statuses from labels into a state machine, each closing a way a
row arrives somewhere it should not:

| Check | What it stops |
|---|---|
| `status='signed'` requires `signed_at` | An executed contract with no execution date |
| `status='approved'` on a supplier requires unexpired papers | A supplier approved once, treated as approved now |
| An amount only on `kind='payment'` | A delivery milestone rolling into a cash schedule |
| `status='signed'` and later implies a signature | `active` with no contract behind it |

The `signed_at` one was **documented in two places and implemented in neither** —
see F77 below. It was found by a behavioural test, one entry after the gap.

### `contract_clauses` is what was read; `contracts` is what was agreed

The Contract Intelligence service writes clauses with a text excerpt and a
mandatory confidence. It does not write `contracts.retention_pct`. That is the
platform's one rule: an agent proposes, a person confirms, and a person's
confirmation is what lands on the contract record. So the terms reports and gates
compute on are columns, and the provenance of *how they were read* lives next to
the sentence they were read from.

### The supplier bank account is a reference

`approved_bank_account_ref` plus `last4`, never a full account number. The only
capability that needs one — refusing to pay a payee that is not the approved one —
is satisfied by a reference and four digits, and the platform does not become a
store of every supplier's bank details.

## 4c. The governance spine — migration `0010`

Tập 1 calls this "xương sống quản trị" and it is the part of the platform that is
worthless without domain data and everything without domain data it gates.

| Table | Holds | Notable |
|---|---|---|
| `gate_definitions` | One of the six Gates | Chair, mandatory members, lead time, max extensions |
| `gate_criteria` | Entry/Exit criteria, Tập 3 §1.3 | `is_mandatory`; a waiver names who granted it |
| `gate_instances` | A project through one Gate | `extension_count` drives the HOLD conversion |
| `gate_criterion_evaluations` | The answered checklist | Generated at registration, never pre-ticked |
| `gate_decisions` | The council's conclusion | The compiler cannot approve |
| `gate_conditions` | Action items from a conditional pass | Deadline and owner both NOT NULL |
| `sop_definitions` | 28 SOPs, by dossier code | The code scheme is a check constraint |
| `sop_versions` | `v[major].[minor]` | Append-only: an old procedure keeps its text |
| `sop_steps` | A step with RACI, SLA, autonomy | The autonomy here is a *claim*, not a permission |
| `sop_raci` | One RACI cell | **An agent is never `A`** |
| `sop_forms` | The FRM catalogue | Tập 1's document control applies to forms too |
| `doa_matrix` | Amount band → approver role | Band is data, not a branch in code |
| `autonomy_policies` | The harness ceiling per action class | `is_hard_block` for the forbidden zones |

### The three that carry the weight

**`sop_raci` refuses `A` for an agent.** Tập 3 §4.1: *"Agent không bao giờ được
gán approval role."* One check constraint, and it is the platform's most
important line of defence — without it, a route by which a model approves its own
work gets added one workflow at a time, each for a good reason.

**`autonomy_policies` is the harness, not a workflow's suggestion.** Tập 1 §5.3
says the level is "mã hóa cứng ở tầng Harness (không thể bị prompt vượt qua)".
So `sop_steps.autonomy_level` is what the procedure *claims* and
`autonomy_policies.max_level` is what is *permitted*; only the policy is
authoritative, and only the policy is read before a workflow runs.

**`gate_decisions` refuses a self-approval.** Tập 1 §1.2 separates proposer,
reviewer, approver and payer. The Gate pack is compiled by PMO and decided by the
council, and one person doing both is refused in Python (clear message) and in
Postgres (the guarantee).

### Seeded, not empty

`scripts/seed_process_spine.py` writes what the dossier specifies, idempotently:

| | Count | Source |
|---|---|---|
| Gates | 6 | Tập 1 §2.2, Tập 2 §E.2 |
| Criteria | 45 | Tập 3 §1.3 |
| SOPs | 28 (5 FO + 10 MO + 7 BO + 6 PMO) | Tập 1 §3.2–3.5 |
| Gate session steps | 5, with RACI and AI level | Tập 2 §E.1 |
| Autonomy policies | 6 (2 hard blocks) | Tập 1 §5.3 |
| DOA bands | 8 | starter set, a company's own policy |

The script asserts the SOP count is 28, because the dossier says "28 SOP" twice
and a list that quietly loses three is a compliance finding rather than a typo.

`domain/gates.py` runs it: `register_session` generates the checklist,
`block_can_pass` returns *what is stopping the Gate* rather than a bool,
`proposed_outcome` derives the outcome and never accepts one as an argument.

## 5. Module layout

`persistence/models.py` stays the agent substrate — tenancy, agents, tasks,
workflows, approvals, audit. It said so in its own docstring and merging the
business tables into it produced a file where finding a table meant reading past
forty tables of someone else's concern.

`persistence/construction.py` is the project spine and the shared base for the
domain: `ConstructionMixin`, `domain_args` and the column-type constants live
there, and the area modules import them. Then one module per area:

| Module | Holds |
|---|---|
| `construction.py` | The shared base, plus projects, zones, WBS, milestones |
| `commercial.py` | Opportunities, tenders, bids |
| `contracts.py` | Suppliers, contracts, milestones, variations, claims, clauses |
| `process.py` | Gates, SOPs, DOA, autonomy policy |

All declare on the same `Base`, so `Base.metadata` is still the whole schema.

All of them are imported by `migrations/env.py`. That import is not cosmetic: a
table only reaches `Base.metadata` when its module is imported, so without it
autogenerate sees a schema with those tables missing and proposes to **drop**
every one of them. The test file binds them to a `DOMAIN_MODULES` tuple it
actually uses, because a linter's autofix once deleted them as "unused" and left
the drift test comparing zero models against 75 tables — see F76 and F79.

`domain/gates.py` is the behaviour behind `process.py`: registration generating a
checklist, what blocks a Gate, and what the outcome would be. It is deliberately
a pure function of state, so the interesting cases are testable without a
database and every caller derives the same answer.

`scripts/finish_migration.py` wraps an autogenerated revision with the docstring,
the RLS loop and the grants, deriving the tenant-table list from the file so it
cannot disagree with what the migration creates. Autogenerate has no opinion
about tenancy, and "add the RLS by hand" is exactly the step that gets skipped on
tranche five. It also adds the `postgresql` import that autogenerate omits for a
JSONB column.

`scripts/seed_process_spine.py` loads the dossier into the spine. Idempotent, and
it asserts the SOP count is 28.

## 6. Tests

Domain and ingest tests, split by what each can catch.

| File | Catches | Needs a database |
|---|---|---|
| `tests/unit/test_construction_schema.py` | A table missing a rule, a float money column, a combined quantity-and-unit column, an unprotected tenant column, a table added without coverage | no |
| `tests/integration/test_construction_domain.py` | The database *accepting* a row it must refuse, and refusing it for the wrong reason | yes |
| `tests/integration/test_schema_matches_models.py` | The ORM and the migrated schema disagreeing, in either direction | yes |
| `tests/unit/test_ingest_numbers.py` | A number read under the wrong convention | no |

The drift test did not exist until migration `0007`, and its absence is why four
points of drift shipped in `0003`, `0004` and `0005`. See §7.

The integration tests assert the **named** constraint in the error message, not
merely that an exception occurred. `pytest.raises(Exception)` passes just as
happily if the insert failed because of a typo, which is the specific shape of
the bug that got through.

## 6a. `ingest/numbers.py` — the first ported unit

The Excel ingest is the one thing being ported rather than rewritten, because it
already parses this corpus. Its first unit is the number parser, and it is a
rewrite rather than a copy.

**The ported implementation is wrong.** `pmo_project_procore`'s `toFloat`:

```javascript
const cleaned = v.replace(',', '.');
return parseFloat(cleaned);
```

One comma replaced, no thousands stripping, no convention detection. Measured:

| Cell | Gives | Should give |
|---|---|---|
| `1.234,5` | `1.234` | `1234.5` |
| `12.500.000` | `12.5` | `12500000` |
| `-1.234,5` | `-1.234` | `-1234.5` |

Every downstream calculation is correct arithmetic on a wrong input. Nothing
raises. A quantity column that is 1000x small reconciles fine against itself and
produces a final account that does not reconcile against the contract.

**And the same repository documents the correct policy.** Its `AGENTS.md` states
that Vietnamese grouped numbers are *refused, not guessed*, with a test named for
it — implemented in `scripts/lib/value-compare.mjs`, the reconciliation path.
`lib/excel.js` is the ingest path. Two implementations, one policy, and the
documentation described only the safe one. The lesson generalises: a documented
rule and an implemented rule are different artifacts, and only one of them has a
test.

The replacement decides separator roles from the whole string:

* **Both separators present** — the rightmost is the decimal point. Grouping
  cannot follow a decimal point, so this is decided, not guessed.
* **One separator, twice** — grouping. A decimal separator cannot appear twice.
* **One separator, once** — ambiguous **iff** it could be a thousands group: 1–3
  digits before, exactly 3 after. `1,234` is refused. `1,5` and `1.234.567` are
  not.

Returns `Decimal`, never `float`, and returns a `Refusal` with a named reason
rather than raising — a spreadsheet has headers, footers, merged cells and blank
rows, and an ingest that throws on the first odd cell never finishes.

### The second trap, and where it is duplicated

~179 workbooks open on an empty sheet named `foxz` with the real content on
sheet 2. The port source handles this — with the **same regex duplicated in four
modules**:

```javascript
if (/^(foxz|sheet\d*)$/i.test(sheetName.trim())) continue;
```

in `construction_schedule.js`, `material_supply.js`, `payment_ar.js` and
`sp_ap.js`. F73's lesson, already present in their code: the knowledge is
duplicated rather than stated once, and a fifth ingest module will forget it. The
port states it once and selects sheets by *content*, not by name — a placeholder
sheet with a real header in it should still be read.

## 7. What went wrong, and what now prevents it

### F71 — a mixin's `__table_args__` is replaced, not merged

**What happened.** The provenance constraints were declared once on
`ConstructionMixin`. Every one of the ten concrete tables declares its own
`__table_args__` for indexes, which *replaces* the mixin's. The constraints
reached **zero of the ten tables**, and every test in the repository stayed green
because the only thing that could have noticed was the database, and nothing
asked the database.

`declared_attr` was tried as a fix and is worse than useless here: returning a
`CheckConstraint` from a `declared_attr` in SQLAlchemy 2.0 attaches nothing at
all, silently. The same is true of `Index`. Verified rather than assumed — see
F75, because it bit three times.

**The fix.** `domain_args(*extra)` mints fresh constraint instances per table
and every table spells `__table_args__ = domain_args(...)`, so the rules cannot
be left off without the words `domain_args` being visibly absent.

**What now prevents it.** A unit test asserting both constraints on every
construction table, a discovery test that fails if a table is added without being
covered, and an integration test that writes the two forbidden rows and requires
Postgres to refuse them **by constraint name**.

### F72 — four points of schema drift shipped undetected

**What happened.** Nothing compared the ORM models to the migrated schema. Found
by accident, when running `alembic revision --autogenerate` for the
construction domain produced four `alter_column` calls against tables written
weeks earlier.

| Drift | Direction |
|---|---|
| `model_profiles.max_classification` VARCHAR(64) vs 128 | schema stale |
| `quarantined_proposals.findings` / `evidence_task_ids` JSON vs JSONB | schema wrong |
| `quarantined_proposals.organization_id` NOT NULL vs nullable | **model wrong** |
| `tasks.procedure_fingerprint` column comment | model missing it |

The third is the one that mattered. A nullable `organization_id` on a
tenant-scoped table means the RLS predicate compares NULL to the tenant GUC,
which is never true — so the row is invisible to its own tenant while still
occupying storage and still appearing in owner-role reports. The migration was
right and the model was wrong, which is not the direction anyone would guess.

**The fix.** Migration `0006` repairs the three schema-side cases; `models.py`
fixes the two model-side cases. Neither half alone is a fix.

**What now prevents it.** `tests/integration/test_schema_matches_models.py`
runs `compare_metadata` on every suite run, configured identically to
`migrations/env.py`. It was verified to fail on injected drift and pass when
clean.

Its own guard test earned its place immediately: on its first run it found that
only 10 of 55 tables were in `Base.metadata`, because `models.py` had not been
imported. The drift test was comparing almost nothing and would have reported
agreement.

### F73 — `alembic_version` was excluded in a test instead of in the source

`verify_rls` reported `alembic_version` as an unprotected table. The existing
`test_tenant_isolation.py` had been passing for as long as it existed by writing
`set(status["unprotected"]) <= set(GLOBAL_TABLES) | {"alembic_version"}` — the
fact lived in a test while the function that needed it did not know.

`alembic_version` is now in `GLOBAL_TABLES`, the test is the simpler form, and
the exclusion is stated once. The same duplication is already present in the code
being ported, where the `foxz` sheet skip is a regex repeated in four ingest
modules.

### F74 — the number parser being ported reads twelve million as twelve and a half

A defect in `pmo_project_procore`, not here, and the reason the ingest port is
not a copy. Covered in §6a: `12.500.000` parses to `12.5`, silently, in the path
that feeds every quantity in the system, while a *different* module in the same
repository documents and implements the correct policy.

The generalisable part: **a documented rule and an implemented rule are different
artifacts, and only one of them has a test.**

### F75 — `declared_attr` silently drops Constraints and Indexes

The documented repair for F71 does not work, and it fails *the same way F71 did*:
no exception, no warning, a subclass that looks completely normal with the
constraint or index simply absent. A feature that appears to work and does
nothing is worse than one that raises, because its absence is discovered by a
downstream query that silently returns the wrong answer.

Measured in a nine-line script:

```python
print(sorted(c.name for c in T.__table__.constraints))
# ['ck_t_id_positive', 'pk_t']   <- the declared_attr constraint is not there
print([(i.name, i.unique) for i in T.__table__.indexes])
# []   <- nothing attached, no warning
```

**The lesson.** Before relying on a declarative feature to attach something to a
class, print what the class actually got and check the list is non-empty. Three
of the last five defects here were found by *measuring* — reading autogenerate's
output while running it for another purpose, running the ported `toFloat` in
node, running a nine-line reproduction — and not one would have been found by
reading the code carefully.

### F76 — the drift test passed only because another test imported the module first

F72's fix passed in the full suite and **failed when run on its own**. It imported `models`
and `construction` but not `commercial`, so it compared 55 models against 61 tables — except
that `test_construction_schema.py` sorts earlier alphabetically, imports `commercial` on its
behalf, and leaves the metadata complete by the time the drift test runs.

Nothing about the drift test required that file. The result was a property of collection
order, not of the schema. And the failure *reporter* then raised `TypeError` — a reporter that
crashes replaces a useful failure with a useless one.

Two fixes: both domain modules imported explicitly, and the hand-written
`assert len(metadata) == 55` replaced with a comparison of the two table *sets*, which cannot
rot and covers both directions. The count was also the wrong shape — it broke the moment
tranche 2 added six tables, for a reason unrelated to what it checked, and **a guard test that
breaks when the thing it guards grows is a guard that gets deleted.**

**The lesson: a test must be runnable on its own, and the check for that is to run it on its
own.** Two entries in this section were invisible to a full green suite and both died in ten
seconds standalone.

### F77 — a control that was documented in two places and implemented in neither

`contracts.status` was documented in the model, in the migration docstring, and in the table
notes as requiring a `signed_at` once signed. The supplier half of the same idea existed. The
contracts half did not:

```
$ grep -c signed_requires_a_date src/ai_orchestrator/persistence/contracts.py
0
```

Nothing checks that a documented control exists. The prose is not executable, the schema is, and
they were written at different moments by a writer who believed they were writing one thing. No
test failed, because there was no test for a constraint that was not there.

Found by a behavioural test that failed on a bind-parameter error in its own *helper* — which is
what prompted reading the schema rather than assuming the constraint existed. One test further
down the file, and the check was real.

**The lesson.** The recurring question in this file has a second form: not *what does this code
do and what is it called* but **what does this documentation promise, and is it enforced?** A
docstring describing a constraint is a claim about the schema, exactly as fallible as a
docstring describing a function. Where a comment states an invariant, a test should assert it —
not because the comment will not be believed, but because the comment and the code are written
at different times and only one of them is checked.

### F78 — the same audit, run immediately, found a second one in the same module

Having written F77, the next thing was to read the rest of the new module's prose against its
own constraints. `claim_events` was documented in three places as **append-only**, and the
schema granted the application role `UPDATE` and `DELETE` on it.

The fix already existed in the repository. Migration 0002 revoked both from `audit_logs` for
exactly this reason, so the codebase had learned the lesson for its audit ledger and not applied
it to the claim chronology two tables away. Migration 0009 now does the same, and three tests
assert an event can be appended and can be neither rewritten nor deleted — append first, because
a grant could otherwise be satisfied by refusing everything.

**F78 was found by deliberately re-reading what had just been written and checking each claim
against the schema.** That is the cheapest defect prevention in this file and it costs one
careful read. It is worth labelling as a technique precisely because seventy-eight entries is a
lot of defects, and the ones that are cheap to prevent should be labelled as such.

### F79 and F80 — found by the tools, and by running the thing

Two from the process tranche, both of which the earlier tranche did not have a
shape for.

**F79: `ruff --fix` deleted four load-bearing imports.** The drift test imported
four `persistence.*` modules purely for the side effect of registering their
tables in `Base.metadata`. A routine autofix removed them as unused. The test
did not skip and did not pass — it compared an **empty** metadata against 75 real
tables and reported every table as undeclared. Two legitimate observations
("the suppression is redundant", "these imports are unused") applied in sequence,
each removing the one thing that made the file work.

Review would not have caught it: the change is four lines *disappearing* in a
commit about something else, in output from a tool you trust.

Fixed by binding the imports to a `DOMAIN_MODULES` tuple the file genuinely uses
— to assert every module in the package is on the list. A really-used import
cannot be auto-removed, and the list is checked rather than trusted. The
generalisation: an import whose purpose is a side effect is invisible to every
tool that reasons about *usage*, and every tool that rewrites code reasons about
usage. Make the reference real rather than annotating it.

**F80: a check constraint named a column that does not exist.**
`gate_criterion_evaluations` shipped a constraint reading `waive_note` against a
column named `waiver_note`. SQLAlchemy does not parse `sqltext`, so it attached
without complaint, and every structural test passed because they assert
constraint *names* and this one had the right name. `alembic upgrade` refused it.

The detector needed care, and the first version was useless: extracting every
`snake_case` word from the SQL and reporting non-columns produces **104 false
positives** on one module, because `IN ('human', 'agent')` is made of quoted
words. Strip string literals first and the count goes to zero. A detector that
cries wolf on 104 findings gets ignored, which is worse than none.

The lesson: **a constraint's presence is not its validity.** Every test asserting
"this check exists" was really asserting "this string is attached to this table" —
a much weaker claim that looked identical. The question that catches the class is
"does it refer to anything that exists", and for a string only a real parser or
the database will answer it.

### A failed migration that announced success

### A failed migration that announced success

Running `alembic upgrade` printed `Running upgrade 0008 -> 0009` and nothing else. The migration
had **rolled back completely** — the version stayed at `0008` — because autogenerate emitted
`postgresql.JSONB(...)` without the import that makes the name resolve, and the real error was
`NameError: name 'postgresql' is not defined`.

It was missed because the check was `grep -E "Running upgrade|ERROR"`, and a Python exception is
not spelled in capitals. **The verification was looking for the wrong string.**

`scripts/finish_migration.py` now adds the import when the body needs it. And the habit behind
it: after running a migration, check the *version number*, not the log line that says a step
started. `alembic current` is the only thing that knows whether it happened.

### F76 again, and the root cause of all three

F72's fix passed in the full suite and **failed when run on its own**. It imported `models`
and `construction` but not `commercial`, so it compared 55 models against 61 tables — except
that `test_construction_schema.py` sorts earlier alphabetically, imports `commercial` on its
behalf, and leaves the metadata complete by the time the drift test runs.

Nothing about the drift test required that file. The result was a property of collection
order, not of the schema. And the failure *reporter* then raised `TypeError` — a reporter that
crashes replaces a useful failure with a useless one.

Two fixes: all domain modules imported explicitly, and the hand-written
`assert len(metadata) == 55` replaced with a comparison of the two table *sets*,
which cannot rot and covers both directions. The count was also the wrong shape —
it broke the moment tranche 2 added six tables, for a reason unrelated to what it
checked, and **a guard test that breaks when the thing it guards grows is a
guard that gets deleted.**

F79 above is the same file's third lesson and the same lesson again: something
asserted a thing was present while the thing was absent, and the assertion had
no way to tell. Three defects in one test file, one root cause.

## 8. What is not built yet

The remaining ~24 tables, in the order the plan sequences them:

1. **Supply chain: steps 1–6 done, step 7 is the finance tranche.** Migration
   `0011` built the material master and supplier diligence. Migration `0012`
   built the ten tables that make the 3-way match *arithmetic* rather than a
   comparison: `rfqs`, `rfq_items`, `quotations`, `quotation_items`,
   `purchase_orders`, `po_items`, `goods_receipts`, `receipt_items`,
   `receipt_checks`, `material_reconciliations`. The structural decision is that
   the documents are referentially connected — a receipt names a purchase order,
   an order names a quotation, a quotation names a requisition, and every line
   traces back to the requisition line it answers. The corpus's own payment form
   (`FRM-BO-002A`) asks for "số PO; số GRN; số hóa đơn" as free text and says the
   system fills in the match; that is what "fills in" means here.
   Ordering and dependencies in `SUPPLY_CHAIN_CORPUS.md` §6.
2. **Finance** — `invoices`, `invoice_lines`, `three_way_matches`,
   `budget_lines`, `commitments`, `actuals`, `payment_certificates`. The 3-way
   match is `ONX-BO-FIN-SOP-002` and needs PO and GRN first, so it cannot come
   before the rest of step 1. Its shape is transcribed from Tập 3 §1.5 in
   `SUPPLY_CHAIN_CORPUS.md` §2 — including that a variance requires a reason
   code, the supplier rating travels with the payment, and the executor is not
   the approver.
3. **Assurance — `progress_snapshots` done** (migration `0014`), the other 12 not.
   `progress_snapshots` is the one assurance table the corpus can actually fill: 199
   of 288 construction sheets carry a header row, and `TĐ BOH.xlsx :: TĐ .BOH` pairs
   `KH` (plan) against `TT` (actual) column for column. The remaining twelve are
   `itp`, `itp_points`, `inspection_requests`, `inspection_results`, `ncr`,
   `ncr_closures`, `hs_events`, `hse_incidents`, `task_permit`, `method_statements`,
   `toolbox_talks`, `site_observations`. 12 files mention ITP and 38 mention safety,
   but there are **zero** permit documents and no filled inspection record, so those
   tables would be built unvalidatable — the reason `progress_snapshots` went first
   and the rest are scheduled against data arriving. See §8e.
4. **Remaining process** — `form_definitions`, `form_versions`,
   `form_instances`, `doa_thresholds`, `risk_register`, `lessons_learned`.
   The Gate and SOP spines are done; the form catalogue is Tập 1's appendix C and
   the two registers are appendix D. Note `doa_thresholds` is already built and
   named `doa_matrix`.

Sequencing note: the Gate spine was built *after* the domain, which is the
reverse of this list's original order. That was deliberate and it was right — a
Gate with nothing to gate is a Gate that cannot be tested, and the four Gate
workflow tests are only meaningful because there are criteria to answer.

Not started and deliberately so: the React frontend (12 workspaces do not fit in
one HTML file), the eight AI services, and site vision — which is capped at L1
permanently, flags only, never concludes.

## 8e. Migration `0014` — construction progress, and the `KH`/`TT` decision

One table, `progress_snapshots`, plus the pure rules in `domain/progress.py` and the
reader in `ingest/progress_reader.py`. Full account in `PRODUCT_GAP.md` §4b; the
three decisions that belong in this file are these.

**The plan and its outcome share a row.** The corpus pairs `KH` (*kế hoạch*) against
`TT` (*thực tế*) column for column — planned start, actual start, planned finish,
actual finish, planned duration, actual duration — and the question the sheet exists
to answer is the difference between them. Six columns of one row makes that a
subtraction; a `basis` discriminator would make it a self-join and would store the
pairing less directly than the file does.

**It is not `material_reconciliations`.** That table models *milestones for
materials*; this one models *durations for construction activities*. Same word
"timeline", different subject and different arithmetic, and the two must not be
merged.

**Two measurements became constraints, and both would have been wrong by reading.**
`% Hoàn thành` holds `0.65`, `0.8`, `0.9` under a header that says *percentage*, so
completion is a ratio and a value above 1 is **refused** rather than divided by 100 —
a genuine `65` on a future sheet and a mis-keyed `0.65` on this one are
indistinguishable, and silently choosing between them is how a figure ends up a
hundred times out with nothing raised. And `Số ngày` is an **inclusive** day count,
five for five, so `planned_duration_days = planned_finish_on - planned_start_on + 1`
is a check constraint; a schedule counting working days is refused rather than
quietly accepted, which is the same trade as `units_dictionary` having no default.

The column is `Numeric(7, 4)` and not the obvious `Numeric(6, 4)` for a related
reason: at width 6 a `100` is refused by the *column width* rather than by the rule,
and a constraint that fires for a reason nobody wrote down is one nobody can reason
about later.

## 8c. Migration `0012` — requisition to receipt, and what the corpus decided

Ten tables. The design rationale is in the module docstring of
`persistence/procurement.py`; what belongs here is the set of decisions that came
from *measuring* the files rather than from reasoning about procurement, because
each of them is a place where the obvious design is wrong.

**Deliveries and payments happen in installments.** The payment form carries
"Lần thanh toán/giao hàng: Đợt 2" — round 2. So `purchase_orders` has
`installment_count` and `current_installment`, and `goods_receipts` has
`installment_no`. A schema with one `quantity` and one receipt looks correct for
round one and is wrong for every round after it, and the retention arithmetic
diverges immediately.

**Origin and brand are separate values.** The GRN sheet has `Nhãn hiệu/Brandname`
and `Xuất xứ/C/O` as two columns; the price appendix collapses them into one
"Nhà sản xuất / Xuất xứ". The sheet is right and the appendix is lossy. A receipt
recording "Cooper / UK" as one string cannot answer "is this locally
manufactured", which is a compliance question.

**There are no material codes.** A scan of 400 workbooks for code-shaped strings
returned `IP20` (an ingress rating), `4000K` (a colour temperature), `1F`–`8F`
(floor numbers) and `T2`–`T7` (type marks). Nothing that is a material master.
Confirms `materials.code` being nullable, from §5's independent reasoning.

**Units are inconsistently cased.** `Bộ` 17 times and `bộ` 7; `Cái` 34 and `cái` 6.
Every line references `units_dictionary` by its ASCII code and the corpus's
Vietnamese label is mapped at ingest. A copy would make `Bộ` and `bộ` two units
and a quantity reconciliation would fail on a capital letter — which is why
`test_the_corpus_label_is_refused` and
`test_the_lower_case_corpus_label_is_refused_too` are tests and not comments.

**One number is knowingly duplicated.** `purchase_orders.retention_pct` copies
the contract's rate rather than referencing it, because the retention actually
withheld is a term of *this order as issued*, and the corpus's own amendment
sheet ("Đề nghị điều chỉnh nội dung hợp đồng") shows amendments happening.

**The receipt's inspection checklist is rows, not a boolean.** The GRN sheet has
two blocks — a checklist (`Nội dung kiểm tra` / `P/P kiểm tra` / `Kết quả` /
`Ghi chú`) and an item list — and both are modelled. `receipt_checks.passed` is
nullable, because "not checked" and "checked and failed" are different facts and a
default of `false` would make them indistinguishable.

## 8d. Migration `0013` — the two constraints Postgres had already eaten

`0013` renames two check constraints that exceeded Postgres's 63-byte identifier
limit and were therefore silently rewritten on arrival:

```
ck_gate_criterion_evaluations_proposal_required_for_age_dd1c
ck_goods_receipts_acceptance_requires_quality_and_qa_hs_5164
```

The rules were enforced correctly the whole time. The names were destroyed, which
made them unfindable — and a test that names the constraint it expects could not
be written at all. Full account in `FAILED_APPROACHES.md` F83, including the two
wrong repair attempts and why a regex matching "Postgres rewrote this" is not a
way to find "the rule I am looking for" (F84).

The provenance check's suffix is now `agent_source_needs_proposal` rather than
`proposal_required_for_agent_source`, which puts the longest table in the schema
at 57 bytes. Two guards prevent recurrence:
`TestIdentifiersFitPostgres` (model, refuses any name over 63 bytes) and
`TestNoConstraintNameLooksPostgresTruncated` (live database, refuses a name
ending in `_` plus four hex digits).

`0013` also prompted the discovery that **the migration chain could not build a
database from empty**: `audit_logs.sequence` defaults to `nextval('audit_log_seq')`
and no migration created that sequence. Fixed in the initial migration, F85. Any
future work that assumes `alembic upgrade head` on a fresh database is a claim
worth testing rather than assuming.

## 8b. The three things the dossier adds that are not tables

Worth naming separately, because each is a *state a thing can be in* and none of
them can be enforced by a schema alone. When the agent tables are built they
become columns, and this is the record of why.

**Shadow mode** (Tập 1 §5.5). Every agent runs in parallel with a human for at
least four weeks, at ≥95% agreement with the specialist, before it is granted L3.
There is no way to demonstrate the four weeks happened unless the comparisons are
recorded — so a `shadow_run` table comparing each agent output to the human's,
and an `agent_definitions.autonomy_ceiling` that starts at L1 and is raised by a
decision rather than by a deployment.

**Kill-switch** (Tập 1 §5.5). PMO and IT can stop all agents or one agent
instantly, and the drill is quarterly. A kill-switch nobody has tested is a
wish, so it wants a *recorded* state and a test, not a boolean in a config file.

**The AI Decision Log** (Tập 1 appendix D). Append-only, joining `audit_logs`,
`approvals`, `gate_decisions` and `claim_events` into one queryable chronology of
what an agent proposed, what a person decided, and why. The substrate's audit log
already has the shape; what is missing is the *agent* dimension, and
`quarantined_proposals` shows the shape of a thing that must be recorded without
its payload being reachable.

## 8a. Also not done, and worth naming

* **The Excel ingest itself.** `ingest/numbers.py` is done; the workbook reader,
  the sheet selection and the domain-specific column mappers are not. The sheet
  selection is the next piece and it has a known trap (§6a).
* **`boq_items`.** Deliberately not created: `bid_items` becomes `wbs_items` on
  award, and a third table holding the same lines would be one more place for the
  quantity and its unit to disagree.
* **No workflow, service, or API reads any of this yet.** Sixteen tables with
  constraints and tests is a schema, not a system. Nothing generates a Gate, and
  nothing routes an approval — that is tranche 5, and until then this is a
  well-typed empty database.
