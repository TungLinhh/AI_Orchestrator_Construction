# AI Orchestrator — local stack
#
# This project runs its local stack as native processes rather than containers:
# the development machine has no Docker daemon, and requiring one would put a
# prerequisite between a developer and `make dev`. See docs/DEPLOYMENT.md for
# the rationale and for the container manifests that exist for production.
#
#   make setup   install deps, create the local Postgres cluster, migrate
#   make dev     start NATS, Temporal, the API and the workers
#   make test    unit + integration against the test schema

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

VENV := .venv
PY := uv run
PGBIN := $(HOME)/.local/share/ao/pg
DEV := .devdata
NATS_BIN := $(DEV)/bin/nats-server
TEMPORAL_BIN := $(DEV)/bin/temporal
RUN := $(DEV)/run
LOGS := $(DEV)/logs
#: `make page` binds this. 8099 rather than 8000 so it cannot collide with a `dev-api`
#: already running, and so nothing else on the machine is assumed to be free.
PAGE_PORT ?= 8099

.DEFAULT_GOAL := help
.PHONY: help install setup migrate migrate-test downgrade seed seed-process dev dev-api dev-worker dev-nats \
        dev-temporal stop status logs test test-unit test-integration test-e2e test-live \
        lint format typecheck check verify-secrets smoke bench clean distclean \
        audit-db audit-outbox verify-page page page-stop seed-construction \
        reset-test-db test-fresh preflight-e2e model-sync seed-test-reference seed-agents seed-docs seed-docs-dry mock-corpus mock-corpus-norun mock-corpus-reset run-fleet fmt seed-free-model demo

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies
	uv sync --all-extras

# ---------------------------------------------------------------- setup ----
setup: install ## Full local setup: cluster, databases, migrations, seed
	@mkdir -p $(RUN) $(LOGS)
	$(PY) scripts/pgctl.py bootstrap
	$(MAKE) migrate
	$(MAKE) seed
	$(MAKE) seed-process
	@echo ""
	@echo "  setup complete. Next:  make dev"
	@echo "  then:                curl localhost:8000/health"

# The six Gates, forty-five criteria, twenty-eight SOPs, the autonomy policies and
# the DOA matrix come from `docs/` — the O-Nexus dossier — and not from code, so
# there is no migration for them and nothing else will ever load them. They are
# seeded here rather than from `seed_process_spine.py` alone because `make setup`
# previously stopped after `make seed`, which left the Gate spine empty: a database
# that looked migrated, reported no drift, and had no Gates in it.
#
# `seed-process` is a separate target so it can be re-run on its own. It is
# idempotent, which is what makes it safe to put in a setup path.
seed-process: ## Seed the Gate spine, SOPs, autonomy policies and DOA matrix from the dossier
	$(PY) scripts/seed_process_spine.py

migrate: ## Apply database migrations
	$(PY) alembic -c migrations/alembic.ini upgrade head

migrate-test: ## Apply database migrations to the *test* schema
	@# The integration suite runs against `ai_orchestrator_test`, which is a
	@# different database from the one `make migrate` prepares. A migration
	@# applied to only one of them leaves the other silently behind, and the
	@# first sign is `UndefinedColumnError` in a test that passed a minute ago.
	AO_MIGRATION_DB=$${AO_TEST_DB_NAME:-ai_orchestrator_test} \
		$(PY) alembic -c migrations/alembic.ini upgrade head

downgrade: ## Roll back the most recent migration
	$(PY) alembic -c migrations/alembic.ini downgrade -1

seed: ## Load the Autonomous Demo Company
	$(PY) -m ai_orchestrator.seed

# ------------------------------------------------------------------ dev ----
dev: dev-nats dev-temporal dev-api ## Start the whole local stack
	@echo ""
	@echo "  API        http://127.0.0.1:8000"
	@echo "  health     http://127.0.0.1:8000/health"
	@echo "  metrics    http://127.0.0.1:8000/metrics"
	@echo "  Temporal UI http://127.0.0.1:8233"
	@echo ""

