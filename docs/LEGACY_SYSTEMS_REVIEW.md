# Legacy Systems Review

Two sibling projects were read in full. This document records what each one got
right, what it got wrong, and the specific consequence for this build. It is
written so that a reader who disagrees can check the claim.

---

## 1. `pmo_project_procore`

A Node/Express/Postgres system in production use. Its value is not its code — it
is that it has *already made the mistakes this project is about to make*, and
several of them are documented by its own authors.

### What is worth taking

**The three-layer row-level security pattern.** `backend/drizzle/9999b_tenant_rls.sql`
plus `9999c_fix_rls_hatch.sql`. The layers are:

1. a policy predicate comparing `organization_id` to `app.current_tenant`;
2. `FORCE ROW LEVEL SECURITY`, without which the table owner bypasses every
   policy — and the owner is the role migrations run as;
3. a fail-closed hatch: `app_tenant_unset()` and `app_current_tenant()` helper
   functions that make the unset case explicit.

What `9999c` also does that matters more than the policy text: it *documents
that an unset GUC passes every policy*. That is the honest limitation of the
whole approach, and a project that hid it would have shipped a data leak with a
clean-looking schema. This project takes the pattern and states the limitation in
the same place — `persistence/rls.py` and the test
`test_no_tenant_guc_means_no_rows`.

**Least-privilege application role.** `9999h_app_role.sql` creates a role that
holds DML and not ownership, with `ALTER DEFAULT PRIVILEGES` so tables created
later are covered, and a grant-audit test that fails closed. Adopted as-is, with
one addition: this project also creates `ao_backup` with `BYPASSRLS`, because
`pg_dump` cannot read rows that `FORCE ROW LEVEL SECURITY` hides. That trick
comes from `deploy/production/00-backup-role.sh`.

**The approval inbox.** `ai_drafts.payload{before, after, fingerprint,
lifecycle}` is a human-in-the-loop draft table, and the `fingerprint` field is the
idea this project took furthest: an approval is bound to a hash of exactly what
was approved, and that hash is re-verified immediately before the side effect.
Without it, an approval obtained for a ten-word email can be replayed against a
ten-thousand-word one. `approvals/service.py` implements this and
`test_modified_payload_is_refused` proves it.

**Permission-first retrieval.** `backend/src/lib/ai/retrieval.js` applies the ACL
filter *before* cosine ranking, not after. This is correct and non-obvious:
filtering after ranking means a chunk from another tenant can win the ordering
and then be discarded, but only after its content has influenced the result set,
and with an approximate index a tight similarity budget can return a neighbour
from outside the tenant. `memory/service.py` puts the predicates in the query,
and `test_another_tenants_memory_is_never_returned` seeds two tenants with
*identical* text precisely to catch the post-filter version.

**A prompt-injection fence.** `backend/src/lib/ai/routes/ai-assistant.js` wraps
retrieved content in random-UUID delimiters and requires citation. Adopted in
spirit: `mcp/client.py` flags untrusted content and *carries the flag with the
result* rather than silently filtering, because a heuristic that dropped text
would make a legitimate result vanish.

### What is rejected

**Hand-ranked migration ordering.** `backend/db/migrate.js:16` is a one-line
ternary that ranks 43 migration files by a hand-maintained number. The failure
mode is silent: a new migration that forgets a number runs in the wrong order and
either fails confusingly or, worse, succeeds into a wrong state. This project
uses plain Alembic, and `migrations/versions/` is a linear history with a
`down_revision` chain.

**The `db.prepare()` shim.** A bespoke parameter-conversion layer. It exists
because the project is JavaScript and SQL is not. Here the equivalent is
SQLAlchemy, and a bespoke shim would be re-implementing it badly.

**Hardcoded column names.** `bql_l1..l5` and `generic_sheets.col_1..col_10` are
the shape of a schema that deferred modelling. The consequence is visible in
`AGENTS.md`, which documents that `NUMERIC` arrives as a float and that the
`?`-to-`$N` conversion has quirks. This project uses `NUMERIC(18,6)` and typed
Pydantic money, and the test `test_estimate_is_reported_before_the_call` asserts
exact decimal equality.

**No TypeScript and no ORM.** Defensible for that project's constraints, not
transferable. This project is typed and uses SQLAlchemy 2.0 with explicit
`Mapped[...]` columns.

