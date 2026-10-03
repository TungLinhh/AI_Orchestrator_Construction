# Current State

What exists, what works, what partially works, what is broken, and what is
deliberately not built.

Dated, because a status document without a date is a status document nobody can
trust. Entries are re-verified by running the gate, not by reading this file.

**Date**: 2026-09-30
**Milestone reached**: M16 — three-tier organisation demonstrated end to end.
**Scope excluded by decision**: authentication (no login, RBAC, SSO, MFA,
secrets — simulated personas and a role switcher) and every external integration
(MISA, Odoo, CDE, MS Project, Primavera, SharePoint). Tenancy is **not** excluded:
`x-organization-id` is required so row-level security stays real.

## What works, measured rather than asserted

* **Three-tier delegation, run for real, on work with an answer.** Seven
  scenarios in `application/scenarios.py` — an expense-limit decision, a tender
  comparison, a CV scored against a rubric, a warranty complaint, a subcontract
  risk review, a progress report, a salary offer — each run through CEO → office →
  department on 2026-09-30. **7/7 finished at the department that owns the
  work**, each producing the keys its `expected_output_schema` declared.
  `scripts/run_real_scenarios.py` is the harness and its exit code is the number
  of scenarios that did not finish.
* **Seven departments under three offices**, two per office except Back, which has
  three: Front (Sales, Procurement), Middle (QA/QC-HSE, Design), Back (Finance, HR,
  IT). IT is the seventh, added so `BO-IT-SOP-007` and `PMO-KNW-SOP-006` stopped
  having no owner (F237).
* **The console draws the hierarchy it is told, and a test asserts the nesting.**
  `GET /departments` returns a server-built `tree` where every department sits inside
  the office its `parent_unit_slug` names, and every box carries `key`, `label` and
  `unit` so all three tiers can be opened. It was flat — `parent_id` was never
  selected — while 100 console checks passed, because the only hierarchy check was a
  substring match (F240).
* **A person pressing Run now runs something.** The button posted
  `start_workflow: false`, printed "Queued." and navigated away — with no Temporal and no
  worker on the `make page` path, nothing ever claimed the row. It now posts the task and
  then `POST /tasks/{id}/run`, which drives the same `execute_task` the Temporal activity
  drives and returns a handle. "With an agent: 0" after pressing Run was true and was the
  button's own doing (F269).

* **The queue drains.** `abandon_unclaimed_tasks` fails any non-terminal task with no
  execution started inside `STUCK_AFTER_SECONDS`, writes one audit row each, and is
  idempotent because the `UPDATE` only matches non-terminal rows. 113 orphans became 113
  failures with `nobody picked this up, so it never ran` (F269).

* **`make page` sweeps the tenant it opens.** It ran the sweep with no `--org`, and the
  script resolves its own — a *different* organisation. Every run printed
  "0 stranded, 0 expired" for a database nobody was looking at. `ORG` is now one make
  variable read by the sweep, the verifier and the printed URL (F267).

* **Four defects that only a screenshot showed, and no test did.** `[object
  HTMLLIElement]` filling the Workflow panel (DOM nodes joined into `innerHTML`); two
  panels sharing four element ids, so one filter's handler was bound to the other panel's
  segment and both render paths wrote to the same list; a filter written
  `state.x === "all" ? items : items` with nothing bound to it; and an agent selector
  stuck on "loading agents…" because the screen that filled it was one of the eight cut
  (F266). The last one meant every task was created with `owner_agent_id: null`.

* **The register's three tiles counted one page, and the rows used another rule.**
  `needs_you + in_flight + settled` summed to exactly the page limit; a `failed` task
  reported `an_agent`, which the register renders as **"With an agent"**. Counts now come
  from a `GROUP BY` over the whole table and `_BUCKETS` is one table beside the SQL `CASE`
  it mirrors. `an_agent` means a running task and nothing else (F268).

* **Departments are separated from each other, and the separation is the database's.**
  There was no unit boundary at all: `app.current_tenant` was the only row predicate, the
  company is one organisation, and `internal_database_query` — which refuses writes,
  multi-statement SQL and catalog reads, three deliberate controls — let a department read
  the whole ledger. Migration 0030 adds `app.agent_unit_ids` as a second, `RESTRICTIVE`
  policy over `tasks`, `executions`, `delegations` and `approvals`:
  higher tier reads lower in full, a peer is **status-only** (visible in the roster,
  not in the rows), and ancestors stay readable so escalation has somewhere to go.
  `domain/access.py` is the policy and is pure; eleven integration tests go through the
  database (F256, F257, F258, F259).

* **A turn budget a normal task could reach.** `max_requests` and `max_tool_calls` were
  both 48, and two real goals died at that ceiling with work still in flight. Both are
  240 now, configurable with `AO_MAX_TOOL_CALLS`. The ceiling that actually stops a bill
  — `max_cost_usd` — is untouched by this, and that is the point of having two numbers
  (F241, F253).

* **The console is three screens, and `make page` exits 0.**
  Departments, Give work and Needs you. Roster duplicated Departments; Dashboard
  duplicated Needs you plus two corpus-dependent views; Decision log duplicated the
  approvals view; Documents and Projects were empty without `AO_CORPUS_ROOT`;
  Recruitment was one SOP. 82 checks, all passing — the five that needed the corpus
  went with the screens rather than being skipped (F247). The cut nearly took the Run
  form with it, because the Console view had been carrying the product's only entry
  point; the form and the delegation tree are inside Give work now.
* **A run that needs a person gets one.** `NEEDS_APPROVAL` writes an approval row
  naming the agent that asked; `AO_APPROVAL_AUTO_APPROVE=true` answers it
  through the same service a person uses. The switch is off by default.
* **Approved work becomes a skill.** A completed run that a person approved has
  its log read and a `SkillVersion` proposed — unpublished, carrying the run log
  as evidence. Publishing pins it to the agent, and the next run receives it as
  instructions.
* **A real free-tier model works.** `qwen3.8-27b:free`, 6 model calls, 8 tool
  calls, 7 of them successful. Before the tool's description carried the schema
  it made 46 tool calls with 13 successes and stopped at the ceiling.
* **A real model delegates.** `liquid/lfm-2.5-2.6b:free` through OpenRouter, on
  2026-10-02, produced `delegation.applied` five times unprompted by the tool schema.
  The prompt fix naming `delegate_to_agent` and listing colleagues (F234) is verified
  against a model rather than assumed.
* **A refused duplicate no longer takes the run down with it.** A duplicate task is
  refused inside a SAVEPOINT, so the caller's transaction — and the work already done
  in it — survives, and the refusal is reported as a duplicate rather than as a
  malformed request. Both were wrong on `main`: the handler matched the wrong unique
  index, so a correct refusal came back as a `ValidationError` the executor does not
  catch, and the `rollback()` used to clean up killed the whole run (F243).
* **A department's work is not lost when it comes back in the wrong shape.** A real
  run produced a 6,713-token report while `tasks.output` was `{}`. The office now
  reads `executions.summary`, distinguishes "no answer" from "an answer in the wrong
  shape", and hands the previous attempt's text to the retry so it reformats instead of
  re-deriving (F239). Measured: five runs had each been told they had produced nothing.
* **The fan-out ceiling is wider than the organisation.** It was 8 against 11 agents,
  so a real run's chief was refused its ninth delegation and the task ended
  `budget_error` — the symptom, reported in place of the cause. Now 16, matching
  `max_active_descendants`, and asserted as a relationship against the seed rather than
  as a literal (F241).
* **A finished run no longer holds the organisation's delegation capacity.**
  `issued_by` fed `current_active_descendants` a count of **every delegation an agent
  had ever issued** — no status filter, no time bound — so `max_active_descendants=16`
  was a lifetime cap. Measured: the chief held 26 historical delegations, was refused
  with `active descendants 16 reached the limit of 16`, and the run ended
  `budget_error`. The organisation could not delegate again on that tenant, for any
  goal, and nothing would have released it. Now it counts descendants whose task is
  live, and three tests hold the count (F249).
