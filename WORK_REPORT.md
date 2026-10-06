# Workflow lifecycle refactor — 2026-10-07

This is the current delivery record for the controller refactor. Earlier sections
retain their original measurements.

## Behaviour and ownership

The API no longer owns native workflow drivers, registries or failure cleanup.
Business and blueprint services share an application lifecycle that owns the
PostgreSQL transaction advisory lock, interruption settlement and checkpoint
recovery. API Run, approval decisions, root cancellation and shutdown use one
per-application dispatcher. Mail polling is also owned by application code.

Normal shutdown suspends unfinished work instead of failing its task tree.
Recovery preserves completed artifacts and human gates, closes orphan attempts,
retains committed usage and decision records, and increments the new stage
attempt. An uncertain external write stays blocked for evidence reconciliation.
An approval wakeup arriving before the previous driver exits is coalesced and
retained. Native dispatch defaults to four concurrent controllers.

Runtime tasks, context and the usage ledger now share the persisted execution
ID. The prior runtime ID was minted separately and had no execution row.
The blueprint console now displays the root interruption or failure reason.
API 8100 was restarted with the updated source; health and the console returned 200.

The architecture alternatives, remaining limits and ordered follow-up work are in
[REFACTOR_PLAN.md](REFACTOR_PLAN.md). No schema migration, framework replacement
or tenant optimization was made.

## Evidence and gates

| Verification | Result |
| --- | --- |
| `make lint` | Ruff passed, 375 files satisfy the formatter |
| `make typecheck` | No errors in 166 source files |
| `make test-fresh` → `make test` | 3,387 passed, 3 skipped, 1 deselected, 10 warnings; 393.16 seconds |
| `make test-e2e` | Preflight PostgreSQL, NATS and Temporal passed; 34 tests passed |
| Final recovery, controller and dispatcher checks | 38 passed after final configuration wiring and console copy changes |
| `make page` | Passed live API rendering, navigation and workflow evidence checks |
| `git diff --check` | Passed |

The subprocess recovery test observes committed running task and execution rows
from another connection, kills the process at the RFQ usage checkpoint, then
runs a new dispatcher. Procurement completes 13/13 stages. Previously completed
steps retain one execution and the same output. The interrupted RFQ attempt keeps
its ModelUsage row and is followed by completed attempt 2. No running execution
is left in that test tree.

Shutdown tests cover business and blueprint recovery. Blueprint recovery still
waits for a human review and does not duplicate the review request. The uncertain
mail-write test refuses repeated Run calls without constructing the mailbox
adapter. Dispatcher tests cover the late approval wakeup, concurrency limits and
independent application registries.

All lifecycle tests use fake model content and the test database. No real model
quality, actual mail delivery or real human business approval is claimed.
The page check executes the served console in Node against a live API; it is not
a browser screenshot.

Logs: `.devdata/reports/refactor-full-suite.log`, `refactor-e2e.log`,
`refactor-final-focused.log`, and `refactor-page.log`.

## Remaining operational work

A new Run or approval wakeup is still needed to recover after restart. Dispatch
waiting in memory is not durable. Uncertain mail delivery has no receipt
reconciliation UI yet. Cross-process cancellation becomes visible at checkpoints;
immediate interruption of another process's external call is not proved.
General executor transaction and heartbeat work described in F190 remains a
separate refactor. These limits precede opening new real write connectors.

---

# Current review and delivery — 2026-10-06

This section is the current delivery record. The sections below it retain historical measurements and gate counts; they do not describe this release's final source.

## Console, approvals and organisation coherence

- Approval request/decision events and outbox records are written with the approval transaction. The bell restores all non-expired pending requests, uses tenant-scoped read markers and stable approval IDs, links to the exact review, and removes resolved requests. Stream refresh is debounced for 100 ms; a five-second durable inbox reconciliation recovers missed notifications. Fixed the hidden unread badge, double-offset pagination and a stream cursor shared across tenants. Timestamp event cursors still are not a guarantee of lossless delivery; the durable inbox is the recovery path.
- Live browser observation found the HR version-2 request in both popup and bell within an upper bound of 5.798 seconds after the submit response. Reload retained one entry. This is one observation, not a latency percentile or SLA guarantee. The later local-console decision removed the resolved request from the bell.
- The sidebar is 216 px with tighter spacing and icon/text gaps, and collapses to a persistent 64 px icon rail. Its toggle, tooltips, labels and notification controls are accessible. Both Vietnamese and English console paths are exercised by the page harness.
- Generic Run/retry/individual cancellation cannot flatten or bypass business workflows or agent-owned workflows. Approval/rejection resumes the appropriate controller. Generic queue maintenance now excludes workflow-owned roots and planned stages, including work waiting for inputs or review; interrupted-driver and stale-execution diagnostics retain their separate responsibilities.

