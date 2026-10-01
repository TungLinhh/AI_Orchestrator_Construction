# Product Requirements

## 1. The problem

Organisations want the work of a company — research, analysis, reporting,
coordination — done by AI. What exists is a chat window with a model behind it.
That shape fails for a specific reason: **it has no organisation.**

A chat window has no structure of authority, no durable unit of work, no record of
who decided what, no budget, and no way to say "this must be approved by a human
before it happens". Everything those properties require has to be built, and
building them ad hoc inside prompts produces a system that cannot be governed,
audited or bounded.

The specific failures this project exists to prevent:

| Failure | What it looks like in production |
|---|---|
| Unbounded delegation | A → B → C → A, or A spawning 500 siblings, or a chain that never terminates but keeps spending |
| Untraceable action | "The agent did something" with no record of who authorised it, under which policy |
| Unapprovable action | An agent that can send external mail or delete data with no human in the loop |
| Unbounded spend | An agent loop that runs up a bill before anyone notices |
| LLM as the database | Business state inferred from a conversation transcript, so nothing can be queried, diffed or replayed |
| Framework as the platform | Org structure, task state, policy or auth owned by an agent framework, so swapping the framework means rewriting the business |

## 2. What is being built

A **governed control plane and agent runtime** for a hierarchical AI
organisation. Not a chatbot, and not a framework wrapper: a distributed software
organisation whose workers happen to be AI agents.

The distinction is carried through the code. Six concepts that are routinely
conflated are separate tables with separate lifecycles:

| Concept | What it is | What it is not |
|---|---|---|
| **Organization** | The tenant. The isolation boundary. | A team |
| **OrganizationalUnit** | A node in the company tree | An agent — a department is not its director |
| **Role** | A function, and the authority profile that grants capability | An agent instance |
| **AgentDefinition** | A versioned blueprint: instructions, model profile, allowed children | A running agent |
| **Agent** | An instance in the registry, with lifecycle and operational status | A prompt |
| **SubagentRun** | An ephemeral, task-bound, budget-bounded worker | A permanent identity — it has no `agents` row |
| **Skill** | Reusable instructions, versioned, with a governance state | A tool — skills say how, tools act |
| **Tool** | An executable capability with a side effect | A skill |

An agent can be `active` (a business fact) while `busy` (an operational fact).
Merging them produces contradictions like "agent ready, task blocked" that are
neither true nor false, so the two columns are separate.

## 3. Users and what each needs

| User | Needs | How the platform serves it |
|---|---|---|
| **CEO / executive** | Ask for work, get a result, know it was checked | Submit a task; see the audit timeline and the cost |
| **Department lead** | Delegate to the right agent, stay within authority | Delegation with depth/fan-out/budget clamps and a recorded path |
| **Agent** | Do the work within explicit bounds | A context that already contains its authorised tools, memory scope, budget and deadline |
| **Human approver** | See exactly what is being approved, decide, be sure the decision binds | An inbox; a payload hash verified before execution; maker-checker enforced structurally |
| **Operator** | Know the system is healthy, bounded and not leaking | `/ready` proves RLS is in force; `/metrics`; the governance dashboard |
| **Auditor** | Reconstruct who did what, under which policy | An append-only log ordered by a monotonic sequence |

## 4. Scope

### In

- Hierarchical organisation: tree, roles, agent registry, versioned definitions
- Tasks as the canonical unit of work, with a state machine and a task graph
- Structured delegation with a persisted ancestor path
- Bounded subagents
- Policy engine with a four-valued decision and a non-editable autonomy floor
- Human approvals with payload binding and expiry
- Tool gateway: authority → policy → autonomy → run mode → rate limit → budget → validation
- MCP boundary against untrusted external servers
- Model gateway: profiles, privacy filtering, budget pre-flight, recorded fallback
- Memory: tiers, scoped retrieval, provenance
- Event bus with a transactional outbox, durable consumers and deduplication
- Durable workflows with an approval pause
- Append-only audit, multi-tenant isolation via RLS
- REST API, structured logging, tracing, metrics

