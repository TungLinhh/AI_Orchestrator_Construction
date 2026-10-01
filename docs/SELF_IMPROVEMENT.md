# Self-Improving Agents and Skills

**Built on the learning loop of Hermes Agent** (Nous Research), researched rather
than assumed. The sources and what was taken are in §2; §8 records what this
platform does *differently* and why.

**Status**: design only. `CURRENT_STATE.md` is the authority on what exists.

---

## 1. What the user actually asked for

> "agents và skill thích nghi tốt với các quy trình và framework trùng lặp nhau
> trước đã"

That is not benchmark scoring. It is: **when the same procedure comes round
enough times, write it down; when it is done again, use the written version; when
it goes wrong, fix the writing.** An earlier draft of this document proposed an
evaluation suite and drift thresholds instead. That was the wrong shape, and
Hermes' loop is the right one.

## 2. What Hermes does, and what is taken from it

Researched from the Hermes documentation and the `hermes-agent` repository.

| Hermes mechanism | What it is | Taken? |
|---|---|---|
| **`observe → distill → reuse → refine`** | The cycle name, verbatim from their docs | **yes** — it is the loop |
| **Skill auto-created after 3+ repetitions** | A repetition threshold, not a score | **yes** |
| **`patch` preferred over `edit`** | A targeted `old_string → new_string` change, because only the diff costs tokens | **yes** — see §4 |
| **Danger scan → quarantine** | A skill whose scan finds prompt-injection directives, credential exfiltration or hidden text is quarantined: absent from the index, refuses to load by name | **yes**, and enforced harder — §6 |
| **`skills.write_approval`** | A switch for whether the agent writes skills freely or every write is approved | **yes**, defaulted differently — §8 |
| **Provenance: "changed" is user-modified and skipped forever** | Human edits are never stomped by an agent update | **yes** — this is the most important idea here |
| **Progressive disclosure** | ~3k tokens of description for all skills; the full body loads only when a task matches | **yes**, already partly present in the skill index |
| **"The agent claims success without testing"** | A named failure mode, fixed by requiring verification before completion | **yes** |
| **Benchmark is not a synthetic score** | Their stated test: *does it need less steering and fewer corrections* | **yes**, and it replaces the whole benchmark idea |

Their own failure list is worth keeping verbatim, because it is the failure list
this design inherits:

* the agent cites stale facts after a project changed;
* skills contain commands that no longer work;
* memory stores temporary run logs instead of stable preferences;
* the agent claims success without testing.

## 3. The loop, in this platform's terms

```
        work happens  (task runs, tools called, a human corrects something)
              │
              ▼
        executions + audit + approvals          ← already exists, already recorded
              │
              ▼
        fingerprint the *procedure*             ← NOT A TASK HASH
        (what shape of work was this?)
              │
              ▼
        seen this shape before? ──no──▶ run as normal
              │ yes
              ▼
        seen ≥ 3 times?  ──no──▶ run as normal, keep counting
              │ yes
              ▼
        ┌─────────────────────────────┐
        │ a procedure now exists      │
        │ → load it instead           │
        │ → did the human correct it? │
        └─────────────────────────────┘
              │
              ▼
        a lesson is proposed, not applied
        (a patch to the procedure, a memory, or a new procedure)
              │
              ▼
        danger scan → approval → publish as a new version
```

**The distinction that makes this work: a task's `fingerprint` is not a
procedure.** The task hash answers *"have I been asked this before?"* and its
whole purpose is to refuse the duplicate. A procedure answers *"is this the same
*shape* of work?"* — purchase requisitions, recruitment, incident triage — and
the same shape arrives with different words every time. Using the task hash here
would produce a procedure per phrasing, which is the same failure as having no
procedure at all.

## 4. Three kinds of change, and which is allowed

| Kind | Example | Mechanism |
|---|---|---|
| **Memory** | "this company approves purchases over $500 with the CFO" | `memory_items`, existing |
| **Procedure patch** | the requisition flow collects the cost centre *before* the supplier | `patch`: `old_string → new_string` |
| **New procedure** | no requisition procedure existed | `create` |

