# Plan: a full autonomous AI organisation

Written 2026-10-03, after the three-tier console, the unit-separation boundary and the
turn-budget change landed. This is the plan for what is **not** built, in the order it
should be built, with the reason each item is first and the thing that would prove it
done.

Every "done" below is a command or an assertion, never a screenshot. Where the evidence
for a claim does not exist yet, this file says so rather than asserting a rate.

---

## Where the organisation actually is

Measured, not described:

| | |
|---|---|
| Shape | 1 chief, 3 offices, 7 departments, `parent_id` correct throughout |
| Routing | `delegation.applied` fires on a real free model |
| Separation | higher tier reads lower in full, peers are status-only, enforced by RLS (0030) |
| Budget | 240 requests and 240 tool calls per task, cost and token ceilings unchanged |
| Console | 3 screens, 82 checks, `make page` exits 0 |
| Suite | `make test` 3130 passed / 8 skipped / 1 deselected / 0 failed |

**And what changed on 2026-10-03**, because a plan that does not record its own wins
is indistinguishable from a plan that is not being followed:

```
supplier-tender, real free model
  delegation.applied                 32          (was 0)
  delegation.refused_by_platform     26          (was 358)
  task.failed                         0          (was 6, all budget_error)
  completed contract-bearing tasks   47
  ... of those, returning every       47         (0 partial, 0 empty)
  ... declared key
```

Five defects were found by running the work and fixed (F262–F265). **The first item
below is therefore closed and the second is open**, which is the opposite of where the
plan started.

---

## 1. Make one goal reach a department and come back `completed` — CLOSED 2026-10-03

**Closed, and the number is above.** Four defects stood between the previous run and
this one, all of them found by running it and none of them found by a test:

| | before | after |
|---|---|---|
| `delegation.applied` | 0 | 32 |
| refusals | 358 | 26 |
| `task.failed` | 6, all `budget_error` | 0 |
| contract keys returned | 0 of 47 | 47 of 47 |

**What is still open is the wall clock**, and it is a different problem: 106 tasks sat
`assigned` when the 55-minute run ended. The organisation dispatches far faster than it
finishes, so the root goal does not settle inside one run. That is §1b.

**Why first.** Every other claim about autonomy is a claim about this. A governance
system that cannot carry one piece of work from a goal to an answer has nothing to
govern yet.

**What is measured now.**

```
supplier-tender   chief delegates 16x, 21 duplicate refusals, 8 fan-out refusals,
                  ends failed / budget_error
offer-approval    chief never delegates, ends failed / budget_error
```

**The four candidate causes, in the order they should be ruled out.** The point of
listing them is that none of them is "raise the limit again" — the limit is already 240
and the runs are not hitting it any more.

1. **The chief over-delegates.** 16 children from one goal, most of them the same work.
   The refusal now says "do not delegate this again, move on", and the near-duplicate
   problem (F244) is still open: two objectives differing by a suffix are different
   work. *Measure:* `delegation.applied` per root, and the ratio of distinct objectives
   to children. *Fix:* a per-parent cap on how many *distinct* intents one coordinator
   may open in one run — bounded by `max_fanout`, which already exists and is already
   correct; what is missing is that near-duplicates do not count against it.
2. **The department cannot answer.** Once a child runs, does it produce the contracted
   keys? *Measure:* one real delegation executed by hand, output inspected. This has not
   been measured on a live model and everything downstream depends on it.
3. **The office review loop cannot converge.** Reruns carry the previous attempt's text
   (F239), but has a rerun ever produced an *accepted* answer on a real model? *Measure:*
   `reviews_accepted > 0` on any real run. It is 0 on every run so far.
4. **The chief's brief does not reach the department in a usable form.** It travels in
   `input` and is carried by `ROUTING_INPUT_KEYS`; whether the department *sees* it as
   instruction or as metadata is unmeasured.

**Done when:** one scenario ends `completed` at the root, with `delegations > 0`,
`reviews_accepted > 0`, and every department in the chain having produced its declared
keys. Until then this plan's later items are unverified.

---

## 1b. The queue is not draining

**Measured 2026-10-03**, on the run that closed §1: `assigned 106, completed 47,
running 4, created 3, failed 15`. The organisation produced work faster than it completed
it, and the root goal did not settle before the run's wall clock.

Three candidate causes, in the order they should be ruled out — and the first is
measurable from data already collected:

1. **Near-duplicate fan-out.** 32 delegations applied for one tender is far more than
   one tender needs. F244 (near-duplicates are not detected) is the likely cause and is
   still open: two objectives differing by a suffix are two pieces of work.
2. **A queue cap that does not exist.** Nothing stops a coordinator opening 32 branches
   when three would do. `max_fanout` is 16 and `max_active_descendants` is 16, so 32
   applied is at least two separate coordinators' worth — but nothing bounds the
   *organisation* per goal.