* **The chief is not handed the department's material.** A root coordinator is shown
  the routing keys — which office and department own the work — and not the `brief`,
  so it knows *who* should do it and not *what* the work is. It was shown both, and it
  answered: `in=54128 out=9223 tools=15 models=16` and a complete procurement
  recommendation, delivered as a `failed` task carrying `no_delegation`. The design
  comment in `run_pipeline.py` described exactly this arrangement and the
  implementation defeated it, because the brief rides in the root's `input` and the
  root reads its own `input` (F251). A root that fails `no_delegation` is also retried
  once per attempt budget, the same way an office retries its department (F252).
  The chief is also told, in its own prompt, that its task type is routing and that it
  will never obtain the material itself — because hiding the brief alone only moved
  the category: given no data it searched for it, 48 times, and delegated never. The
  absence of data is not an instruction (F253).
* **`run_pipeline.py` can be run twice.** Running a scenario again used to die with
  `ConflictError` and a traceback, because a scenario's goal is constant and the
  deduplication is not optional. It carries a run marker now, as `demo_real_run.py`
  already did (F250).

## What is not ready, stated plainly

* **No deployment.** No Dockerfile, no compose file, no Kubernetes. `make setup`
  and `make dev` run it natively.
* **The organisation is a demonstration.** `seed.py` plants the three tiers and
  the seven departments. There is no provisioning path for an operator's own org.
* **The scenario harness is told where the work goes.** It routes from
  `Scenario.office` and `DEPARTMENT_AGENT` rather than discovering the route, so
  it proves a chain reaches the owning department and that the department
  produces its declared output — it does not prove a live model picks the right
  office. Run it with a real provider to test that (F214).
* **Recruitment stages 2–8 have never run.** One stage completed; six are `created`
  and have produced nothing.
* **Tool calls are counted per execution but the ceiling is still a judgement.**
  `max_tool_calls` (48) has one real observation behind it, not a distribution.
* **Six of the eight agents the dossier registers are not agents the code knows
  about.** The register is data; the seed plants a different set.

## Two things that look like bugs and are not

* The tenant you have been looking at shows a **two-tier** tree. It was seeded
  before the three-tier change and is deliberately not re-seeded, because
  re-seeding would discard the data in it. A tenant seeded now is three-tier.
* `Departments → Executive Agent` opens a panel whose "Offices" row was empty for
  a while. The API had the data and the page did not draw it; both are fixed.

---

## What exists and works

### The domain layer — 426 unit tests

Pure business rules, no I/O, verified by an AST check rather than by convention
(`tests/unit/test_domain_purity.py` asserts the domain imports nothing that can
touch a database or a socket).

| Rule | Where | Tests |
|---|---|---|
| Task / agent / approval / delegation state machines | `domain/state_machines.py` | 40+ |
| Delegation cycle detection, depth, fan-out, descendant caps | `domain/delegation.py` | 20 |
| Budget reservation, commit, release, narrowing | `domain/budget.py` | 8 |
| Authority: default deny, no self-approval, no agent approver | `domain/authority.py` | 6 |
| Policy: four-valued decision, fail-closed default, autonomy floor | `domain/policy.py` | 8 |
| Task fingerprinting and duplicate-work detection | `domain/delegation.py` | 6 |
| Error taxonomy and the single retry decision | `domain/errors.py` | — |

A detail worth stating because it is easy to get wrong: terminal states are
absorbing. `completed → running` raises, and re-running creates a *new* task,
because mutating the row would make the audit trail lie about what was attempted.

### Persistence — 104 tables, 101 with RLS, 29 migrations

Every table carries `organization_id NOT NULL` and leads its composite index, so
tenant isolation is a property of the schema rather than something a query author
must remember.

- **42 of 45 tables have RLS enabled and `FORCE`d**, with one uniform policy.
  The three exceptions (`organizations`, `consumer_offsets`, `alembic_version`)
  are the tenant root, the durable-consumer bookkeeping, and Alembic's own.
- **The application role cannot bypass RLS.** `ao_app` is `NOBYPASSRLS`,
  `NOSUPERUSER`, and holds `SELECT, INSERT` but *not* `UPDATE, DELETE` on
  `audit_logs`. Asserted against the catalogue, not against a connection that
  would pass trivially.
- **The audit ledger is append-only at the database level.** A compromised agent
  can record what it did and cannot erase it.
- **`dedup_key` is separate from `fingerprint`.** The intent hash never changes;
  the index applies to a key that is salted when the caller explicitly asked for
  parallel work. That keeps "did these two agents ask for the same thing?"
  answerable while allowing deliberate parallelism.
- **`capabilities` is `text[]` with a GIN index**, not `JSONB`. See
  `FAILED_APPROACHES.md` F6.

### Model gateway — provider routing, privacy, budget, fallback

- Profiles name a *capability requirement*; agents never name a provider. Swapping
  the provider behind `reasoning_high` is a data change.
- The classification filter is exclusive (`classification_exceeds`), and the
  effective ceiling is the strictest of the profile's and the provider's, so
  neither can be widened by editing the other.
- **A fallback is recorded as a fallback.** `routing_reason` is persisted, so an
  evaluation cannot count a fallback-served run as a primary pass.
- A call whose estimate exceeds the remaining budget is refused *before* the
  provider is called.
- **A real provider call was made and verified.** `openai/gpt-4.1-mini` through
  OpenRouter, structured output conforming to the requested schema, 57 tokens,
  $0.00003, 1826 ms.

### Tools and MCP — exercised against a real subprocess

`examples/mcp_demo_server.py` is a genuine JSON-RPC-over-stdio server. The tests
spawn it and speak to it over pipes, because the handshake, the timeout, the
payload cap and the untrusted-content flag are exactly the parts a mock hides.

- MCP output is **flagged, not filtered**: the injection heuristic produces a
  flag that travels with the result, because silently dropping text makes a
  legitimate result disappear.
- The frame reader is bounded by the server's declared `max_payload_bytes`. A
  server returning 2 MB on one line is refused rather than buffered — `readline()`
  would otherwise raise an unhandled `LimitOverrunError`, and raising the limit
  would turn an untrusted server into a memory-exhaustion vector.
- `calculator` walks a restricted AST. `__import__('os').system('id')`,
  `(1).__class__` and `9**9**9` are all refused, and each is a test.
- `internal_database_query` refuses `INSERT`/`UPDATE`/`DELETE`/`DROP`/`COPY`,
  `pg_read_file`, `set_config`, multiple statements, and any query that does not
  reference `organization_id`.

### Memory — permission-first retrieval

Tenant, classification and ownership predicates are in the SQL query. The
cross-tenant test seeds two tenants with byte-identical content, because that is
the case where a post-ranking filter fails.

A memory without a source is stored and marked uncitable, not refused: refusing
would push agents to invent a source, which is worse.

### Approvals and audit

- An approval is bound to a SHA-256 of exactly what was approved, and the hash
  is re-verified immediately before the side effect.
- An agent is never an acceptable approver; the requester is never the approver
  of their own request. Both are structural.
- An unanswered approval expires and fails closed.
- The audit timeline is ordered by a monotonic sequence, not a wall clock.

### The API — 97 endpoints, running

```
/health                  liveness, dependency-free
/ready                   readiness; fails if the role is not least-privilege
/metrics                 Prometheus text
/api/v1/system/secrets   presence only, never a value
```

Verified live over HTTP: task creation with idempotent replay, duplicate-goal
refusal (`CONFLICT`, naming the existing task), capability discovery through the
GIN-indexed array operator, and the agent registry.

### Temporal

`TaskExecutionWorkflow` and `DelegationWorkflow` are registered and their input
models round-trip. The approval gate has a bounded wait, so an expired approval
becomes a decision rather than a task that waits forever.

---

## What partially works

Stated plainly, because a status document that claims everything is finished is
worse than none.

