# Operations

Runbooks, not prose. Each entry is: how to tell it is happening, what it means,
what to do.

**Reviewed**: 2026-09-25

---

## 1. Health

Three endpoints, and the split between them is deliberate.

| Endpoint | Checks dependencies? | Use for |
|---|---|---|
| `GET /health` | **no** | liveness probe, load-balancer check |
| `GET /ready` | **yes** | readiness probe, deployment gate, alerting |
| `GET /metrics` | no | Prometheus |

```console
$ curl -s localhost:8000/ready | jq
{
  "status": "ready",
  "database": { "reachable": true, "least_privilege_role": true, "latency_ms": 1.8 },
  "rls": { "protected_tables": 42, "total_tables": 45, "unprotected": ["organizations", "consumer_offsets", "alembic_version"] },
  "event_bus": { "connected": true, "pending_outbox": 0 },
  "version": "0.1.0"
}
```

A liveness probe that checks the database takes the process out of rotation when
the database is down, and a process that cannot serve anything is not helping.
`/ready` is where the dependency truth lives.

`/ready` returns non-200 when PostgreSQL is unreachable, when the application
role can bypass RLS, or when fewer than 40 tenant tables have a policy. All three
mean the same thing operationally: **do not route traffic here**.

## 2. Logs

Structured JSON via structlog, one object per line, with a redaction processor
that runs on every record. Secrets are redacted by *value* — the processor is
given the resolved secret set, so a developer who adds a field containing a
token does not have to remember to mark it.

```console
$ jq -r 'select(.event=="task.execute") | "\(.task_id) \(.agent_id) \(.definition_version)"' \
    .devdata/logs/api.log
```

Every state-changing operation logs `organization_id`, `actor_id`, `resource_id`
and the version stamps of everything it touched. An operation that cannot be
attributed is not in the log.

`AO_LOG_LEVEL=debug` adds SQL and gateway decisions. Do not run it in
production: the gateway decisions include the tool arguments, and those can
contain customer data that a redaction rule for *secrets* does not cover.

## 3. Traces

OpenTelemetry, OTLP export, off by default (`AO_OTEL_ENABLED=false`). A missing
or unreachable collector degrades to a warning — telemetry is not a
prerequisite for availability, and conflating them makes observability an
outage's first casualty.

Span attributes come from the same allow-list as the log redaction. A span
carrying a secret is a secret in a place nobody greps.

## 4. Metrics

| Metric | Type | Why it is worth alerting on |
|---|---|---|
| `ao_tasks_total{status}` | counter | Work not completing |
| `ao_task_duration_seconds` | histogram | Latency regression |
| `ao_delegations_refused_total{reason}` | counter | **`cycle_detected` spiking means something is looping** |
| `ao_approvals_pending` | gauge | An approval inbox nobody is reading is a pipeline that has stopped |
| `ao_approvals_expired_total` | counter | Expiry means the human path is not staffed |
| `ao_tool_calls_total{tool,outcome}` | counter | `denied` spiking means policy and behaviour diverged |
| `ao_model_cost_usd_total{provider,model}` | counter | The actual bill |
| `ao_model_fallback_total` | counter | Resilience degrading |
| `ao_budget_exceeded_total` | counter | A task asked for more than it was allowed |
| `ao_outbox_pending` | gauge | The bus is behind, or the relay is down |
| `ao_nats_connected` | gauge | 0 = events accumulating |
| `ao_dead_lettered_total{subject}` | counter | **A poisoned message is a bug in a handler** |
| `ao_rls_policy_violations` | counter | Should be structurally impossible; if non-zero, something is wrong |
| `ao_audit_writes_total` | counter | A drop means the chain of custody has a hole |

`ao_delegations_refused_total{reason="cycle_detected"}` is the one to put on a
dashboard. A cycle that was blocked is the system working; a *rate* of cycles
means an agent's decomposition has a bug, and it will keep burning budget until
someone looks.

## 5. Runbooks

### 5.1 The outbox is not draining

**Symptom**: `ao_outbox_pending` climbing; no events on the bus.

```bash
curl -s localhost:8000/ready | jq '.event_bus'
psql … -c "SELECT count(*), max(attempts), max(last_error)
         FROM outbox_events WHERE published_at IS NULL GROUP BY 1"
```

**Decide in this order**:

1. `ao_nats_connected == 0` → the bus is down. The outbox is *supposed* to
   accumulate; this is a degraded control plane, not a lost state. Fix the bus.
2. `attempts > 0` with a `last_error` → a specific row is poisoned. It is
   already quarantined; inspect it and fix the handler.
3. `attempts == 0` and the relay is not running → `make dev` or start
   `python -m ai_orchestrator.events.relay`.
4. Everything looks fine → the relay's own log. `relay.publish_failed` with an
   empty error usually means a schema mismatch between the model and the table.

**Do not** "fix" it by marking rows published. That discards events, and the
outbox is the only durable record that the state change happened.

### 5.2 A task is stuck in `waiting_for_approval`

```sql
SELECT id, task_id, action, created_at, expires_at, required_approver_roles
FROM approvals
WHERE status = 'pending' ORDER BY created_at;
```

This is not an error. It is a human being asked to decide. The workflow's wait
is **bounded**: past `expires_at` the approval fails closed and the task moves
to `failed` with `APPROVAL_EXPIRED`.

Two alerts on this table: pending age over the SLA, and
`ao_approvals_expired_total > 0`. Expiry means the approval path is unstaffed,
and an unstaffed approval path is a path where every external effect is
refused.

### 5.3 A delegation cycle is blocked in a loop

```sql
SELECT source_agent_id, target_agent_id, count(*), max(created_at)
FROM delegations GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10;
```

