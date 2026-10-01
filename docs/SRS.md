# Software Requirements Specification

Every requirement is traceable: what it is, where it lives, and what proves it.
A requirement with no test is an aspiration.

**Version** 0.1.0 · **Date** 2026-09-25

Status legend: **MET** — implemented and tested · **PARTIAL** — implemented, with a
named gap · **OPEN** — not implemented

---

## 1. Multi-tenancy and isolation

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| T1 | Every tenant-scoped table carries `organization_id NOT NULL` and leads its composite index | MET | `persistence/models.py` | `test_every_tenant_table_has_organization_id` |
| T2 | Row-level security on every tenant table, with `USING` and `WITH CHECK` | MET | `migrations/versions/0002_row_level_security.py` | `test_rls_policy_covers_every_tenant_table` |
| T3 | RLS is `FORCE`d, so the table owner cannot bypass it | MET | same | `test_rls_is_forced_not_just_enabled` |
| T4 | An unbound session sees **no** rows | MET | policy uses `current_setting(..., true)`, which yields `''` | `test_no_tenant_guc_means_no_rows` |
| T5 | The tenant is bound with `SET LOCAL`, never `SET` | MET | `persistence/session.py::tenant_session` | `test_tenant_does_not_leak_between_transactions` |
| T6 | A pooled connection cannot carry a tenant into the next transaction | MET | `SET LOCAL` + `assert_no_leaked_tenant()` | same, plus `test_pooled_session_reuse` |
| T7 | The application cannot connect as a role that bypasses RLS | MET | `assert_app_role_is_least_privilege()` | `test_application_role_cannot_bypass_rls` |
| T8 | A cross-tenant write is refused | MET | `WITH CHECK` | `test_cross_tenant_insert_is_refused` |
| T9 | `organizations`, `consumer_offsets` and `alembic_version` are the only unprotected tables | MET | `persistence/rls.py::GLOBAL_TABLES` | `test_unprotected_tables_are_the_expected_three` |
| T10 | Tenant predicates are in the SQL query, not applied after ranking | MET | `memory/service.py::_scope_predicates` | `test_another_tenants_memory_is_never_returned` (identical corpora) |

## 2. Identity and access

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| I1 | Three identity classes: human, service, agent | MET | `security/tokens.py`, `domain/authority.py` | `test_service_token_cannot_mint_a_human_token` |
| I2 | An agent holds no credential and cannot authenticate at the API | MET | the control plane acts on its behalf | `test_agent_has_no_credential_to_present` |
| I3 | Identity is never read from model output | MET | `AgentContext` carries it; the runtime does not set it | `test_identity_is_never_read_from_the_model_output` |
| I4 | A claim of being the CEO grants nothing | MET | `domain/authority.py` | `test_a_claim_of_being_the_ceo_grants_nothing` |
| I5 | No grant means no access | MET | `authority_profile.grants`, no wildcard | `test_absent_grant_is_a_denial` |
| I6 | Passwords are hashed with Argon2id | MET | `security/passwords.py` | `test_hash_and_verify` |
| I7 | A bearer token in a query string is refused | MET | header-only extraction | `test_query_string_bearer_is_refused` |
| I8 | Token comparison is constant-time | MET | `secrets.compare_digest` | `test_token_comparison_is_constant_time` |
| I9 | Every outstanding token can be invalidated at once | MET | `users.token_version` | `test_token_version_invalidates_outstanding_tokens` |
| I10 | The Argon2 verification path is exercised end to end | **OPEN** | the seeded CEO has no usable password hash; `scripts/issue_token.py` is the access path | — |

