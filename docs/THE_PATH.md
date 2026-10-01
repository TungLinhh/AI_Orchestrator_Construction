# The path to the end product

Written 2026-09-27, after migrations `0012`–`0014`. Every "done" below is a thing
that can be *run* and checked, not a thing that can be described as finished.

This corrects the plan in two places, both because the dossier said something and I
had assumed otherwise. §1 is the correction; §3 is the path that follows from it.

---

## 0. What the end product is, by the dossier's own definition

Tập 1 §1.1 gives five governing principles. They are the acceptance criteria, and
checking them against what exists is the shortest honest description of the gap:

| Principle | State |
|---|---|
| **Governance by Gate** — every critical process passes G0–G5 | **Built.** 6 gates, 45 criteria, state machine, 4 outcomes, 6 autonomy policies |
| **Mandatory RACI** — exactly one Accountable per step | **Built.** `sop_raci` carries the constraint; 14 rows seeded |
| **Segregation of Duties** — proposer ≠ reviewer ≠ approver ≠ executor | **Built** where it can be. `executed_by <> approved_by` on `gate_decisions`; the tenant read tool refuses writes |
| **Single Source of Truth** — one system of record per data type | **Built.** Material master, units dictionary, Procurement sole owner of codes |
| **Document Control** — unified codes, versioning, distribution matrix, expiry | **NOT BUILT.** `documents` is a *store* (hash, size, retention). No code scheme, no version table, no distribution matrix, no expiry |

Plus Tầng 1: *"giao diện hội thoại & dashboard cho từng vai trò (CEO cockpit, PM
workspace, phê duyệt HITL trên mobile)"*. Not built, and never seen — no browser has
been connected to this session.

So the end product is: **five principles enforced, six role-facing surfaces, eight
agents, 76 domain tables, and every row traceable to a person or a model.**

## 1. The correction: the agent register is not construction-first

Tập 1's table 10 names eight agents with priorities. I had been assuming a
construction order. The dossier's actual order:

| Pri | Agent | Block | Runs on | Model tier |
|---|---|---|---|---|
| **P1** | **HR Agent** | Back Office | `BO-HR-SOP-004/005` | Sonnet |
| **P2** | **Procurement Agent** | Middle Office | `MO-PRC-SOP-005/006` | Opus |
| **P2** | **Knowledge Agent** | PMO, whole company | `PMO-KNW-SOP-006` + library | Haiku/Sonnet, RAG |
| **P3** | Finance Agent | Back Office | `BO-FIN-SOP-001/002/003` | Sonnet |
| **P3** | Project Mgmt Agent | Middle + PMO | `MO-PM-SOP-002/003`, `PMO-REP-SOP-002` | Sonnet |
| **P4** | Design/M&E Agent | Middle Office | `MO-DES-SOP-001` | Opus |
| **P4** | Sales/BD Agent | Front Office | `FO-BD-SOP-001`, `FO-TE-SOP-002` | Sonnet |
| **P4** | QA/QC–HSE Agent | Middle Office | `MO-QA-SOP-007`, `MO-HSE-SOP-008` | **Vision (ONNX/TensorRT) + Sonnet** |

Tập 2 §G.2 says the HR Agent is the *"pilot chuẩn cho mọi agent sau"* — the standard
pilot for every agent after it. **The first agent is HR, and neither of the first two
needs construction data.**

That is not an accident and it is the right shape for this build:

- **HR first** because it needs no domain tables to be useful. The dossier is explicit
  that it is the template; a pilot that needs 76 tables finished before it can run is
  not a pilot.
- **Procurement P2** because the supply chain is **already built** — `rfqs`,
  `rfq_items`, `quotations`, `quotation_items`, `purchase_orders`, `po_items`,
  `goods_receipts`, `receipt_items`, `receipt_checks`, `material_reconciliations`.
  The dossier's P2 agent is backed by the tranche already migrated.
- **Knowledge P2, in parallel** because it is RAG over the document library, and the
  library is **1,540 documents** with two readers already proved against it.

**All 16 SOPs these eight agents run on are already seeded** — verified against
`sop_definitions`, which carries all 28. The governance backbone exists; the agents
do not.

## 2. The gap that binds everything else

**52 domain tables. 0 reachable through the API.** Measured three ways in
`PRODUCT_GAP.md` §2: statically (9 named outside persistence, 6 of those the Gate
spine), at runtime (`gate_operations.py` is mounted on no router), and in the UI
(was 44 KB with zero construction vocabulary; Phase 5 added the three role
surfaces, and it still has not been seen rendered -- see `PRODUCT_GAP.md` §5).

Nothing downstream is possible without closing this. The UI has nothing to show, the
Knowledge Agent has nothing to retrieve, the Procurement Agent has nothing to
propose against, and the ingest readers have proven 34 activities and 13 priced lines
with nowhere to put them.