Cycle detection is refusing correctly. The bug is in whichever agent is
re-delegating, almost always a decomposition prompt that says "coordinate with
the other departments" — the model reads that as "delegate to whoever is
available", and one of them is the parent.

**Do not raise the depth limit.** Depth is not what stops a cycle; the
ancestor-path check is, and it works at any depth. Raising the limit makes the
loop deeper without making it shorter.

### 5.4 A task exceeded its budget

```sql
SELECT task_id, budget_id, reserved_usd, spent_usd, cost_usd, reason
FROM budgets WHERE status = 'released' AND spent_usd > 0 ORDER BY updated_at DESC;
```

The estimate exceeded the remaining budget, so the provider was **not** called —
the refusal is pre-flight, not after the fact. The remedy is a task-level limit
increase or a cheaper model profile, not a retry. A retry with the same
estimate and the same budget will be refused identically, and the ledger records
the attempt either way.

### 5.5 The relay is quarantining messages

```sql
SELECT subject, reason, count(*) FROM outbox_events
WHERE status = 'quarantined' GROUP BY 1,2 ORDER BY 3 DESC;
```

A quarantined event means the serialiser raised on that payload. The usual
cause is a new field on a model that a `NOT NULL` column does not accept.

**Quarantine, do not delete.** The row is the evidence of what the state change
was, and deleting it converts a recoverable bug into an unexplained divergence
between `tasks` and the event stream.

### 5.6 RLS coverage drops

`/ready` reports `protected_tables`. A new table added without a policy appears
in `unprotected` and is **fully readable by any tenant**. That is the failure
this check exists for: a table created without a policy is indistinguishable
from a protected one, and nothing else would tell you.

```bash
uv run python scripts/audit_db.py     # table count, columns, RLS coverage
```

Fix: add the `ENABLE` + `FORCE` + policy triple, then re-run the check.

### 5.7 The application connected as the owner

`/ready` → `least_privilege_role: false`. Startup refuses, so this happens when
`AO_APP_DB_USER` is set to `ao`.

Every RLS policy in the schema is inert while the application runs as the owner,
and nothing in the application will report it. This is the single most dangerous
misconfiguration in the platform, which is why it is a gate and not a log line.

## 6. Maintenance tasks

| Task | Cadence | What it does |
|---|---|---|
| `MemoryService.purge_expired` | daily | Deletes memory items past `retention_until`. Implemented and callable; the operations task calls it. |
| Outbox sweep | hourly | Deletes `published_at` older than 7 days. The table grows without bound otherwise. |
| Dead-letter review | daily | Every quarantined message is a bug in a handler. |
| `audit_db.py` | on schema change | RLS coverage, row counts, index usage. |
| Rotation of `JWT_SECRET` | quarterly | Bump `users.token_version` to invalidate everything. |

## 7. Backup and restore

```bash
# Backup — must use ao_backup; pg_dump as ao_app returns nothing under FORCE RLS
PGPASSWORD=… pg_dump -h 127.0.0.1 -p 55432 -U ao_backup -Fc ai_orchestrator > ao-$(date +%F).dump

# Verify the dump is not empty — this is the check people skip
pg_restore -l ao-$(date +%F).dump | wc -l
pg_restore -l ao-$(date +%F).dump | grep -c 'TABLE DATA'

# Restore
PGPASSWORD=… pg_restore -h 127.0.0.1 -p 55432 -U ao -d ai_orchestrator_restore --no-owner ao-*.dump
```

The `TABLE DATA` count is the real verification. A `pg_dump` run as the
application role produces a well-formed archive containing **zero rows for every
tenant** and no error, because `FORCE ROW LEVEL SECURITY` hides them rather than
refusing them. A restore from that archive looks successful and loses everything.

**Not yet built**: point-in-time recovery, retention policy, or a restore drill.
`ao_backup` is the mechanism; the operational discipline around it is not
documented as a process because it has not been performed.

## 8. Scaling and failure behaviour

| Failure | Behaviour | Correct? |
|---|---|---|
| NATS down | Outbox accumulates, task creation unaffected, `/ready` degraded | yes — a broker outage is not an outage |
| Temporal down | API accepts tasks; the outbox holds the events; execution waits | yes |
| PostgreSQL down | `/ready` fails, no traffic routed, writes refused | yes |
| Model provider down | Fallback chain, `routing_reason=FALLBACK` recorded | yes |
| All providers down | One terminal `ModelUnavailable`, not a retry storm | yes |
| Poisoned event | Quarantined after 5 deliveries, queue keeps moving | yes |
| Approval unanswered | Expires, fails closed | yes |
| Budget exhausted | Refused pre-flight, handler never runs | yes |
| Cycle detected | Refused, audited, parent notified | yes |

The design bias throughout: **degrade and accumulate rather than lose or loop**.
The outbox exists so a bus outage is survivable; the dead-letter exists so one
bad message does not become a queue that has stopped; the bounded approval wait
exists so a human who never answers is a decision rather than a hang.

## 9. Escalation

| Severity | Definition | First action |
|---|---|---|
| **SEV1** | Cross-tenant data exposure, or `least_privilege_role: false` in `/ready` | Stop routing traffic. Treat as a security incident. |
| **SEV2** | PostgreSQL unreachable; audit writes failing | Restore the database; do **not** bypass RLS to keep serving. |
| **SEV3** | Outbox not draining; approvals expiring | Runbook §5.1 / §5.2. |
| **SEV4** | A model provider failing over consistently | Check `ao_model_fallback_total` and the profile's candidate list. |
| **SEV5** | A quarantined event; one dead-lettered subject | Runbook §5.5. |

The one instruction that is absolute: **never** switch the application to the
owner role to work around a permission error. Every symptom that suggests it
will disappear immediately, and the cost is that tenant isolation is off for
every request until someone notices.