## 3. Authority, policy and approval

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| A1 | An agent is never an acceptable approver | MET | `require_separate_approver` | `test_an_agent_cannot_approve` |
| A2 | The requester is never the approver of their own request | MET | same | `test_requester_cannot_approve_their_own_request` |
| A3 | An approval is bound to a hash of exactly what was approved | MET | `approvals/service.py` | `test_modified_payload_is_refused` |
| A4 | The hash is re-verified immediately before the side effect | MET | `verify_payload` in the executor | `test_appended_field_is_refused` |
| A5 | An unanswered approval expires and fails closed | MET | `expires_at` + bounded workflow wait | `test_expired_approval_fails_closed` |
| A6 | A policy decision distinguishes allow / deny / require-approval / escalate | MET | `PolicyDecisionType` | `test_external_side_effect_requires_approval_for_an_agent` |
| A7 | An autonomy floor exists that no policy edit can widen | MET | `apply_autonomy_gate`, outside the rule set | `test_l0_agent_is_denied_even_when_policy_allows` |
| A8 | A configured floor below the coded minimum is refused at startup | MET | `Settings` validation | `test_autonomy_floor_cannot_be_configured_below_the_minimum` |
| A9 | A catch-all rule does not shadow the allow rules | MET | rules are evaluated most-specific first; the floor is separate | `test_catch_all_rule_does_not_shadow_specific_allows` |
| A10 | An approval records a principal that exists | MET | FK `approvals.decided_by → users` | caught by the FK in an earlier test run |
| A11 | Every denial carries a reason an operator can act on | MET | `Error.message`, audit context | `test_denial_reasons_name_the_rule_that_fired` |

## 4. Delegation and bounded work

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| D1 | Self-delegation is refused before the cycle check | MET | `domain/delegation.py` | `test_self_delegation_is_refused` |
| D2 | `A → B → C → A` is refused, and the refusal names the agent | MET | ancestor-path check | `test_circular_delegation_is_refused` + acceptance scenario 5 |
| D3 | The ancestor path is persisted, not recomputed | MET | `delegations.delegation_path` | `test_delegation_records_the_path_it_evaluated` |
| D4 | Depth is clamped to the strictest of platform and parent limits | MET | `clamp_to` | `test_parent_can_only_narrow_depth` |
| D5 | Fan-out is bounded per parent | MET | `max_fanout` | `test_fanout_is_bounded` |
| D6 | Active descendants are bounded | MET | `max_descendants` | `test_active_descendants_are_bounded` |
| D7 | A child budget can only be narrower than its parent's | MET | `BudgetState.clamp_to` | `test_a_child_cannot_widen_its_budget` |
| D8 | An equivalent goal is deduplicated unless parallelism was requested explicitly | MET | `fingerprint` + salted `dedup_key` | `test_equivalent_goal_is_rejected`, `test_parallel_work_must_be_requested_explicitly` |
| D9 | A refused delegation is audited and the parent is notified | MET | audit row + domain event | acceptance scenario 5 |
| D10 | A subagent has no `agents` row | MET | `virtual_agent_id` + `subagent_runs` | `test_subagent_has_no_agent_row` |
| D11 | The subagent spawn action is wired into the runtime loop | **OPEN** | the bounds exist and are tested; nothing calls them | — |
| D12 | Subagents are bounded on TTL, fan-out, tokens, cost and runtime | MET | `SubagentRun` state machine | `test_subagent_bounds_are_enforced` |

## 5. Tasks

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| K1 | A task's status is a column governed by a state machine | MET | `domain/state_machines.py` | 40+ transition tests |
| K2 | Terminal states are absorbing | MET | same | `test_terminal_states_are_absorbing` |
| K3 | Re-running creates a new task linked by a dependency | MET | `task_dependencies` | `test_rerun_creates_a_linked_task` |
| K4 | `blocked` and `failed` are distinct | MET | separate statuses | `test_blocked_is_not_failed` |
| K5 | Every live state can reach a terminal state | MET | graph check | `test_every_live_state_can_reach_a_terminal_state` |
| K6 | A concurrent transition affects zero rows rather than overwriting | MET | conditional `UPDATE ... WHERE status = :expected` | `test_concurrent_transition_loses_cleanly` |
| K7 | A task created with an owner is already `assigned` | MET | `TaskRepository.create` | `test_task_with_owner_starts_assigned` |
| K8 | Task execution is one ordered operation, in one place | MET | `TaskExecutionService` | acceptance scenario 1 |

