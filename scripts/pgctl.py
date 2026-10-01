"""Local PostgreSQL cluster management.

This project has no Docker, so the dev stack runs against a PostgreSQL cluster
that lives inside the repository (`.devdata/pg`) and is owned by this script.

Roles created here:

  ao          superuser/owner. Used by Alembic and the seed script only.
  ao_app      the application role. NOBYPASSRLS, so the RLS policies apply.
  ao_backup   BYPASSRLS, so `pg_dump` can read rows that FORCE ROW LEVEL
              SECURITY would otherwise hide. Never used by the application.

The split between `ao` and `ao_app` is the whole tenant-isolation story. An
application connected as `ao` would run every query with RLS switched off, and
the policies would still be present in the schema looking correct.
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_DATA = ROOT / ".devdata"
PGDATA = DEV_DATA / "pg"
PGLOG = DEV_DATA / "pg.log"
DEFAULT_PORT = 55432
APP_ROLE = "ao_app"
BACKUP_ROLE = "ao_backup"
OWNER_USER = "ao"

APP_DB = "ai_orchestrator"
TEST_DB = "ai_orchestrator_test"


def pg_bin(name: str) -> str:
    """Locate a PostgreSQL binary.

    Checks, in order: the caller, Homebrew's prefix, then PATH. Homebrew's
    postgresql@16 is the supported local install for this project because it
    matches the pgvector build in `migrations/README.md`.
    """
    if name == "initdb" and not shutil.which("initdb"):
        for candidate in (
            Path("/home/linuxbrew/.linuxbrew/opt/postgresql@16/bin"),
            Path("/opt/homebrew/opt/postgresql@16/bin"),
            Path("/usr/lib/postgresql/16/bin"),
        ):
            if (candidate / "initdb").exists():
                return str(candidate / name)
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        Path("/home/linuxbrew/.linuxbrew/opt/postgresql@16/bin"),
        Path("/opt/homebrew/opt/postgresql@16/bin"),
        Path("/usr/lib/postgresql/16/bin"),
    ):
        if (candidate / name).exists():
            return str(candidate / name)
    msg = f"cannot find PostgreSQL binary: {name}"
    raise SystemExit(msg)


def run(
    cmd: list[str], *, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if check and result.returncode != 0:
        sys.stderr.write(f"$ {' '.join(cmd)}\n{result.stdout}{result.stderr}\n")
        msg = f"command failed with exit {result.returncode}"
        raise SystemExit(msg)
    return result


def secrets_value(name: str) -> str:
    """Read a secret from .secrets/runtime.env, generating it when absent."""
    path = ROOT / ".secrets" / "runtime.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in text.splitlines():
        if line.startswith(f"{name}="):
            value = line.split("=", 1)[1].strip()
            if value:
                return value
    value = secrets.token_urlsafe(32)
    with path.open("a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(f"{name}={value}\n")
    path.chmod(0o600)
    return value


def is_running(port: int) -> bool:
    return (
        run([pg_bin("pg_isready"), "-h", "127.0.0.1", "-p", str(port)], check=False).returncode == 0
    )


def cmd_init(args: argparse.Namespace) -> None:
    """Create the cluster. Idempotent."""
    if (PGDATA / "PG_VERSION").exists():
        print(f"cluster already initialised at {PGDATA}")
        return
    DEV_DATA.mkdir(parents=True, exist_ok=True)
    print(f"initialising PostgreSQL cluster in {PGDATA}")
    run(
        [
            pg_bin("initdb"),
            "-D",
            str(PGDATA),
            "-U",
            OWNER_USER,
            # Local socket: trust. TCP: scram, so a password is genuinely required
            # to reach the server over the network interface.
            "--auth-local=trust",
            "--auth-host=scram-sha-256",
            "-E",
            "UTF8",
        ]
    )


def cmd_start(args: argparse.Namespace) -> None:
    if is_running(args.port):
        print(f"postgres already accepting connections on {args.port}")
        return
    cmd_init(args)
    run(
        [
            pg_bin("pg_ctl"),
            "-D",
            str(PGDATA),
            "-o",
            f"-p {args.port} -c listen_addresses=127.0.0.1",
            "-l",
            str(PGLOG),
            "-w",
            "start",
        ]
    )
    print(f"postgres started on 127.0.0.1:{args.port}")


def cmd_stop(args: argparse.Namespace) -> None:
    if not (PGDATA / "PG_VERSION").exists():
        print("no cluster to stop")
        return
    run([pg_bin("pg_ctl"), "-D", str(PGDATA), "-m", "fast", "-w", "stop"], check=False)
    print("postgres stopped")


def _psql_super(port: int, db: str, sql: str, *, tuples: bool = True) -> str:
    """Run SQL as the owner over the local socket (trust auth, no password)."""
    result = run(
        [
            pg_bin("psql"),
            "-h",
            "/tmp",
            "-p",
            str(port),
            "-U",
            OWNER_USER,
            "-d",
            db,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ]
    )
    return result.stdout.strip()


def cmd_bootstrap(args: argparse.Namespace) -> None:
    """Start the server, create roles and databases, and set the owner password.

    Run this before `make migrate`.
    """
    cmd_start(args)
    port = args.port
    password = secrets_value("POSTGRES_PASSWORD")

    # The owner password is set over the local socket, so it never appears in
    # argv and never needs to be echoed by the user.
    _psql_super(port, "postgres", f"ALTER ROLE {OWNER_USER} WITH PASSWORD '{password}'")

    existing = _psql_super(
        port,
        "postgres",
        f"SELECT 1 FROM pg_roles WHERE rolname='{APP_ROLE}'",
    )
    if existing != "1":
        print(f"creating role {APP_ROLE} (NOBYPASSRLS)")
        _psql_super(
            port,
            "postgres",
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"NOBYPASSRLS NOINHERIT PASSWORD '{password}'",
        )
    else:
        _psql_super(port, "postgres", f"ALTER ROLE {APP_ROLE} WITH PASSWORD '{password}'")

    existing_backup = _psql_super(
        port, "postgres", f"SELECT 1 FROM pg_roles WHERE rolname='{BACKUP_ROLE}'"
    )
    if existing_backup != "1":
        # BYPASSRLS so pg_dump can read rows hidden by FORCE ROW LEVEL SECURITY.
        print(f"creating role {BACKUP_ROLE} (BYPASSRLS, for pg_dump only)")
        _psql_super(
            port,
            "postgres",
            f"CREATE ROLE {BACKUP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"BYPASSRLS NOINHERIT PASSWORD '{password}'",
        )

    for db in (APP_DB, TEST_DB):
        exists = _psql_super(port, "postgres", f"SELECT 1 FROM pg_database WHERE datname='{db}'")
        if exists != "1":
            print(f"creating database {db}")
            _psql_super(port, "postgres", f"CREATE DATABASE {db}")
        # pgvector must exist before migration 0003 tries to create the index.
        _psql_super(port, db, "CREATE EXTENSION IF NOT EXISTS vector")
        _psql_super(port, db, 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        _psql_super(port, db, "CREATE EXTENSION IF NOT EXISTS pgcrypto")
        _psql_super(port, db, f"GRANT ALL ON SCHEMA public TO {APP_ROLE}")
        _psql_super(port, db, f"GRANT ALL ON SCHEMA public TO {OWNER_USER}")

    print("bootstrap complete")


def cmd_password(args: argparse.Namespace) -> None:
    """Rotate the database passwords in place.

    Both roles share one generated secret, so a single write updates both.
    """
    password = secrets.token_urlsafe(32)
    path = ROOT / ".secrets" / "runtime.env"
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            lines.append(f"POSTGRES_PASSWORD={password}")
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    if is_running(args.port):
        for role in (OWNER_USER, APP_ROLE, BACKUP_ROLE):
            _psql_super(args.port, "postgres", f"ALTER ROLE {role} WITH PASSWORD '{password}'")
    print("password rotated in .secrets/runtime.env")


def cmd_status(args: argparse.Namespace) -> None:
    running = is_running(args.port)
    print(f"cluster: {PGDATA} ({'running' if running else 'stopped'})")
    print(f"port:    {args.port}")
    if running:
        tables = _psql_super(
            args.port,
            APP_DB,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'",
        )
        print(f"tables in {APP_DB}: {tables}")
        rls = _psql_super(
            args.port,
            APP_DB,
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity "
            "AND c.relforcerowsecurity",
        )
        print(f"tables with forced RLS: {rls}")


def main() -> None:
    parser = argparse.ArgumentParser(description="local PostgreSQL cluster for ai_orchestrator")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("AO_POSTGRES_PORT", DEFAULT_PORT))
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, help_text in (
        ("init", cmd_init, "create the cluster if it does not exist"),
        ("start", cmd_start, "start the cluster"),
        ("stop", cmd_stop, "stop the cluster"),
        ("bootstrap", cmd_bootstrap, "start, create roles and databases, install extensions"),
        ("password", cmd_password, "rotate the database password"),
        ("status", cmd_status, "report cluster state"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=fn)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
