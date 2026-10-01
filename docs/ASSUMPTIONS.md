# Assumptions

Every assumption the build rests on, and what happens if it is wrong. Written
during the work rather than reconstructed afterwards, so it reflects what was
actually assumed at the time.

Each entry is one of:

- **Verified** — checked by running a command.
- **Inferred** — reasoned from evidence, not proven.
- **Deferred** — a decision that was made and could reasonably go the other way.

---

## Environment

| # | Assumption | Status | If wrong |
|---|---|---|---|
| A1 | Python 3.14 is the target, and `StrEnum`, `Self` and the `enum` behaviour used in `domain/enums.py` behave as on 3.14.7 | Verified (`python3 -VV` → 3.14.7) | The enum ordering helpers and the `BrandedId` metaclass hooks would need review. A CPython 3.14 fault encountered during this build is written up in `FAILED_APPROACHES.md` §F-notes on `errors.py`; a different patch release may not reproduce it. |
| A2 | A local PostgreSQL 16 cluster can be created under `.devdata/` and is not a production dependency | Verified — cluster created, 104 tables migrated across 29 revisions, 101 with forced RLS, tenant-isolation tests pass | Nothing in the platform depends on it; `Settings` points at a DSN. |
| A3 | pgvector 0.8.6 must be built from source for PG16, because the Homebrew bottle ships only PG17/18 extensions | Verified — `vector.control` is absent from the PG16 extension directory after install | `scripts/pgctl.py` would need to build it, which it deliberately does not: hiding the mismatch in a bootstrap is worse than surfacing it. |
| A4 | Port 55432 is free; 5433 belongs to another project and must not be touched | Verified — cluster on 55432, 5433 untouched | Port collision only. `AO_POSTGRES_PORT` overrides it. |
| A5 | No container runtime is available, so the dev stack is native processes | Verified — `which docker podman kubectl` all empty | A compose file could have been written, but not run, and an unrun compose file is a fake production path. |
| A6 | NATS 2.15.0 and Temporal CLI 1.9.1 (server 1.32.0) release binaries run on this machine | Verified — both start, NATS JetStream accepts publishes, Temporal's dev server listens | The event bus and workflow engine would need a different local install. |

## Security and identity

| # | Assumption | Status | If wrong |
|---|---|---|---|
| A7 | The application must connect as a least-privilege role, and connecting as the owner is a misconfiguration worth failing startup over | Verified — `assert_app_role_is_least_privilege()` runs in the lifespan and in `/ready` | If an operator genuinely needs the owner role, they must set `AO_APP_DB_USER` to it and accept that RLS is inert. The refusal is loud rather than silent, which is the point. |
| A8 | An unset `app.current_tenant` must match *nothing*, not everything | Verified — `test_no_tenant_guc_means_no_rows` | If migrations or the seed needed an unbound session to write tenant rows, they would break. Both use the owner role explicitly instead. |
| A9 | Human, service and agent are three identity classes with three credentials, and an agent never authenticates at the API | Inferred from the brief; the control plane acts on an agent's behalf with its own credentials | If an agent needed a direct API credential, a fourth class would be needed. Not built, deliberately: it is the design that lets one compromised agent act as another. |
| A10 | A JWT access token valid for 30 minutes is short enough | Inferred | A longer TTL trades revocation latency for fewer logins. The `token_version` column allows invalidating every outstanding token without a per-token denylist, so the TTL is the only window. |
| A11 | Argon2id with library defaults is the right password hash | Inferred | Library defaults are currently reasonable; a hostile-hardware deployment would want a lower memory cost or a higher one. |
| A12 | The seeded demo CEO has **no usable password hash**, and access is via `scripts/issue_token.py` | Verified by design | This means there is no password login path exercised end to end. The token path is exercised; the Argon2 verification path is not. That is a real gap, recorded in `CURRENT_STATE.md`. |

## Domain and governance

