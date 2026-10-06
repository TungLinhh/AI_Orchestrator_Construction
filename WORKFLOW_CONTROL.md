# Workflow controls — 2026-10-07

The console follows Boss brief → campaign → decisions → onboarding. Feedback
pauses a workflow, creates a bounded model assessment, asks questions, reassesses
answers and waits for operator confirmation. Remaining work receives confirmed
feedback. A new campaign revision keeps old artifacts and approvals unchanged.
Skill lessons become experimental, unpublished candidates with source task IDs.
Every proposed action names an actual stage and follows catalog order. Operator
answers, known brief, SOP references and recorded stage states enter the assessment.
Malformed text and protocol placeholders are refused. Proposals are bounded by
existing controllers; confirming prose does not rewrite a brief, change simulation
to live mode, or grant new connector permissions.

`workflow_commands` stores requested and settled generations per organization and
root. Human decisions and continuation requests commit together. Advisory locks
select one process owner; shutdown and kill leave generations pending for restart.
Only explicitly requested commands resume. Workflow gates still require decisions.
Automatic scanning is disabled for the test environment and fake providers; explicit
test dispatch remains available. General tasks are outside workflow feedback scope.

`connector_actions` stores prepared SMTP self-test inputs, hashes, stable action
keys, deterministic Message-IDs and receipts. Prepared → sending commits before
network write. Lost replies and process death stay uncertain. Read-only inbox
reconciliation must observe matching Message-IDs and attachment SHA-256 values.
Missing mail does not prove failure and never triggers an automatic resend.
Confirmed receipts are reused only for identical input. Other write connectors
require their own contracts; historical actions without a ledger are not certified.

Message-ID identifies a message and is not an SMTP idempotency guarantee. See
[RFC 5322](https://www.rfc-editor.org/rfc/rfc5322#section-3.6.4) and
[RFC 5321](https://www.rfc-editor.org/rfc/rfc5321#section-6.1).

`#/settings/appearance` saves language, six palettes, light/dark/system mode,
density, reduced motion and start
screen per browser. Preferences do not change permissions or agent instructions.
New MEP mail/onboarding tasks belong to HR; old campaign owners remain recorded.

Migration 0034 adds both tenant tables, RLS and composite task foreign keys. Apply
to development and test databases; model table args are generated from the schema.
The focused tests kill processes at dispatch and before/after injected writes.
Those tests use fixtures. The real-provider feedback probe leaves a synthetic
campaign paused, sends no mail and grants no human approval or production access.

Feedback applies to workflow roots. Agent blueprint permissions and approved
plans retain their existing editor/review flow. Human source records remain
necessary for real interviews, offer acceptance and onboarding. Future 30/60/90
milestones are plans, not completed work. Separate HR templates, paired candidate
evaluation and a bounded read-only load probe are implemented. Expert-reviewed
corpora, concurrent live campaign throughput and internal API pool queue wait
remain future milestones; see FUTURE_WORK.md for their acceptance conditions.

Final gate and model measurements are recorded in WORK_REPORT.md.

## Hiring with no qualifying candidate

Scoring and HR review remain recorded when nobody reaches the approved threshold.
Before technical interviews, the domain checks the reviewed shortlist. Before
selection, it also checks passing evidence for both interview rounds. An empty
set blocks the campaign and marks the next stage waiting for input. The report
includes the reason, approved threshold, highest score and candidate count.
Repeated Run requests cannot create more model calls or rewrite scores.

The console explains the stop and directs the operator to feedback and a fresh
revision with new sources, or cancellation. The existing campaign does not ingest
new CVs after its reviewed intake. A revision gets a new mail subject and reviews;
feedback prose cannot change salary, threshold or connector permissions. The
structured editor below explicitly shows and reviews brief changes.

Two designs were considered: nullable model selection, or a domain check before
interview/selection. The domain check avoids asking a model to choose from an empty
set and preserves the required selection contract. Updating completed intake in
place was rejected because it would invalidate the scoring and approval snapshots.
The domain owns eligibility, the controller owns persistent waiting and audit,
and the report drives the UI's next action.

Review also fixed missing form-helper ownership in the campaign module and supplies
the server-computed artifact hash of the actual offer to its acceptance form.
Conditional UI probes explicitly use fixture payloads; other console checks read
the live API. They do not certify model quality or real human approvals.

Cancellation of a coroutine awaiting blocking mail I/O cannot prove that SMTP did
not write. Keep the action uncertain and reconcile its Message-ID and attachment
hash instead of resending. Transaction advisory locks expire with the owning
transaction. Sources: [Python task cancellation and thread execution](https://docs.python.org/3/library/asyncio-task.html),
[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS),
and the SMTP protocol references above.

## Structured brief revisions and learning evaluation

`POST /workflows/{root}/revisions` validates a typed hiring brief and exact source
hash. It creates a separate draft and `workflow.revision` approval containing the
diff, affected stages, mode and evidence policy. A different privileged human must
approve. Application verifies the snapshot again, creates one fresh unstarted
campaign and links both roots. Repeated application returns that same destination.
The original workflow remains unchanged and may be paused independently. The new
campaign has its own gates/mail subject and requires new interview, acceptance
and onboarding evidence. Existing simulation CV fixtures remain synthetic.

HR draft creation accepts an explicit cycle: recruitment, performance, payroll or
offboarding. The catalogue snapshots the trigger and source input names and scopes
the authoring SOP steps. Existing approved mixed-cycle plans are not migrated.
Templates do not grant payroll, dismissal, access provisioning or other write rights.

The paired feedback evaluator runs baseline and candidate in independent tasks,
alternates order, and stores output/instruction/corpus hashes with an audit receipt.
It never binds a candidate while evaluating. Feedback publication requires that
persisted live ledger, actual free-model responses in every task and expert review
attestation. Synthetic development proposals cannot publish. Direct skill binding
accepts published versions only. A candidate passing six synthetic cases does not
prove expert quality or production readiness; failed baseline tasks also prevent
publication. Reruns preserve previous tasks and audit receipts.

`POST /workflows/procurement` accepts BOQ and complete quotations from at least
three suppliers. It creates an unstarted live campaign through the existing
controller. Sources and compliance are operator declarations for subsequent QA
and human review, not connector verification or authorization to issue a PO/pay.

Campaign forms preserve drafts after blur only within the same stage, offer hash
and feedback revision. A new scope clears the draft. Polling defers while the
operator edits, including when a request was already in flight.