| Area | Works | Does not yet |
|---|---|---|
| **PydanticAI runtime** | **The adapter exists and works.** `agent_runtime/pydanticai_agent.py` implements PydanticAI's `Model` protocol on top of *our* `ModelGateway`, so every call still passes the privacy filter, the capability check, the budget pre-flight and the recorded fallback. 16 unit tests cover the message translation both ways, a live OpenRouter call through it returned a typed `AgentResult`, and the tool loop is now driven from inside PydanticAI: the live model calls `write_report` and `delegate_to_agent` for real. | The model is given a 2048-token allowance per turn and a ceiling of 24 model calls and 32 tool calls (`BudgetEnvelope.max_requests` / `max_tool_calls`). Both are enforced and tested; neither is tuned against a real workload yet. |
| **Delegation** | `delegate_to_agent` is a registered tool behind the same gates as any other, `DelegationExecutor` turns proposals into real child tasks in the same transaction as the `delegations` row, and cycle, depth, fan-out, budget and duplicate-work checks all refuse with a recorded reason. `tests/integration/test_delegation_creates_work.py` asserts an accepted delegation leaves both a `delegations` row and a child task, and that the parent does not report `completed` while children are open. | Whether a *second* model decomposes is untested. One model on one goal is a demonstration, not a rate. |
| **How long delegation was broken** | `ToolGateway.invoke` accepted `context_attributes` and never passed them on, so the delegation callable never reached the tool and every call returned `DELEGATION_UNAVAILABLE`. Behind it, two `AttributeError`s reaching for a `session` no repository exposes. Four wrong diagnoses were built on top. F42–F45. | Nothing. It is fixed and covered. Recorded because the *shape* is the point: a dropped keyword argument looks exactly like a broken feature, an optimistic `getattr` default turns a broken trace into a confident answer, and a gate that reads an in-memory summary of a distributed process will eventually be wrong about a case the summary does not cover. |
| **The demonstration, run for real** | `demo_real_run.py` on the seeded company, live model: an accepted delegation from the Executive to a real department agent, a child task created and executed, the parent refusing to report itself complete while its child was open, and a repeat request for the same work refused rather than duplicated. Three case studies run: purchase requisition, headcount request, training plan. | One level of a tree, not a tree. Each child completes rather than delegating further, so nothing has been shown about a *second* level of the hierarchy. |
| **Repetition counting** | `domain/procedure.py` fingerprints a run by `(task_type, ordered tools with consecutive repeats collapsed, argument *keys*, refusal per step)`, distinct from the task fingerprint that hashes the goal's wording. `application/procedures.py` reads the trace, stores it on the task (migration 0004) and counts prior occurrences per `(organization, agent)`. The gate opens inline in `_finish`, not on a schedule, so it is never a day stale. 14 unit and 11 integration tests. | **It could not have opened.** The fingerprint hashed repetition counts, so two runs of the same report that searched 18 times and 21 times were different procedures — and the gate counts identical fingerprints. Consecutive repeats now collapse, `FINGERPRINT_VERSION` is `proc-v2`, and the three real sequences from the live run are pinned in `tests/unit/test_procedure_shape.py`. F57. |
| **Proposed changes** | `domain/learning.py` types a proposal as a frozen object that must cite its evidence, state what would disprove it, and say which of `patch`/`create`/`memory` it is. `falsifier` is a validator, not a field: blank, `"n/a"`, `"tbd"` and `"because"` are refused, because a placeholder in a table of proposals looks answered to a reviewer skimming it. `occurrences` may not exceed the cited evidence. `application/learning.py` runs gates 1–3 and reports *every* failure. 36 unit and 14 integration tests. | Superseded by the rows below: a generator, an approval packet, and a hash-bound approval now exist, so the gates can refuse *and* something can say yes. |
| **Danger scanning** | `domain/scan.py` scans every field a proposal carries — prompt injection, credential exfiltration, hidden text — against three forms of the text, because `ignore-previous-instructions`, `i g n o r e previous instructions` and `postgres://u:p@h` each need a different one. The scan is **unconditional**: it was a parameter that refused on `None`, which was still passable by a caller passing a permissive lambda. 44 unit and 8 integration tests. | Pattern coverage is not the same as safety. Unicode tag blocks and bidi overrides are detected by codepoint inspection; a novel obfuscation of a *word-level* instruction is not, and the false-positive rate on real procedure text is unmeasured. |
| **Proposal generation** | `application/proposal_generation.py` asks a *second* model for one change, with the trace inside a labelled `<runs>` delimiter because a trace row is something a model wrote under instructions that may not have been its own. The reply is parsed into a `ProcedureProposal` and validated; a malformed one is refused, not repaired, and **no proposal is a normal outcome** rather than a failure. `occurrences` comes from the evidence, never from the reply. | **Measured, and it works.** A real second model (`nemotron-3-ultra-550b:free`) read the trace of four failed runs and proposed a real fix: the platform offers `safe_web_search` for "draft a one-line status update", which needs no external information. The proposal carried a diff, a falsifier, and a stated limit of its own confidence. |
| **The approval packet** | `application/approval_packet.py` assembles what a human reads: the change as a `-`/`+` diff, the runs with their tool sequences, the counter-examples, the falsifier, and a list of warnings about what is *missing* from the packet. `application/proposal_approval.py` binds an approval to `packet_hash()` and re-verifies on the way out, so a packet edited after approval cannot be published. | The hash is verified by `load_approved`, and nothing calls it in production, because nothing publishes yet. The live demo exercises both: it approves a real packet and then edits it, and the platform refuses. |
| **The review job** | `application/procedure_review.py`: bounded (`max_agents`, `max_procedures_per_agent`, both small by default), idempotent (a procedure already proposed about is skipped), and it records every skip — a sweep that silently skips things looks exactly like one that found nothing. `TraceEvidenceLookup` reads the runs and names the subject model by majority, refusing on a tie rather than flipping a coin. | Bounded and idempotent are tested. Whether a nightly schedule is right is unmeasured, because nothing is being learned often enough to tell nightly from weekly. |
| **The proposer profile** | The one profile in the catalogue with **no deterministic candidate and no fallback profile**. A canned proposal is a lesson no model wrote, stamped with a model that did not produce it, hashed and put in front of a human as a real suggestion. Enforced by `tests/unit/test_proposer_profile.py`, not by a comment. | 550B and 120B free models, both verified to make a valid tool call. Free tiers rate-limit, and a `429` on a proposer simply means no proposal that cycle. |
| **Quarantine** | `application/quarantine.py` records a verdict and **never the payload** — the proposed text is not written to the database at all, which is the deliberate difference from Hermes, where a quarantined skill stays on disk and is merely hidden from the index. The cited runs are kept, so the text is reconstructible by a human who decides to. Migration 0005, RLS enforced. | No operator interface. `QuarantineSink.recent` exists and is the only read path, and nothing calls it. |
| **The operator view** | `GET /api/v1/ui` serves a single page from the API's own origin. It reads the event stream with `fetch` and a hand-rolled SSE parser rather than `EventSource`, because `EventSource` cannot set an `Authorization` header and the two alternatives — token in the URL, token in a cookie — are both worse; the token stays in the header and in `sessionStorage`, and a test asserts it never appears in the HTML or in a query string. iOS/Apple styling: one set of design tokens for light and dark, translucent materials, spring easing, and a sheet that rises on a phone and centres on a desktop. The delegation tree is drawn with a rail, so a delegation reads as a delegation and not as indentation. **Pause** stops *applying* frames without dropping the connection, because dropping it would advance the consumer offsets and lose events rather than defer them. | **Nobody has looked at it.** No browser was attached to the session that built it, so the CSS, the layout and the animations are unverified — only the frames on the wire are. Treat the styling as a claim until a person has seen it. |
| **The event stream** | `GET /api/v1/stream` is SSE over the existing event log. It backfills **300** events on connect, so opening the page after the interesting part still shows it, then tails live. 300 rather than 60 because the tenant holds 533 events and the most recent delegation sits 208 back: a 60-event window showed a page of task churn and no delegation at all, so the flow view — the reason the page exists — looked broken when it was showing the wrong slice. The page carries no copy of that number, so it cannot drift from the server's. Frames carry an `id` so a reconnect resumes. One in-process pump feeds every subscriber through a bounded fan-out that drops the **oldest** frame when a tab falls behind — an unbounded per-subscriber queue is a memory leak with a browser attached, and on a live view the newest frame is the one worth having. Frames are enriched with names and titles in the projection, never by rewriting `data`, because other consumers read that payload. | It polls the durable log every 2s rather than consuming NATS. That was chosen because it cannot lose an event and cannot ack-then-fail-to-render, and it is right until the event rate makes 2s visibly late. `EventFanout.close()` is tested; a real browser reconnecting under load is not. |
| **The workflow path** | `POST /api/v1/tasks` starts a durable Temporal workflow after the commit and reports the outcome in the response: `{"workflow": {"started": true, "workflow_id": "task:tsk_..."}}`. A dispatch failure is *not* an exception — the task is committed and legitimate, so rolling it back would destroy the caller's work; the row stays and the reason travels with it. A worker polls `ao-workflows` on both the workflow and activity queues. **The worker must be started with `AO_MODEL_PROVIDER_DEFAULT=pydantic_ai`**: without it `build_runtime` returns `ScriptedRuntime`, and a coordination task completes with *zero* tool calls and is refused for not delegating — a correct refusal about a run where no model was consulted. F69. The tool layer has a fourth defect: `_as_tool` builds a `Tool(max_retries=2)` and then hands that object to `FunctionToolset.add_tool`, which takes plain callables and constructs its own `Tool` with `max_retries=0` — so the retry budget, the name and the description are all discarded, and a real model dies on its first tool call with `Tool 'invoke name="delegate_to_agent' exceeded max retries count of 0`. **Not fixed, deliberately**: this is the layer four earlier defects came from. F70 — and its first diagnosis was wrong: the wiring was never broken, the *model* emitted a malformed tool name, and PydanticAI reported it as retry exhaustion. The platform now classifies that case explicitly and names the tools the agent was offered, so the message points at the model rather than at a retry budget that is correct. | **This path had never executed before today**, and it took four separate defects to get it running with a real model: the namespace did not exist (F67), the worker would not start (F67), the activity payload arrived as a raw dict (F67), and the runtime defaulted to scripted (F69). Every prior demonstration drove `TaskExecutionService` directly, so the *execution* path was proven and the *orchestration* path was not. |
| **Case studies** | Four real goals, each run against the live model with a real seeded company: purchase requisition, headcount request, training plan, quarterly review. Between them they found fourteen defects that 736 unit and integration tests had not — a dropped keyword argument, a tool that reported reading while reading nothing, a retry knob that governed validation and execution at once, and three separate ways of disturbing the session a piece of code was handed. `SELF_IMPROVEMENT.md` §10 has each case and what it taught. | Four cases is not a sample. The fifth is what proved the loop could work at all: three real runs of one procedure, and the repetition gate opened for the first time — but only after F57, because until then it *could not* have opened. |
| **Assumed, not read** | Five defects, one question: *what does this code do to the session, the enum and the data it was handed?* A write inside a write (F50), a failure handler that could not report its own failure (F53), a read that autoflushed (F54), two guards that could not fail (F55), and then F56: a method carrying a fifteen-line docstring explaining it must not flush, calling the method that does. Also two enums read from memory (F57, F58) — the trace vocabulary is `success`/`failure`/`blocked`, and one reader checked for `ok`. All five killed live runs; 951 tests passed throughout, because in two cases the suite agreed with the comment. | No general rule is encoded in code. The one that works is in the tests: assert the *mechanism* — no flush happened, the value is in the known set, the guard can actually fire — not the result it was supposed to produce. |
| **The tool-call trace** | 69 tool calls across 4 tools, with outcomes and reasons: `write_report` 27 success, `delegate_to_agent` 24 success / 5 refused, `safe_web_search` 6 success, `internal_database_query` 7 refused. It is already answering questions it was built to answer — the 5 delegation refusals all read *"an equivalent task is already active"*, which is F46's fix working on live traffic, and the 7 query refusals are the tenant predicate being enforced. | The trace is now read programmatically: `procedure_fingerprint` over it, and `TraceEvidenceLookup` which turns a fingerprint into the runs, their tool sequences and the model that produced them. |
| **Tools fail soft** | A refused tool is a result the model reads, never a raised error. The writer and reader catch `OSError`; the bridge serialises `error_kind`/`error_message`, the real field names. `write_report` returning "could not write" and `document_reader` returning "could not read" are ordinary outcomes, not dead runs. F49. | Whether the artifact root is healthy is untested. The refusals arrived because it was unwritable in that run, and the reason it was unwritable was never established. |
| **The audit trail** | Every state change is recorded, and every tool call within it. `AuditService.add` stages a row without flushing, because a tool handler is already inside the task's flush — flushing again raised `Session is already flushing`, rolled the transaction back, and made every later statement fail with a *different* error. F50. | The audit trail records tool calls but nothing reads them yet. The next consumer is the procedure fingerprint in `SELF_IMPROVEMENT.md` §9 step 1. |
| **Failure reporting** | A failure is reported with its own cause. When the transaction is already gone, the write is attempted and the failure falls back to a log-and-outcome path, because the *second* exception used to replace the first in the traceback. F53, F55. | A task whose transaction was lost keeps no `failed` row. That is recorded as `task.failed_transaction_lost` at `error` level, but there is no reconciliation pass that would find those tasks again. |
| **`internal_database_query`** | Executes for real, through a reader bound to the caller's transaction so RLS applies exactly as it does everywhere else, and inside `no_autoflush` so a read cannot trigger a write. `QUERY_UNAVAILABLE` when no reader is configured, rather than an empty result the model would read as "no data". Its description names the real tables, and the refusal names the mistake the model actually made. F48, F54. | Never yet returned a row on live traffic. Every call so far has been refused: the model reaches for `information_schema` and a singular `organization` because it was never shown a schema. Better messages are not a schema. |
| **Tool argument validation** | A model that omits a required argument is re-asked once, up to two times. Validation happens before the handler, so there is no side effect to double. `max_retries` was 0, which was right for the *handler* and wrong for validation — one knob doing two jobs, and three live runs died on it. F52. | Two retries is a guess. Nothing measures how often a model needs a third. |
| **The delegate roster** | Built from the organisation tree, not from the model's imagination, and rendered into the system prompt. Tested: every name offered is an agent that exists. | It exists because the first live run delegated to a department called `Procurement`, which this company does not have. A platform concern, not a prompt concern. |
| **Turn-loop bounds** | `UsageLimits(request_limit, tool_calls_limit)` from the envelope, reported as `budget_exhausted` naming the knob. Found by a live run that spent 23 turns on one goal inside every per-call limit. `FAILED_APPROACHES.md` F39. | The default 24 requests is a guess. Nothing yet measures how many turns a real task needs. |
| **The turn's token allowance** | 8192, and it is the number that makes delegation happen. Measured on the real goal: at 2048 the model calls `write_report` and does the work itself; at 8192 it calls `delegate_to_agent`. Pinned in `tests/unit/test_turn_allowance.py`. | It was originally `max_tokens / 8`, then over-corrected to a flat 2048, and both were wrong. F36. There is no principled derivation for it yet — only a measurement, and one goal is not a distribution. |
| **Temporal end-to-end** | Workflow and activity definitions register; the client wrapper starts, cancels, signals and queries | The worker has never been run against a live Temporal server with a real task. The acceptance test for scenario 8 asserts the *durability property* (the task row and its history survive) without a workflow engine, and says so in its docstring. |
| **A2A** | **Built and proven against a real process.** `a2a/` has card validation, a typed JSON-RPC protocol, a bounded client and a gateway. Acceptance scenario 2 runs `examples/a2a_remote_agent.py` as a **separate process** over a socket, registers it, calls it, and asserts on the answer. 13 tests, including card-version refusal, unknown-capability refusal, an unverified agent refusing to be called, a cross-host redirect refused, and an oversized reply refused before parsing. | A remote agent's reply is treated as untrusted text, not as a typed result. `message/stream` and push notifications are not implemented, and the card does not advertise them. |
| **Outbox relay** | Commits with the state change; `FOR UPDATE SKIP LOCKED` claiming; bounded retries; quarantine. **Tenant-scoped**: it enumerates tenants from `organizations` and works inside one at a time, because a single unbound session matches zero rows under RLS. | Not yet run as a long-lived loop under real load. |
| **Durable consumers** | Dedup, redelivery, dead-letter, terminal-failure path. Dedup is tenant-bound from the event envelope rather than from ambient state. | The projection consumer that writes `messages` is not implemented. The `messages` table exists and is documented as a projection, but nothing writes it. |
| **Subagents** | The bounds (depth, fan-out, tokens, cost, runtime) are enforced and tested | The `subagent_runs` table and `SubagentRun` state machine exist; the spawn *action* is not wired into the runtime loop. |
| **Tool-call trace** | Every tool call writes a `tool.invoke` audit row: tool name, redacted arguments, outcome, real `execution_id`, ordered by the audit sequence and queryable through `AuditService`. Found by asking the platform what an agent had done and finding that it could not. F41. | Steps 1 and 2 of `SELF_IMPROVEMENT.md` §9 were blocked on exactly this and are now unblocked. Nothing reads the trace yet — there is no procedure fingerprint over it, no repeat counting, and no view. |
| **Cost ledger** | `model_usage` is written, routing decision included, so a fallback is stored as a fallback. Three integration tests. | Was read by the API and written by nobody until a live call showed the count at zero. `FAILED_APPROACHES.md` F31. |
| **Rate limiting** | Per-instance sliding window, keyed on token or IP | Per-instance, not per-deployment: behind N instances the effective limit is N×. Documented in `security/rate_limit.py` rather than hidden. |
| **Encryption key** | `ENCRYPTION_KEY` is generated and validated | **Unused.** No column is encrypted. The setting exists because the brief requires it, and an unused secret that looks like a feature is worse than an absent one. |
| **`model_profiles.privacy_rules`** | Declared, defaulted, indexed | **Never read or written.** The per-provider classification ceilings live in the code catalogue, so the database and the gateway can disagree about which provider may see which data. Wire it up or drop it. |