### Out, by decision

| Excluded | Why |
|---|---|
| **Docker / Kubernetes** | No container runtime on the build machine, so a compose file could not be run or verified. Shipping one would be exactly the fake production path the brief forbids. `DEPLOYMENT.md` states the deviation. |
| **A chat UI** | The brief's central thesis is that chat is a *projection*, not the source of state. The `messages` table exists for that projection; the projection consumer is not built. `CURRENT_STATE.md`. |
| **Department business logic** | The seed's departments are generic. Encoding how a real company works is an operator's data, not the platform's. |
| **A real embedding model in CI** | A hash embedding is reproducible, offline and free. A paid provider in a test suite is a suite that eventually spends money. |
| **Benchmarks (M14), container packaging, K8s manifests, licence matrix, SBOM (M15)** | Deferred by decision, not forgotten. |

## 5. The non-negotiable constraints

These are the properties that make the thing a platform rather than a demo. Each
is enforced by code and proved by a test, not stated in prose.

1. **Chat is never the source of state.** Business state lives in columns. The
   conversation is a projection of events.
2. **Delegation is bounded on four independent axes** — depth, fan-out, active
   descendants, and budget/time. Plus explicit cycle detection over the ancestor
   path, because depth alone does not catch `A → B → C → A`.
3. **An agent can never approve.** Structural: the check compares the requester's
   id against the approver's and cannot be satisfied by an agent at any autonomy
   level.
4. **Default deny.** No grant means no access. A role that forgets a capability
   denies it.
5. **Identity is never read from the model.** Authority data is attached by the
   control plane. An agent claiming to be the CEO gets nothing, because the claim
   is not read.
6. **Approval is bound to what was approved.** A hash, verified immediately
   before the side effect.
7. **Secrets never reach a log, an event, a trace, an audit row, a task payload or
   a memory item.** Enforced at the log processor, not at each call site.
8. **The framework is replaceable.** `AgentRuntime` is a `Protocol`. A test
   proves the swap changes nothing in the organisation, task, policy,
   authorisation, memory or event contracts.
9. **Tenant isolation is a database property.** RLS enabled *and forced*, and the
   application cannot connect as a role that bypasses it.
10. **The audit log is append-only at the database level.** Not by convention.

## 6. Success criteria

The MVP is done when these are demonstrated by running code, not described:

| # | Scenario | Where it is proved |
|---|---|---|
| 1 | Human → Executive → Department → Specialist → Skill → MCP tool → Result → Parent → Done | `tests/e2e/test_acceptance_scenarios.py::TestScenario1` |
| 2 | Agent → A2A → remote agent as a **separate process** | **Not built.** See `CURRENT_STATE.md`. |
| 3 | Agent → workflow → approval requested → waits → human approves → resumes | `TestScenario4` (approval half) + `TestScenario8` (durability half) |
| 4 | Event → JetStream → durable consumer → one logical effect despite physical redelivery | `TestScenario6` + `tests/e2e/test_event_pipeline.py` |
| 5 | Primary model failure → governed fallback, recorded | `TestScenario7` |
| 6 | Worker killed → workflow survives and resumes | `TestScenario8` |
| 7 | `A → B → C → A` → blocked, audited, parent notified | `TestScenario5` |
| 8 | Duplicate task and duplicate event both deduplicated | `TestScenario6` + `test_task_delegation.py` |

Seven of eight. The one gap is A2A, and it is named in the status document
rather than approximated with a mock.

## 7. Explicitly not claimed

- That the demo company reflects how any real company is organised.
- That the deterministic runtime demonstrates model quality. It demonstrates
  that the *platform* works; model quality is a different question, and one a
  benchmark harness would answer (M14, not built).
- That the hash embedding is semantic. It is not, and `CURRENT_STATE.md` says so.
- That per-instance rate limiting bounds a multi-instance deployment. It does
  not; the limit is N× behind N instances.
- That `ENCRYPTION_KEY` protects anything. It currently protects nothing, and
  that is recorded rather than implied otherwise.
