# Development progress

A running record of what is built, what is measured, and what is verified. Written as
work completes rather than reconstructed afterwards, because the interesting part is
usually the gap between what was planned and what turned out to be true.

`PRODUCT_GAP.md` holds the measured state of the product. This file holds the state of
the *work*: which phase is in progress, what each phase actually cost, and what the
last tranche taught.

---

## Where the work is

| Phase | What it is | State |
|---|---|---|
| 0 | Substrate: `ai_orchestrator` ported to Python, construction domain ported in, auth and ERP tables deleted on arrival | **done** |
| 1 | Ingest the corpus, then one vertical slice (progress) end to end | **done** |
| 1a | Passive-task reaper — the thing the substrate documents and does not have | **done** (this tranche) |
| 4a | Agent framework's missing parts + the missing end of the learning loop | **schema done**, domain rules next |
| 2 | Role-shaped read services so the domain tables are reachable | **not started** |
| 3 | Document control — a governing principle with nothing behind it | **not started** |
| 4b | Autonomy rules, promotion rules, the AI Decision Log writer | **done** (this tranche) |
| 2a | Project spine: `projects` and `wbs` from the corpus | **done** — readers, writers, and `0017` joining them |
| 2a-i | `clients` | **blocked on a decision** — the corpus supplies no client code or name |
| 5–9 | The 8 agents, Tầng 1 UI, finance tranche, assurance tables | **not started** |

The path itself is in `THE_PATH.md`. The agent order is the dossier's own register
(Tập 1 table 10), not an invented one, and Tập 2 §G.2 names HR the pilot that every
later agent is measured against.

---

## The reaper, and why it came before Phase 2

The instruction for this tranche was to make sure the work *gets done* — delegation,
task progress control, adaptation. Delegation and learning turned out to be already
wired: `authorize_delegation` is called from `task_execution.py` and
`delegation_executor.py`, `assess_duplicate_work` from the task repository, and
`ProcedureProposal` from five places including the approval packet. Nothing to build
there.

**"Ensuring the work gets done" was a real gap, and the codebase said so itself.**
`domain/state_machines.py` defines `PASSIVE_TASK_STATUSES` and comments:

> States in which no progress happens unless an external event arrives. A task stuck
> in one of these needs a timer; see `docs/OPERATIONS.md` stuck-task sweeps.

`docs/OPERATIONS.md` §5.2 answers with a `SELECT`. There is no timer anywhere — no
sweeper, no reaper, no worker loop that looks. Meanwhile `tasks` carries
`lease_expires_at`, `attempt_count`, `failure_category`, `deadline_at` and
`last_error`: **all written, none read**.

So the platform could tell you a task was running, and could tell you a worker had
crashed, and could not tell you either. A task in `running` whose worker died stays
in `running` for ever, and a task waiting on a human nobody has heard from is
indistinguishable from one waiting on a human who is answering.

### The design principle, which is the opposite of the obvious one

**The default is to surface, not to act.** A reaper that changes things is a machine
that quietly cancels people's work, and the states it would be tempted to act on are
exactly the ones where a *person* owes something.

Only two actions are automatic, and both are cases where a contract that already
exists has expired:

- **Reclaim** a `running` task whose `lease_expires_at` has passed. Whoever held the
  lease promised to renew it by then.
- **Requeue** a `failed` task that is retryable and has attempts left.

Everything else becomes a `SURFACE` finding. A `waiting_for_approval` older than any
threshold is escalated, **never expired** — a pending approval is a person owing a
commercial decision, and a system that cancels it because it is old has made that
decision for them. `approvals.expires_at` is where genuine expiry lives, and that is a
different question with a different owner.

### The part that took the most care

The decision is made from a row, and by the time the write lands a worker can have
renewed the lease. So every write re-states the condition it acted on:

```sql
WHERE id = :id AND status = 'running' AND lease_expires_at IS NOT NULL
      AND lease_expires_at <= :now
```

`rowcount == 0` then means something specific: the task was reclaimed, **or a live
worker beat the reaper to it**. Those are different facts, so they are separate keys in
the report — `reclaimed` and `contended` — and a report that conflated them would claim
a reclaim that never happened. `TestTheRaceGuardFires` fires a real competing worker
immediately before the reclaim executes, so the contended path is proven reachable
rather than argued for.

This is also why the domain refuses to reclaim a `running` task with *no* lease: there
the condition cannot be re-stated, and an unconditional write is exactly the race the
clause exists to prevent.

### Two thresholds, and the mistake that forced them apart

The first version gated everything on one 48-hour `surface_after`. Three tests caught
it: a task that failed **a second** ago was being requeued, racing whoever was already
on it and burning an attempt from the ceiling on a failure nothing had recovered from.

