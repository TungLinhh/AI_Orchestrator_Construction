# Repository Audit

**Scope**: `/home/vutun/ai_orchestrator/` and its sibling projects.
**Date**: 2026-09-25.
**Method**: read-only inspection. Nothing outside `ai_orchestrator/` was created,
renamed, moved, deleted or modified.

## Target directory

`/home/vutun/ai_orchestrator/` did not exist as a tracked project. It was an
empty directory, not a git repository, with no files, no VCS history and no
recoverable prior content. Everything in it is new work.

That fact is the reason this document is short: there was no prior state to
audit, so there is no migration, no deprecation plan, and no list of things that
might break. The audit that mattered was of what already existed on the machine
and could be learned from or reused.

## Sibling projects surveyed

| Path | Nature | Usable as | Never usable as |
|---|---|---|---|
| `pmo_project_procore` | Running production system, Node + Postgres, RLS, real users | Source of the three-layer RLS pattern, approval inbox, permission-first retrieval, cost accounting | A code base to fork |
| `pmo_project` | Data/reporting project sharing the Postgres host | Domain vocabulary, reporting schemas | A control-plane model |
| `O-Nexus-AI-orchestration-deployment` | Backup archive: docs, Bash, one 22-line prompt | Effect-class taxonomy, role presets, governance rules, the action-gate design | An implementation to adopt |

## Machine capabilities (probed, not assumed)

| Capability | Finding | Consequence |
|---|---|---|
| Python | 3.14.7 (Homebrew CPython) | Matches the required version |
| uv | 0.12.3 | Dependency resolution and venv management |
| PostgreSQL | 16.15 via Homebrew | Local cluster is real, not a container |
| pgvector | **0.8.6, prebuilt only for PG17/18** | Had to build from source for PG16 — see below |
| NATS server | not installed | Downloaded v2.15.0 release binary |
| Temporal CLI | not installed | Downloaded v1.9.1 release binary (server 1.32.0) |
| Docker / podman / kubectl | **none present** | Container-based dev stack is impossible to verify here |
| Live Postgres cluster | `/home/vutun/pgdata`, port 5433, owned by another project | Must not be touched |

### The pgvector finding

`brew install pgvector` reports success and pours a keg — but the bottle
contains only `postgresql@17` and `postgresql@18` extension directories, while
the machine's PostgreSQL is 16. `CREATE EXTENSION vector` fails with "could not
open extension control file".

This is the kind of thing that looks fine until a migration runs. The fix was to
build pgvector 0.8.6 from source against PG16:

```bash
make PG_CONFIG=/opt/postgresql@16/bin/pg_config
make PG_CONFIG=/opt/postgresql@16/bin/pg_config install
```

`scripts/pgctl.py` assumes the extension is already installed and says so in its
docstring, because a bootstrap that installs the extension would hide the
mismatch rather than surface it.

### The port conflict

A live cluster on 5433 belongs to another project. This project uses **55432**
and its own data directory under `.devdata/pg`. `scripts/pgctl.py` never probes
or touches 5433.

## Findings that changed the plan

1. **No container runtime.** The brief's Docker Compose dev stack cannot be run,
   so it cannot be verified. Resolved by making the native stack the only
   documented path and stating the deviation explicitly rather than shipping a
   compose file that has never been executed.

2. **Two PostgreSQL installations on the PATH story.** `initdb` is not on `PATH`
   but exists at a Homebrew prefix. `scripts/pgctl.py` resolves binaries from
   three candidate locations, because assuming `PATH` is correct makes the setup
   fail on a machine where it works everywhere else.

3. **An older cluster is already running.** Confirms the port choice above.

4. **A credential already leaked on this machine.** `/home/vutun/pmo_project_procore/backend/.env:6`
   contains a live, un-rotated `OPENROUTER_API_KEY` that was previously baked
   into an image layer. Reported to the user; **not copied, not used, not
   rotated from here** — rotation is the credential owner's action.

5. **Personal data in a committed archive.** A real Telegram user id
   (`5624438820`) and a real personal name appear in at least nine files under
   `O-Nexus-AI-orchestration-deployment/`, including its `hazard.md`, which
   documents the leak as severity-red and never fixes it. This project does not
   copy any of it. See `docs/LEGACY_SYSTEMS_REVIEW.md`.

## Verification of this audit

Every claim above was produced by running a command, not by inference. The
capability table is the output of `python3 -VV`, `uv --version`, `brew --version`,
`pg_config --version`, `which docker kubectl podman`, and `ls` of the pgvector
keg. The credential and personal-data findings come from `grep -rn` over those
specific trees and are reproduced in `docs/FAILED_APPROACHES.md`.