## What is broken

| Thing | How it was found | State |
|---|---|---|
| **The organisation does not finish a real goal unattended.** Measured 2026-10-02 on both scenarios the owner asked for: `supplier-tender` gets the chief to delegate 16 times and then ends `failed` `budget_error` with 21 duplicate refusals and 8 fan-out refusals; `offer-approval` ends `failed` `budget_error` having never delegated. Four real defects were found and fixed getting here — F249, F250, F251, F253 — and the first hop now works on a real model. What remains is that the chief spends its 48-request budget re-asking, and no department completes. | Running the two scenarios on a real free model | **NOT FIXED**, and the number that would prove it either way has not been taken: whether 48 requests is too few, or the model cannot stop re-asking, is a measurement this project has not made. |
| **Every log line in the process was being dropped.** A renderer ran before `wrap_for_formatter`, so the formatter called `.copy()` on a `str`, and `logging` swallowed the `AttributeError` and moved on. | Reading a log file that contained nothing but `--- Logging error ---`. `ruff` and `mypy` were both clean. | **Fixed.** F35. `tests/unit/test_logging_pipeline.py`, 6 tests, all of which fail on the old chain. |
| **A free model call can take six minutes.** 60s timeout × 2 retries × 2 profile candidates, and the `primary` profile's second candidate is the *deterministic* provider — so a timeout silently swaps in a canned answer that looks like a real one. | A live run that appeared to hang. | **Partly fixed.** `demo_real_run.py` now prints the model that actually served each call. The retry multiplication is still untuned, and a fallback that produces plausible output is still a hazard. |