The fix was not a smaller number. It was noticing that there are two different
questions:

- *When should a **person** hear about this?* Hours. The cost of being early is a
  notification.
- *When should a **machine** retry this?* Minutes. A transient model timeout should
  recover in minutes, not two days.

One threshold for both means one of the two is always wrong. `ReapPolicy` now carries
`surface_after` (48h) and `requeue_after` (5m) separately, and
`test_a_retryable_failure_is_retried_in_minutes_not_days` is the test that separates
them.

### What it does not do, on purpose

- It does not touch `attempt_count`. That belongs to the execution path; a reaper that
  ran often enough would otherwise exhaust the retry budget of tasks it never tried.
- It does not write a finding to `audit_logs`. `audit_logs` records **actions**; a
  sweep that surfaced forty overdue approvals performed zero actions, and forty audit
  rows would make the trail claim forty decisions the system never took.
- It does not expire approvals. See above.
- It does not reclaim a live worker. See above.

---

## Measured, not assumed

| Claim | How it was checked |
|---|---|
| 169 tests across the three tranches, in 7 files | reaper, promotion, project reader, invariants |
  | *(43 + 64 + 41 + 30 + 18 + 16 + 6 parametrised cases, counted from the files)* |
| 2013 passed, 7 skipped, exit 0 | full suite, 12m40s |
| Lint and types | `make lint`, `make typecheck` — clean on 112 files |
| `0016` builds from an empty database | scratch DB, extensions installed, 102 tables, 99 RLS policies |
| `0016` round-trips | up / down / up / down / up, exit 0 on each of the five steps |
| Both real databases at `0016`, zero drift | `alembic current` on all three; `test_schema_matches_models` |
| 9 seeded agents all landed on L1/L1 | read back after the migration |
| The reaper never touches a completed task | exhaustive over every `TaskStatus` |
| A second sweep changes nothing | whole sweep run twice; second report empty |
| The contended path is reachable | a real competing `UPDATE` fired mid-sweep |
| A terminal task is refused by the **database** too | `assess_task` bypassed; `_RECLAIM` still refuses |
| The candidate query and the domain agree | the two status lists compared, both directions |
| **Two `active` versions of one procedure is impossible** | the forbidden insert; raises `uq_procedure_versions_one_active` |
| **An agent proposal cannot omit its approval** | the forbidden insert; raises `ck_procedure_versions_agent_needs_approval` |
| **An agent cannot be granted above its ceiling** | the forbidden update; raises `ck_agents_granted_within_ceiling` |
| **A kill switch needs a reason and a moment** | both directions refused |
| **A refusal needs a rationale** | the forbidden insert; raises `ck_ai_decision_log_refusal_is_explained` |
| The invariants survive a *second* writer | a partial unique index, not application code |
| **The learning loop closes end to end** | propose → shadow → 20 runs → promote; `current_version_id` moves |
| **A refused promotion writes a decision row** | `ai_decision_log` holds the refusal and its reason |
| **A refusal leaves its evidence intact** | 2 runs recorded, 2 refusals, 2 runs still there |
| **A hard block refuses at every level** | 5 levels x 2 action classes, all refused |
| **`L10` cannot outrank `L2`** | position, not string; `L10` refused |
| **3 projects, 10 address spellings, 2 truncated packages** | `read_corpus` over 59 workbooks, 83 sheets |

Every constraint in `0016` is tested by **trying the forbidden thing** and asserting
the database says no. A test that inserts a legal row and reads it back proves the
table is writable, not that the rule is enforced.

---

## Phase 4a, and the correction that came first

Phase 4 was going to be "build the agent framework". Measuring the schema first
instead of designing against an assumption found that the framework has existed since
the initial schema: `agents`, `agent_definitions`, `agent_relationships`,
`agent_skill_bindings`, with lifecycle status, runtime status, health, budgets, a
heartbeat and a delegation graph. My first draft of `0016` created a *fifth* agent
table, 200 lines of it, and was stopped by `DuplicateTableError` on an empty database
(F108).

What was genuinely missing, measured against the live schema:

| Need | State before `0016` |
|---|---|
| autonomy **ceiling**, distinct from the level granted | `agents.autonomy_level` — unconstrained `varchar` holding `l1_low_risk_autonomous` / `l2_parent_review`, a *different scale in prose* |
| kill switch as a recorded state | nothing; all 9 seeded agents read `active` |
| shadow runs | no table |
| decision log, refusals included | no table |
| anything to apply an approved change to | **no `procedures` table existed at all** |

### The learning loop was open at both ends

This is the fourth of the user's named concerns — "improving and adapting with the
task as time goes on" — and it was the weakest of the four. Delegation was already
wired. Work getting done was a real gap and is now built. Adaptation was:

```
ProposalGenerator.generate()   -> an ApprovalPacket, or a reason there is none
submit_for_approval()          -> an `approvals` row
ApprovalService.decide()       -> a human approves or rejects
load_approved()                -> ZERO CALLERS outside its own module
```

and no `procedures` table, and no writer for one. The platform could notice a
repeated failure, propose a change to a procedure, record a human's approval of that
change, and then do nothing with it. **The next run does the same thing again.** That
is a suggestion box with an audit trail, not adaptation.

`procedure_versions` is the table that was missing, built so the loop cannot close the
wrong way round:

- A version is written **`shadow`**. Getting it there records the approval that
  authorised it; it does not change behaviour.
- At most one version per procedure can be `active` — enforced by a **partial unique
  index**, not by application code, because two agents running two versions of one SOP
  has to be impossible rather than unlikely.
- Promotion is a separate act with per-run evidence behind it. A count can tell you a
  promotion happened and cannot tell you whether it was right.
- The link that closes the loop is a `CHECK`: an `agent_proposal` version **must** cite
  the approval that authorised it. A learned change that cannot name its approval is a
  change nobody agreed to.

### The autonomy pair, and the column left alone

`autonomy_ceiling` (the most the dossier permits) and `granted_level` (what has
actually been granted) with `CHECK (granted_level <= autonomy_ceiling)`. A single
column cannot record "permitted L3, currently L1", which is the state every agent is
in and the state that makes an audit answerable.

Both default to `L1`, so all 9 seeded agents land on the most cautious value. Verified
by reading them back after the migration: `9 agents | ceiling L1..L1 | granted L1..L1`.

`autonomy_level` is **not** rewritten. It holds prose on a different scale, is
unconstrained, and has nine live rows. Mapping one scale onto the other is a judgement
about what those strings were meant to mean, not a schema change — so it is separate
work rather than a side effect of putting a ceiling beside it.

### What `0016` actually adds

Six columns on `agents` and four tables — `procedures`, `procedure_versions`,
`agent_shadow_runs`, `ai_decision_log`. Verified: builds from an empty database, 102
tables, 99 RLS policies, round-trips five times (up/down/up/down/up) with exit 0 on
each, zero drift against the ORM on both databases, lint and mypy clean on 112 files.

Three defects came out of writing it and are recorded as F108, F109 and F110. The
shortest useful one: **a migration that builds is not a migration that matches.**
Building proved the SQL was legal; four rounds of drift-test failures proved the ORM
and the schema did not describe the same table, which is the only one of those two
things a query depends on.

---

## Phase 4b — the loop closes

Two pure modules and one writer.

`domain/autonomy.py` exists because the database already held **two** autonomy scales:
the dossier's `autonomy_policies.max_level` (`L1`-`L4`) and the substrate's
`AutonomyLevel` enum (`l1_low_risk_autonomous`). Nothing converted between them, so every
comparison across that gap was a string comparison — correct today only because the
codes happen to be single digits in ascending order, in the same case, in the same
alphabet. `rank()` is position, not string, and `L10` is refused rather than sorted
below `L2`.

`is_hard_block` is checked **before** the level comparison, and refuses at every level
including L4. The seed data gives a hard block `max_level = 'L1'`, which read as a
ceiling says "an L1 agent may do this"; read as a flag it says "no agent may do this",
and the second is what Tập 1 §5.3's *vùng cấm tuyệt đối* means.

`domain/promotion.py` decides, `application/procedure_operations.py` writes. The loop is
propose → shadow → record runs → promote, and `TestTheLoopCloses` asserts the last step
is a change in behaviour: `procedures.current_version_id` points at the new version.

**The refusal order is the design.** Run count is checked *before* the agreement rate,
because 3-of-3 is 100% and means nothing, and a rate over too few samples beats any
floor. That is F101's shape again — a statistic designed from a fixture rather than from
the sample it is measured on — and a run count is what the rate cannot launder.

Every refusal is written to `ai_decision_log`, and the table's `CHECK` refuses a
`refused` row with an empty rationale. The interesting row in a decision log is the one
where the system declined.

**A design error the tests found.** `ai_decision_log.agent_id` was `NOT NULL` with an FK
to `agents`, so a promotion could never be logged — a promotion is a *system* decision
about an agent, and a human-written version has no proposing agent. Made nullable with a
required `actor_type`, which is the shape `audit_logs` already uses for the same reason.

---

## Phase 2, redirected by measurement

The plan was "role-shaped read services over 42 unreachable tables". Measuring first
showed the premise was wrong:

| table | writers | dev rows |
|---|---|---|
| `projects`, `wbs_items`, `clients` | 0 | 0 |
| `contracts`, `suppliers`, `materials` | 0 | 0 |
| `rfqs`, `purchase_orders`, `goods_receipts` | 0 | 0 |
| `progress_snapshots`, `gate_instances` | 1 | 0 |