dev-nats: ## Start NATS JetStream
	@mkdir -p $(RUN) $(LOGS)
	@if [ -f $(RUN)/nats.pid ] && kill -0 $$(cat $(RUN)/nats.pid) 2>/dev/null; then \
	  echo "nats already running ($$(cat $(RUN)/nats.pid))"; \
	else \
	  $(NATS_BIN) -js -sd $(DEV)/nats-data -p 4222 -m 8222 > $(LOGS)/nats.log 2>&1 & \
	  echo $$! > $(RUN)/nats.pid; sleep 1; echo "nats started ($$(cat $(RUN)/nats.pid))"; \
	fi

dev-temporal: ## Start the Temporal dev server
	@mkdir -p $(RUN) $(LOGS)
	@if [ -f $(RUN)/temporal.pid ] && kill -0 $$(cat $(RUN)/temporal.pid) 2>/dev/null; then \
	  echo "temporal already running ($$(cat $(RUN)/temporal.pid))"; \
	else \
	  $(TEMPORAL_BIN) server start-dev --ip 127.0.0.1 --port 7233 \
	    --ui-port 8233 --db-filename $(DEV)/temporal.db --log-level warn \
	    > $(LOGS)/temporal.log 2>&1 & \
	  echo $$! > $(RUN)/temporal.pid; sleep 2; echo "temporal started ($$(cat $(RUN)/temporal.pid))"; \
	fi

dev-api: ## Run the control-plane API in the foreground
	$(PY) uvicorn ai_orchestrator.main:app --host 127.0.0.1 --port 8000 --reload

dev-worker: ## Run the Temporal workflow worker
	$(PY) -m ai_orchestrator.worker

stop: ## Stop the background dev processes
	@for svc in nats temporal; do \
	  if [ -f $(RUN)/$$svc.pid ]; then \
	    pid=$$(cat $(RUN)/$$svc.pid); \
	    if kill -0 $$pid 2>/dev/null; then kill $$pid; echo "stopped $$svc ($$pid)"; fi; \
	    rm -f $(RUN)/$$svc.pid; \
	  fi; \
	done

status: ## Report the state of the local stack
	@$(PY) scripts/pgctl.py status
	@for svc in nats temporal; do \
	  if [ -f $(RUN)/$$svc.pid ] && kill -0 $$(cat $(RUN)/$$svc.pid) 2>/dev/null; then \
	    echo "$$svc: running ($$(cat $(RUN)/$$svc.pid))"; \
	  else echo "$$svc: stopped"; fi; \
	done

logs: ## Tail the background service logs
	@tail -n 40 $(LOGS)/nats.log $(LOGS)/temporal.log 2>/dev/null || echo "no logs yet"

# ----------------------------------------------------------------- test ----
# Every integration test creates one organization and never deletes it, so the test
# database grows by ~700 rows per run and keeps every row from every run before it.
# This is the half that bounds it; the `tenant` fixture's pid-plus-counter slug is the
# half that stops an unbounded database from breaking a green suite.
# The database name has to be set **here**, not left to the script's default.
# `tests/conftest.py` points the suite at the test schema from inside pytest, so a
# separate `make` step has no such environment and defaults to development. The script
# then refuses, correctly -- which is a good guard being a nuisance because the caller
# was wrong. `AO_TEST_DB_NAME` is the same variable `migrate-test` uses, so the two
# cannot disagree about which database the tests run against.
reset-test-db: ## Empty the test database (refuses anything not named *_test)
	AO_POSTGRES_DB=$${AO_TEST_DB_NAME:-ai_orchestrator_test} \
	AO_TEST_DB_NAME=$${AO_TEST_DB_NAME:-ai_orchestrator_test} \
		$(PY) scripts/reset_test_db.py

