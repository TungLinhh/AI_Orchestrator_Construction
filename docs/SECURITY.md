# Security

The threats below are the ones the platform's design actually addresses. A
security document listing controls the system does not have is worse than none,
so where a control is missing it says so.

**Reviewed**: 2026-09-25

---

## 1. The model

Three identity classes with three credentials, and no path by which one becomes
another.

| Class | Credential | Can it authenticate at the API? | Typical holder |
|---|---|---|---|
| **Human** | Argon2id password → JWT | yes | an operator or an approver |
| **Service** | JWT, non-expiring, service-scoped | yes | the Temporal worker, the relay |
| **Agent** | **no credential at all** | **no** | — |

An agent has no credential. The control plane holds a *service* credential and
acts on an agent's behalf with the authority profile the agent's role carries.
The agent's own output is never trusted as identity data.

That is the single most important security decision in the platform. An agent
with a credential is a compromised-model problem with blast radius equal to the
agent's grants; an agent without one is a compromised-model problem with blast
radius equal to "proposes something". Identity is attached by the control plane
and read from a column, never from the prompt — an agent claiming to be the CEO
gets nothing, because the claim is not read.

**Proved by**: `test_identity_is_never_read_from_the_model_output`,
`test_a_claim_of_being_the_ceo_grants_nothing`.

## 2. Authentication

| Aspect | Decision | Reason |
|---|---|---|
| Hash | Argon2id, library defaults | A deliberately weak KDF is a downgrade an attacker can force |
| Access token TTL | 30 minutes | The revocation window |
| Revocation | `users.token_version` | Invalidates every outstanding token without a per-token denylist, so the TTL is the *only* window |
| Refresh tokens | not implemented | A refresh token is a long-lived credential; the cost/benefit did not justify it at this stage |
| Algorithm | HS256, one configured secret | Asymmetric keys need a JWKS endpoint and a rotation story; neither is needed yet and a half-built JWKS is a worse failure than HS256 |
| Timing | `secrets.compare_digest` | Constant-time comparison |
| Bearer in a query string | refused | Query strings are logged, cached and leaked by `Referer` |
| Service tokens | distinct claim (`typ: service`), cannot mint a human token | A service token must not be usable as a user token |

**Known gap**: the seeded demo CEO has no usable password hash, and access is via
`scripts/issue_token.py`. The Argon2 *verification* path is therefore not
exercised end to end by the test suite. Recorded in `CURRENT_STATE.md`.

## 3. Tenant isolation

Isolation is a property of the **database**, not of query discipline. Three
layers, and each one covers a failure the others do not.

### 3.1 Row-level security

```sql
ALTER TABLE "tasks" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "tasks" FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON "tasks"
  USING      (organization_id = current_setting('app.current_tenant', true))
  WITH CHECK (organization_id = current_setting('app.current_tenant', true));
```

Applied uniformly to **42 of 45 tables**. The three exceptions are named
explicitly in `persistence/rls.py::GLOBAL_TABLES`: `organizations` (the tenant
root itself), `consumer_offsets` (per-consumer bookkeeping) and
`alembic_version`.

Two details that are commonly wrong:

- **`FORCE` is load-bearing.** Without it the table owner bypasses every policy,
  and the owner is the role migrations run as. The policies would sit in the
  schema looking correct while every query ran unfiltered.
- **`WITH CHECK` is the half that is usually omitted.** `USING` filters reads;
  `WITH CHECK` stops a cross-tenant *write*. Both are present.

An unset `app.current_tenant` yields `''`, which matches no `organization_id`, so
an unbound session sees **nothing**. The tempting alternative —
`... OR current_setting(...) = ''` — silently disables isolation for every code
path that forgot to bind a tenant, and does so in a way that looks like a
convenience.

**Proved by**: 12 tests, including `test_no_tenant_guc_means_no_rows`,
`test_cross_tenant_read_by_known_id_is_empty`,
`test_cross_tenant_insert_is_refused`, and
`test_tenant_does_not_leak_between_transactions` (two tenants interleaved on one
pooled connection).

### 3.2 Transaction-scoped binding

```python
async with session.begin():
    await session.execute(text("SET LOCAL app.current_tenant = :org"), {"org": org_id})
```