## What is obsolete or rejected

| Thing | Why |
|---|---|
| A `docker-compose.yml` | No container runtime on this machine. The brief asked for one; it could not be run, so it could not be verified, and shipping an unrun compose file would be exactly the "fake production path" the brief forbids. `docs/DEPLOYMENT.md` states the deviation. |
| `pmo_project_procore`'s migration ranking | See `REUSABLE_COMPONENTS.md`. |
| Any O-Nexus code | There is none to reuse. |
| The leaked `OPENROUTER_API_KEY` and the Telegram id | Reported to their owners; never copied. `FAILED_APPROACHES.md` F16, F17. |

## The delegation path, proven

`demo_real_run.py`, live model, seeded company, `dots-studio/dots-3-note-preview:free`:

```
status                     : completed
proposals                  : [('tool_call', 'delegate_to_agent')]
model used                 : openrouter/dots-studio/dots-3-note-preview:free
delegations from this goal : 1
  -> Back Office Agent     [accepted]  'Lên đơn mua hàng cho phòng Marketing: 5 chiếc laptop...'
  L1 Back Office Agent     completed  'Tạo đơn mua hàng cho phòng Marketing thành công...'
```

The Executive received a goal, named a real agent from its own organisation, the
platform created a child task in the same transaction, and a repeat request for
the same work was refused rather than duplicated. That is the whole chain, end to
end, on the real model.

**One caveat on "rather than duplicated", measured and not fixed.** Exact and
*concurrent* duplicates are refused reliably (F243). Near-duplicates are not: one
measured run accepted five delegations from one parent, three of which share their
first 56 characters and differ only in a marker the model appended — "for run 3",
"(run 3217)". `intent_fingerprint` hashes the objective text, so the marker makes each
one distinct. No fix is proposed, because the heuristic that catches those five — strip
digits before hashing — also merges "approve invoice 1" with "approve invoice 2", and a
Finance department that merges those is worse than one that repeats itself. F244.

It took eleven defects to get here, and the reason is in `FAILED_APPROACHES.md`:
one dropped keyword argument, one optimistic `getattr` default, one gate reading an
in-memory summary, and one counter whose scope did not match its label. The pattern
is consistent enough to be worth stating — **every one of them was diagnosed by
reading a single artefact without asking whether that artefact could see the thing
being diagnosed.**


## What must be built next

In priority order, with the reason each is first.

1. **Measure decomposition across models and goals.** The mechanism is proven end
   to end: the model calls `delegate_to_agent`, the roster is real, the executor
   creates real child tasks, seven checks refuse with recorded reasons, and the
   platform fails a coordination task that never delegated. What is *not* proven is
   that it generalises — one model on one goal is a demonstration, not a rate. The
   second evaluation case (`headcount-request`) has never been run, and no second
   model has been tried.
2. **A live Temporal run** with the worker, proving the approval round trip end
   to end rather than as two halves that each work.
3. **The messages projection consumer**, so the operator-facing conversation
   view is actually written from events.
4. **The subagent spawn action**, so `subagent_runs` is used rather than merely
   defined.
5. **`model_profiles.privacy_rules`** is declared and never read or written. The
   per-provider classification ceilings the column was meant for live in the
   code catalogue instead, so the database and the gateway can disagree about
   which provider may see which data. Either wire it up or drop it; a column
   that looks like a control and is not is worse than no column.
6. **Tune the free-model path**: one call can take six minutes, and the
   `primary` profile falls back to a deterministic provider, so a timeout
   produces a plausible-looking answer from a model that was never asked. The
   demo now reports which model served each call; the profile should stop
   treating "the provider is slow" and "the model is unavailable" as the same
   event.
7. ~~**The approval packet**~~ — done. The diff, the evidence with run ids, the
   counter-examples, and an approval bound to a hash of that exact packet,
   re-verified on the way out. `SELF_IMPROVEMENT.md` §9 step 6.
8. **The loop can change itself and cannot yet undo the change.** This is the
   most important gap and it is not a missing feature so much as a missing half.
   An approved packet is approved and hash-verified, and then nothing applies it;
   and a proposal states what would show it was wrong, but nothing watches for
   that. A system that can rewrite its own procedures and cannot revert them is
   not a self-improvement loop, it is a ratchet. Hermes' `refine` step and
   "a human edit is never stomped" both depend on this, and there is no
   human-editable procedure store for the second to protect yet. Build the
   revert before the publish, or the publish will look like progress while
   removing the only thing that makes it safe.
