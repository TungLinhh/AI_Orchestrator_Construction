"""Verify fresh migrations and catalogue provisioning in two disposable databases.

Creates uniquely named databases on this project's native PostgreSQL port only.
Never resets an existing database. Failed databases remain for diagnosis; successful
ones are dropped without FORCE, after all owned connections have been closed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import asyncpg

from ai_orchestrator.config.settings import get_settings


async def verify(output: Path) -> int:
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    settings = get_settings()
    if settings.postgres_port != 55432 or settings.postgres_host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Clean-install verification requires local project PostgreSQL port 55432")
    prefix = "ao_install_" + secrets.token_hex(6)
    names = [prefix, prefix + "_ref"]
    created: list[str] = []
    report: dict = {"synthetic": True, "status": "FAIL", "databases": names, "steps": []}
    receipt = output / "receipt.json"

    def save() -> None:
        receipt.write_text(json.dumps(report, indent=2) + "\n")

    def run(name: str, command: list[str], database: str) -> None:
        started = time.monotonic()
        env = {
            **os.environ,
            "AO_POSTGRES_DB": database,
            "AO_MIGRATION_DB": database,
            "AO_MODEL_PROVIDER_DEFAULT": "fake",
            "AO_EMBEDDING_PROVIDER_DEFAULT": "hash",
        }
        log = output / (name + ".log")
        with log.open("w") as handle:
            result = subprocess.run(
                command, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=300, check=False
            )
        report["steps"].append(
            {
                "name": name,
                "command": command,
                "database": database,
                "exit_code": result.returncode,
                "seconds": time.monotonic() - started,
                "log": str(log),
            }
        )
        save()
        if result.returncode:
            raise RuntimeError(f"{name} failed; inspect {log}")

    def dsn(database: str) -> str:
        return settings.owner_dsn_for(database).replace("postgresql+asyncpg://", "postgresql://", 1)

    async def catalogue_sizes(database: str) -> dict[str, int]:
        connection = await asyncpg.connect(dsn(database))
        try:
            return {
                table: await connection.fetchval(f"SELECT count(*) FROM {table}")
                for table in ("roles", "sop_definitions", "autonomy_policies")
            }
        finally:
            await connection.close()

    save()
    owner = None
    try:
        owner = await asyncpg.connect(dsn("postgres"))
        for name in names:
            # Names come only from a fixed prefix and locally generated hex.
            await owner.execute(f'CREATE DATABASE "{name}"')
            created.append(name)
            save()
            bootstrap = await asyncpg.connect(dsn(name))
            try:
                # Same database-local prerequisites as scripts/pgctl.py bootstrap.
                for extension in ("vector", "uuid-ossp", "pgcrypto"):
                    await bootstrap.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')
                app_role = settings.app_db_user.replace('"', '""')
                await bootstrap.execute(f'GRANT ALL ON SCHEMA public TO "{app_role}"')
            finally:
                await bootstrap.close()
            run(
                "migrate-" + name,
                [
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    "migrations/alembic.ini",
                    "upgrade",
                    "head",
                ],
                name,
            )
        run("seed-first", [sys.executable, "-m", "ai_orchestrator.seed"], names[0])
        run("seed-idempotence", [sys.executable, "-m", "ai_orchestrator.seed"], names[0])
        run("process", [sys.executable, "scripts/seed_process_spine.py"], names[0])
        run(
            "reference-first",
            [
                sys.executable,
                "scripts/seed_reference_catalogue.py",
                "--from",
                names[0],
                "--to",
                names[1],
            ],
            names[0],
        )
        reference_first = await catalogue_sizes(names[1])
        assert reference_first == await catalogue_sizes(names[0]), (
            "Reference copy lost catalogue rows"
        )
        run(
            "reference-idempotence",
            [
                sys.executable,
                "scripts/seed_reference_catalogue.py",
                "--from",
                names[0],
                "--to",
                names[1],
            ],
            names[0],
        )
        reference_second = await catalogue_sizes(names[1])
        assert reference_first == reference_second, "Reference second pass changed row counts"
        report["reference_catalogue_counts"] = reference_second
        source = await asyncpg.connect(dsn(names[0]))
        target = await asyncpg.connect(dsn(names[1]))
        try:
            from ai_orchestrator.domain.agent_register import REGISTER

            required_roles = {spec.role_name for spec in REGISTER}
            required_sops = {code for spec in REGISTER for code in spec.sop_codes_prefixed}
            rows = await target.fetch("SELECT name FROM roles")
            assert required_roles <= {r["name"] for r in rows}, "Missing reference roles"
            rows = await target.fetch("SELECT code FROM sop_definitions")
            assert required_sops <= {r["code"] for r in rows}, "Missing reference SOPs"
            organization = await source.fetchval("SELECT id FROM organizations ORDER BY id LIMIT 1")
            counts_before = {
                table: await source.fetchval(f"SELECT count(*) FROM {table}")
                for table in [
                    "roles",
                    "agents",
                    "agent_definitions",
                    "procedures",
                    "procedure_versions",
                ]
            }
        finally:
            await source.close()
            await target.close()
        run(
            "register-first",
            [sys.executable, "scripts/seed_agent_register.py", "--org", organization],
            names[0],
        )
        source = await asyncpg.connect(dsn(names[0]))
        try:
            counts_first = {
                table: await source.fetchval(f"SELECT count(*) FROM {table}")
                for table in counts_before
            }
        finally:
            await source.close()
        run(
            "register-idempotence",
            [sys.executable, "scripts/seed_agent_register.py", "--org", organization],
            names[0],
        )
        source = await asyncpg.connect(dsn(names[0]))
        try:
            counts_second = {
                table: await source.fetchval(f"SELECT count(*) FROM {table}")
                for table in counts_before
            }
            assert counts_first == counts_second, "Register second pass changed row counts"
            grants = await source.fetch(
                "SELECT name, granted_level FROM agents WHERE name = ANY($1::text[])",
                [spec.name for spec in REGISTER],
            )
            assert len(grants) == len(REGISTER), "Not all register agents were provisioned"
            assert all(row["granted_level"] == "L1" for row in grants), "Seed widened authority"
        finally:
            await source.close()
        report.update(
            status="PASS",
            catalogue_roles=len(required_roles),
            catalogue_sops=len(required_sops),
            register_counts=counts_second,
            limitation="Provisioning with fake model; no production or live-model acceptance",
        )
        for name in reversed(created):
            await owner.execute(f'DROP DATABASE "{name}"')
        report["cleanup"] = "PASS: only databases created by this invocation were dropped"
    except Exception as exc:
        report["status"] = "FAIL"
        report["error"] = str(exc)
        report["retained_databases"] = created
        report["cleanup"] = "SKIP: preserve failed scratch databases for diagnosis"
    finally:
        save()
        if owner is not None:
            await owner.close()
    print(
        json.dumps(
            {"status": report["status"], "receipt": str(receipt), "error": report.get("error")}
        )
    )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    raise SystemExit(asyncio.run(verify(parser.parse_args().output)))
