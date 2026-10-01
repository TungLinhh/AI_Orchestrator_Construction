# Reusable Components

What was taken from elsewhere, what it became here, and where it is proved.

The bar for inclusion: a component had to solve a problem this platform also has,
and the adaptation had to be testable. "It looked relevant" was not enough.

---

## From `pmo_project_procore`

### 1. Three-layer row-level security

**Source**: `backend/drizzle/9999b_tenant_rls.sql`, `9999c_fix_rls_hatch.sql`
**Became**: `persistence/rls.py`, migration `0002_row_level_security.py`
**Change**: added the `ao_backup` role with `BYPASSRLS`, because `pg_dump` cannot
read rows that `FORCE ROW LEVEL SECURITY` hides. Without it, a backup of this
platform would silently contain zero rows for every tenant.

The policy is one predicate, applied uniformly:

```sql
USING      (organization_id = current_setting('app.current_tenant', true))
WITH CHECK (organization_id = current_setting('app.current_tenant', true))
```

The `WITH CHECK` clause is the half that is usually omitted and the half that
matters: `USING` filters reads, `WITH CHECK` stops a cross-tenant *write*.

**Proved by**: `tests/integration/test_tenant_isolation.py` — 12 tests including
unbound-session, cross-tenant read by known id, cross-tenant insert, and pooled
connection reuse.

### 2. Least-privilege application role with `ALTER DEFAULT PRIVILEGES`

**Source**: `backend/drizzle/9999h_app_role.sql`
**Became**: migration `0002`, `Settings.app_db_user`, `assert_app_role_is_least_privilege()`
**Change**: the application role is now the *default*, and connecting as the
owner is a configuration error that fails startup. The source project made
least-privilege available; making it mandatory is the improvement.

**Proved by**: `test_application_role_cannot_bypass_rls`,
`test_application_role_cannot_delete_audit_rows`, and the `/ready` endpoint's
`least_privilege_role` check.

### 3. Approval payload fingerprinting

**Source**: `ai_drafts.payload.fingerprint` in the HITL inbox
**Became**: `approvals/service.py::ApprovalRequest.payload_hash` and
`verify_payload`
**Change**: the hash is verified *immediately before the side effect*, not when
the approval was granted. Anything that could change the payload in between — a
summarising model, a template expansion, a re-read of external data — makes that
the only place the check can catch it.

**Proved by**: `test_modified_payload_is_refused`, `test_appended_field_is_refused`.

### 4. Maker-checker separation

**Source**: implicit in the inbox's role model
**Became**: `domain/authority.py::require_separate_approver`
**Change**: made structural. An agent is never an acceptable approver at any
autonomy level, and the requester can never approve their own request. It is
code rather than configuration, because configuration is precisely what an
attacker with write access would change.

**Proved by**: `test_an_agent_cannot_approve`, `test_requester_cannot_approve_their_own_request`.

### 5. Permission-first retrieval

**Source**: `backend/src/lib/ai/retrieval.js`
**Became**: `memory/service.py::_scope_predicates`
**Change**: the predicates were placed in the SQL query rather than applied after
ranking, and a fallback was added when no embedder is configured (lexical
overlap, never "return everything").

**Proved by**: `test_another_tenants_memory_is_never_returned`, which seeds two
tenants with *byte-identical* content so that a post-ranking filter fails.

### 6. Secret references, never secret values

**Source**: `ai_provider_configs.api_key_env` — stores the environment variable
*name*
**Became**: `mcp_servers.auth_secret_env`, `connectors.secret_refs`,
`credentials_metadata.secret_ref`
**Change**: `security/secrets.py` adds a `SecretProvider` protocol and a
redaction processor that runs on every log record, so a developer who adds a
field does not have to remember.

**Proved by**: `redact()` tests, and `/api/v1/system/secrets` which reports
presence only.

### 7. Backfill-friendly partial unique indexes

**Source**: the idempotency indexes on payments
**Became**: `tasks` partial unique index on `(organization_id, dedup_key) WHERE
status NOT IN (terminal states)`, and `policy_versions` on `is_current`
**Change**: two columns rather than one. `fingerprint` is the *intent* hash and
never changes; `dedup_key` is what the index applies to, salted when the caller
explicitly asked for parallel work. Keeping them separate means "did these two
agents ask for the same thing?" stays answerable by querying `fingerprint` alone.