**Every construction table has zero rows and zero writers.** Read services over empty
tables return nothing and cannot be measured — the position the tender agent is already
in, for the same reason.

So Phase 2's first step is not reads, it is rows. And the project spine turned out to be
sitting in files this repository already reads: every construction sheet opens with
`Dự án:`, `Địa điểm/Address:`, `Gói thầu/Package:`, `Hạng Mục/Item:` — and the Phase 1
progress reader **discards it**, classifying the row as a roll-up because a roll-up has a
window and no duration.

`ingest/project_reader.py` reads it, and reports disagreements rather than resolving
them. Measured over 59 workbooks and 83 sheets:

| | |
|---|---|
| sheets carrying an identity block | **43** |
| projects | **3** — `BÃI TRÀM ESTATES`, `MELIA CAM RANH BAY VILLA & RESORT`, `LAWRENCE STING SCHOOL 2` |
| address spellings | **10**, for 2-3 real places |
| package spellings | **5**, of which **2 are strict prefixes of a third** |

**The docs said "3 Hoabinh Group projects". The count was right and everything else was
wrong** — no project in the corpus is called Hoabinh Group.

Which spelling of an address is canonical is a business decision, so the reader records
all ten verbatim and picks none. And `Cơ` and `Cơ điện` are both strict prefixes of
`Cơ điện khách sạn và nhà phụ trợ` — cells cut short by their column width. Writing `Cơ`
would put a value in a column that cannot be told from a real one.

---

## Phase 2a — the project spine writer

`application/project_operations.py`. The design claim is one sentence: **the decision is
an input, not an output.**

`write_project` takes an `AddressResolution`, not a string. Which of the ten spellings is
canonical is a business decision, and a caller that has not made one — and recorded who
made it and when — cannot write a project. The three refusals:

- **Unsettled** — no canonical form, or chosen by nobody, or chosen on no date. A
  canonical form with no hand attached to it is the silent decision the service exists
  to prevent.
- **Invented** — a canonical form that is not among the spellings the corpus contains.
  It would be a form nobody approved, matching nothing in any file.
- **Truncated** — a package that is a strict prefix of another value the corpus has.
  This needs the **survey**, not the file, which is why `observed` is a required
  argument: a single sheet cannot know `Cơ điện khách sạn và nhà phụ trợ` exists in a
  different workbook. One survey, three projects.

`clients` is deliberately optional. `clients` needs a `code` and a `name` and the
header block supplies neither — no project, address, package or item line names a
client — so a project with no client is a real, writable state rather than a gap.

**One measurement corrected a plan.** `wbs_items` is not the WBS hierarchy; it is a
priced BOQ line (`code`, `unit_code`, `quantity`, `unit_rate`, `amount`). The tree is
`wbs`, which self-references on `parent_id` and hangs off `project_id`. So the spine is
`clients → projects → wbs → wbs_items`, and the progress sheet's three-level hierarchy
maps to `wbs`, not to `wbs_items`.

---

## Phase 2a, part two — the work breakdown, and what the sheet will not say

`ingest/wbs_reader.py`. The central claim, and the reason it exists: **the label does
not say what level a row is.**

    row  16   A   2019-03-13..2019-09-20   BOH                    SYSTEM
    row  18   I   2019-03-13..2019-09-08   Hệ thống cấp thoát nước   SYSTEM
    row  47   A   (no window)             Zone A                  ZONE

`A` is a system at row 16 and a zone at row 47. Any rule keyed on the label files one of
them a level too shallow, and the tree reads correctly while misplacing half the work.
**The discriminator is whether the node has a date window** — 8 have one, 4 have none,
and the split is exactly the two levels.

| measured on `TĐ BOH.xlsx` | |
|---|---|
| nodes | **12** — 8 systems, 4 zones |
| activities | **110**, with **0 orphans** |
| attachments | 22 directly under a system, 88 under a zone |
| nodes sharing one description | 6 (`I`–`VI`, all "Hệ thống cấp thoát nước") |

Activities attach at **two different depths** and the sheet uses both, so the rule is
"the most recent node of each level, whichever is nearer", and a node at either level
resets only its own level and below.

### Three findings, none of them repaired

**The first node is provably the report's own total.** `BOH`'s window is
`(2019-03-13, 2019-09-20)`; the union of all 110 activities' windows is
`(2019-03-13, 2019-09-20)` — the same, exactly. A structural proof rather than an
assertion about a label, and a second signal agrees: it is the only node with neither
activities nor zones beneath it. This is F96's finding, recorded by eye for activities,
now measured for the tree.