## Free-model design, review and setup

The new agent flow first requests a real OpenRouter `:free` model to expand the Boss mandate into an editable plan: responsibilities, instructions, source inputs, ordered tasks, outputs, criteria, source SOP codes/step references, and human review gates. The selected profile is reduced to explicitly zero-priced free candidates with no paid or scripted fallback. Errors, rejected artifacts and token usage remain recorded. Schema validation rejects missing source references, invented references, future-output inputs and stale edits; reference coverage alone does not prove semantic quality.

The six enabled departments use the tenant SOP catalogue plus the explicit O-Nexus playbook mapping. This includes Sales sources filed under BD/CR, QA/QC-HSE sources under HSE/RSK and supporting mapped procedures for Design, Procurement and Finance. IT development is deferred; existing reference records and HR mail/onboarding support are retained. No new Bai Tram project work was started.

Boss can edit every plan field and add, delete or reorder steps. Edits increment the revision and expire prior pending requests. Approval binds the exact reviewed configuration/plan hash. Setup is atomic with the decision: definition, agent at parent-review autonomy, published pinned skills, read-only tool bindings, workflow root, all stage tasks, review tasks and dependencies. Changed resource availability rolls the operation back. Sources arrive when their stage needs them; already supplied sources are immutable once a stage completes. Stages receive source inputs and preceding outputs, and later work waits for hash-bound human decisions. Local in-flight cancellation settles execution rows and stops descendants. Rework after a request for more information, durable controller recovery, owned retries and independent repeating department processes remain future work.

## Actual HR authoring and local-console setup

- Draft `tsk_01m48f9egjw00p1qrzrkwhf7zn` completed through the real free provider: three successful model responses, 55,971 total recorded tokens and zero USD model usage. No fake or paid provider was used. Earlier failed attempts remain failed in their historical rows.
- The generated plan has 26 stages covering all 14 supplied step references from two HR SOPs. A system-authored editorial revision corrected criteria, source wiring and wording that could otherwise suggest interviews, offer transmission or payments had already occurred. The original model artifact remains in its execution record; the review is version 2.
- Approval `apr_01m48fv39ej8ezped7k0frwg1v` was subsequently accepted through the local console at 2026-10-06 11:50:29 UTC (18:50:29 Asia/Saigon), recorded as `dev:no-auth:1fk2ahswkwhm`. This is a recorded local operator decision, not authenticated proof of the person's identity. The agent did not manufacture a human decision.
- That decision actually created agent `agt_01m48gt2t1vdpfnr9z7dztktcb` and workflow `tsk_01m48gt2v1y0kyn9q1m6p3mf0k`: 26 stage tasks and 26 review tasks, all assigned. The root is `waiting_for_input`, with no supplied source inputs and no completed operational hiring stages. The source HR agent had no published pinned skills; its two read-only tools were copied, rather than claiming invented skills.
- This generated workflow prepares and evaluates document artifacts. It does not itself send offers, conduct interviews, pay salaries or provision production accounts. It currently joins several HR procedures into one linear plan. This review fixed intake to wait at each stage rather than requiring future evidence up front. The next milestone splits recruitment, payroll, performance and offboarding into independent reusable workflows. The existing native MEP controller remains the concrete mail/evidence integration.

Evidence under `.devdata/reports/`: `blueprint-hr-live.json`, `blueprint-hr-reviewed.json`, `blueprint-hr-live-audit.json`, `blueprint-review-request.json`, and `blueprint-latest-console-state.json`. The earlier pending-review audit is a dated snapshot; the last file records the subsequent decision and actual setup. Model credentials remain outside Git and reports.

## Re-audit of the inherited business workflows