| # | Assumption | Status | If wrong |
|---|---|---|---|
| A13 | "Needs approval" is a legitimate outcome, not a failure | Inferred — this is the single most consequential design decision in the platform | If operators treat it as a failure anyway, agents will bypass gates. The four-valued decision and the audit dashboard's per-rule denial counts exist to make the distinction visible. |
| A14 | Similarity ranking is not authorisation, and the tenant predicate must be in the query | Verified — two-tenant identical-corpus test | If HNSW could be trusted never to return a cross-tenant neighbour, post-filtering would be simpler. It cannot be trusted, and the test is the proof rather than the argument. |
| A15 | A memory without a source is uncitable, not forbidden | Inferred from O-Nexus's three-valued evidence rule | Refusing storage would push agents to invent sources. Recorded as a flag on the result instead. |
| A16 | An approval bound to a payload hash closes the replay hole | Verified — `test_modified_payload_is_refused`, `test_appended_field_is_refused` | A hash cannot be forged without the secret, so this is sound. What it cannot do is stop a human approving something they did not read; that is a process control, not a code one. |
| A17 | Delegation depth of 4 and fan-out of 8 are sane defaults for a three-level company | Inferred from the seeded org shape | Too high permits runaway trees; too low blocks legitimate decomposition. They are configuration (`global_max_delegation_depth`), clamped per-role, and the test proves the clamp works regardless of the number. |
| A18 | Retrying a `FAILED` task should create a new task, not reopen the old one | Inferred | Reopening is more convenient and makes the audit trail ambiguous. `Terminal states are absorbing` is asserted for every terminal state and every event. |
| A19 | Agents are `retired`, never deleted | Inferred from the legacy review | A right-to-delete request over agent data would need an explicit purge path, which does not exist. Recorded rather than assumed away. |

## Data modelling

| # | Assumption | Status | If wrong |
|---|---|---|---|
| A20 | Primary keys are prefixed ULIDs, not sequences | Verified — monotonic under same-millisecond minting, which required a counter because naive randomness does not order | Any component that assumed sortable-by-insertion would break. This was caught by a test, not by inspection. |
| A21 | `capabilities` and `required_approver_roles` are `text[]`, not `JSONB` | Verified — `jsonb && jsonb` does not exist | A JSONB list of strings is a modelling error, not a syntax problem. See `FAILED_APPROACHES.md` F6. |
| A22 | Money is `NUMERIC(18,6)` and `Decimal` end to end, not float | Verified — exact-equality test on a three-decimal cost | Float would lose a cent per thousand calls and could answer "am I over budget?" incorrectly. |
| A23 | The audit log needs a monotonic `sequence` separate from its `id` | Verified — ordering test | Using `created_at` gives no defined order for two entries in the same millisecond. |
| A24 | A task with an `owner_agent_id` at creation is already `assigned` | Verified — state machine test | Forcing a separate assign call made the most common API shape two operations, and a caller who forgot produced an unowned task. |

## Operations and delivery

| # | Assumption | Status | If wrong |
|---|---|---|---|
| A25 | Missing telemetry must degrade to a warning, while an unreachable database must refuse startup | Inferred | Conflating them would make observability a prerequisite for availability. The two are handled separately in `api/app.py` and `telemetry/setup.py`. |
| A26 | Rate limiting per instance is acceptable at this scale | Inferred | Behind N instances the effective limit is N×. Documented in `security/rate_limit.py`; a shared store is not worth adding until the limit is actually hit across instances. |
| A27 | A hash-based embedding is the right default for tests and CI | Verified — reproducible, offline, zero-cost | It is not semantic: paraphrases score poorly and different topics with the same words score highly. That is why it is the *test* default and why the real provider is selected for anything else. |
| A28 | The seeded organisation's departments are generic and carry no business logic | Deferred by decision | An operator with real departments supplies their own. The seed is a shape to exercise the hierarchy, not a claim about how a company is run. |
| A29 | Tests use the deterministic provider; only the M13 E2E run uses a real one | Verified — 463 tests, zero cost | A test suite that could silently spend money is a test suite that eventually does. The provider is chosen by `AO_MODEL_PROVIDER_DEFAULT`, and the test environment forces it. |
| A30 | A2A and the real PydanticAI adapter are the two largest remaining gaps, and both are honestly recorded as unbuilt | Verified — `CURRENT_STATE.md` | If they are in fact required for the MVP, the scope of this pass was wrong. They are called out rather than papered over, which is the only thing that makes the rest of the status list trustworthy. |