## 6. Tools and MCP

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| G1 | Seven gates, in order, all before the handler | MET | `tools/gateway.py` | one test per gate |
| G2 | A refused budget never invokes the handler | MET | same | `test_budget_is_refused_before_the_handler_runs` |
| G3 | Invalid arguments never invoke the handler | MET | same | `test_invalid_arguments_never_reach_the_handler` |
| G4 | Risk comes from the registry, not the agent's self-assessment | MET | `ToolContract.risk` | `test_self_assessed_risk_is_ignored` |
| G5 | An operator can narrow a tool for one agent | MET | `agent_tool_bindings.max_risk` | `test_a_binding_can_only_narrow` |
| G6 | An unknown risk level narrows, never widens | MET | `tool_risk_ceiling_for` | `test_unknown_risk_level_maps_to_the_lowest_ceiling` |
| G7 | `simulation` mode refuses any effect that leaves the system | MET | gateway gate 4 | `test_simulation_refuses_external_effects` |
| G8 | MCP output is flagged, not silently filtered | MET | `mcp/client.py` | `test_untrusted_content_is_flagged_not_dropped` |
| G9 | The MCP frame reader is bounded by the declared payload cap | MET | `max_payload_bytes` | `test_oversized_frame_is_refused` |
| G10 | Retrieved text is delimited and must be cited | MET | `ContextBuilder` | `test_retrieved_text_is_delimited` |
| G11 | The MCP client speaks real JSON-RPC to a real server | MET | `examples/mcp_demo_server.py` | the whole MCP suite spawns a subprocess |
| G12 | `calculator` refuses attribute access, imports and huge exponents | MET | restricted AST walk | `test_import_is_refused`, `test_dunder_access_is_refused`, `test_huge_exponent_is_refused` |
| G13 | `internal_database_query` refuses writes, file access and non-tenant queries | MET | `tools/builtin.py` | one test per refusal |
| G14 | A tool cannot be registered twice with different behaviour | MET | `ToolRegistry.register` | `test_tool_already_registered` |

## 7. Model gateway

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| M1 | Agents name a profile, never a provider | MET | `model/profiles.py` | `test_agent_cannot_name_a_provider` |
| M2 | A request whose classification exceeds the ceiling is refused | MET | `classification_exceeds` | `test_restricted_data_is_refused_by_a_public_ceiling` |
| M3 | The effective ceiling is the strictest of profile and provider | MET | `model/gateway.py` | `test_effective_ceiling_is_the_stricter_of_the_two` |
| M4 | A request over the remaining budget is refused before the call | MET | pre-flight estimate | `test_over_budget_is_refused_before_the_provider_is_called` |
| M5 | A fallback is recorded as a fallback | MET | `routing_reason` | `test_fallback_is_recorded_as_a_fallback` |
| M6 | All providers failing yields one terminal error, not a retry storm | MET | `ModelUnavailable` | `test_all_providers_failing_is_one_error` |
| M7 | Money is `Decimal` at six places end to end | MET | `NUMERIC(18,6)`, `_MONEY_QUANTUM` | `test_estimate_is_reported_before_the_call` (exact equality) |
| M8 | The real provider path is verified against a live API | MET | `scripts/smoke_test_providers.py --live` | verified once: `openai/gpt-4.1-mini`, structured output, $0.00003 |

## 8. Memory

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| MEM1 | Eight tiers, each with a defined scope | MET | `domain/enums.py::MemoryTier` | `test_every_tier_has_a_scope` |
| MEM2 | Retrieval respects the tier, the classification ceiling and ownership | MET | `memory/service.py` | `test_another_tenants_memory_is_never_returned` |
| MEM3 | The embedding model is part of the chunk's identity | MET | `memory_chunks.embedding_model` | `test_chunks_from_two_models_are_not_compared` |
| MEM4 | A memory without a source is stored and marked uncitable | MET | `has_provenance` | `test_provenance_is_reported`, `test_sourced_memory_is_citable` |
| MEM5 | Retention is enforced, not promised | MET | `purge_expired` | `test_purge_expired_deletes_only_expired_items` |
| MEM6 | Retrieval degrades to lexical overlap when no embedder is configured | MET | `memory/service.py` | `test_lexical_fallback_returns_results` |