**Five nodes finish before they start** — `II`, `III`, `IV`, `V`, `VI`, each ending one
day before it begins. The corpus counts days inclusively, so a one-day item has
`start == finish`; a backwards window is a typo in the file, so the reader reports it
and refuses to schedule the node rather than normalising a date it did not invent. My
first count was four and the real number is five.

**Six systems share one description.** Not a defect — six zones of one system is a real
shape — but a WBS keyed on description would collapse them and lose five of six.

---

## Phase 2a, part three — the WBS writer, and a refusal that was wrong

`application/wbs_operations.py`, and migration `0017` which attaches a progress reading
to the work package containing it.

`wbs` holds `code`, `name`, `parent_id`, `sequence`, `is_leaf` — and **no date
columns**, because a breakdown is a structure and a structure is not a schedule. The
schedule lives in `progress_snapshots`, and nothing joined them: `progress_snapshots`
had `wbs_item_id`, which points at a **priced BOQ line** in `wbs_items`, not at `wbs`.
The corpus's progress activities have a window and a duration and no quantity and no
rate, so writing them into `wbs_items` would mean inventing a number for 110
activities. So the two halves of the construction record sat in tables with no path
between them, and "which zone is late?" was unanswerable.

`0017` adds `progress_snapshots.wbs_id` as a **composite** `(organization_id, wbs_id)`
foreign key — a bare one would let a reading in tenant A attach to a work package in
tenant B, a row RLS could never follow.

### The refusal that was wrong, found by the end-to-end test

The first version refused a node for any of three reader findings. One of them was
`BACKWARDS_WINDOW` — a window ending the day before it starts, which five nodes on the
real file have. The end-to-end test over `TĐ BOH.xlsx` is what showed that to be wrong:

    12 nodes read, 6 written, 6 refused
      1  BOH          correctly refused -- it is the report's own total
      5  II..VI       real work packages with activity groups under them

Those five are all "Hệ thống cấp thoát nước", one per zone, each with four or five
activities beneath it. Refusing them orphaned those activities and lost a third of the
breakdown.

**And it fixed nothing**, because `wbs` has no date columns to put the bad dates in.
The refusal traded real structure for no repair. So `BACKWARDS_WINDOW` is now reported
and the node is written: the finding says the *file's* dates are wrong, the node says
the work package exists, and only one of those is about this table.

The lesson is the question to ask before refusing a write: **what does refusing
prevent?** Here the answer was nothing, because the thing that was wrong is not the
thing being written.

---

## Phase 5 — the product became reachable, then visible

Read the whole codebase first: 116 source files, 72 test files, and the API layer
endpoint by endpoint. The finding was one sentence long.

**The API had 72 operations and not one of them touched a project, a WBS, a progress
reading, a contract, a supplier, a purchase order, an RFQ, a tender, a zone or a gate.**
The only path with any of that vocabulary was `/api/v1/tasks/{task_id}/delegate`,
matching on "gate" inside "delegate". Six projects, 240 WBS nodes and 2778 progress
readings were in the development database, reachable only from a Python shell.

So the previous five tranches had built a library with no consumer. That is the whole
gap, and no amount of further domain work would have closed it.

### 5a — `api/construction.py`, seven operations

Transports, and held to one rule: **it must not lose a distinction the read makes.**

* `unmeasured` is its own list, not something a client is expected to filter out.
* `actual_updated` travels with every reading. 100% of the real corpus's actual columns
  are byte-identical to its planned ones, so a client that ignored the flag would render
  2778 fabricated measurements.
* An empty tenant is `200` with `items: []`, never a 404.
* Another tenant's project is **404, not 403** — "forbidden" would confirm the row exists.

Underneath it: three new reads (`wbs_tree`, `node_by_id`, `readings_for_node`) and one
new type, `application/ports.SqlRunner`. The type was needed because `api/deps.py` hands
handlers an `AsyncSession` and mypy was right to refuse to call that an
`AsyncConnection`. The first attempt typed the Protocol's return as `CursorResult`,
which looked more precise and made it match **nothing** — a Protocol that a real object
does not satisfy is a lie the type checker is right to reject.

### 5b — the UI

The substrate console was **kept, not replaced**. It is working, tested capability, and
deleting it to make room for new capability would have been the wrong trade. The
construction product went around it: **Operations / Fleet / Console** behind a nav, plus
the **role switcher** (CEO / PM / Approver) that makes three views one product rather
than three pages.

One stylesheet rule carries the whole thing: **an unmeasured work package does not look
like an on-time one.** Hatched outline, no bar, against a solid green bar. A bar is a
width and a width is a measurement, and the corpus makes "unmeasured" the *normal* case.

### 5c — verifying the page by *executing* it

22 page tests, including two that matter more than the rest:

* **every `$("id")` in the script resolves to an element in the served document** — this
  is what catches a page of `undefined`;
* **the script parses** under `node --check` — a parse error serves a 200, renders a
  frame, and shows nothing.

Then `scripts/verify_page.mjs`, wired up as `make verify-page`. There is no browser in
this environment and no headless one, so it does the next best thing: it runs the
served document's JavaScript in Node against a small DOM shim and the **live API**, then
asserts on what it rendered.

    runtime:  script loaded without throwing · 5 fetches · none failed
    rendered: portfolio stats · 6 projects · 9 agents · above-ceiling 0
              40 zone bars — 36 unmeasured, 1 late
              288 of the progress rows marked unmeasured
    all checks passed

**It found F125, which 18 unit tests had not.** `role_views.pm_workspace` returned the
same measurement two different ways, and the two answers disagreed: `late` said a zone
was 2 days behind, `zones` said it was on time. The page drew it green while its own
legend said one zone was late.

The fix took two attempts and the first was worse than the bug. Filtering the aggregate
on `actual_updated` was necessary and not sufficient, because
`max(actual_finish_on) - max(planned_finish_on)` are **two independent aggregates** that
need not come from the same row — and on `S028` they did not, producing **-65 days** for
a zone 2 days late. The answer is a `LEFT JOIN LATERAL` over the newest measured reading
per node, so the two operands are the same row, and `_LATE_ZONES` is a filter over the
same lateral rather than a second opinion.

`make page` wraps the whole thing: it starts Postgres if needed, brings the API up in its
documented demo mode, finds a real tenant, runs the verification, and prints the URL.
`docs/CHECK_THE_PAGE.md` is the walkthrough — what to click, what to count, and what a
failure would look like.

**What is still unproved:** nothing has been seen. There is no browser and no headless
one. `verify-page` proves the page *computes* the right thing; it cannot tell you the
layout is readable at 375px, that a colour passes contrast, or that a bar does not
overflow its track. That still needs a human with a window.



---

## Phase 6 — an agent that actually does something

*"tôi chưa thấy dùng agent cho task gì?"* — and the honest answer was that agents **had**
run and the page could not see them. Once that was fixed, the path was measured to the
end and a real delegation now happens.

    make demo

    nvidia/nemotron-3-ultra-550b-a55b:free answered 'ONLINE'
    Executive Agent: bound to profile 'primary'

    task      tsk_01m3jeyzzaa0w3rkn8qe05cd9t  (coordination)
    agent     Executive Agent  profile=primary
    running…

    runtime.tools_exposed  names=['delegate_to_agent', 'safe_web_search', 'write_report']
    delegation.applied     target='Program Agent'  status=accepted

    --- what happened ---
    status     completed

    --- delegation tree (1 hop(s)) ---
        └ Program Agent  [accepted]
          Produce a weekly progress report for BÃI TRÀM ESTATES. Analyze work packages…

    --- model calls ---
      2 x openrouter/dots-studio/dots-3-note-preview:free  $0.0000

A real agent, on a real free-tier model, handing work to another agent, at no cost — and
the console shows the tree.

### What it took to get there

Three defects in a row, and none of them were the model:

| | |
|---|---|
| **F131** | The console read `ev.detail` and `ev.at`; the payload has `data`, `occurred_at`, `view`. 189 real events rendered as blank lines. |
| **F132** | `build_runtime` had no entry for `openrouter`, so a real provider fell through to `NullRuntime` and every task failed with *"no runtime adapter is available"* — confident, specific, and wrong. |
| | `ModelGateway` is built from `default_profiles()` in code and never reads the `model_profiles` table, so a row written there is invisible and an agent bound to its id fails with *"unknown model profile"*. |

**The gap that is real and is not papered over:** a tenant cannot yet choose its model
from the database. The table exists, is writable, and is read by nothing on the execution
path. `scripts/seed_free_model.py` writes the row anyway — so the intent is recorded and
the day the gateway reads the table it becomes the switch — and binds the agent by **name**,
because that is what resolves today.

### The demo runs a *coordination* task on purpose

A coordination task that does the work itself is **failed by the platform**, on purpose:
*"a coordination task completed without delegating: the agent had 8 agents it could have
handed work to and did the work itself."* 24 of the 47 tasks in the seeded history failed
exactly that way, and that is the separation-of-duties rule working rather than a bug.

---

## Phase 5d — the UI rebuilt, because it was hard to use

Reported as: *"UI làm không đủ chuyên nghiệp, lại còn khó sử dụng và đọc. Khi click vào
bất cứ dự án hay vị trí thì không thể thoát ra ngoài dễ dàng."* Three specific complaints,
and all three were true. The middle one is the important one.