# **`-m 'not live_model'` is the whole reason this line has an argument.**
#
# `test_the_real_demo_script_runs_end_to_end` runs the demo against the *development*
# database with the seeded agent profiles, whose first candidate is a real free model.
# It timed out at 900s with `--depth 0`, having passed at 66.90s before the chief
# learned to delegate at all (F246, F251) -- the same code, the same goal, and the only
# thing that changed is how much work the model chose to do.
#
# A gate whose pass/fail is a third party's response time is not a gate. The test is
# still here and still asserts what it always asserted; it runs under `make test-live`,
# which is the honest place for a measurement of somebody else's server.
test: ## Run unit and integration tests (everything that does not call a real provider)
	$(PY) pytest -q -m 'not live_model'

test-live: ## The tests that call a real model. Slow, and their runtime is the provider's
	$(PY) pytest -q -m live_model

# The reference catalogue is re-seeded because `reset-test-db` truncates **every** table,
# and `sop_definitions`, `autonomy_policies` and `roles` are tenant-scoped -- so a truncated
# test schema has no dossier catalogue and `test_agent_register_seeded.py` skips all eleven
# of its tests. It skips *loudly*, with the reason, which is how the gap was found; but a
# suite that needs a manual step to be complete is a suite nobody runs complete.
test-fresh: reset-test-db seed-test-reference ## Empty the test database, seed the reference catalogue, run the suite
	$(PY) pytest -q

test-unit: ## Run unit tests only
	$(PY) pytest tests/unit -q

test-integration: ## Run integration tests only
	$(PY) pytest tests/integration -q

test-e2e: preflight-e2e ## Run the end-to-end acceptance scenarios
	$(PY) pytest tests/e2e -q -m e2e

preflight-e2e: ## Fail loudly if a dependency the e2e suite needs is down
	@# The event-pipeline tests *skip* when NATS is unreachable, which is right for
	@# a developer without a broker and wrong for a gate: a suite that reports
	@# "16 passed, 5 skipped" reads as success while five scenarios did not run.
	@# This turns a missing dependency into a failure before the suite starts,
	@# and says which one.
	@$(PY) scripts/preflight_e2e.py

# --------------------------------------------------------------- quality ---
# The ORM is generated from the schema, not hand-maintained against it. `--verify` runs the
# pipeline twice and requires the second pass to change nothing, which is the only cheap
# way to catch a step that appends on every invocation -- and every step of it was
# non-idempotent on first write.
model-sync:
	@./scripts/sync_model.sh --verify

# The dossier's SOP, autonomy-policy and role catalogue, copied into a schema that has
# none. Tenant-scoped reference data, so a schema that has never been seeded cannot run the
# eight agents; `test_agent_register_seeded.py` skips itself without this.
seed-test-reference:
	@$(PY) scripts/seed_reference_catalogue.py --to ai_orchestrator_test

# `ORGS` defaults to whichever organization holds the dossier's SOP catalogue. It is a
# default rather than a discovery inside the seeder because **the tenant to seed is a
# decision, not a question**: the seeder requires `--org` on purpose, and a version that
# guessed would seed whichever tenant it happened to find first.
#
# The variable had no default at all, so the recipe expanded to a bare `--org` and argparse
# refused -- meaning `make seed-agents` had never run as written. A make target that only
# works if the caller happens to set a variable it reads is a target nobody has run.
ORGS ?= $(shell $(PY) scripts/catalogue_org.py 2>/dev/null)

# How many departments the seed builds, read from the seed.
#
# The console check used to carry a literal of 6 and failed with "all six departments
# are present -- 7" the day the seventh was added, which reads as though the platform
# had invented a department. A number passed in here cannot drift from the roster.
# `uv run python -c`, not `uv run -c`: the latter is not a valid invocation and fails
# silently inside `$$(shell)`, which is how the first version of this came back empty
# and the check compared the department count against **0**.
DEPARTMENT_COUNT = $(shell $(PY) python -c "import sys; sys.path.insert(0, 'src'); \
  from ai_orchestrator.seed import DEPARTMENTS; print(len(DEPARTMENTS) - 1)")