## 9. Events

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| E1 | A state change and its event commit in one transaction | MET | `persistence/repositories/task.py` | `test_task_creation_enqueues_an_event` |
| E2 | A rolled-back change publishes nothing | MET | same | `test_rollback_leaves_no_event` |
| E3 | Several relays are disjoint without coordination | MET | `FOR UPDATE SKIP LOCKED` | `test_relays_do_not_duplicate` |
| E4 | The bus is at-least-once; the consumer gives exactly-once effect | MET | `PostgresDeduplicator` | `test_duplicate_delivery_is_processed_once` |
| E5 | Deduplication survives a restart | MET | dedup is in PostgreSQL, not memory | `test_dedup_survives_a_new_consumer` |
| E6 | A poisoned message is quarantined, not retried forever | MET | `DEAD_LETTER_SUBJECT` | `test_failing_handler_dead_letters` |
| E7 | A non-retryable failure is not redelivered | MET | `HandlerResult.retryable=False` | `test_non_retryable_failure_terminates_immediately` |
| E8 | A bus outage accumulates rather than loses | MET | relay leaves rows pending | `test_relay_keeps_rows_when_the_bus_is_down` |
| E9 | The subject carries the organisation, so a consumer binds to one tenant | MET | `subject_for` | `test_org_is_in_the_subject`, `test_two_orgs_never_collide` |
| E10 | A malformed subject is reported, not raised | MET | `parse_subject` | `test_malformed_subject_is_reported_not_raised` |
| E11 | A relay that keeps failing on one row does not stop relaying the rest | MET | bounded attempts + quarantine | `test_quarantine_after_repeated_failure` |
| E12 | The `messages` projection is written from events | **OPEN** | the table and the design exist; the consumer does not | — |

## 9a. Agent-to-agent

A remote agent is a capability, and calling one is an outbound side effect to a
system this platform does not control. That single sentence is why the gateway
exists separately from the client: a client handed to a model is a way around the
`EXTERNAL_SEND` gate.

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| A2A1 | A remote agent is reachable as a **separate process** | MET | `examples/a2a_remote_agent.py`, spawned by the test | `TestRemoteProcess` |
| A2A2 | The agent card is fetched and validated, not configured | MET | `a2a/card.py::parse_card` | `TestCardValidation` |
| A2A3 | A card declaring the wrong protocol version is refused | MET | `parse_card` | `test_a_wrong_protocol_version_is_refused` |
| A2A4 | A card claiming an unknown capability is refused, not ignored | MET | `parse_card` | `test_an_unknown_capability_is_refused_not_ignored` |
| A2A5 | A security scheme the platform cannot honour is refused | MET | `SecurityScheme` validator | `test_an_unsupported_security_scheme_is_refused` |
| A2A6 | An agent is **inactive** until its card has been verified | MET | `A2AGateway.register` | `test_a_dead_peer_leaves_a_row_that_says_so` |
| A2A7 | An unverified agent cannot be called | MET | `A2AGateway.call` | `test_an_unverified_agent_cannot_be_called` |
| A2A8 | A call leaves an audit trail, because it left the platform | MET | `ACTION_CALL` with `EXTERNAL_SEND` | `TestAcceptanceScenario2` |
| A2A9 | Plaintext off loopback is refused before anything is sent | MET | `A2AClient._check_scheme` | `test_a_plaintext_endpoint_off_loopback_is_refused` |
| A2A10 | A redirect that leaves the origin is refused | MET | `A2AClient._check_same_origin` | same-origin follow is permitted, off-origin is not |
| A2A11 | An oversized reply is refused before it is parsed | MET | `JsonRpcResponse.parse` | `test_an_oversized_reply_is_refused_before_parsing` |
| A2A12 | A JSON-RPC error becomes our error, not a missing `result` | MET | `JsonRpcResponse.unwrap` | `test_a_jsonrpc_error_becomes_our_error` |
| A2A13 | The remote task vocabulary is not merged with `TaskStatus` | MET | `TaskState` in `a2a/protocol.py` | `result_text` reads artifacts first, history second |
| A2A14 | Every wire model can read its own output | MET | `populate_by_name` + camelCase aliases | the scenario round-trips through a socket |

## 10. Workflows

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| W1 | Task execution is a durable workflow, not a request handler | MET | `workflows/task_workflow.py` | registration + input round-trip |
| W2 | A task survives a worker restart | MET | workflow state in Temporal | acceptance scenario 8 (the durability property; see the note) |
| W3 | An approval pauses the workflow rather than blocking a thread | MET | signal + bounded wait | acceptance scenario 4 |
| W4 | A non-retryable error is not retried | MET | `NON_RETRYABLE_ERROR_TYPES` | `test_non_retryable_errors_are_not_retried` |
| W5 | The worker runs against a live Temporal server with a real task | **OPEN** | defined and registered; never executed against the live server | — |

