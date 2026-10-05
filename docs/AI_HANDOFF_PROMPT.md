# Handoff prompt — finish the end product

Copy everything below the line into a new AI session working in this repo.
It contains the codebase map, the rules, what is done (measured), what is left,
and the definition of done.

---

You are taking over the build of an **autonomous AI organisation** in
`/home/vutun/ai_orchestrator` (git repo, `main` branch, Linux, Python 3.14,
Postgres, NATS JetStream, Temporal, single-file web console, no bundler).

## 1. The ultimate goal

A company of AI agents (1 chief Executive, 3 offices, 7 departments: Sales,
Procurement, QA/QC-HSE, Design, Finance, HR, IT) that **finishes assigned work
without a human most of the time**, plus a console a **non-technical chairman**
uses to give work in plain words (Vietnamese or English), watch it happen live,
read answers as sentences (never JSON), and make approval decisions in one tap.
Human-in-the-loop exists only for exceptions (DOA bands, irreversible/external
acts, failed escalations) — the main path must not need a person.

 Сейчас the console is bilingual EN–VI (toggle, EN default, dynamic data never
translated). Chairman works on port **8100** (`make serve`, real model);
port **8099** (`make page`) is the dev/verify server on a scripted fake.

## 2. Commands (the whole onboarding)

| Need | Command |
|---|---|
| Everything before dev | `make setup` (venv + cluster + migrate + seed) |
| Start NATS + Temporal | `make dev-nats dev-temporal` |
| Chairman server (REAL model) | `make serve` → http://127.0.0.1:8100/api/v1/ui?org=ORG (stop: `make serve-stop`) |
| Dev/verify server (FAKE) | `make page` → port 8099 (stop: `make page-stop`) |
| Temporal worker | `.venv/bin/python -m ai_orchestrator.worker` (background it) |
| Unit + integration | `make test` (~15–40 min, background it) |
| Lint + format check | `make lint` (`ruff format --check` is inside it) |
| Types | `make typecheck` |
| Lint → typecheck → test | `make check` |
| E2E (needs NATS + Temporal) | `make test-e2e` |
| Single unit test | `uv run pytest tests/unit/<file> -q` |
| Regenerate ORM from schema | `make model-sync` (must stay idempotent; `--verify`) |

Tenant used everywhere: `org_01m3ycsehgwz7v1fk2ahswkwhm`.

## 3. Gotchas that will bite you (each cost hours before)

- **Postgres is on port 55432, not 5432.** 5433 belongs to another project, never
  touch it. `uv run python scripts/pgctl.py {start,stop,status}`. The machine
  reboots often — when nothing connects, the cluster is simply down; start it.
- **After any reboot, restart in order:** postgres → `make dev-nats
  dev-temporal` → worker → `make serve`. APIs die with the machine; runs in
  flight die with the API (they become `infrastructure` failures — retryable).
- **Never `pkill -f` a pattern that appears in your own command line** — it
  self-matches and kills your own shell. Use bracket patterns (`[r]un_pipeline`).
- **Migrations and the test schema are separate.** `make migrate` (dev DB) vs
  `make migrate-test` (test DB `ai_orchestrator_test`). One without the other
  shows up as `UndefinedColumnError` in a passing test.
- **`make reset-test-db` wipes the reference catalogue too** — afterwards use
  `make test-fresh`, or `test_agent_register_seeded.py` skips everything.
- **Integration tests connect as `ao_app` (NOBYPASSRLS), never owner.** Use the
  `db` fixture, not `admin_db`, unless testing privileges themselves.
- **`tests/unit/test_hiring_process.py` is marked integration** and needs the
  seeded catalogue + running cluster (fails with `TimeoutError` otherwise).
- **The ORM is generated: edit the migration, then `make model-sync`.** Never
  hand-edit generated model code. Never reorder `scripts/sync_model.sh` steps.
- **`ruff format --check` is part of `make lint`.** A clean `ruff check` ≠ lint passing.
- **`make page` kills and restarts the 8099 API.** Never run it while live runs
  execute — it murders them. It also sweeps + seeds fixture data (scripted, fake).
- **Free-model quota resets ~00:00 UTC daily and is consumed within hours.**
  Schedule live-model runs accordingly; `make test` forces fake providers.

## 4. Invariants the suite enforces (read before touching these)

- **`domain/` is pure.** No I/O, no clock, no sibling-layer imports (AST test).
  Business rules go there, not in `application/`.
- **Constraint names:** `CheckConstraint` carries the *suffix*
  (`name="status_known"` → `ck_table_status_known`); `UniqueConstraint` carries
  the *full* name. Composing the prefix twice broke four times.
- **Identifiers must fit Postgres's 63 bytes** (else truncated downgrade breaks).
- **Router registration order in `api/app.py` is load-bearing** (Starlette
  matches in order; `/x/stats` above `/x/{id}`).
- **Schema and ORM must agree** (`test_schema_matches_models.py` via Alembic
  `compare_metadata`; new table modules must be imported there).