**1. There was no way out.** The selected project lived in a JavaScript variable. That is
a dead end three separate ways: no Back control, the browser's own Back went to wherever
the person had been *before the site*, and a refresh threw the selection away.

Now: the URL is the state (`#/projects/:id`), so Back works, a link is shareable, and a
refresh lands where you were. A visible `‹ Back` sits in the breadcrumb and is **never
turned off** after you have drilled in anywhere — there is no state in which somebody is
somewhere with no visible route out. `Esc` is bound on the document, so there is one
predictable exit. And the list is never replaced by its detail: side by side above
1080px, and one keystroke away below it.

**2. Work could be seen but not handled.** There was an approval inbox and **no approve
button**. Approve / Ask / Reject now, with a confirmation, and a failure reported in the
page rather than swallowed. The agent roster has a **Stop** control, and it asks for a
reason before sending — because `ck_agents_kill_is_recorded` refuses a kill without one.

**3. No information hierarchy.** Cards stacked in a scrolling column, no nav, no density.
Now: a persistent sidebar, a breadcrumb bar, and six sections —
Dashboard · Projects · Approvals · Agents · Decisions · Console.

### The role switcher, rebuilt to be additive

The old one toggled `hidden` on panels, which is *why* the page felt like it was hiding
things from you. It now sets CSS `order` — reorders what the page leads with, hides
nothing, and leaves the sidebar identical. Tập 1 §Tầng 1 asks for "a dashboard per role";
reordering satisfies that without navigation that changes underfoot.

### The console stayed

The substrate console is working, tested capability. Deleting it to make room for new
capability would have been the wrong trade, so it is now one section, unchanged in
behaviour, with all 21 of its existing tests still passing against the new page.

### What the harness found, again

`make verify-page` **navigates into a project and back** now, which the first version
never did. It found that leaving a project left 136 KB of bars alive in a hidden view —
invisible, and still a leak. And it found `applyRole` using `insertBefore`, which
happened to work but mutates the tree on every switch; `order` is the right tool.

### F129 — the cost of a rewrite

I replaced the page almost wholesale and silently dropped two behaviours it had:
unwrapping a pasted `export INTERNAL_SERVICE_SECRET=…` line, and forgetting a rejected
token. Both were caught by tests written for the old page, which is the whole argument
for having them. **A rewrite is a subtraction, and nobody lists what they are
subtracting.**

---

## Phase 2b — the three role surfaces, and a pipeline that finally has data to read

`application/role_views.py` — the three reads Tầng 1 names, as questions rather than
CRUD — and `scripts/ingest_construction.py`, the pipeline that puts the corpus into the
database.

The reads were unbuildable before this. The construction side of the development schema
had **zero rows** — 0 projects, 0 wbs, 0 progress_snapshots — so every endpoint returned
an empty list that was indistinguishable from a broken one. Writing queries over empty
tables produces something that cannot be checked, which is the position the tender agent
has been in for four tranches.

### What the pipeline produced, over 59 workbooks and 96 progress sheets

    projects written        6      (+1 refused, and the refusal is the finding)
    wbs nodes written      240     169 refused
    progress readings      2778    12 refused
    sheets with no header  0
    second run              a clean no-op: 96 reports skipped, 6 projects skipped

### And what the three surfaces say about it

    CEO COCKPIT   6 projects · 240 wbs nodes · 9 agents · 0 killed · 0 above L1
    PM WORKSPACE  40 zones · 1 late (2 days) · 36 unmeasured
    HITL INBOX    0 pending

**36 of 40 zones unmeasured** is the number worth reading. The corpus's progress sheets
record planned dates and leave the actual columns empty, so a naive
`variance = actual − planned` would report this project as *perfectly on schedule* — and
`PRODUCT_GAP.md` measured that 100% of the real file has empty actual columns. The
workspace refuses that reading: a zone with a plan and no actual is in `unmeasured`,
not in `late`, and a zone with both is in `late` only when the actual is genuinely
marked as recorded. Both numbers are reported side by side, because a project with four
late zones and two unmeasured ones is a different situation from one with six late.

### Two defects the run found, and one of them was mine

**F120** — the corpus writes its project label as **`DƠ án`** (a capital `Ơ`, U+01B0,
where `ự` belongs) on **48 of 59 workbooks**, so the reader dropped every project name
and the pipeline wrote zero readings. The reader now carries the typo explicitly and
still reports any *other* unknown label. But the real failure was the script's
`if not header.accepted: continue` — **48 sheets read, 0 written, no reason given**,
which I nearly wrote up as a corpus fact.

**F121** — `progress_snapshots`' unique key was wrong for **3 of 16** progress sheets, and
no combination of the fields a reading carries fixes the fourth: `TĐ Hạ Tầng.xlsx` lists
the same activity twice, identically. `0018` makes the identity **positional**
(`source_row`), which is unique by construction.