3. **Throughput.** If each child takes ~90 s of model time, 32 children cannot finish in
   55 minutes on one serial loop. `make run-fleet` exists for this and is not on the
   scenario path.

**Done when:** the counts at the end of a run satisfy `completed >= assigned`, on a run
that ends without hitting the clock.

---

## 2. The seven departments, one by one

**Why now and not before.** The platform proves a chain; it does not prove a *company*.
Each department is a different shape of work and each exposes a different way for the
platform to be wrong about it.

| Department | Owns | What makes it its own test | Measured 2026-10-04, live free model |
|---|---|---|---|
| Procurement | tendering, supplier choice | Ranked criteria with a stated order; a defensible wrong answer to compare against | **47/47** full keys |
| HR | hiring, offers, headcount | A policy with a threshold, where the interesting output is *whether approval is required* | **4/4** keys, both approvals named |
| Finance | policy bands, arithmetic | The only department where the answer is checkable mechanically | **32/32** full keys |
| QA | audit | Must **not** delegate (the auditor independence rule) — a negative test | **6/6** full keys |
| Design | technical risk | Judgment with no arithmetic; the hardest to verify and the most honest test | **4/4** full keys, 1 failed on translated keys (prompt hardened after) |
| Sales | pipeline, complaints | State that changes over time, so the contract needs a shape the others do not | **4/4** full keys |
| IT | access, backup, knowledge register | Holds the two SOPs nothing else could own; the simplest possible smoke test | **9/9** full keys on its first scenario |

**Zero partial completions anywhere**: every completed contract-bearing task in the
window returned every key it declared. The failures observed were queue/throughput
(tasks still `assigned` when the wall clock killed the run — drained afterwards with
`--resume`), not capability.


**IT first, deliberately.** It is the least interesting department and therefore the
cheapest place to find out whether the chain works at all. If the pipeline cannot carry
"grant access to X and record it", it cannot carry a tender, and finding that out on the
simplest case is worth more than finding it out on procurement.

**Done when:** each department has a scenario, a declared output contract, and a run
that ends `completed` — or a written record of why it cannot, with the measurement.

---

## 3. Human in the loop, on the exceptions only

**Why this is third and not first.** The owner said it plainly: the product must finish
work without a person most of the time. HITL is the exception path, and building it first
would make the common path look finished while it is not.

**What exists.** `approvals`, `NEEDS_APPROVAL`, the DOA matrix (F232), shadow mode
(F236). What is missing is the *decision*: which classes of work ever reach a person.

**The proposed split, to be tested rather than assumed:**

| Class | Who decides | Why |
|---|---|---|
| Routine analysis, reporting, drafting | nobody | the department has authority; the contract is the check |
| Anything crossing a DOA band | the named signer | the band exists and is already enforced |
| Irreversible or externally visible | a person | sending an email, spending money, publishing |
| A failed escalation | a person | the organisation has already tried its own budget |

**Done when:** a run that needs no person ends without one, measured over a corpus of
scenarios; and every run that stops for a person says *which* rule sent it there.

---

## 4. Learning, and the ability to undo

`SELF_IMPROACHMENT.md` §9. An approved packet is approved and hash-verified, and then
nothing applies it; a proposal states what would show it was wrong, and nothing watches
for that. **A system that can rewrite its own procedures and cannot revert them is a
ratchet, not a self-improvement loop.** The revert has to exist before the publish, or
the publish removes the only thing that makes it safe.

**Done when:** an approved procedure is applied, and a later run that falsifies its own
stated condition reverts it without a person.

---

## 5. Deployment and the shadow-mode precondition

**Why last, and stated plainly.** `make setup && make page` is the whole product today.
There is no Dockerfile, no compose, no Kubernetes — deliberately, because there is no
container runtime on the machine it was built on and a compose file that has never run
is a claim rather than a deployment.

The dossier's go-live precondition is **4 weeks of parallel running at ≥95% agreement**.
That cannot be manufactured by code and cannot be shortened. Shadow mode records the
comparisons; the report says "not ready, N days of 28" until they exist.

**Done when:** a deployment target runs `make setup && make page`, and the shadow report
says ready — on a clock, not on a decision.

---

## What is deliberately not on this list

- **Authentication and multi-tenancy beyond `x-organization-id`.** The tenant boundary
  is real and enforced by RLS; who sets that header is not this product's problem yet.
- **A second model provider.** One free model is a development convenience. A second
  would prove the gateway routes, which it already does, at a cost this plan does not need.
- **A corpus of client documents.** 26 of the 28 SOPs have no mock corpus, so their runs
  exercise the machinery and not the domain content. Two screens that needed one were
  removed rather than shipped empty (F247).