- **A failure must be reported, not swallowed** (F18–F30: consumers consuming
  nothing, relays reporting `published 0`, probes healthy with no stream).
- **A thing only in the DOM is not on the screen** (F287: the task detail wrote
  into a hidden section while all content checks stayed green — assert
  `hidden`, not just content).
- **A check that reports the harness instead of the page is a false alarm**
  (F281: TDZ reads, shim nodes without `style` — guard, don't assume).

## 5. Configuration

Every knob resolves once in `config/settings.py`; nothing else reads
`os.environ`. Precedence: default → `.secrets/runtime.env` → environment.
`model_provider_default` is `"fake"` unless `AO_MODEL_PROVIDER_DEFAULT=openrouter`
(`make serve` sets it). Tests force fake + hash providers session-wide.

## 6. Layout worth knowing

- `web/index.html` (~150KB, served by `api/stream.py`) — the whole console, no
  bundler. `scripts/verify_page.mjs` (~100 checks) executes its JS in Node
  against the live API and asserts rendered output. placeholders `__ORG_ID__`,
  `__AUTH_OFF__`, `__BUILD__`, `__PROVIDER__` are substituted per request.
- `application/task_execution.py` — the real execution path (UI Run, Temporal
  activity, fleet all call `execute_task`).
- `application/task_reaper.py` — three sweep steps (terminal-state executions,
  stale running executions >6h, abandoned unclaimed) + `reap()`; wired into
  `scripts/sweep_stranded.py`, run by every `make page`.
- `src/ai_orchestrator/web/index.html` JS: `tr(key, fallback)` i18n — NEVER name
  a task variable `t` in a scope that calls `tr/t` (F285: `.map((t) => …)`
  shadowed the helper and blanked two lists while tiles stayed green).
- `docs/CURRENT_STATE.md` (dated authority), `docs/FAILED_APPROACHES.md`
  (F1–F288 decision record — read before changing anything), `docs/ACCEPTANCE_STANDARD.md`
  (exit-code definition of done), `docs/AUTONOMOUS_ORGANISATION_PLAN.md` (§1
  closed, §1b open, §§3–5 backlog).

## 7. Rules of engagement (from the owner — they stay active until lifted)

- Research and plan **before** implementing anything; never stop for
  budget/reasoning limits — finish the work.
- **Never claim models are "not reliable enough"** — state only what was measured.
- Verify every solution by execution (run code, run tests, run the work).
  Evidence before synthesis: inspect files yourself; trust runs over comments.
- Fix root causes, not symptoms. Disagree with the owner when the evidence does.
- Be short and concise. No emojis. Reference code as `file_path:line_number`.
- No auth, no integrations, no mobile. Tenancy via `x-organization-id` stays real.
- Communicate in Vietnamese; console stays bilingual (EN default, verified).
- **Every completion report ends with the link that opens exactly what finished**
  (task/màn hình liên quan), e.g.
  `http://127.0.0.1:8100/api/v1/ui?org=org_01m3ycsehgwz7v1fk2ahswkwhm#/give/<task_id>`.

## 8. Done, measured (latest commits on `main`)

- 3-tier delegation chief→office→department on a live free model; per-department
  completion with full contract keys (Finance 32/32, Procurement 47/47, IT 9/9,
  QA 6/6, Sales 4/4, Design 4/4, HR 4/4); zero partial completions anywhere.
- Output-contract repair before failing (`repair_from_text`), delegation ceilings
  refuse once per run, rerun briefs carry verbatim required keys; contract keys
  are identifiers (never translated); required tool params sorted first (F280).
- Console: 3 screens + task detail with Answer card; live workflow tree (cards on
  a rail, kind-colored feed, 10s register refresh); provider pill on both Run
  controls; `--resume` drains interrupted roots; sweep converges stranded work.
- Gates at last push: `make page` 105/105, `make lint` clean, `make typecheck`
  clean (147 files), targeted integration 38/38 + 15/15.

## 9. Left to finish, in order

1. **Queue draining (§1b):** per-goal distinct-intent cap + concurrent dispatch;
   done when a run ends `completed >= assigned` without hitting the clock.
2. **HITL exception classes** (routine / DOA-band / irreversible / failed
   escalation) + measured person-free runs over the scenario corpus.
3. **Revert-before-publish** for the learning loop (publish without revert is a ratchet).
4. **Deployment target + 4-week shadow precondition** (≥95% agreement; cannot be shortened).
5. Re-run the scenario corpus on the live model after the repair/closed-ceiling/
   rerun-keys fixes and publish the new per-department numbers.

## 10. Definition of done

`make lint` → `make typecheck` → `make test` (+ `make test-e2e` when touching
events/delegation/approvals/A2A) → `make page` exits 0 with all checks green →
commit → push `main`. Every number reported is a command's output; anything
unverified is labelled unverified. Then report concisely **with the link**.