The existing MEP root `tsk_01m47gke82xpv0savs9jc9akn1` still passes all 111 independent checks across 21/21 stages. SMTP/readonly IMAP and three synthetic CV hashes were real; scores remain 96/50/9 with only the strong CV shortlisted. Interviews, reviews and offer acceptance were explicit fixtures/system simulations. Five onboarding sandbox artifacts were written and read back; production provisioning remains false, and 30/60/90-day reviews are scheduled future work.

The existing procurement root `tsk_01m47gke6va1a9x5246m6n3ewt` still passes all 75 independent checks across 13/13 stages, all three BOQ lines and an exact 77,000,000 VND fixture PO/GRN/invoice match. Materials reviews are simulated. No actual order, delivery or payment is claimed. Fresh reports are `review-mep-audit.json` and `review-procurement-audit.json`; this review did not repeat SMTP or supplier actions.

## Additional review fixes and independent setup audit

Source fields in the plan editor now support adding, removing and renaming keys, with duplicate-key feedback. Workflow detail shows only the current missing sources, accepts partial intake, preserves earlier evidence, exposes refresh, and links to the native business workflow controls. Integration coverage proves that screening can finish before interview evidence exists, later evidence resumes the workflow, and historical source text cannot be overwritten. A request for information no longer signals Temporal as a rejection; answering/resubmitting the review is still a future milestone.

The page verifier had treated the seed's seven departments as an exact maximum. Operators had added an eighth unit, so this failed despite correct rendering. The check now uses the seed as a baseline; separate structural assertions still require every live department to appear in the correct office.

`uv run python scripts/audit_agent_blueprint.py --org <org_id> --draft <draft_id> --output <report.json>` independently checks the saved approved revision, hash, agent autonomy, stage instructions/contracts, review tasks, exact dependency edges, read-only grants and real zero-cost model records. The actual HR setup passed 141 checks with zero completed stages; output explicitly says `agent_setup_only` and does not certify artifact quality. Evidence: `.devdata/reports/review-blueprint-provision.json`. The inherited MEP/procurement data were re-audited again at 111/75 checks without repeating email or supplier actions.

## Release verification

Verification passed: `make lint` (360 formatted files), `make typecheck` (162 source files), the full suite (3,336 passed, 3 skipped, 1 deselected), dedicated E2E (34 passed), and the final `make page` (exit 0). The full suite also includes E2E. Targeted checks passed 88 tests for blueprints/approvals/stream/maintenance and 54 for the final runner guard; after factoring the maintenance read-back selection, 43 maintenance/queue tests passed. The resulting mutation SQL was compared with its previous form and is unchanged apart from whitespace. Test model providers are fake/hash; actual authoring/model records are separately audited.

The final page run initially exposed two maintenance integration defects: demo intake selected owned stages, and the sweep's read-back independently counted 52 protected HR tasks as abandoned after they aged past the queue window. The demo selector and runner now reject controller-owned work before scheduling; mutation and read-back share the same SQL selection and timestamp. The last page run started 0 of 0 ordinary runnable tasks, had no owned-workflow traceback, and passed all navigation checks. All 52 HR tasks remain assigned with no operational executions.

API 8100 was refreshed and read back: primary is `dots-studio/dots-3-note-preview:free`; the HR root waits for `boss_brief` and `salary_band_policy` at its first stage, rather than every future source. This review executes the served UI against the live API in Node; it does not add a new browser screenshot of the final layout. The inherited browser bell observation above remains a separate measurement.

Shadow readiness was rechecked: zero observations, zero elapsed weeks and no eligible deployment. The four-week/28-day, at-least-95-percent gate remains unmet. The ordered next milestones and acceptance conditions are maintained in FUTURE_WORK.md.

---

# Verification of the autonomous organisation, 2026-10-06

The implementation continues the interrupted queue, approval, learning, and native deployment work. Reference files under `docs/` remain unchanged.

## Resulting behaviour