9. **A provider content filter is not a platform policy decision.** It is now
   classified `POLICY_DENIED` with a short message, which is an improvement, but
   this platform has its own policy engine and its own audit trail. A third-party
   filter firing on a legitimate HR document — which is what happened — should not
   appear in the same column as a platform denial, because the two have different
   authority and different remedies. Decide whether the provider's refusal is
   evidence, a hint, or nothing, and record which.
10. **The free model does not converge on real work.** Three consecutive live runs
   of "compile a quarterly cost report from three departments" ended in the platform
   stopping it at the 32-call tool budget, having searched 33 times. That is the
   platform working — a turn budget that does not fire is not a budget — but it means
   the demo's default task cannot complete, and a demonstration that always fails
   teaches nothing about the thing after it. Either pick a task the model can finish,
   or raise the ceiling and accept the cost, or report the refusal as the finding.
   What must not happen is tuning the task until the demo goes green without
   recording that it used to fail and why.
11. **The operator view exists but has never been seen by a person.** `GET /api/v1/ui`
   is served from the API's own origin, streams the event log over SSE, and renders the
   task/delegation tree from it. It was built and verified by reading frames off the
   wire — no browser was attached to this session, so nobody has actually *looked* at
   it. Treat the CSS as unverified.
12. **A failed run leaves no durable record of why it failed** (F64). `task.execute`
   is audited as `success` *before* the run and never corrected, so a failed run
   leaves an audit trail saying it succeeded; the task row keeps a category but not
   the reason; and the human-readable reason goes only to a log line. The consequence
   is visible in the platform's own first proposal, which said *"the runs don't
   contain error details, so I cannot specify the exact fix."* It was being asked to
   diagnose from symptoms. Deliberately not fixed here — this is the audit write path,
   where F50, F53, F54 and F56 all happened, and each of those was a small confident
   change to how a row is written mid-run.
13. **M14: benchmark harness** and **M15: container packaging, Kubernetes
   manifests, licence matrix, SBOM** — deferred by decision, not forgotten.

## Quality gates

| Gate | Command | Result |
|---|---|---|
| Unit + integration | `make test` | **869 pass** |
| End-to-end | `make test-e2e` | **34 pass**, no skips, gated by `preflight-e2e` |
| Lint | `make lint` | clean (669 findings fixed) |
| Types | `make typecheck` | clean (126 errors fixed) |
| Live readiness | `GET /ready` | `ready`, `least_privilege_role: true`, 42/45 RLS |

Both linters ran *after* the suite was green, and found thirteen defects the
the 687 tests did not cover — including a task-failure path that raised
`AttributeError` instead of reporting the failure, a JetStream consumer that
consumed nothing while reporting itself healthy, and eleven admin endpoints
returning 500 where 403 was correct. All are written up in
`FAILED_APPROACHES.md` F18–F30.

Six of the thirteen shared one shape: the code returned a *plausible wrong
answer* rather than crashing. A relay that reports `published 0`, a health
check that reports ready with no stream behind it, a dedup store that cannot
write — none of them raise, and none of them are caught by a test that asserts
on a summary field instead of on the effect.

## Verified on 2026-09-30 (second pass)

* `make lint`, `make typecheck`: clean. `make test`: **2849 passed, 8 skipped,
  0 failed** in 17m38s. Note that `make test` includes `tests/e2e`; the narrower
  `pytest tests/unit tests/integration` reports 2819 and would not have shown the
  five acceptance scenarios that referenced a removed department (F216).
* `make page` verification: **115 checks, 0 failed** against the live API.
* `scripts/run_real_scenarios.py`: **7/7** finished at the owning department.
* **`POST /tasks` works from any tenant.** It did not: with authentication off the
  human stand-in's id has a `users` row in one organisation only, so every task
  created from the Run button failed a foreign key (F215). The button was broken
  and nothing in the suite pressed it.
* The "Start a task" card offers the seven scenarios from the server, fills the
  goal from the chosen one, hands the work to the chief so the tiers below do it,
  and sends `owner_agent_id` — which the agent selector had never sent, making the
  choice on screen do nothing.

## Verified on 2026-09-30 (third pass: the office as a manager)

* `make lint`, `make typecheck`: clean.
* **The organisation runs a goal by itself.** `application/pipeline.py` picks the
  deepest ready task, runs it, has the office review whatever just finished, and
  settles upward — a goal handed to the chief reaches `completed` through all
  three tiers with nobody pressing Run. Proven end to end, and 6 tests including
  one where a department deliberately produces placeholders so the rejection and
  the retry happen in the same pass.
* **The office is a manager.** `domain/review.py` decides, without a model, whether
  an output is an answer: promised keys present, no placeholders, substantive. On a
  failure `application/work_review.py` orders a **real second task** to the same
  department carrying the findings as its brief; after two attempts it escalates
  instead of asking again. 26 unit + 8 integration tests.
* **A2A is reachable.** `call_a2a_agent` and `A2AGateway.resolve`, with 8 tests
  that spawn a real peer process and call it over a socket. The reasoning — why
  delegation is a row, and where the line is — is in `ARCHITECTURE_DECISIONS.md` §2.
* **Three platform bugs the loop exposed**, all fixed: a coordination task
  reported `completed` while its children were open; an office could never
  complete, so the driver re-ran it to the execution bound; and
  `tasks.root_task_id` holds the *immediate parent*, so "everything under this
  task" found nothing two tiers down. F218–F220.

### Blocked, and it is external

**No free model is reachable today.** Every free model on this OpenRouter key
returns `429 free-models-per-day-high-balance` — an account-level *daily* cap,
because the account holds a balance. Measured directly against the API on all
candidates, not inferred from a log line. The pipeline fails with a clear reason
and names every candidate it tried, which is the correct behaviour and not a
substitute for a working model. It resets tomorrow.

### Damaged, and rebuilt

`docs/ARCHITECTURE_DECISIONS.md` was **overwritten by mistake** by an agent that had
not read it first, and this repository is not under version control, so it could
not be recovered. It has been reconstructed from the dossier, `ASSUMPTIONS.md`,
`LEGACY_SYSTEMS_REVIEW.md` and this file, with every claim checked against the code
and the check named. Decisions that lived only in its prose are gone and are not
invented. The file says so at the top.

## Verified on 2026-09-30 (fourth pass: the playbook)

* **All 28 playbook SOPs are executable work.** `application/playbook.py` carries
  each procedure's step chain, its control point, its owning department and the
  keys a finished answer must contain. 20 tests prove the catalogue is whole
  against the seeded governance spine, that every department it names exists, and
  that a stretched mapping says why.
* **They run.** `scripts/run_sop.py` turns a procedure into a task and hands it to
  the pipeline. 8 tests run SOPs from all three offices end to end, assert the
  owning department produced the declared keys, and assert a department answering
  with placeholders gets **sent back and retried**.
* **The dossier's four AI-forbidden zones are contracts, not prose.** Hiring,
  payroll, Gate decisions and stop-work orders carry `forbidden_zone`, their
  contracts ask for preparation rather than a decision, the zone travels into the
  task's input, and a test fails if the contract and the restriction disagree.
* **Two SOPs have no home and say so.** `ONX-BO-IT-SOP-007` and
  `ONX-PMO-KNW-SOP-006` are refused by the runner rather than assigned to a
  department that does not do the work. See `ARCHITECTURE_DECISIONS.md` §1 (D8).
* **A fifth platform bug, found by running the catalogue:** `owning_office` was
  not in `ROUTING_INPUT_KEYS`, so the office survived one hop and was gone by the
  second — a procurement SOP and a sales SOP both finished at Finance, having asked
  for a buyer and a salesperson, and reported success. F223.

### Page verification, and the four things it still wants