## 3. The path

Nine phases. Each names its done-criterion, and each is checkable by running it.

### Phase 1 — One vertical slice, proved end to end
**`progress` area: reader → application service → router → table → response.**

Not a plan for six areas. A plan for *one*, because `gate_operations.py` is 339 lines
for six tables and is the only reason the Gate spine is testable against a database.
The pattern has to be right once before it is repeated six times, and the newest area
is the right one to fix it on because its reader is the freshest and the corpus row
count is known.

Done when: a `POST` of a `ProgressRead` writes rows that survive a re-read, refuses
the rows that carry refusals, and is reachable by `curl`.

### Phase 2 — The pattern applied
Procurement, supply, contracts, commercial, process. Same shape each time.

Done when: all 52 tables have a service and a router, and `docs/PRODUCT_GAP.md` §2
reads *52 reachable* instead of *0*.

### Phase 3 — Document control
The one governing principle with nothing behind it. Document code per Tập 1's
`ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]` scheme, a version table, a distribution matrix, and
an expiry distinct from `documents.retention_until` — retention is when we may delete
it, expiry is when it stops being valid, and conflating them is a records defect.

Done when: a document can be versioned, distributed to a named set, expired, and the
corpus's own files are registered under their real codes.

### Phase 4 — The agent framework
`agent_definitions` with an `autonomy_ceiling` that starts at L1. `shadow_runs`
comparing each agent output to the human's. A kill-switch that is a *recorded state*,
because a kill-switch nobody has tested is a wish. The AI Decision Log joining
`audit_logs`, `approvals`, `gate_decisions` and `claim_events` into one chronology.

This is the dossier's pilot standard and it gates every agent after it, so it comes
before any agent regardless of which agent is first.

Done when: an agent can run at L1, a human approves or rejects, the pair is recorded,
and the drill to stop all agents is a test that passes.

### Phase 5 — P1 and P2: HR, then Procurement and Knowledge
HR first and small, because that is the pilot. Then the two the corpus and the
already-migrated supply chain back.

Done when: the HR agent's shadow agreement rate is *measurable* — which is the only
evidence that the framework works, and which is impossible without Phase 4.

### Phase 6 — Finance tranche and the Finance Agent
7 tables: `invoices`, `invoice_lines`, `three_way_matches`, `budget_lines`,
`commitments`, `actuals`, `payment_certificates`. The 3-way match is
`ONX-BO-FIN-SOP-002` and is the dossier's own SOP, so this is where the platform
stops being a database and starts being a process.

Done when: an invoice, a receipt and a PO produce a match whose variance, reason
code, tolerance decision and supplier rating are all computed rather than typed.

### Phase 7 — Tầng 1: CEO cockpit, PM workspace, mobile HITL
Three surfaces, not twelve — the dossier names three, and the "12 workspaces" figure
in this repository's own notes was never traced to the dossier. Conversational
interface plus dashboards per role; HITL approval on mobile is the one that has to
work on a phone.

Done when: someone can run a Gate, approve a payment, and see the portfolio on a
phone. **And it has been looked at** — no surface ships unverified, which is the one
thing this repository has been consistently bad at.

### Phase 8 — P3 and P4 agents, and the remaining tables
Finance and PM agents (P3), then Design/M&E, Sales/BD and QA/QC–HSE (P4). Twelve
assurance tables and the form catalogue.

**Blocked on data, deliberately last.** Zero permit documents, no filled inspection
record, no tender packages, no supplier qualification files. Building those tables
now would repeat the exact mistake this document exists to prevent: 24 tables that
cannot be validated by anything.

### Phase 9 — Shadow mode to autonomy elevation
The governance gate on everything above. Tập 1 §5.5: four weeks at ≥95% agreement
before L3. Quarterly AI Governance Board review; acceptance ≥90% is the dossier's own
bar.

Done when: no agent is above L1 without four weeks of recorded agreement behind it,
and that claim is a query rather than a belief.

## 4. What I will not do, and why

**Not build tables the corpus cannot validate.** Twelve assurance tables, the form
catalogue, and the tender tables would be ~20 unmeasurable tables. The corpus survey
found 1,540 documents and 221 workbooks; the ones that fill tables are the ones being
built.

**Not skip straight to agents.** Tập 2 §G.2's pilot standard is shadow mode with a
measured agreement rate. An agent without it is a demo.

**Not call anything done that has not been run.** The UI has never been seen. The 3-way
match cannot be measured. Supplier due diligence has zero source files. These are
stated in `PRODUCT_GAP.md` §5 and stay stated until they change.

---

**The immediate queue is in [`PLAN_NEXT.md`](PLAN_NEXT.md)**, and the state of the work is in [`PROGRESS.md`](PROGRESS.md). This file is the whole path; those two are where it currently stands.