`SET LOCAL`, not `SET`. A pooled connection retains `SET` for the life of the
connection, so `SET` lets one tenant's identity leak into the next request. The
failure is invisible in a single-tenant test and leaks only under concurrency —
the one condition nobody reproduces locally.

`assert_no_leaked_tenant()` queries `pg_stat_activity` and returns the offending
connections rather than asserting vaguely.

### 3.3 A cross-tenant `UPDATE` does not raise

RLS filters the row out of the scan, so the statement matches nothing and
returns `rowcount = 0` **with success**. Code that checks "did the UPDATE raise?"
rather than "did it affect a row?" will believe a cross-tenant write succeeded.
The tests assert on the data, not on the absence of an exception.

## 4. The application role cannot bypass its own isolation

```sql
CREATE ROLE ao_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT;
```

`assert_app_role_is_least_privilege()` runs at startup and again in `/ready`.
This is a startup *gate*, not a warning: an application connected as the owner
would run every query with isolation off while the policies still looked
correct, and no test written against a fresh database would catch it.

**Proved by**: `test_application_role_cannot_bypass_rls`, asserted against
`pg_roles`, not against a connection that would pass trivially.

## 5. The audit ledger is append-only

```sql
REVOKE UPDATE, DELETE ON audit_logs FROM ao_app;
GRANT   SELECT, INSERT ON audit_logs TO ao_app;
```

Enforced by the grant, not by convention in the application. A compromised agent
can record what it did and cannot erase it.

Identity and order are separate columns: `id` is a prefixed ULID so it can be
referenced from an approval or an event, and `sequence` is a monotonic integer
fed by a dedicated sequence, because two entries in the same millisecond must
still have a defined order and a wall clock does not provide one.

**Proved by**: `test_application_role_cannot_delete_audit_rows`,
`test_timeline_is_ordered_by_sequence_not_the_clock`.

## 6. Authorisation

### Default deny

`authority_profile.grants` is an explicit set. No grant means no access, and a
role that forgets a capability denies it. A wildcard capability does not exist in
the model.

### The autonomy floor is not policy

`apply_autonomy_gate` sits **outside** the rule set. No policy edit can widen it,
and an attempt to configure a lower floor than the coded minimum is refused at
startup. The rules a policy author can edit and the rules they cannot are
different objects, because a policy author is exactly the person an attacker
with write access would be.

### Maker-checker, structurally

`require_separate_approver` compares the requester's id to the approver's and
refuses an agent approver outright, at every autonomy level. Code, not
configuration, because configuration is what an attacker with write access
changes.

**Proved by**: `test_an_agent_cannot_approve`,
`test_requester_cannot_approve_their_own_request`.

### Risk comes from the registry

A tool's risk is read from the tool registry, never from the agent's
`self_assessed_risk`. A miscalibrated model must not be able to authorise itself
out of a gate.

## 7. Approval integrity

An approval is bound to a SHA-256 of exactly what was approved, and the hash is
re-verified **immediately before the side effect** — not when the approval was
granted. Anything that could change the payload in between (a summarising model,
a template expansion, a re-read of external data) is caught there and nowhere
else.

An unanswered approval expires and fails closed. The workflow's wait is bounded;
an approval that nobody answers becomes a decision, not a task that hangs
forever.

**Proved by**: `test_modified_payload_is_refused`, `test_appended_field_is_refused`,
`test_expired_approval_fails_closed`.

## 8. The tool gateway

Seven gates, in an order chosen so a cheap, decisive check runs first. Every
refusal happens **before** the handler.

1. authority — is this agent allowed this tool at all
2. policy — effect class and risk
3. autonomy floor — not editable by any rule
4. run mode — `simulation` refuses anything that leaves the system
5. binding risk ceiling — an operator can restrict a tool for one agent
6. rate limit
7. budget — refused *before* the provider is called
8. argument validation — before the handler

A refused budget is not a saving discovered afterwards; the handler is never
invoked. The tests assert the handler was **not called**, not merely that an
error came back, because a test asserting the error alone passes when the
handler ran and then the effect was rolled back.

### Untrusted tool output

MCP results are **flagged, not filtered**. An injection heuristic produces a
flag that travels with the result, because silently dropping text makes a
legitimate result disappear — and an operator who cannot see the flag is worse
off than one who can.