`make page` now opens a tenant that **has the current shape**, chosen structurally
rather than by age. It used to open the *oldest* organisation on the machine — the
one seeded before the three-tier restructure — so the page was verified against a
snapshot of how the product used to be, and reported `all six departments are
present — 8`. True, and about a tenant that no longer describes it.

`scripts/first_org.py` enumerates from the `organizations` directory, binds one
candidate, and accepts the first whose units are 3 offices and 6 departments. It
keeps the application role: `organizations` is readable by `ao_app`, and
`organizational_units` is not until a tenant is bound, which is why this cannot be
a single join. `scripts/ingest_construction.py` and `scripts/seed_process_spine.py`
both take `--org` for the same reason — the corpus was landing in the oldest tenant
while the page showed an empty portfolio.

**101 checks pass. Four do not**, and all four want *content*, not correctness:

* the document register is empty — no corpus has been published to this tenant;
* therefore no document row to open;
* this tenant has no failed work, so the retry button has nothing to retry;
* the Gates view wants a gate session with a person on it, and the criteria are
  seeded (6 gates, 45 criteria) but no session is.

None of them is a defect in the pipeline, the offices, or the playbook. They are a
tenant that has the organisation and none of the paperwork, and they are named
rather than worked around.

## Verified on 2026-10-01 (fifth pass: the queue tells the truth, and a real model)

* **The approval queue no longer offers a dead action.** A person clicked
  **Approve** on two `hr.open_headcount` requests and got
  `Could not approve: approval apr_… expired at 2026-09-29T15:46:06` — the expiry
  was visible *only* in the error. Two faults: `PendingApproval` carried no
  `expires_at`, so the queue could not know; and `ApprovalService.expire_stale`
  had **never been called by anything** — its docstring says "Run by a scheduled
  task" and there is no scheduled task, so expired rows stayed `pending` in the
  table too. Now the row carries `expires_at` and a `task_title`, the page renders
  no buttons for an expired row, and `sweep_stranded.py` (run by `make page` on
  every start) converges the statuses. 7 tests, one of which reads the sweep and
  the Makefile to assert the call and the target both exist.
* **The two rows were never duplicates.** They were two different tasks from two
  runs of the hiring stage, and the row carried no task title, so the page drew
  two identical lines. The title is now shown.
* **A real free model runs the whole chain**, and found three bugs no test had:
  an empty output was **accepted three times** (the contract was a description
  map, so nothing was promised, and a review with nothing promised cannot fail);
  an empty output passed review with no contract at all; and `retries=0` disabled
  pydantic-ai's **output** repair while the comment claimed the gateway already
  owned retry policy — it owns *transport*. F225, F226.
* The daily free-model cap reset; `qwen/qwen3.8-27b:free` was still limited, and
  the gateway fell through to a working candidate — which is the fallback list
  earning its place.

## Verified on 2026-10-01 (sixth pass: who the contract binds)

* **A coordinator is judged on its coordination, not on the work it forwarded.**
  Found on the third real-model run: the chief was failed for not producing the
  *department's* `verdicts`, because the contract propagates down the chain and
  was then enforced at the top of it. A task that delegated is now judged on
  having delegated and having everything it delegated finished; a task that
  delegated nothing is still held to the contract, and an empty answer still
  fails. Both directions tested. F227.
* Gate: **2914 passed, 3 skipped, 0 failed**; `make lint` and `make typecheck`
  clean.

### Five faults a real model found that tests did not

Every one of these was invisible to 30 passing tests, and each is now a test named
after the failure rather than after the function:

1. a contract sent as a *description* map promised nothing, so nothing could fail;
2. `{}` passed review when no keys were promised -- a checker with nothing to check
   returned "pass";
3. `retries=0` disabled pydantic-ai's **output** repair while the comment claimed
   the gateway already owned retry policy -- it owns **transport**;
4. the chief was failed for not doing the department's work;
5. the office rejected the chief for the same reason, and the root never settled.

That ratio is the argument for running real work rather than trusting a green suite.

## Verified on 2026-10-01 (seventh pass: it works, end to end, on a real model)

```
d0  Executive Agent   running → settled
d1  Back Office Agent running → settled
d2  Finance Agent     completed  [accepted]
d2  Finance Agent     completed  [accepted]

root -> completed | executions 4 | review accepted 3 | rerun 0 | escalated 0
```

The chief routed (its goal carried no data, so answering alone was impossible), the
office decomposed the work into two tasks and delegated both to Finance, Finance
analysed the real brief, the office reviewed and accepted both, and it settled
upward twice to the root. All on a **free OpenRouter model**, no scripting.

And the answer is correct, which is the part that matters:

| Khoản chi | Quy định | Kết luận của agent |
|---|---|---|
| Minh Châu 3.600.000 | dưới 5tr, không cần duyệt | **duyệt** |
| Văn phòng pháp luật 32.000.000 | trên 20tr, cần GĐH | **duyệt có điều kiện** |
| Kiên Phát 18.500.000 | 5–20tr, **thiếu hợp đồng thuê kho** | **từ chối** |

All three right, including the one that turns on a *missing document*, and the
arithmetic checks out — it reported "thấp hơn ngưỡng 1.400.000 VND".

**Gate: 2926 passed, 3 skipped, 0 failed.** `make lint` and `make typecheck` clean.

### Seven faults that only running real work found

None of these had a failing test. All are recorded as F225–F228 with the
measurement, and each has a test named after the failure:

1. a contract sent as a *description* map promised nothing, so nothing could fail;
2. `{}` passed review when no keys were promised;
3. `retries=0` disabled pydantic-ai's **output** repair while the comment claimed
   the gateway owned retry — it owns **transport**;
4. the chief failed for not doing the department's work;
5. the office rejected the chief for the same reason, so the root never settled;
6. the retry carried `work_key`/`attempt`/findings and **not the brief**, so every
   retry was guaranteed to fail for the reason the first attempt did;
7. **`expected_output_schema` was nowhere in the runtime, and `AgentResult` was
   built with no `output=`.** Every declared contract was unsatisfiable by a real
   model. `ScriptedRuntime` sets `output`, which is why 2914 tests passed.

**The lesson worth keeping:** a green suite covering a function is not evidence
about the path a real producer takes. Every one of these lived in the gap between
what the tests exercised and what the model did.

## Verified on 2026-10-01 (eighth pass: somebody else clones it)

The previous seven passes all ran inside the directory the project was built in.
This one did not: clone the repository to an empty directory and follow the
README, which is the only reading of it that a new person will ever have.

```
$ git clone git@github.com:TungLinhh/AI_Orchestrator_Construction.git
$ cd AI_Orchestrator_Construction
$ AO_POSTGRES_PORT=55444 make setup
...
upgrade 0028 -> 0029, Record how many calls a run actually made, so the ceiling
                stops being a guess.
seeded organization: autonomous-demo-company (org_01m3tp2a44ze3j02q30bmpm7fc)
spine for org_01m3tp2a44ze3j02q30bmpm7fc: {'gates': 6, 'criteria': 45, 'sops': 28,
  'forbidden_zones': 6, 'doa': 8, 'sop_steps': 5, 'raci': 14}
  setup complete. Next:  make dev
```

Clean clone, no `.secrets/`, no `.venv/`, no `.devdata/`. All 29 migrations, the
organisation and the governance spine built from nothing, on a port that is not
the one this machine normally uses.

**It found a defect nothing else had.** The seed's own log line said
`{"agents": 7, "units": 7}` over a tenant holding ten of each. The seed was
correct; the report about the seed was not, because it counted `len(DEPARTMENTS)`
— a list that predates the office tier and excludes it. Fixed and covered by
`test_the_seed_reports_what_it_actually_created`, which had to be wrong twice
first: `caplog` cannot observe a structlog line at all, and re-seeding the
fixture's tenant failed on an integrity constraint rather than on the count.
F229.

**What the clone also forced into the open**, all of it invisible from inside:

- The corpus paths were absolute and pointed at another project on this machine.
  `AO_CORPUS_ROOT` / `AO_CORPUS_ROOTS` now override them, and the construction
  ingest names the directory it searched, so a report of zeros is diagnosable.