**Mine, and only the numbers caught it:** `AgentPosture.above_l1` counted
`granted_level = 'L1'` — the agents *at* the ceiling — and reported 9 of 9 agents above
it. And `_link_activities` matched on `line_label` alone, so every activity numbered `1`
in a report landed on one node: the workspace showed 40 zones of which 36 had no
readings and one had all of them. Both fixed against the now-populated database, which
is the only reason they were visible at all.

---

## What the tranche cost, in defects

Fifty, all in `FAILED_APPROACHES.md` as F102–F145. Three were found by reading the
code and thirteen by running it, which is the right way round and the ratio has been
improving:

- **F102** — `Tenant.run()` committed without re-establishing the `SET LOCAL` tenant
  GUC, so *every read after a write through it returned zero rows*, as a
  `NoResultFound` with no explanation. Four test files were silently doing this.
- **F103** — one threshold for two different decisions: a task that failed a *second*
  ago got requeued, or a recoverable timeout was held for 48 hours, depending on which
  way the single number was wrong.
- **F104** — a naive `now` against `timestamptz` columns raised `TypeError` from inside
  `_age`, four frames below the caller, naming neither cause nor fix.
- **F105** — the `CAST(:param AS timestamp)` idiom, copied into a module whose columns
  are `timestamptz`, pinned an aware datetime to a naive type.
- **F106** — Postgres was **stopped**, and the suite reported it as several hundred
  unrelated errors at 4% with no mention of the cause. Check `pgctl.py status` before
  believing an error wall.
- **F107** — a `hash()`-derived test id collided, surfacing as
  `UniqueViolationError: pk_tasks` in whichever test lost the race.
- **F108** — two hundred lines of migration for `agent_definitions`, a table that had
  existed since the initial schema. Caught by building from empty.
- **F109** — `create_check_constraint` and `drop_constraint` both re-apply the naming
  convention, and the downgrade dropped nothing, so it exited 0 without round-tripping.
- **F110** — four rounds of drift-test failure on hand-written names, each fix
  revealing the next: a `String(32)` against a `SHORT` mixin, a hand-written FK name
  one word off the convention, a missing `ondelete`, and three supporting indexes
  created on existing tables whose models were never told.

### The recurring pattern, restated

*What does this actually do, and what is it actually called?* — with two further
questions attached: **what does the fixture contain that the real thing does not?**
(F107) and **what is actually in the schema already?** (F108).

The four sharpest, in the order they bit:

**F108** — I did grep before designing, but I asked *"is there a procedures table?"*,
which is a question about what I expected to be missing. One grep for `agent` would
have answered it, and 200 lines of migration were written for a table that had existed
since the initial schema. A targeted grep finds the thing you predicted; a table
listing finds the thing that contradicts you, and only one of those is a check.

**F112** — a normalisation applied to one side of a comparison only. A regex matched 71
times against a lookup that resolved 0 of them, and the survey reported "1 project out
of 59 workbooks": plausible, unremarkable, and completely wrong. **71 matches and 0
resolutions is not a data problem.**

**F113** — the sample was one file and the population is 83 sheets. Bilingual labels
are written `Địa điểm/Address:`, and the reader demanded a colon straight after the
first label — missing every bilingual header in the corpus, including 3 of the 4 lines
in the file the tests were written from.

**F114** — "about a dozen" bare foreign keys was 135, across 75 tables. The *examples*
were right, which is exactly what made the estimate survive for twelve tranches. A
reader checks the examples, finds them right, and has no reason to doubt the rest.

---

## Next

**Phase 4b — the rules, not just the tables.** `0016` made the invariants impossible;
nothing yet *uses* them. Next: the domain module that decides whether a shadow version
may be promoted (minimum run count, agreement rate, and what happens to a version whose
author is killed), the promotion service, and the AI Decision Log writer. The promotion
rule is the one place where "improve and adapt" becomes a behaviour change, so it is
the one place worth getting right before there is anything to promote.

**Phase 2 — the largest remaining piece of visible value.** 42 construction tables and
0 of them reachable through the API. The shape to build is not per-table CRUD. Tầng 1
asks for a conversational interface per role — CEO cockpit, PM workspace, mobile HITL —
so the read services should be *role-shaped questions*, and write services should exist
only where ingest and HITL actions need them.

**Phase 3 — document control.** One of Tập 1's five governing principles with nothing
behind it: the `ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]` code scheme, a version table, a
distribution matrix, and an expiry distinct from `documents.retention_until`.

Then the 8 agents, in the dossier's own order, each gated behind the framework the way
Tập 2 §G.2 describes HR behind the pilot.
