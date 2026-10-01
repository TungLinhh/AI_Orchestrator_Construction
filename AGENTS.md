# AGENTS.md

Read-only reference material lives in `docs/`. `docs/CURRENT_STATE.md` is the dated
authority on what exists; `docs/FAILED_APPROACHES.md` (~200 entries, `F###`) is the
record of what was tried and why it was replaced. Both cite `F###` numbers, so a
comment referencing one is a pointer into a real decision, not folklore.

## Commands

There is no container runtime here. The stack is native processes; `make setup`
(venv + cluster + migrate + seed + `seed-process`) then `make dev` is the whole
onboarding.

| Need | Command |
|---|---|
| Everything before `make dev` | `make setup` |
| Start NATS + Temporal + API (foreground) | `make dev` |
| Unit + integration | `make test` |
| Unit only / integration only | `make test-unit` / `make test-integration` |
| Acceptance scenarios (needs NATS + Temporal) | `make test-e2e` (runs `preflight-e2e` first) |
| Lint **and** `ruff format --check` | `make lint` |
| mypy on `src` | `make typecheck` |
| Lint → typecheck → test | `make check` |
| Open the console UI | `make page` (self-contained: starts the API on 8099, no NATS/Temporal needed) |
| Regenerate ORM `__table_args__` from the schema | `make model-sync` (`--verify`, must be idempotent) |

Single test: `uv run pytest tests/unit/test_delegation_safety.py -q` — nothing else
is needed for unit tests. Integration tests need a live database and a migrated
*test* schema; see the gotchas below.

`make fmt` reformat-then-lint, `make format` is `ruff check --fix` plus format.

## Gotchas that will bite you

- **Postgres is on port 55432, not 5432.** 5433 belongs to another project and is
  never touched. `uv run python scripts/pgctl.py {init,start,stop,bootstrap,status}`.
  There is no `make pgctl` target; `make setup` calls `pgctl.py bootstrap` for
  you.
- **Migrations and the test schema are separate.** `make migrate` prepares the dev
  database; integration tests run against `ai_orchestrator_test` and need
  `make migrate-test`. A migration applied to only one of them shows up as a
  `UndefinedColumnError` in a test that passed a minute ago.
- **`make reset-test-db` truncates the reference catalogue too.** `sop_definitions`,
  `autonomy_policies` and `roles` are tenant-scoped, so after a reset use
  `make test-fresh` (reset + `seed-test-reference` + suite) or
  `test_agent_register_seeded.py` will skip all of its tests.
- **Integration tests connect as `ao_app` (`NOBYPASSRLS`), never the owner.**
  Use the `db` fixture, not `admin_db`, unless the point of the test is the
  privilege configuration itself. Connecting as owner makes every tenant-isolation
  assertion vacuous.
- **`tests/unit` is not all pure.** `tests/unit/test_hiring_process.py` is marked
  `integration` and reads the seeded catalogue; with the cluster stopped it fails
  with a `TimeoutError`, not a skip.
- **The ORM is generated from the schema, not hand-maintained.** Edit the
  migration, then `make model-sync`. `scripts/sync_model.sh` explains why its steps
  run in the order they do; do not reorder them.
- **`ruff format --check` is part of `make lint`.** Formatting is enforced, not
  aspirational. A clean `ruff check` does not mean `make lint` passes.

## Invariants the suite enforces (read before touching these)

- **`domain/` is pure.** No I/O, no clock, no imports from sibling layers. An AST
  test parses every module in `src/ai_orchestrator/domain` and fails the build
  otherwise. Business rules go there, not in `application/`.
- **Constraint names follow a convention with an asymmetry**: `CheckConstraint`
  carries the *suffix* (`name="status_known"` → `ck_table_status_known`), while
  `UniqueConstraint` carries the *full* name. Four separate incidents came from
  composing the prefix twice; `tests/unit/test_constraint_names_match_convention.py`
  is the instrument.
- **Identifiers must fit Postgres's 63 bytes.** Long names come back truncated with
  a hash, which breaks `downgrade`. `test_construction_schema.py::TestIdentifiersFitPostgres`.
- **Router registration order in `api/app.py` is load-bearing.** Starlette matches
  in registration order, so a literal path declared after a parameterised sibling
  is captured by it and 404s forever. Declare `/x/stats` above `/x/{id}`.
- **The schema and the ORM must agree.** `tests/integration/test_schema_matches_models.py`
  runs Alembic's `compare_metadata` in the suite. New table modules must be
  imported there, or the test passes only because another test imported them first.
- **A failure must be reported, not swallowed.** Several tests exist solely because
  code returned a plausible wrong answer (a consumer that consumed nothing, a
  relay reporting `published 0`, a probe reporting a healthy bus with no stream).
  F18–F30.

## Configuration

Every knob is resolved once in `config/settings.py`; nothing else reads
`os.environ`. Precedence: default → `.secrets/runtime.env` → environment. Secrets
in that file are unprefixed (`POSTGRES_PASSWORD`) by design. `reset_settings_cache()`
is required after mutating env in a test or fixture.

`tests/conftest.py` forces `AO_MODEL_PROVIDER_DEFAULT=fake` and
`AO_EMBEDDING_PROVIDER_DEFAULT=hash` session-wide, so the suite cannot spend money
or reach a shared broker. Real-provider calls are a separate, explicit step
(`make smoke`, `make run-fleet`).

## Layout worth knowing

- `web/index.html` is a single ~140 KB page served by `api/stream.py`. It is not a
  build artifact and has no bundler. `make page` executes its JavaScript in Node
  against the live API (`scripts/verify_page.mjs`) and asserts on what rendered.
- `application/task_execution.py` is the real execution path. `make run-fleet`,
  `make demo`, and the Temporal activity all call the same
  `TaskExecutionService.execute_task`.
- `scripts/` holds both operational tools and one-shot repair scripts from past
  migrations. If a name looks historical (`repair_*`, `merge_*`, `dedupe_*`,
  `finish_migration.py`), it probably is — don't wire it into a new path.
- `mcp/` and `tests/integration/test_tools_and_mcp.py` speak real JSON-RPC to
  `examples/mcp_demo_server.py` spawned as a subprocess. Keep it that way; a mock
  hides the handshake, the timeout, the payload cap and the untrusted-content flag.
- `tests/e2e/test_a2a_remote_agent.py` spawns `examples/a2a_remote_agent.py` as a
  separate process and reaches it over a socket.

## Commit-gate order

`make lint` → `make typecheck` → `make test`. Add `make test-e2e` when the change
touches events, delegation, approvals or the A2A boundary. `make check` runs the
first three.