# The eight agents of the dossier's register, for one organization. Discovers which
# organization holds the SOP catalogue and copies it in first.
seed-agents:
	@$(PY) scripts/seed_agent_register.py $(if $(ORGS),--org $(ORGS))

# Publish the dossier's twenty-eight SOPs as controlled documents: a parsed code on
# `documents`, revision 1, and one mandatory distribution to each SOP's own owner role.
#
# Nobody is acknowledged. That is deliberate: inventing acknowledgements would make the
# compliance tile show a number, and a number built from a fabricated name in an audit
# column is worse than an honest zero. So the seed leaves the obligations outstanding and
# the page's "Confirm read" action is what clears them -- the figure is then a measurement
# of a real state. Idempotent, and it converges rather than only adding (F146).
# `ORGS` is optional and the flag is omitted when it is unset. Written as `--org $(ORGS)`
# the recipe expanded to a bare `--org` and argparse refused -- so `make seed-agents` had
# never run as written, and neither had the target beside it. A make target that only works
# if you happen to pass the variable it reads is a target nobody has run.
seed-docs:
	@$(PY) scripts/seed_document_register.py $(if $(ORGS),--org $(ORGS))

seed-docs-dry:
	@$(PY) scripts/seed_document_register.py $(if $(ORGS),--org $(ORGS)) --dry-run

# A coordination corpus, so the CEO flow can be walked end to end.
#
# The real corpus is a schedule and nothing else: six projects, 240 WBS nodes, 2778
# progress readings, and zero contracts, suppliers, purchase orders, approvals, and only
# two delegations across 91 tasks. Every operation for the whole flow exists and works --
# `execute_task` delegates, `DelegationExecutor` writes the tree, the approvals endpoints
# decide -- and there is nothing on the other side of it to look at.
#
# The showcase task is executed through the **real** `TaskExecutionService`, the same call
# the Temporal worker makes, so the delegation tree is the product's output rather than a
# row written to look like one. The one hand-written row is a pending approval: there is
# no public writer for a task-scoped approval, so the mock supplies the situation and the
# product does the deciding.
# The recruitment demo: ONX-BO-HR-SOP-004 as eight taskable stages with a person in the loop.
# Idempotent -- a re-run adopts the existing root rather than making a second chain.
seed-hiring:
	@$(PY) scripts/seed_hiring_request.py $(if $(ORGS),--org $(ORGS))

# `reset_hiring_request.py` requires `--org`, so the guard has to be a sentence rather
# than an omitted flag: dropping it turned a clear refusal into argparse's "the
# following arguments are required: --org", which says nothing about *why* there is no
# org. `ORGS` is empty precisely when several tenants hold the catalogue, and that is
# the thing worth saying.
hiring-reset:
	@test -n "$(ORGS)" || { \
	  echo "ORGS is empty, so no tenant could be chosen. Several tenants hold the dossier"; \
	  echo "catalogue, and refusing to guess between them is deliberate. Pass one:"; \
	  echo "    make hiring-reset ORGS=org_..."; \
	  exit 2; }
	@$(PY) scripts/reset_hiring_request.py --org $(ORGS)

mock-corpus:
	@$(PY) scripts/mock_coordination_corpus.py $(if $(ORGS),--org $(ORGS))

mock-corpus-norun:
	@$(PY) scripts/mock_coordination_corpus.py $(if $(ORGS),--org $(ORGS)) --no-run

# Remove what `scripts/demo_real_run.py` leaves behind. **Dry run unless --apply**, because
# this deletes 630+ rows of audit history and the application role is not allowed to do that
# on its own -- see scripts/clear_demo_residue.py.
# Cancel the two children the parent superseded, and write down why in the audit log. Named
# pairs, not a rule: deciding that two instructions are the same work is a judgement about
# meaning, and a rule would make it invisibly. See scripts/supersede_duplicate_tasks.py.
supersede-duplicates:
	@$(PY) scripts/supersede_duplicate_tasks.py

clear-demo:
	@$(PY) scripts/clear_demo_residue.py $(if $(ORGS),--org $(ORGS))