`patch` is the default and `edit` is the exception, following Hermes. A full
rewrite spends tokens on text nobody read, and a reviewer who skims a 400-line
diff approves things they did not look at. A patch shows the two lines that moved.

## 5. A proposal is data, and it must be falsifiable

```json
{
  "procedure": "purchase-requisition",
  "occurrences": 4,
  "proposal": {
    "kind": "patch",
    "old_string": "Ask the requester for the supplier first.",
    "new_string": "Ask for the cost centre first; the supplier comes from the approved list.",
    "why": "3 of 4 runs were reworked because the supplier question came before the cost centre.",
    "evidence_tasks": ["tsk_01m3f…", "tsk_01m3a…", "tsk_01m3b…"]
  },
  "falsifier": "if requisitions reworked for this reason stop appearing, revert",
  "proposed_by": "dots-studio/dots-3-note-preview:free"
}
```

`falsifier` is required, not optional. A change that cannot say what would
disprove it is not a hypothesis. Shipping those anyway is how a self-improving
system gets steadily worse with a clean audit trail.

## 6. The gates, in order

1. **Repetition gate** — at least 3 occurrences, named. No evidence, no proposal.
   This is the single most important gate: it is what stops the system inventing
   a procedure from one confusing run.
2. **Verification gate** — the run that teaches the procedure must have
   *succeeded*, or have been corrected by a human. A lesson extracted from a run
   that failed teaches the failure.
3. **Danger scan → quarantine** — prompt-injection directives, credential
   exfiltration, hidden text, or an instruction to ignore prior instructions.
   A proposal that trips the scan is **quarantined**: it does not appear in the
   index, cannot be loaded by name, and an operator is told why. Hermes warns
   that "external directories are not a write-protection boundary"; here the
   boundary is the database, because `ao_app` has no grant that could write a
   published procedure.
4. **Diff gate** — a human reads the diff, not a summary. A proposal that
   rewrites the whole procedure is refused for the same reason: an unreadable
   diff is an unreadable change.
5. **Approval gate** — bound to a hash of the exact diff, reusing the existing
   approval mechanism. Cannot be reused for a different patch.
6. **Publish gate** — a new `skill_versions` row, `is_published`. The old one
   stays. Reversible by moving the pointer back, which is why a version is a row
   and not an edit.

## 7. What an agent may never do

Carried over unchanged from the earlier draft, because these were right and
nothing Hermes does changes them:

1. **Widen its own authority.** `authority_profile` is human-edited.
2. **Publish.** Every write is a proposal; a human publishes.
3. **Edit the policy set.** `apply_autonomy_gate` sits outside the rule set
   precisely so a rule author cannot widen it.
4. **Edit its own procedure suite or its own verification.** Otherwise the check
   drifts toward whatever the system already does and the score rises while
   nothing improves.
5. **Learn from anything it was not authorised to see.** The same tier and
   classification rules apply to a lesson as to memory.
6. **Overwrite a human edit.** A procedure a human has touched is *user-modified*
   and is never the target of an automatic patch. This is Hermes' provenance
   rule and it is the single most important one to copy.

## 8. Where this platform deliberately differs from Hermes

| | Hermes | Here | Why |
|---|---|---|---|
| **Skill write approval** | off by default; `skills.write_approval` opts in | **on, always** | Single-user and a personal machine versus multi-tenant, with money and outbound effects. A free-writing agent in a company that can send email is a different risk than a free-writing agent on a laptop. |
| **Boundary** | a directory; "external dirs are not a write-protection boundary" | a **role grant**: `ao_app` cannot write a published procedure | Already built. RLS makes the claim checkable rather than advisory. |
| **Quarantine** | a scan verdict; the file is left in place but hidden | a scan verdict, and the row is **not written at all** | A quarantined row in the database is one query away from being used. |
| **Model** | whatever the user configured | a **second, stronger model** proposes | A proposer identical to the subject is an echo. |
| **Verification** | the agent verifies itself | the platform verifies: the run must be `completed` or human-corrected | "The agent claims success without testing" is in their own failure list. |

The honest summary: **take the loop, keep the gates.** Hermes is built for one
person's machine. This is built for a company.

