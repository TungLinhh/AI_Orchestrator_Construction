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