clear-demo-apply:
	@$(PY) scripts/clear_demo_residue.py $(if $(ORGS),--org $(ORGS)) --apply

mock-corpus-reset:
	@$(PY) scripts/mock_coordination_corpus.py $(if $(ORGS),--org $(ORGS)) --reset

# Run the whole dossier fleet, once, on real work from the real corpus.
#
# One task per agent in `domain/agent_register.py`, phrased from what that agent's SOPs say
# it owns, run through the real `TaskExecutionService` -- the same call the Temporal
# activity makes. This is the loop that finds defects nothing else does: it found
# `AutonomyLevel('L1')` raising on 7 of 8 agents, a model profile whose only reachable
# candidate was an unregistered provider, a seeder that reported writes it had rolled back,
# and an `internal_error` category on a run that had merely run out of turn budget.
#
# A run is ~11 minutes on the free model. `--concurrency` caps parallel calls; the default
# of 4 is deliberate, because eight at once against one free endpoint is how you get
# rate-limited and then diagnose it as "the agent is broken".
run-fleet:
	@$(PY) scripts/run_fleet.py --org $(ORGS) $(ARGS)

# `ruff format --check` is **in** the lint target, which it was not.
#
# Measured: `ruff check` passed cleanly on all 257 files while `ruff format --check`
# reported **135 of 255** needing reformatting. So the formatting standard the project
# follows was documented nowhere and enforced nowhere, and a contributor had no way to find
# out except by running a command the Makefile did not mention.
#
# Both halves are needed and they are not the same thing. `ruff check` is a linter and
# finds defects; `ruff format` is a formatter and finds *drift from a convention*. A
# project can have a clean lint and no formatting standard, and this one did, for 257
# files, until now.
#
# The two do disagree occasionally -- a formatter will join a line the linter then finds
# too long. The resolution used for the two that showed up is to write the line so both
# accept it, and to disable neither rule.
lint: ## Lint and check formatting
	$(PY) ruff check src tests scripts examples
	$(PY) ruff format --check src tests scripts examples

fmt: ## Reformat, then lint
	$(PY) ruff format src tests scripts examples
	$(MAKE) lint

format: ## Auto-fix lint findings
	$(PY) ruff check --fix src tests scripts examples
	$(PY) ruff format src tests scripts examples

typecheck: ## Static type check
	$(PY) mypy src

check: lint typecheck test ## Everything

# --------------------------------------------------------------- secrets ---
verify-secrets: ## Report which credentials are configured (never their values)
	$(PY) scripts/verify_secrets.py

smoke: ## Smoke-test the configured model providers
	$(PY) scripts/smoke_test_providers.py

# ----------------------------------------------------------------- audit ---
audit-db: ## Report row counts and RLS coverage
	$(PY) scripts/audit_db.py

audit-outbox: ## Show unpublished outbox events and consumer lag
	$(PY) scripts/audit_outbox.py

# -------------------------------------------------------------- benchmark --
bench: ## Run the runtime benchmark harness
	$(PY) -m ai_orchestrator.benchmarks.runner

# ----------------------------------------------------------------- demo ---
# What the console shows when there is something in it: a real agent, on a real free
# model, delegating to another agent. Run `make demo` and then open the console.
seed-free-model: ## Point an agent at a real free-tier model, after verifying it answers
	$(PY) scripts/seed_free_model.py

demo: seed-free-model ## Run one real coordination task and print the delegation tree
	$(PY) scripts/run_demo_task.py

# ------------------------------------------------------------------- ui ---
# Verify the page by *executing it*, not by reading it.
#
# The page has no browser available in this environment and no headless one either, so
# `verify-page` runs the served document's JavaScript in Node against a small DOM shim
# and the live API, then asserts on what it rendered. That catches a `TypeError` in the
# boot path, a property no payload carries, and a panel left empty -- none of which a
# substring test can see, and all of which serve a 200.
#
# It found F125: two queries in `role_views` disagreeing about one node, so the page
# drew a late zone green while its own legend said it was late. The 18 unit tests for
# that module did not, because every one of them feeds a single reading per node.
seed-construction: ## Ingest the reference corpus so the construction surfaces have rows
	$(PY) scripts/ingest_construction.py