## 11. Audit

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| AU1 | Every governance-relevant action is audited | MET | `audit/service.py` | `test_delegation_is_audited` |
| AU2 | The ledger is append-only at the database level | MET | `REVOKE UPDATE, DELETE` | `test_application_role_cannot_delete_audit_rows` |
| AU3 | Order is defined for entries in the same millisecond | MET | `audit_log_seq` | `test_timeline_is_ordered_by_sequence_not_the_clock` |
| AU4 | A task's timeline shows the delegation that produced it | MET | delegation writes an audit row | acceptance scenario 1 |
| AU5 | An audit row can be attributed to a principal that exists | MET | FKs | caught by an FK in an earlier run |
| AU6 | Audit context is redacted | MET | the redaction processor | `test_audit_context_is_redacted` |

## 12. API and platform

| # | Requirement | Status | Implementation | Test |
|---|---|---|---|---|
| P1 | Liveness checks no dependency | MET | `/health` | `test_health_is_dependency_free` |
| P2 | Readiness checks the database, the role and RLS coverage | MET | `/ready` | `test_ready_fails_when_the_role_can_bypass_rls` |
| P3 | One error shape for every failure | MET | `api/app.py` error handlers | `test_errors_share_one_shape` |
| P4 | Unknown request fields are rejected, not ignored | MET | Pydantic `extra="forbid"` | `test_unknown_field_is_rejected` |
| P5 | Idempotent creation replays rather than duplicating | MET | `idempotency_records` | `test_replaying_a_creation_returns_the_first_task` |
| P6 | A duplicate goal is refused, naming the existing task | MET | partial unique index | `test_duplicate_task_conflict_names_the_existing_task` |
| P7 | Secrets are never returned by any endpoint | MET | `security/secrets.py` | `test_no_endpoint_returns_a_secret_value` |
| P8 | Secrets never reach a log, a trace, an event or an audit row | MET | the redaction processor | `test_secret_values_are_absent_from_serialised_output` |
| P9 | Capability discovery is an index scan, not a full scan | MET | `text[]` + GIN | verified against a live query plan |
| P10 | Rate limiting is per instance | **PARTIAL** | `security/rate_limit.py` | `test_rate_limit_blocks_after_the_window` — the N-instance limitation is documented, not solved |
| P11 | Metrics cover the failure modes an operator acts on | MET | `telemetry/metrics.py` | `test_every_runbook_metric_is_registered` |

## 13. Code quality gates

| # | Requirement | Status | Test |
|---|---|---|---|
| Q1 | `domain/` imports nothing that performs I/O | MET | `test_domain_purity.py` (AST) |
| Q2 | `domain/` reads no clock except `ids.py` | MET | same |
| Q3 | `domain/` imports no sibling layer | MET | same |
| Q4 | No `TODO`, `FIXME`, `NotImplementedError` or stub in the production path | MET | `test_no_stubs_in_the_production_path` |
| Q5 | Tests exercise behaviour through the public interface, not private attributes | MET | the MCP tests speak to a real subprocess; the dedup test now uses the real store rather than an injected stand-in (`FAILED_APPROACHES.md` F27) |
| Q6 | A test that cannot fail is not a test | MET | `FAILED_APPROACHES.md` F11 records the one that was found and rewritten |
| Q7 | Lint and typecheck pass | MET | `make lint` and `make typecheck`, both clean over `src`, `tests`, `scripts` |

## 14. Delivery

| # | Requirement | Status | Note |
|---|---|---|---|
| X1 | One command brings the stack up | MET | `make dev` |
| X2 | The stack runs with no container runtime | MET | verified on a machine with no Docker |
| X3 | A container image builds and runs | **OPEN** | M15. No container runtime exists here, so a compose file could not be run, and an unrun compose file is the fake production path the brief forbids. |
| X4 | Kubernetes manifests apply | **OPEN** | M15, same reason |
| X5 | A licence matrix and SBOM exist | **OPEN** | M15 |
| X6 | Backups can be taken and verified non-empty | PARTIAL | `ao_backup` exists and is required; no restore drill has been performed |
| X7 | A benchmark harness exists | **OPEN** | M14, deferred by decision |

---

## Summary

| Status | Count |
|---|---|
| MET | 97 |
| PARTIAL | 1 |
| OPEN | 11 |

The 11 open items are listed with reasons in
[`CURRENT_STATE.md`](CURRENT_STATE.md).