## 9. What to build, in order

| # | Piece | Why here |
|---|---|---|
| 1 | ~~A **procedure fingerprint** — distinct from the task fingerprint, over task type + the ordered tool sequence + the shape of the input~~ | **Done.** `domain/procedure.py`. Over `(task_type, ordered tool names, argument *keys*, refusal per step)`. Argument values are excluded on purpose: "buy 5 laptops" and "buy 6 laptops" are one procedure, and a value-sensitive hash would call them two — the failure mode of having no procedures, wearing a hash. Migration 0004 adds `tasks.procedure_fingerprint`; `application/procedures.py` reads the trace and stores it. 14 unit tests |
| 2 | ~~A read-only `WorkflowTrace` view: one row per run, listing the tools called in order~~ | **Done.** Every tool call writes a `tool.invoke` audit row with the tool name, redacted arguments, outcome and the real `execution_id`, ordered by the audit sequence. `FAILED_APPROACHES.md` F41, F50 |
| 3 | ~~Repetition counting per `(organization, agent, procedure fingerprint)`~~ | **Done.** `ProcedureReader.repetition_count` and `has_repeated`, counted from the database rather than from the run's in-memory outcome, excluding the run being recorded so a procedure never counts itself. Recorded inline in `_finish`, not by a scheduler: a nightly job would make the gate open a day late every time, and a gate that opens late is a gate nobody trusts. **The gate has now opened on real traffic.** 11 integration tests |
| 4 | ~~`ProcedureProposal` as a typed object, `falsifier` required, `patch` as the default kind~~ | **Done.** `domain/learning.py`. `falsifier` is a validator, not a field: blank, `"n/a"`, `"tbd"` and `"because"` are all refused, because a placeholder in a table of proposals looks answered to a reviewer skimming it. `occurrences` may not exceed the cited evidence. A patch needs both strings and may not be identical; a `memory` may not carry patch fields, so a fact cannot become a change in behaviour. 36 unit tests. `application/learning.py` runs gates 1–3 and collects *every* failure, not the first |
| 5 | ~~The danger scanner and the quarantine path~~ | **Done.** `domain/scan.py` scans every field a proposal carries — injection, exfiltration, hidden text — matched against three forms of the text because `ignore-previous-instructions`, `i g n o r e previous instructions` and `postgres://u:p@h` each need a different one. `application/quarantine.py` records a verdict and **never the payload**: the proposed text is not written at all, which is the deliberate difference from Hermes in §8. Migration 0005, with RLS. 44 unit and 8 integration tests |
| 6 | The approval packet: the diff, the evidence, the counter-examples | What a human actually reads. `ChangeSet.is_readable_as_a_diff` is the first half of this and already exists: a `create` or a rewrite does not qualify as reviewable, and the gate has to be able to say so |
| 7 | The scheduler — nightly review, weekly proposal batch | Last, because it is the easy part and the least valuable |

**The order changed for a reason worth recording.** The trace was originally step 2
and the reason it was needed at all is that the platform could not answer *what an
agent did* — a question asked of it directly, long before there was any intention
of building a learning loop. Asking a system what it did is the cheapest possible
diagnostic, and it turned out to be the same thing the self-improvement design
needed. Instrument first, then infer.

## 10. The two first procedures

Not a benchmark. Two real workflows, to be *watched* until they repeat, which is
the only way the repetition gate ever opens.

### 10.1 `purchase-requisition`

```
observe:  task_type='analysis' | tools: [internal_database_query, write_report]
          | input mentions a cost, a department, and an approval threshold
the procedure:  1. find the cost centre   2. check the approval threshold
                3. draft the requisition   4. name the approver
teaches:        which step was reworked, and why
```

### 10.2 `headcount-request`

```
observe:  task_type='coordination' | tools: [delegate_to_agent, write_report]
          | the Executive delegates; a department agent then drafts
the procedure:  1. name the owning department   2. read the open role
                3. check headcount against budget   4. draft   5. name the approver
teaches:        what a good requisition for this company contains
```