**A `drizzle.config.js` pointing at a file that does not exist.** A broken tool
config that nobody noticed. The lesson is not "avoid drizzle"; it is that a
tool config which is never exercised is a tool config that is broken.

### Its own self-assessment

`AGENTS.md` and `docs/CODEBASE_BUG_AUDIT.md` are unusually honest: the second is
a dated list of twelve unaccepted gaps with `ready=false`. That document is the
most valuable artefact in the repository, because it demonstrates the practice
this project adopts — a list of what is *not* finished, dated, with the reason.
`docs/CURRENT_STATE.md` here does the same job.

---

## 2. `O-Nexus-AI-orchestration-deployment`

A backup archive of an HR-agent project. **Almost none of it is code.**

### What is worth taking

**The effect-class taxonomy.** `o-nexus-hr-agent/config/action-registry.json`
defines 25 capabilities across 6 effect classes: `READ`, `PREPARE`,
`MUTATE_INTERNAL`, `APPROVE`, `DECIDE`, `EXTERNAL_SEND`. This is a better basis
for approval policy than a flat risk score, because it describes the *kind* of
consequence, which is what a rule can reason about. "Risk: 7" is not actionable;
"this leaves the system" is. `domain/enums.py::EffectClass` adopts it and adds
`DESTRUCTIVE` and `PRIVILEGED`, because the two things this platform most needs
to gate — deleting data and acting with elevated privilege — do not fit the
original six.

**`automation/unified/action_gate.py`.** Ninety lines of hash-bound
authorisation: an action's fingerprint, a rule set, and a decision with a reason.
It is the best single reusable artefact in that repository and the model for
`domain/authority.py`.

**The governance rules.** `GOVERNANCE.md` states an authority ceiling, a
three-valued evidence rule ("missing evidence is not automatic failure"), and
anti-bias rules about who may approve what. The three-valued rule is the
important one: a binary pass/fail evaluation makes an agent look competent for
the wrong reason whenever a source is unavailable.

**What actually worked.** The audit's most useful finding is negative: O-Nexus
*passed* its live E2E with a deterministic Bash controller and one LLM call per
item, and *failed* at the thing it was built to demonstrate. The
multi-department orchestrator was seven empty directories; the cross-department
router existed only as a prompt; `AGENTS.md:24` explicitly banned subagents and
A2A.

The lesson generalises, and it is why this project treats delegation, cycle
detection and budget bounds as typed code with tests rather than as prompt
instructions. An orchestrator that exists only in a prompt has already been
built once and it did not work.

### What is rejected

Everything, as code. The repository contains no application code: documentation,
Bash, and a 22-line system prompt. Its `docker-compose.yml` references a
Dockerfile that does not exist. `o-nexus-deploy/` is seven empty directories.

Its `hazard.md` documents thirteen self-reported defects, one of which is
severity-red: a real Telegram user id (`5624438820`) and a real personal name
appear in at least nine files, in a backup archive, unfixed.

**That finding is reported, not reused.** No identifier, name, endpoint or
configuration from those files appears anywhere in this repository. The user who
owns that data needs to rotate and purge it; that is their action, not this
project's, and re-publishing the identifier here would compound the leak.

---

## 3. `pmo_project`

A data and reporting project. Contributes domain vocabulary and the reporting
schemas, and one hard operational constraint: it shares the PostgreSQL host with
`pmo_project_procore`, which is why a second live cluster was found on port 5433
during the capability probe. No code, schema or configuration is reused.

---

## What this changed in the design

| Legacy lesson | Consequence in this codebase |
|---|---|
| Unset GUC passes every RLS policy | `test_no_tenant_guc_means_no_rows` asserts the opposite, and `/ready` reports RLS coverage |
| Owner bypasses RLS | App connects as `ao_app`; `assert_app_role_is_least_privilege()` runs at startup |
| `pg_dump` cannot read `FORCE RLS` tables | Dedicated `ao_backup` role with `BYPASSRLS` |
| Approval can be replayed on a different payload | `payload_hash` bound at creation, verified before the side effect |
| Filter-after-rank leaks across tenants | Tenant predicate in the query; two-tenant identical-corpus test |
| Hand-ranked migrations | Linear Alembic history |
| Orchestrator-as-prompt failed | Delegation graph, cycle detection and budgets are typed code with 20+ tests |
| Twelve unaccepted gaps, dated | `CURRENT_STATE.md` |