- One console Run drives the complete goal tree with independent worker sessions. The configured task deadline, goal intent limit, retry limit, and execution limit bound the work. Completed intents still consume their goal slots. Reviews settle committed work even when the execution limit is reached.
- Approval decisions restart the whole local goal. Rejection stops the rejected branch without commissioning it again. Pending reviews do not call the model. Decisions carry hash-verified draft snapshots, and resumed runs receive those snapshots from the database.
- The hiring scenario declares separate JD and rubric reviews in its output contract through `x-human-review-order`. A department claiming completion without the corresponding decisions is paused with only the current draft persisted. Tests exercise both reviews with a producer that always claims completion and refuse decisions received out of order or for changed drafts.
- `x-source-summary` marks fields whose job is to report supplied facts. Progress and backup facts can retain the supplied wording. Assessment fields still undergo the echo, placeholder, and content checks. The console and CLI use the same full scenario contract.
- Publishing a learned skill saves its previous binding before changing it. A later output contract failure restores that binding automatically. Provider failures do not revert procedures, and a late result cannot overwrite a newer publication. The supported falsifier is `output_contract`, not arbitrary prose conditions.
- Native deployment readiness requires four elapsed weeks, comparisons in each week, and at least 95% agreement. Shorter windows, lower rates, and future observations cannot satisfy the gate.

## Verification commands

All checks passed against the final source:

| Command | Result |
|---|---|
| `make lint` | Ruff passed; all 336 files satisfy the formatter. |
| `make typecheck` | No errors in 149 source files. |
| `make test` | 3,257 passed, 3 skipped, 1 deselected; 710.42 seconds. |
| `make test-e2e` | 34 passed; 177.27 seconds. Postgres, NATS and Temporal passed preflight. |
| `make page` | Passed the console render checks against the live API and existing organisation data. |

The live API on port 8100 was restarted with the final source. `/health` returned 200, and the linked IT root returned `completed` with no error.

Logs are retained under `.devdata/logs/finish-final-{lint,types,test,e2e}.log` and `.devdata/logs/finish-page.log`. The test suite uses fake/hash providers; the model measurements below are separate.

## Live measurements

These are the latest completed routine results from three measurement reports on 2026-10-06 in Asia/Saigon. They are separate runs, not a fresh full corpus run after the final hiring contract change. Each root completed with zero open tasks and zero successful fake-provider calls. The department count measures declared keys, not independent domain accuracy.

| Scenario | Department | Completed tree tasks | Department tasks with all keys | Successful real calls | Seconds | Source under `.devdata/reports/` |
|---|---|---:|---:|---:|---:|---|
| expense-policy | finance | 4 | 2 | 11 | 76.01 | queue-corpus-approved-gates.json |
| supplier-tender | procurement | 5 | 3 | 20 | 132.13 | queue-corpus-approved-gates.json |
| cv-screen | qa | 17 | 15 | 51 | 303.79 | queue-corpus-approved-gates.json |
| customer-complaint | sales | 12 | 9 | 41 | 289.1 | queue-corpus-approved-gates.json |
| contract-review | design | 6 | 4 | 29 | 249.59 | queue-corpus-approved-gates.json |
| progress-report | design | 12 | 6 | 68 | 440.22 | finish-live-corrections.json |
| offer-approval | hr | 3 | 1 | 13 | 97.75 | queue-corpus-approved-gates.json |
| bom-sourcing | procurement | 7 | 4 | 25 | 195.68 | queue-corpus-approved-gates.json |
| site-safety-review | qa | 4 | 2 | 7 | 48.4 | queue-corpus-approved-gates.json |
| access-review | it | 3 | 1 | 12 | 75.69 | finish-live-final.json |

Completed department tasks by owner: finance 2, procurement 7, qa 17, sales 9, design 10, hr 1, it 1.

The earlier hiring run exposed work completing before required reviews. That outcome is preserved as a failure in `finish-live-final.json`. The final contract guard is verified by application and domain tests. Its live verification attempts are recorded separately in `finish-live-hiring-gates.json` and `finish-live-hiring-retry.json`: no successful model calls, first transport errors and then unavailable or rate-limited free models. The live result of the final staged hiring guard remains unverified. No human approval was granted by the measurement script.

## Remaining conditions