**Run twice on 2026-09-26, and the two runs are the reason this section exists.**
The first produced three delegations to the *same* agent with two different
wordings for the same request, and both child agents then died on a refused
`write_report`. The second produced a clean delegation and one completed child.

That difference is the whole argument for a trace and a fingerprint. The first
run's three children were one piece of work, and nothing in the system could say
so: the objectives differed by a prefix, so the intent hashes differed, and the
audit trail recorded three assignments where there was one. Once the trace
existed, the shape was visible — and the second run was a different shape with the
same model, which is exactly the signal a repetition gate needs and a human
reading summaries does not.

Both are the kind of thing an organisation does repeatedly with slightly different
words, and both produce something a human signs. That is the entire selection
criterion: **repeats, with the same shape, and produces something a human signs.**

### 10.3 `training-plan`

```
observe:  task_type='coordination' | tools: [delegate_to_agent] then
          [write_report, document_reader] on the child
the procedure:  1. name the owning department   2. read what exists already
                3. draft the schedule   4. write it
teaches:        what a plan needs before it is a plan
```

**Run twice; the first run paid for itself.** The first delegated correctly and then
the child died on `Tool 'write_report' exceeded max retries count of 0` — which
turned out to be the model omitting a required argument, not a tool failure (F52).
A case study is worth more than a unit test precisely because of this: the unit
tests for `write_report` all passed, and the tool worked perfectly when called
directly with the same arguments. Only a live run through a real model surfaced
that the *call* was malformed.

The second run, after the fix:

```
status                     : completed
proposals                  : [delegate_to_agent, write_report, write_report]
delegations from this goal : 1  ->  Program Agent  [accepted]
  L1 Program Agent          completed  'Dưới đây là kế hoạch đào tạo nhân sự quý 3...'
```

The Executive delegated once to the right department, and the department produced
the plan. No refusals, no retries, one piece of work.

### 10.4 `quarterly-review`

```
observe:  task_type='coordination' | tools: [delegate_to_agent, safe_web_search]
          | a department agent may then [write_report, internal_database_query]
the procedure:  1. read the numbers   2. compare against the target
                3. propose actions   4. write it up
teaches:        which numbers a review needs before it can propose anything
```

This is the first case that should *need* `internal_database_query`, and the first
whose child has both a reporting tool and a reading tool. Run 1 found F53: the
failure handler replaced the failure with its own error, so the traceback named a
session bug and the log named an `AttributeError` that appeared nowhere else.

### 10.5 What the four runs already show

Both procedures have now been run once each against the live model. What the runs
established is the prerequisite list, and each item was found by the run rather than
by reading the design:

> **Corrected later.** This section originally concluded that the repetition gate
> "has never opened" because the procedures had not repeated. That was the wrong
> explanation, and the wrong explanation is worse than no explanation, because it
> looked like a property of the model. The gate could not have opened: the
> fingerprint counted repetitions, so runs of the same work never matched. F57.

| Found by running it | Consequence |
|---|---|
| The model invented an agent name (`Procurement`) | The roster is now supplied by the platform, from the tree |
| One turn's token allowance decided whether the model delegated at all | Measured and pinned in `tests/unit/test_turn_allowance.py` |
| 23 turns inside every per-call limit | `max_requests` and `max_tool_calls` on the envelope |
| The platform had no record of any tool call | `tool.invoke` audit rows, ordered — §9 step 2, done |
| `internal_database_query` reported reading and read nothing | F48; it executes now |
| A refused tool killed the run, and refusals serialised as successes | F49; the bridge reads the real field names |
| One request produced three children with two wordings | The trace makes the shape visible; a fingerprint would collapse it |
| A child died on a tool call that never happened | `max_retries` governed validation and execution with one knob; F52 |
| A provider content filter arrived as a 500-word blob | Classified `POLICY_DENIED` with a short message; F51 |
| One nested flush surfaced as a session error | `AuditService.add` stages without flushing; F50 |
| The failure handler's own error replaced the failure | The cause in hand is reported even when the session is gone; F53 |
| The fingerprint counted repetitions, so the gate could never open | Consecutive repeats collapse; F57 |
| A policy-blocked call fingerprinted as a success | `blocked` is read as not-effective; F58 |
| The tool trace was never written, and 951 tests agreed | `add` not `record`, and the mechanism is tested; F56 |