**Proved by**: `test_equivalent_goal_is_rejected`, `test_parallel_work_must_be_requested_explicitly`.

### 8. Monotonic ordering for append-only records

**Source**: the audit sequencing in the source project
**Became**: `audit_logs.sequence`, a dedicated `audit_log_seq`
**Change**: identity and order are separate columns. `id` is a prefixed ULID so it
can be referenced from an approval or an event; `sequence` is a monotonic
integer, because two audit entries in the same millisecond must still have a
defined order and a wall clock does not provide one.

**Proved by**: `test_timeline_is_ordered_by_sequence_not_the_clock`.

---

## From `O-Nexus-AI-orchestration-deployment`

### 9. Effect-class taxonomy

**Source**: `o-nexus-hr-agent/config/action-registry.json` — 25 capabilities,
6 effect classes
**Became**: `domain/enums.py::EffectClass`
**Change**: added `DESTRUCTIVE` and `PRIVILEGED`. The two things this platform
most needs to gate — destroying data and acting with elevated privilege — do not
fit the original six, and folding them into `MUTATE_INTERNAL` would make the
single most important gate unexpressible.

**Proved by**: `test_external_side_effect_requires_approval_for_an_agent`,
`test_admitting_a_destructive_tool_registers_it_at_that_risk`.

### 10. The hash-bound action gate

**Source**: `o-nexus-hr-agent/automation/unified/action_gate.py` — 90 lines
**Became**: `domain/authority.py` (~250 lines) and `domain/policy.py`
**Change**: grew three rules the original did not have, each of which is a
documented failure mode elsewhere:
- default deny (no grant means no access, so a role that forgets a capability
  denies it, which is the correct direction);
- identity is never read from the model (an agent claiming to be the CEO gets
  nothing, because the claim is not read);
- `DESTROY`, `DECIDE`, `EXTERNAL_SEND` and `PRIVILEGED` effects always require a
  human regardless of how the policy set is edited.

The last one is split out as `apply_autonomy_gate`, *outside* the rule set, so
no policy edit can widen it.

**Proved by**: `test_l0_agent_is_denied_even_when_policy_allows`,
`test_l0_agent_is_denied_even_when_policy_allows`, and the whole
`test_delegation_safety.py`.

### 11. The three-valued recommendation

**Source**: `GOVERNANCE.md`
**Became**: `PolicyDecisionType` — `ALLOW` / `DENY` / `REQUIRE_APPROVAL` / `ESCALATE`
**Change**: four values, not three, and the reason is the failure this avoids. A
binary pass/fail makes `REQUIRE_APPROVAL` look like `DENY`, and an agent whose
correct action is blocked either bypasses the gate or stops working. Both outcomes
teach the operator the gate is advisory.

**Proved by**: `test_external_side_effect_requires_approval_for_an_agent`
asserts the gate returns *requires approval*, not *denied*.

### 12. The "missing evidence is not automatic failure" rule

**Source**: `GOVERNANCE.md`
**Became**: `memory/service.py::RetrievedMemory.has_provenance`, and the
requirement that a memory without a source is retrievable but marked uncitable.
**Change**: the rule is enforced structurally. Refusing to store an unsourced
memory would push agents to invent a source, which is worse than recording that
they had none.

**Proved by**: `test_provenance_is_reported`, `test_sourced_memory_is_citable`.

---

## Rejected, and why

| Component | Reason |
|---|---|
| Hand-ranked migration ordering (`db/migrate.js:16`) | Silent misordering when a migration forgets its number |
| `db.prepare()` parameter shim | Re-implements the ORM, badly |
| `bql_l1..l5`, `generic_sheets.col_1..col_10` | Deferred modelling; the bug report documents its cost |
| No-ORM + no-TypeScript | Defensible there, not transferable |
| `drizzle.config.js` pointing at a missing file | A tool config nobody exercised is a broken tool config |
| O-Nexus's Bash controller | It worked, but for a task that is not this one. The determinism is worth copying; the architecture is not |
| O-Nexus's system prompt | "Orchestrator as prompt" is the specific failure this project exists to avoid |
| Any identifier, name or endpoint from O-Nexus | Personal data in a public backup archive. See `FAILED_APPROACHES.md` |
| The leaked `OPENROUTER_API_KEY` in `pmo_project_procore/backend/.env` | A live credential. Reported to the user; never copied |