- The deployment report has zero shadow runs and returns exit code 1. The mandatory four-week record at 95% agreement remains unmet. Test fixtures do not count as production shadow evidence.
- Exact and concurrent duplicates are refused. Near-duplicate wording can still open several branches, up to the whole-goal limit. One measured hiring run reached 16 intents. The final hiring verification used an explicit limit of 4 to isolate the review path; it failed before delegation because no live provider was usable.
- Live model output varies. The access review first went to QA and failed. After the ownership instruction was made explicit, a new run reached IT and completed. This is a measured improvement, not a guarantee that every future routing decision is correct.

## Reproduce the measurements

```bash
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python scripts/measure_goal_queue.py \
  --org org_01m3ycsehgwz7v1fk2ahswkwhm --output .devdata/reports/goal-queue.json
uv run python scripts/deployment_readiness.py \
  --org org_01m3ycsehgwz7v1fk2ahswkwhm --output .devdata/reports/readiness.json
```

The first command counts unexpected outcomes in its exit status. Required human pauses are separate from autonomous completions. The readiness command exits 1 until its recorded preconditions hold.

Completed IT goal: [access review](http://127.0.0.1:8100/api/v1/ui?org=org_01m3ycsehgwz7v1fk2ahswkwhm#/give/tsk_01m46xevdsymrh2xysgxfzztze).

## 2026-10-06 — Console management review completed

Reviewed and finished the other chat's console changes: readable business records, typed tool controls, skill-version publication guard, tenant profile diagnostics, null/profile validation, scoped pagination and navigation history. Verification: make lint (343 files), make typecheck (152 source files), make test (3275 passed, 3 skipped, 1 deselected), make test-e2e (34 passed), make page (exit 0 including management routes, bilingual controls and history). Direct selected Dots model tool-call smoke passed; this does not measure business workflow quality.

The legacy live corpus was interrupted at the owner's direction after the scope changed to isolated MEP hiring and procurement. The current progress-report root and its open child were canceled through TaskRepository; completed prior measurements remain historical and are excluded from the new acceptance cases. No Bãi Tràm work is part of the new workflow. Shadow readiness remains false: zero observations, not four weeks.

Next implementation is specified in FUTURE_WORK.md: real Gmail self-test intake, source-checked CV scoring and explicitly simulated approval/onboarding; procurement complete-line coverage, quality review and 3-way match. Those workflows are not claimed complete in this entry.

## 2026-10-06 — Governed business workflow acceptance

Finished the ordered workflow controller, bounded Gmail intake, evidence runtime and console controls. Every stage is a durable task with dependencies, executions, events and audits. Normal task execution cannot bypass the controller. Real gates bind decisions to the reviewed artifact hash; simulation uses system-authored simulated-review audits and creates no forged human approvals. Restart/retry keeps historical failures and only reuses source-checked artifacts. Failed/interrupted runs settle their executions and cancel downstream work.

MEP acceptance root `tsk_01m47gke82xpv0savs9jc9akn1` completed 21/21 stages and passed 111 independent checks. SMTP sent three synthetic CVs to the connected mailbox itself; read-only IMAP received the matching hashes and extracted their source text. The model assessed each CV separately: 96/100, 50/100 and 9/100. Only the strong CV was shortlisted; the injection CV stayed below threshold. Interviews, reviews and offer acceptance used explicitly marked fixtures. Onboarding wrote and read back five sandbox evidence artifacts; production account provisioning remains false, and future 30/60/90 reviews remain scheduled. Real Dots and configured free fallback calls are recorded; no fake model was used.

Procurement acceptance root `tsk_01m47gke6va1a9x5246m6n3ewt` completed 13/13 stages and passed 75 independent checks. All three BOQ lines received an award from eligible suppliers with quality evidence, followed by a simulated materials review and approval. Draft PO, independent fixture GRN/invoice records and exact 3-way matching total 77,000,000 VND. No actual supplier order, delivery or payment is claimed.

Console → Processes → Workflows opens each exact run and stage, score evidence, source procedures, execution logs and responsible agent. Boss can create a live MEP brief separately from acceptance exercises. Live runs wait for real approval and source evidence; the screen shows the required mailbox subject and accepts evidence at the waiting stage. The five later department milestones are detailed in FUTURE_WORK.md; they are a backlog, not executions reported as complete. No Bãi Tràm workflow was initiated.

Acceptance artifacts: `.devdata/reports/finish-mep.json`, `finish-mep-audit.json`, `finish-procurement.json`, `finish-procurement-audit.json`. Reproducible runner/auditor commands are in FUTURE_WORK.md. Credentials remain outside Git and are omitted from API responses and reports.

The final UI check also caught generic task retry flattening business work into one task. Generic controls now preserve the workflow tree, duplicate retry returns the same active run, and individual stages cannot run or cancel around its controller. Cancellation publishes the root stop before interrupting the driver and cleaning up child rows; the in-flight cancellation regression test reproduced the former lock wait and now passes. One malformed retry created by the old UI verification was canceled with a system cleanup audit, preserving its historical row.

Final verification: make lint passed (354 formatted files), make typecheck passed (159 source files), make test passed (3312 passed, 3 skipped, 1 deselected), make test-e2e passed (34), and make page passed after updating the legacy task-only retry assertion to check the owned workflow tree and its unstarted execution. Logs: `.devdata/logs/finish-release-gates.log` and `finish-release-page.log`. The UI was executed against the live API through the repository's Node harness; browser screenshot verification remains unavailable because the browser tool rejected access. The tests' source-corpus reads are regression checks, not a new Bãi Tràm workflow.

## 2026-10-06 — Department evaluation and operational hardening

Added a versioned 21-case synthetic corpus for seven departments with normal,
missing-source and injection variants. The runner uses TaskExecutionService and
records executions, model usage and independent grading audits. Gold values do
not enter live prompts or numeric validator feedback. Reference playback is
explicitly labeled as testing execution machinery, never model competence.
Training observations create unpublished development proposals. Holdout creates
no lessons; sandbox candidates use one successful proposal per training case
and cannot promote skills or change production instructions. No Bãi Tràm data,
real external actions or invented human approvals were added.

The MEP integration test executes the original 21-stage controller with three
file-drop CVs, six explicitly fake model stages and seven simulated system
reviews. It writes and reads back five onboarding artifacts, records zero human
approvals, keeps production access false and leaves future reviews scheduled.
Procurement's original 13-stage controller tests remain active. These results
do not replace the earlier separately recorded SMTP/IMAP acceptance.

Fixed the scenario auditor selecting unrelated completed tasks elsewhere in
the tenant. A new failed run cannot pass from historical Finance output. Fixed
the reference-catalogue test selecting a blueprint fixture as the original SOP;
it now reads the actual seeded holder using the RLS-protected app role.

Changed the evidence runtime's single-tool choice to required after reproducing
a provider incompatibility with named choice and schema references. Real Dots
and free fallback calls now produce verified artifacts; upstream errors remain
visible. The baseline scored 15/21, train 6/7 and candidate holdout 13/14. Sales
revealed ambiguous status vocabulary, so protocol 2 declares a status enum;
both Sales retests passed without changing gold answers. These are separate
observations and do not establish causality or production readiness.

The final protocol-2 holdout run passed 14/14 with one training proposal per
case: 20 recorded real responses, zero fake, four error responses handled via
free fallback, 65,512 tokens and 0 USD recorded. Task p50 was 19.874 seconds,
p95 59.26 seconds. The final explicit mock playback passed 21/21. Both reports
retain production_ready=false. Corpus data stayed unchanged; protocol and
per-case schema hashes make the contract revision visible. Existing failures
were not rewritten or included as successes in this run.

Console colors and selected navigation were refined. Cached the migration stamp
and moved its subprocess off the event loop. Local warm-cache UI p50 improved
from 1999.31 to 23.47 ms; Departments from 4022.33 to 134.99 ms. Cold startup and
larger workloads still require separate measurements. Browser inspection
confirmed organization layout and task-specific log navigation; a screenshot
was saved outside the repository.

Verification: lint/format passed (367 files), mypy passed (162 source files),
the fresh full suite passed (3374, 3 skipped, 1 deselected), E2E passed (34),
related final contract/cache/proposal checks passed (38), and make page passed.
Logs and JSON measurements are in `.devdata/reports/`; commands are documented
in examples/organization_eval/README.md. Research and acceptance milestones are
in RESEARCH_REPORT.md and FUTURE_WORK.md. Retain Pydantic/PydanticAI/Temporal;
HR templates, controller lifecycle and relay aggregation are proposed refactors
awaiting the owner's decision. No schema, dependency or major refactor changed.