## 11. What is built, and what is not

Steps 1 through 7 of §9 are built. The chain runs end to end:

| Step | What it is | Where |
|---|---|---|
| 1 | The fingerprint: task type, ordered tools, argument keys, refusals | `domain/procedure.py` |
| 2 | The trace: one ordered `tool.invoke` row per call | `application/task_execution.py` |
| 3 | The repetition count, read from the database, excluding the run being recorded | `application/procedures.py` |
| 4 | The proposal shape: frozen, validated, evidence and falsifier mandatory | `domain/learning.py` |
| 5 | The danger scan and quarantine — unconditional, payload never written | `domain/scan.py`, `application/quarantine.py` |
| 6 | The approval packet, and an approval bound to a hash of its exact bytes | `application/approval_packet.py`, `application/proposal_approval.py` |
| 7 | The review job: bounded, idempotent, every skip recorded | `application/procedure_review.py` |
| — | The generator, and the evidence it reads | `application/proposal_generation.py`, `application/trace_evidence.py` |

A proposal is a frozen, validated object that must cite its evidence and state what
would disprove it; it is scanned across every field it carries; a dangerous one is
refused and its payload is never written. If it survives, it is rendered as a **diff
with its evidence and its counter-examples**, hashed, and put in a human's inbox. An
approval covers that hash and nothing else, and the hash is re-verified on the way
out — so a packet edited after approval cannot be published.

**The scan is unconditional.** It used to be a parameter that refused on `None`,
which was fail-closed and still left a caller who could pass a permissive lambda and
get an admission. There is no argument to turn it off now, so there is no state in
which the check is skipped.

**A missing check refuses, and then must stop being passable.** A provider that does
not report which model answered leaves the proposer unidentified, so it is refused
rather than recorded as `unknown` — a default on a security check is a hole with a
comment on it. The same applies to the subject: a tie between two models in the
evidence is a refusal, not a coin flip, because "proposer is not the subject" cannot
be checked against a name the platform chose at random.

**The proposer is shown the counter-examples, not only the runs.** They are derived
from the same evidence, so this is framing rather than new information — and framing
is what decides whether a model concludes there is a lesson. A proposer shown a list
of runs that all look alike will find a pattern in them, because that is what a list
of alike things is for. This is the last point at which a proposal can be declined
*by a model*; everything after it is a gate, and a gate only checks well-formedness.

### 11.1 Three things that were assumed rather than read

Building this found three defects that the test suite could not see, all of them
documented in `FAILED_APPROACHES.md` as F56–F58. They are recorded here because the
shape of them is the point.

**The repetition gate was dead and looked idle.** The fingerprint hashed repetition
counts, so two runs of the same report that searched 18 times and 21 times were two
different procedures. Since the gate counts *identical* fingerprints, and a
repetition count is the one property of a run guaranteed to vary, the gate could
never open. It sat at 1 while the platform did the same work four times running — and
that reads exactly like a system with no lessons to learn. Only *consecutive* repeats
collapse now; going back to search after writing is a different shape of work.

**A method documented as not flushing, which flushed.** F56: a fifteen-line docstring
explaining why the method must not flush, and a call to the method that does. The
fix existed as a sibling function with a docstring naming this caller. It surfaced on
the first live run, and the traceback ended on the *second* error, twelve lines below
the cause.

**An enum read from memory, twice in one day.** The trace vocabulary is
`success`/`failure`/`blocked` with `success` as the column default. One reader checked
for `ok` and marked every call in every run as a refusal; another checked only
`failure` and fingerprinted policy-blocked calls as successes.

**The pattern is five for five now.** F50, F53, F54, F55, F56 — *what does this code
do to the session, the enum, and the data it was handed?* In two of the three new
cases the **comment was right and the code was wrong**, and the suite agreed with the
comment. A docstring states an intention, not a fact about behaviour. The tests that
catch this class assert the *mechanism* — that no flush happened, that a value is in
the known set, that a guard can actually fire — not just the result it was supposed
to produce.

### 11.2 The proposer is a second model, and that is a real constraint

