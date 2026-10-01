# Architecture Decisions

> **This file was destroyed and rebuilt on 2026-09-30.** The original recorded
> where the O-Nexus dossier and this implementation disagree, which of them won,
> and why — and it was overwritten by mistake by an agent that had not read it
> first. It could not be recovered: this repository is not under version control.
>
> What follows is a **reconstruction from evidence that survives elsewhere** — the
> dossier (`docs/O-Nexus_Playbook_Trien_Khai_Chi_Tiet.docx`),
> `docs/ASSUMPTIONS.md`, `docs/LEGACY_SYSTEMS_REVIEW.md` and
> `docs/FAILED_APPROACHES.md`. Every row below is checked against the code or the
> dossier, and the verification is named. **The original wording, and any decision
> that left no trace in those four sources, is gone.** Decisions recorded only in
> prose are not reconstructable and are not invented here.

`docs/CONSTRUCTION_DOMAIN.md` points here for exactly this purpose: where the
dossier and the implementation disagree.

---

## 1. Where the dossier and this build disagree

| # | The dossier says | This build | Which won, and why |
|---|---|---|---|
| D1 | The orchestrator is **stateless, Go/TypeScript** (`G.2`: "dựng skeleton Orchestrator (stateless, Go/TypeScript)") | Python, and **stateful by design** — the task row *is* the state | **The implementation, and the dossier is wrong about statelessness here.** A hand-off that is a row survives a crash; a message does not. See §2. |
| D2 | Per-function **OpenClaw Gateway instances** (HR/Fin separate) | No gateway. Outbound calls go through `ToolGateway` with `EffectClass.EXTERNAL_SEND` | **Deferred, not decided.** Integrations were excluded by the brief, so there is nothing to put behind a gateway yet. The *gate* exists; the gateway does not. |
| D3 | **28 SOPs**, of which one is written out in full (`B.1`, `ONX-FO-BD-SOP-001`) | 28 rows in `sop_definitions`; **one** has executable machinery behind it (`ONX-BO-HR-SOP-004`, 8 stages) | **Partial.** The catalogue is seeded (verified: `SELECT count(*) FROM sop_definitions` → 28); the machinery is not. The other 27 are rows, not processes. |
| D4 | **45 criteria** across 6 Gates, a DOA matrix, autonomy policies | 45 in `gate_criteria` (verified), 6 in `autonomy_policies` (verified); **no DOA table** | **Partial.** `role_doa` does not exist (verified by query). Approval thresholds are not modelled as money limits. |
| D5 | HITL levels **L1–L4** per step, with L1 = human decides | `AutonomyLevel` L0–L4 (`L0_SUGGEST`…`L4_BOUNDED_AUTONOMOUS`) | **The implementation, deliberately off by one.** The dossier's L1 is a human; ours is `L1_LOW_RISK_AUTONOMOUS`. L0 was added because "no autonomy" has to be expressible and the dossier has no name for it. |
| D6 | **Shadow-mode 4 weeks**, ≥95% agreement, weekly human/machine reconciliation before go-live | Not built. `ApprovalService` records decisions; nothing compares them | **Not attempted.** It needs 4 weeks of real traffic. Stated rather than implied. |
| D7 | Kill-switch, drilled quarterly (`G.4`) | `agents.kill_switch` exists (verified) with `/agents/{id}/kill` and `/revive` | **The implementation, partially.** The switch exists and is tested; the quarterly drill does not. |
| D8 | Front/Middle/Back Office with a specific department list (BO-FIN, BO-HR, BO-LEG, BO-IT, FO-BD, MO-DES, MO-PM, PMO-*, …) | Six: Sales, Procurement, QA/QC-HSE, Design, Finance, HR | **The brief, which overrode the dossier.** The owner named the six. Two of them — **HR and Procurement** — were dropped during a restructure and not reported until the owner asked why the IT office had no work (F213). |
| D9 | Every agent calls Odoo, MISA, a tender portal, SharePoint | `call_a2a_agent` and the MCP client exist; **no product integration is built or attempted** | **The brief.** Integrations were excluded by decision. The A2A boundary is real and tested (`tests/integration/test_a2a_is_reachable_from_a_task.py`, 8 tests, a real peer process over a socket). |
| D10 | "Agent Dossier" per agent: scope, tools, model tier, token budget, **output quality criteria** | The first five exist; output quality criteria are new | **Extended.** The quality criteria became `domain/review.py` — a deterministic rule rather than a document, because a criterion nobody checks is a comment in a column. See §3. |

## 2. Why delegation is a row, and when it is not

The dossier is silent on the transport. This is the decision, and it is the one
most likely to be questioned from the outside.

**Both, on either side of one line: who shares the database.**

Inside the line — two agents in this company — a row is strictly stronger than a
message, because it has three properties a message cannot have:

| | a task row | an A2A message |
|---|---|---|
| survives a crash mid-handoff | yes, committed with its delegation | no, in flight |
| can be queried afterwards | yes | no, a transcript at best |
| can be reviewed, budgeted, retried, audited | yes, it *is* the record | no, a reply once |
| atomic with what it records | yes, one transaction | no, a remote side effect |

Outside the line — a peer that cannot see this database — a row is not even
possible. `A2AAgent` has no `agents` row, no org unit, no role, no autonomy level
and no budget, so it cannot be written into `tasks.owner_agent_id`. That is not an
omission: such a peer cannot be held to an output contract, cannot be reviewed by
an office, cannot be given a task that survives a restart, and cannot be
governed. Dressing it up as an agent would produce a department that can never be
held to anything. Asserted in
`test_a2a_is_reachable_from_a_task.py::test_a_peer_is_not_a_delegate_target`.

The boundary is enforced, not documented: `call_a2a_agent` is the only outbound
path and is `EXTERNAL_SIDE_EFFECT`, which the run-mode gate refuses under
`SIMULATION`; a registration is `is_active=False` until a real card fetch verifies
it; the reply comes back `untrusted: True`; and `is_idempotent=False`, because the
peer is outside our transaction.

**The gap this filled:** `register` took a *name* and the lookup took an *id*,
with nothing joining them, so an agent could only call a peer whose id it already
held — the one thing it cannot know. `A2AGateway.resolve` now does that lookup,
tenant-scoped, active-and-verified only.

## 3. The middle tier is a manager, and that needed a rule

The dossier's "output quality criteria" (D10) is the requirement that produced the
largest change. A chain ran top to bottom and whatever a department wrote reached
the executive: a run declared `approved_headcount` and produced
`{"scripted": true, "proposal_count": 0}`, and a scripted runtime's `[draft] <key>`
output was marked `completed`.

`domain/review.py` holds the criteria as **pure functions** — promised keys
present, not placeholders, substantive — with the attempt bound in the same
decision, because a manager who rejects forever is not managing either. Only after
those pass is a judgement worth asking a model for. Three bugs came out of
building it and are recorded as F218–F222.

## 4. Open, and named

- The **DOA matrix** (D4) is the largest missing piece of the dossier's governance
  spine. Approval thresholds exist as `AutonomyLevel`, not as money limits.
- **Shadow-mode** (D6) is the precondition the dossier sets for go-live and this
  build has not met it.
- 26 of the 28 SOPs (D3) are catalogue rows.
- The free model tier is currently **exhausted for the day**
  (`free-models-per-day-high-balance`), so no real-model run is possible until the
  daily reset. Measured directly against the API, on every free model.