The frame reader is bounded by the server's declared `max_payload_bytes`. A
server returning 2 MB on one line is refused rather than buffered:
`readline()` would otherwise raise an unhandled `LimitOverrunError`, and raising
the limit would turn an untrusted server into a memory-exhaustion vector.

Retrieved text is wrapped in random delimiters and citation is required, so a
model can be told "this is data, not instructions" in a way it cannot confuse
with its own prompt.

## 9. Secrets

| Rule | Enforcement |
|---|---|
| Secrets are referenced by environment variable *name*, never stored | `mcp_servers.auth_secret_env`, `connectors.secret_refs`, `credentials_metadata.secret_ref` |
| A secret never reaches a log | structlog processor in `telemetry/logging.py`, not at each call site |
| A secret never reaches a trace | OTel span attribute filter, same list |
| A secret never reaches an event | the event serialiser runs the same redaction |
| A secret never reaches an audit row | the audit writer redacts `context` |
| A secret never reaches a task payload or a memory item | checked at write time |
| `/api/v1/system/secrets` reports presence only | no endpoint returns a value |

Redaction is verified by a test that asserts the *value* is absent from the
serialised record, not that the code "looks like" it redacts.

## 10. Input validation at the boundary

Concentrated at the edges, because that is where untyped data enters:

- **HTTP**: Pydantic models on every request body; unknown fields rejected
  rather than ignored, so a typo in a policy field is an error rather than a
  silently ignored instruction.
- **MCP**: JSON-RPC responses validated against a schema; the payload cap above.
- **Calculator**: a restricted AST walk. `__import__('os').system('id')`,
  `(1).__class__` and `9**9**9` are each refused, and each is a test. `eval` is
  never reached.
- **Internal SQL tool**: refuses `INSERT`/`UPDATE`/`DELETE`/`DROP`/`COPY`,
  `pg_read_file`, `set_config`, multiple statements, and any query that does not
  reference `organization_id`. The last is the one that matters: a
  read-only-looking query that omits the tenant column reads every tenant.

The domain layer imports nothing that can touch a database or a socket —
`tests/unit/test_domain_purity.py` asserts it by parsing the AST of every module
in it. If the rules that stop an agent from doing something harmful are the
hardest to test, they will be the least tested.

## 11. Rate limiting

A per-instance sliding window, keyed on the token for an authenticated caller
and on the IP otherwise.

**Limitation, stated rather than hidden**: behind N instances the effective
limit is N×. A limit that silently scales with traffic looks like a control and
is not one. A shared store is not added until the limit is actually hit across
instances, because a Redis dependency that is never the bottleneck is a second
thing to operate for no benefit.

## 12. Data protection

| Control | State |
|---|---|
| Transport encryption (TLS) | **not implemented.** Loopback-only in this pass; a production deployment terminates TLS at the ingress. |
| Encryption at rest for sensitive columns | **not implemented.** `ENCRYPTION_KEY` is generated and validated but **currently protects nothing.** Recorded rather than implied otherwise. |
| Password hashing | Argon2id |
| PII in memory items | classified; the retrieval predicate enforces the ceiling |
| Retention | `MemoryService.purge_expired` is implemented and callable; the operations task calls it |
| Right to erasure | **not implemented.** Agents are `retired`, never deleted, because the audit chain of custody is the product. A right-to-erasure request over agent data needs an explicit purge path that does not exist. Recorded in `ASSUMPTIONS.md` A19. |

## 13. Supply chain

**Not done.** The licence matrix and SBOM are M15. `FAILED_APPROACHES.md` F16
records a live, un-rotated API key in a sibling repository that was also baked
into an image layer — an image layer is immutable, so rotating the key alone
does not remove it from any layer already containing it. That is the argument for
doing the SBOM properly rather than quickly.

## 14. Findings reported to their owners

Not this project's to fix, and not touched:

| # | Finding | Location | Action for the owner |
|---|---|---|---|
| F16 | Live `OPENROUTER_API_KEY`, previously baked into an image layer | `pmo_project_procore/backend/.env:6` | Rotate, then rebuild and re-push any image built from that layer |
| F17 | Real Telegram user id and personal name in ≥9 files, including one documenting the leak as severity-red and leaving it unfixed | `O-Nexus-AI-orchestration-deployment/` | Treat the identifier as compromised; rotate the credential; purge the archive |

Neither value was copied into this repository. Re-publishing the identifier in
documentation would compound the leak rather than document it.