# The one command a human needs. It starts the API on a free port with authentication
# in its documented demo mode, looks up a real tenant, executes the page against it, and
# prints the URL to open.
#
# It deliberately does **not** depend on NATS or Temporal. The construction surfaces --
# portfolio, project workspace, progress, approvals, the agent roster -- read Postgres
# and nothing else, and `make dev` requires both brokers, so following the obvious
# instruction on a machine without them leaves a person stuck at step one.
page: ## Start the API, verify the page against real data, print the URL to open
	@$(PY) scripts/pgctl.py start >/dev/null 2>&1 || true
	@$(PY) scripts/seed_local_operator.py >/dev/null 2>&1 || true
	@$(PY) scripts/sweep_stranded.py >/dev/null 2>&1 || true
	@mkdir -p $(RUN) $(LOGS) .devdata/ui
	@if [ -f $(RUN)/page.pid ] && kill -0 $$(cat $(RUN)/page.pid) 2>/dev/null; then \
		kill $$(cat $(RUN)/page.pid) 2>/dev/null || true; sleep 1; fi
	@AO_API_AUTH_DISABLED=true $(PY) -m uvicorn ai_orchestrator.main:app \
		--host 127.0.0.1 --port $(PAGE_PORT) --log-level warning \
		> $(LOGS)/page.log 2>&1 & echo $$! > $(RUN)/page.pid
	@for i in $$(seq 1 30); do \
		curl -sf -o /dev/null "http://127.0.0.1:$(PAGE_PORT)/health" && break; sleep 1; \
	done; \
	curl -sf -o /dev/null "http://127.0.0.1:$(PAGE_PORT)/health" || \
		{ echo "the API did not come up. $(LOGS)/page.log:"; tail -20 $(LOGS)/page.log; exit 1; }
	@ORG=$$($(PY) scripts/first_org.py 2>.devdata/ui/tenant.txt); \
	echo ""; echo "  tenant: $$ORG"; cat .devdata/ui/tenant.txt; echo ""
	@$(MAKE) --no-print-directory verify-page BASE=http://127.0.0.1:$(PAGE_PORT) ORG=$$($(PY) scripts/first_org.py 2>/dev/null)
	@echo ""
	@echo "  Open this:  http://127.0.0.1:$(PAGE_PORT)/api/v1/ui?org=$$($(PY) scripts/first_org.py 2>/dev/null)"
	@echo "  Stop it:    make page-stop"
	@echo ""

page-stop: ## Stop the API started by `make page`
	@if [ -f $(RUN)/page.pid ]; then \
		kill $$(cat $(RUN)/page.pid) 2>/dev/null || true; rm -f $(RUN)/page.pid; \
		echo "stopped"; \
	else echo "nothing started by \`make page\`"; fi

verify-page: ## Execute the served UI against the live API and check what it rendered
	@echo "The API must already be running.  \`make page\` does that for you;"
	@echo "or start it with:  AO_API_AUTH_DISABLED=true $(PY) -m uvicorn ai_orchestrator.main:app --port 8000"
	@test -n "$(BASE)" || (echo "set BASE=http://127.0.0.1:8000 and ORG=<org_...>" && exit 2)
	@mkdir -p .devdata/ui
	@curl -sf -o .devdata/ui/served.html "$(BASE)/api/v1/ui?org=$(ORG)" \
		|| (echo "could not fetch the page from $(BASE)" && exit 1)
	@node scripts/verify_page.mjs "$(BASE)" "$(ORG)" .devdata/ui/served.html \
	  --departments "$(DEPARTMENT_COUNT)"

# ----------------------------------------------------------------- clean ---
clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

distclean: clean stop ## Also remove the local cluster and NATS/Temporal data
	rm -rf $(DEV)