- `docs/DEPLOYMENT.md` documented `make pgvector` and `make pgctl`. Neither
  target exists.
- `.devdata/` is git-ignored, so the NATS and Temporal binaries are not in the
  repository and `make dev` cannot work on a fresh clone until they are fetched.
  The fetch commands are now written down, and the URLs were checked with
  `curl -IL`, returning 200.
- `make page` executes the page's JavaScript under Node. Node is a prerequisite
  and was not listed as one.

**The gate, run clean after the fixes** — with nothing else touching the
database, because the first attempt at this number came back `1 failed` and the
failure was mine: `make setup` on the verification clone and a construction ingest
were writing to the same cluster while the suite ran. It is worth recording that
the run only became trustworthy once nothing else was running.

```
make lint      311 files already formatted, all checks passed
make typecheck Success: no issues found in 141 source files
make test      2956 passed, 8 skipped, 0 failed in 845.87s (0:14:05)
```

Eight skips, not three: an earlier entry in this file reported three for a
*narrower* selection. Nothing regressed to skip, and the corpus-dependent tests
were re-run on their own to confirm it — 172 passed, none skipped, because those
tests carry their own corpus path rather than inheriting the ingest default.

**The lesson, and it is the same one as F18-F30 and F225-F229:** seven faults
came from running a real model, and the eighth came from letting somebody who had
never seen the project read the instructions. Neither was found by a test, and
the test suite was green throughout both.

## Verified on 2026-10-01 (ninth pass: the controls, not the model)

The eighth pass was about a fresh clone. This one is about what the clone revealed
about the controls themselves — and every item below was a wrong answer with a
specific number attached to it, not an absence of work.

**An escalation now concludes instead of hanging.** F231. Before: `root -> running`,
`escalated 1`, `settled upward 0`, `stopped because: nothing is runnable and no gate
can be cleared`, with a docstring claiming the executive was being told. Fixing that
exposed two more, in sequence: the root then settled `completed` over the failure,
because a coordinator's review never looked at whether the coordinator finished; and
the failed office was then re-dispatched, making a 4-task run into a 7-task one.
Now:

```
root -> failed | executions 4 | rerun 1 | escalated 1 | failed upward 2
```

with the department's own finding carried up two tiers in `last_error`. A failed task
is never retried.

**A restatement is caught.** F231's companion. Three plausible-but-empty answers got
through the contract checks before the echo check existed — the brief returned under
the promised keys, and the goal copied verbatim. Comparing the answer against the ask
rejects those. It deliberately does **not** require the answers to disagree: "all
three claims approved" is a real finding and passes.

**The DOA matrix is enforced as money limits.** F232. Eight seeded bands, consulted by
nobody, so a 30bn payment and a 3.000đ one were recorded with identical approvers.
`domain/doa.py` resolves the band and refuses where the matrix is silent, and every
approval now passes through it on the way in — so the amount decides who signs, and an
agent cannot name its own approver.

Two things about that were nearly wrong in a way that would have cost money:

* `L3_HUMAN_APPROVAL` sorts before `L4_BOUNDED_AUTONOMOUS`, so an ordinal reading
  says L4 outranks L3 — and L3 is where a *human* signs. Read ordinally, the seeded
  matrix (every band capped at `L3`) was open to any agent. Read correctly, it means a
  human approves every amount.
* `Decimal("3.600.000")` raises, so every amount in the dossier's own format was
  refused as unreadable. The grouping character is `.`, and the locale is now declared
  by the caller rather than guessed.

**Gate** — see the eighth pass for the full gate. The numbers moved with these tests:
the suite is 3179 unit and integration tests, 3 skipped, 1 deselected, 0 failed, and
`make lint` and `make typecheck` are clean.

**Two existing tests caught a wrong fix in this pass, which is worth recording.** The
rule was first written as "never retry a task that has already failed", and it broke
`test_a_failed_department_run_is_sent_back_not_ignored` — correctly. A *crashed*
department run must be sent back: a crash is often transient, and treating it as
unjudgeable lets a broken department look idle. The distinction that matters is *who*
decided the failure, not what the status says, so the two are now separated by
`failure_category`: a crash is retried, an escalation is not.

**On model quality, the claim was removed rather than softened.** This file previously
said the models "are not reliable enough to run unattended". One free model was run on
one procedure and got all three expense verdicts right — that is the whole of the
evidence, and it does not support a general claim in either direction. What the
evidence does support is that all eight faults were in the loop around the model, not
in the model.

### The same day, two more findings

**The console's document register now has content** — `seed-process` had never been
run on the tenant `first_org.py` selects, so it held 0 `sop_definitions` and
`seed-document-register` therefore had nothing to publish: 28 controlled documents
and 84 distribution rows went in, and those checks pass. The remaining page failures
are projects (no corpus), events and tasks (no work run on this tenant), a Gate
session with a person on it, and failed work to retry.

**A delegation was enforced on a tool the prompt never named** — F234. The prompt said
"call the delegation tool"; the model had to guess the tool is called
`delegate_to_agent`. It guessed wrong and the run failed. The prompt now names the
tool and lists the colleagues.

**That fix is unverified against a model, and is recorded as such.** The provider
gateway had no usable model for the `primary` profile when it was written — the free
tier's daily cap — so no claim is made that it fixes the outcome. It removes a stated
ambiguity in the prompt and nothing more.

**And one thing that looks broken but is not:** with no provider key, `run_pipeline.py`
picks `ScriptedRuntime`, which never delegates, so a coordination task fails with
`no_delegation`. That is the separation-of-duties rule working against a test double,
not the platform failing to delegate. The script prints its `provider:` line for
precisely this reason.

## Verified on 2026-10-02 (tenth pass: the seventh department, and shadow mode)

Two things recorded as "not built" have been built, and one of them had been
recorded as not built **for the wrong reason**.

**Shadow mode has its machinery. `ARCHITECTURE_DECISIONS.md` §D6 said "not
attempted — it needs 4 weeks of real traffic", and that was half right.** Four weeks
cannot be manufactured. But `domain/promotion.py` already gated on `shadow_runs` and
`shadow_agreements`, and `agent_shadow_runs` already had `would_have_decided`,
`actually_decided`, `agreed` and a required `divergence`. The gate, the table and the
thresholds all existed and **nothing wrote to them**, so the number the promotion gate
read was zero forever — which reads as "the model disagrees with everybody" rather than
"nothing is recorded". That is the third time this project has shipped a control fully
specified and never consulted, after the DOA matrix.

`domain/shadow.py` decides whether two answers are the same answer and
`application/shadow.py` writes the comparison down. 41 unit and 11 integration tests.

Three refusals in it are the design: a partial answer is refused rather than scored on
what it answered; an answer that cannot be normalised is refused rather than coerced;
and a run too young to support a rate is reported as too young however well it agreed.
The dossier's thresholds are **4 weeks and 95%**, kept separate from
`promotion.PromotionPolicy`'s engineered 5-runs-and-80%, which answers a different
question and must not be allowed to soften a business precondition.

**The seventh department.** IT exists under Back Office, with the other two of its
tier. The roster is now 2 / 2 / 3 across the three offices. It exists because
`ONX-BO-IT-SOP-007` (IT administration, access control, backup) and
`ONX-PMO-KNW-SOP-006` (the knowledge register) had no owner, and the runner refusing
both is correct and is also not a resolution. **HR was never missing** — it is in the
roster and has been throughout; the two that were dropped in an earlier restructure
were HR and Procurement *as standalone offices*, which is F213.

**A seventh department seeded without its instructions** — F235. `KeyError: 'IT
Director'`, twenty seconds into a demo, because `SYSTEM_PROMPTS` is keyed by agent
title and adding a department meant touching four lists. The test that exists for
exactly this fired correctly; I ran the integration suite, read the errors as the
whole picture, and it was a unit test in a file I did not think to run.

**What is still not done, precisely:** the four weeks of traffic, which cannot be
shortened; a corpus for the Projects surface; and a Gate session with a person on it
and some failed work to retry, both of which are content rather than code.
