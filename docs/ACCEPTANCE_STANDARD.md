# Acceptance standard

What "finished" means for this product, written down before the work so that the
work can be measured against it rather than against whoever last ran a command.

Every criterion below is a command with an exit code. None of them is a claim about
the code, and none of them is a feeling about a screenshot.

---

## The standard

### A. The gate is green, and the numbers are current

```bash
make lint        # exit 0
make typecheck   # exit 0
make test        # exit 0   (unit + integration)
make test-e2e    # exit 0   (needs NATS + Temporal; preflight must pass)
```

A criterion is met when it was run in the last pass, not when it was once true.

### B. A person can start it and see the organisation

```bash
make setup && make page
```

* the console opens and the API answers `/health`;
* the tree shows **three tiers**: 1 chief, 3 offices, 7 departments;
* **every department box is nested inside its office**, and every office inside the
  chief — asserted structurally, not by counting boxes;
* clicking an office opens that office's own panel; clicking a department opens the
  department's;
* no user-visible text states a number the data does not support (no "six" when there
  are seven).

### C. The organisation does the work, unattended

```bash
AO_MODEL_PROVIDER_DEFAULT=openrouter \
  uv run python scripts/run_pipeline.py --org "$ORG" --key expense-policy
```

* the chief **delegates** — at least one `delegation.applied`;
* the department's answer is **judged against its contract**, and a non-conforming
  answer produces a rerun carrying the finding *and* the substance of the previous
  attempt;
* the run reaches a **terminal** state: `completed` or `failed`. A run that stops with
  "nothing is runnable" is a failure, not a conclusion;
* if it ends `failed`, the root's `last_error` names the reason and the reason is
  **the real one** — not a symptom.

### D. A claim in the README is a measured number

Every number in the README is either a command's output or is labelled as a threshold.
"Works" is not a number; "3037 passed, 3 skipped" is.

### E. Nothing is claimed that is not measured

Anything unverified is written down as unverified. A fix that has not been shown to
change an outcome is described as removing an ambiguity, not as fixing a defect.

---

## What is explicitly *not* in the standard

* **"The model is good enough to run unattended."** One model on one procedure is not
  evidence in either direction, and no number here measures it. The review loop is a
  control on the organisation, not a workaround for a weak model.
* **Four weeks of shadow traffic.** The dossier's go-live precondition cannot be
  shortened and is not a code deliverable. The report says how long the record covers.
* **A real client corpus.** The Projects surface is empty without one, by design. Five
  page checks fail for that reason and are reported as failing, not skipped.

---

## The current standing

Measured on 2026-10-02, tenant `org_01m3wmm6vj25zcf25se6kvk0nv`:

| Criterion | State | Evidence |
|---|---|---|
| A. gate green | **met** | `make lint` 319 files formatted · `make typecheck` 144 source files clean · `make test` **3086 passed, 8 skipped, 0 failed** · `make test-e2e` see CURRENT_STATE |
| B. three tiers, nested | **met** | 3 office groups, 7 departments, every one inside the group its `parent_unit_slug` names — asserted in `test_departments_reports_the_tree.py` and again structurally in `verify_page.mjs` |
| B. office + chief have own panels | **met** | every box has `key`/`label`/`unit`; clicking an office opens that office and draws its departments under it |
| B. no hard-coded counts on screen | **met** | the heading, the subtitle and the tile captions are all counted from the payload; "six" is gone from user-visible text |
| C. delegation works | **met** | `delegation.applied` on a real free model, repeatedly |
| C. terminal, truthful outcome | **met** | a run that could not finish ended `failed` with the real reason; the ceiling that stopped it was smaller than the organisation (F241) and is fixed |
| D. numbers current | **met** | every figure in the README is a command's output |
| E. unverified marked | **met** | the free-model run is labelled as a free-model run, not as a capability claim |

**All of the above except the corpus.** 119 console checks pass; the 5 that fail need
`AO_CORPUS_ROOT`, the client spreadsheet corpus that is deliberately not in a public
repository. They are reported as failing, not skipped.

### What would still make this unacceptable

Stated in advance so it cannot be quietly dropped:

* **The console depends on one free model behaving acceptably.** Nothing here measures
  model quality in either direction. A paid model changes what the organisation can
  complete, and that is a purchase decision, not a defect.
* **The dossier's 4 weeks / ≥95% precondition is unmet and cannot be manufactured.**
  Shadow mode records the runs; the record has to grow.
* **No corpus means the Projects surface is empty.** That is five red checks and a
  page with nothing in it.
* **Near-duplicate delegations are not detected** (F244). Exact and concurrent
  duplicates are refused reliably; two objectives that differ only by a suffix are
  treated as different work, and one measured run accepted five delegations where
  three were visibly the same task. No fix is proposed because the obvious one also
  merges work that must stay apart, and that trade has not been measured.
* **One gate still depends on a provider.** `make test-e2e` and the acceptance
  scenarios use `fake`, and the console checks use the live API. The demo-script gate
  was depending on a free model's latency and was measured timing out at 900s against
  66.90s after `--depth 0` (F246), so it no longer does — but any future gate that
  executes a real model inherits the same defect, and nothing prevents it.

---

## Why this file exists

Because "nghiệm thu được" is a claim, and every failure in this project's history has
the same shape: something reported a confident answer that was not the situation. A
seed that said `units 7` over a tenant holding ten. A review that said a department
"produced nothing" when it had written 6.7k tokens. A page that said "all six
departments" over seven.

Each of those was invisible to a green suite, because the suite was not asserting the
property that mattered. A standard made of exit codes and named artefacts is the
cheapest thing available against that, and it only works if it is written down *before*
the work and read *after* it.