A model may not propose a change to its own behaviour: a subject correcting itself is
an echo of what it already does, and an echo is what the system would have done
anyway. So the proposer has to be a *different* model.

**With one model configured, the loop is structurally closed.** Every proposal would
be the subject judging itself, and the platform refuses those — so the sweep finds
repeats, the generator asks, and the answer is always no. It looks like a system with
nothing to learn, and it is actually a system with one model.

The `proposer` profile therefore exists, and it is the one profile in the catalogue
with **no deterministic candidate and no fallback profile**. A canned proposal is a
lesson no model wrote, stamped with the name of a model that did not produce it,
rendered into a packet, hashed and put in front of a human as a real suggestion — and
a human cannot tell by reading it. Every other profile degrades to a scripted answer
on a provider timeout, which is a nuisance; here it would be a fabrication. The rule
is a test (`tests/unit/test_proposer_profile.py`), not a comment, because a comment is
edited away the first time somebody adds a fallback "just to be safe".

### 11.2b It works, once, on the real thing

`scripts/demo_proposal_loop.py` closes the loop against the live database and the
live model. The run that mattered:

1. Four real runs of "draft a one-line status update" for the Executive Agent, all
   ending `failed`, all calling `safe_web_search` once or three times.
2. The repetition gate **opened** — `e34e5ef5352b`, three occurrences. This is the
   first time it has opened on live traffic, and it could not have before F57.
3. A second model (`nemotron-3-ultra-550b:free`) read the trace and proposed a real
   fix: do not offer web search for a task that needs no external information.
4. The packet rendered as a diff, with four runs of evidence, **no counter-examples**,
   and a warning saying the evidence was one-sided — which is true, and which the
   reviewer has to be told.
5. A human approved it; the approval bound to `2c7e5941…`.
6. The packet was then edited. The platform refused: *"the packet changed after it
   was approved."*

The proposal is real and it is also weak — "add explicit query validation and retry
logic" is a vague patch, and the model said so itself: *"the runs don't contain error
details or query content, so I cannot specify the exact fix."*

**It was being asked to diagnose from symptoms.** See F64: a failed run leaves no
durable record of why it failed, the `task.execute` audit row says `success`, and the
explanation goes to a log line. This is the most useful thing the run found, and it
is not a defect in the proposal loop — it is a defect in the trace the proposal loop
reads.

### 11.3 What is still missing

Everything that would *use* a passing proposal, and everything that would take it
back:

- **Publishing.** An approved packet is approved and verified, and then nothing
  applies it. There is no writer that turns a `ChangeSet` into a live procedure.
- **The falsifier firing.** A proposal states what would show it was wrong. Nothing
  watches for that, so a change that turns out to be harmful is never reverted. This
  is the most important gap: a loop that can change itself and cannot undo the change
  is not a loop, it is a ratchet.
- **A human edit is never stomped**, as Hermes insists. There is no human-editable
  procedure store to protect yet, so the guarantee has nothing to attach to.
- **Nightly versus weekly.** §9 step 7 asks for a nightly review and a weekly proposal
  batch. The job is bounded and idempotent and has no schedule attached; a schedule
  that runs it nightly would work, and the distinction between nightly and weekly has
  not been measured because nothing is being learned often enough to tell.

**Four cautions, all learned the hard way.**

The fingerprint must be over argument *keys*, never values. "Buy 5 laptops" and "buy
6 laptops" are one procedure; a value-sensitive hash calls them two, which is the
failure mode of having no procedures at all, wearing a hash.

The count must come from the database and must exclude the run being recorded. A
count taken from the run's own in-memory outcome is a different number, and a
procedure that counts itself opens the gate one repetition early — which is the
difference between a gate and a formality.

A missing check must **refuse**, and then must stop being *passable*. The first
version of the scan gate took a `scan` argument for exactly this reason and was
still wrong in the way that mattered.

Invisible characters must be written as **codepoints**. The scanner's zero-width
set was first typed as a string literal and came out five characters instead of
seven, missing U+200B — the most common of them — with nothing anywhere to say so.
A literal you cannot see is a check you cannot verify.
