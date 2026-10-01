"""The models and the database must be the same schema.

This is the test whose absence allowed four points of drift to ship in
migrations 0003, 0004 and 0005. Nothing in the repository compared the ORM to
the migrated schema, so the two were free to disagree and every other test still
passed — because every other test used the models, and the models were the thing
that was wrong. The drift was found by accident, by running `alembic revision
--autogenerate` for an unrelated purpose and noticing four `alter_column` calls
against tables nobody was working on.

`alembic check` does this comparison, but only when a human remembers to run it.
This runs it in the suite, against the same connection the tests already use, so
the failure mode is a red test rather than a surprise during a migration.

What it catches:
  * a model column the schema does not have
  * a schema column no model declares (the dangerous direction: the code and the
    data quietly diverge and only a query notices)
  * a type or server-default difference
  * a column comment dropped from the model, which is how `tasks.
    procedure_fingerprint` nearly lost its only explanation

The comparison is deliberately run with the same options as `migrations/env.py`.
Comparing with different options gives a different answer, and a test that
disagrees with the tool it is emulating is worse than no test.

**Both domain modules are imported here explicitly.** That is not tidiness. A
table only reaches `Base.metadata` when its module is imported, so this file used
to import only `models` and `construction` and pass in the full suite — because
`test_construction_schema.py` runs earlier alphabetically and imports
`commercial` on its behalf. Run on its own, it failed. A test that passes only
because of collection order is a test that will pass for the wrong reason again.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from ai_orchestrator.persistence import (
    agent_framework,
    commercial,
    construction,
    contracts,
    models,
    process,
    procurement,
    progress,
    supply,
)
from ai_orchestrator.persistence.base import Base
from ai_orchestrator.persistence.rls import GLOBAL_TABLES
from ai_orchestrator.persistence.session import Database

pytestmark = [pytest.mark.integration]

#: Every module that must be imported for `Base.metadata` to be the whole schema.
#:
#: This exists as a *used* value rather than as four bare imports because of what
#: happened the first time. Those imports carried a blanket unused-import
#: suppression; ruff decided the suppression was redundant, a later
#: `ruff check --fix` then deleted the imports themselves as unused, and
#: `Base.metadata` became empty. The drift test still *ran* — it compared zero
#: models against 75 tables and reported every table as undeclared, which is how
#: it was noticed.
#:
#: A linter deleted the load-bearing part of a test, and nothing in the review
#: would have shown it: `ruff check --fix` is trusted, and the change is four
#: lines disappearing rather than four lines appearing. Binding them to a tuple
#: this file uses makes the imports genuinely referenced, so the fix cannot be
#: applied — and the set comparison below still catches a module dropped from the
#: list.
DOMAIN_MODULES = (
    models,
    construction,
    commercial,
    contracts,
    process,
    supply,
    procurement,
    progress,
    agent_framework,
)

#: Must match `migrations/env.py`. A comparison configured differently answers a
#: different question, and the whole value of this test is that it answers the
#: one `alembic upgrade` would have.
COMPARE_OPTIONS = {
    "compare_type": True,
    "compare_server_default": True,
}


def _diff(connection: object) -> list[tuple[object, ...]]:
    context = MigrationContext.configure(connection, opts=COMPARE_OPTIONS)  # type: ignore[arg-type]
    return list(compare_metadata(context, Base.metadata))


def _describe(diffs: list[tuple[object, ...]]) -> str:
    """Render a diff so a failure names the table and the column.

    `compare_metadata` returns tuples whose first element is the operation and
    whose second is a directive or a string, not dicts. The first version of
    this function indexed them as dicts and raised `TypeError`, which replaced a
    useful failure with a useless one — the worst outcome a reporting helper can
    have, because the reader learns nothing about the actual mismatch.
    """
    lines = []
    for diff in diffs:
        if not diff:  # pragma: no cover - alembic never emits an empty diff
            continue
        kind = str(diff[0])
        parts = [kind]
        for item in diff[1:]:
            text_ = str(item)
            # Keep the line short and the useful part: a table name, a column
            # name, a type. The directive's full repr is often a page long.
            for token in text_.replace("(", " ").replace(",", " ").split():
                if token.strip(")'\"") and len(token) > 2:
                    parts.append(token.strip(")'\""))
                    break
        lines.append("  " + " ".join(parts[:4]))
    return "\n".join(lines)


class TestModelsMatchTheSchema:
    async def test_there_is_no_drift(self, db: Database) -> None:
        async with db.engine.connect() as conn:
            # `run_sync` because Alembic's API is synchronous and the engine is
            # asyncpg. This is the same bridge `migrations/env.py` uses, so the
            # comparison sees the connection the application sees.
            diffs = await conn.run_sync(_diff)
        assert not diffs, (
            "The ORM models and the database disagree. Either a migration is "
            "missing, or a model was changed without regenerating one.\n"
            f"{_describe(diffs)}\n"
            "Run `make migrate-test` if the migration exists but was never "
            "applied; otherwise write it."
        )

    async def test_every_table_exists_on_both_sides(self, db: Database, admin_db: Database) -> None:
        """The check that cannot go stale, replacing a hand-counted total.

        The previous version asserted `len(Base.metadata.tables) == 55`. That was
        correct until tranche 2 added six tables, at which point it failed for a
        reason that had nothing to do with what it was checking — and a guard
        test that breaks when the thing it guards grows is a guard that gets
        deleted.

        Comparing the two *sets* is stronger than comparing a count and needs no
        maintenance: a module that fails to import shows up as models missing,
        and a migration that was never applied shows up as tables missing. Both
        directions are covered, and neither can rot.
        """
        async with admin_db.engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
            in_database = {t for t in rows.scalars() if t not in GLOBAL_TABLES}
        in_models = set(Base.metadata.tables) - GLOBAL_TABLES

        assert not (in_models - in_database), (
            f"models with no table: {sorted(in_models - in_database)}. "
            "A module is probably not imported, so its tables never reached "
            "Base.metadata — or its migration was never written."
        )
        assert not (in_database - in_models), (
            f"tables with no model: {sorted(in_database - in_models)}. "
            "A migration created something no module declares, so nothing can "
            "read it correctly."
        )

    async def test_every_domain_tranche_is_fully_declared(self) -> None:
        """Named, rather than counted.

        A `len(...) == N` assertion breaks whenever the thing it guards grows,
        and a guard that breaks on the normal case gets deleted. The set
        comparison above already catches an unimported module; this names the
        tables so a *deleted* model is reported by name rather than as a
        mysterious one-element difference.
        """
        expected = {
            # migration 0007
            "clients",
            "client_contacts",
            "projects",
            "project_roles",
            "project_phases",
            "units_dictionary",
            "zones",
            "wbs",
            "wbs_items",
            "milestones",
            # migration 0008
            "opportunities",
            "tenders",
            "tender_documents",
            "tender_requirements",
            "bids",
            "bid_items",
            # migration 0009
            "suppliers",
            "contracts",
            "contract_milestones",
            "contract_variations",
            "contract_claims",
            "claim_events",
            "contract_clauses",
            # migration 0010 - the governance spine
            "gate_definitions",
            "gate_criteria",
            "gate_instances",
            "gate_criterion_evaluations",
            "gate_decisions",
            "gate_conditions",
            "sop_definitions",
            "sop_versions",
            "sop_steps",
            "sop_raci",
            "sop_forms",
            "doa_matrix",
            "autonomy_policies",
            # migration 0011 - material master and supplier diligence
            "material_categories",
            "materials",
            "supplier_documents",
            "supplier_risk_flags",
            "supplier_assessments",
            # migration 0013 - requisition through goods receipt
            "rfqs",
            "rfq_items",
            "quotations",
            "quotation_items",
            "purchase_orders",
            "po_items",
            "goods_receipts",
            "receipt_items",
            "receipt_checks",
            "material_reconciliations",
            # migration 0014 - construction progress, planned against actual
            "progress_snapshots",
        }
        present = set(Base.metadata.tables)
        assert expected <= present, f"missing from metadata: {sorted(expected - present)}"

    async def test_no_module_in_the_package_is_missing_from_the_tuple(self) -> None:
        """`DOMAIN_MODULES` is itself a hand-maintained list.

        A new module in the persistence package that is not on the list is
        invisible to this whole file — which is precisely the failure the list
        above exists to prevent, one level up. So the list is compared against
        what the package actually exports.
        """
        import ai_orchestrator.persistence as pkg

        on_list = {m.__name__ for m in DOMAIN_MODULES}
        exported = {
            name
            for name, obj in vars(pkg).items()
            if isinstance(obj, type) and getattr(obj, "__tablename__", None)
        }
        assert not (exported - on_list), (
            f"persistence modules declaring tables but absent from "
            f"DOMAIN_MODULES: {sorted(exported - on_list)}"
        )

    async def test_a_known_table_is_really_present(self, db: Database) -> None:
        """One explicit existence check, so a wholesale table loss is loud.

        `compare_metadata` reports *differences*. If the whole schema were
        missing, the diff would be large and the first test would fail — but with
        a message about 61 added tables rather than about anything actionable.
        This says "clients is not there" instead.
        """
        async with db.engine.connect() as conn:
            found = await conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'clients'"
                )
            )
        assert found.scalar() == 1


class TestEveryTenantTableIsIsolated:
    """RLS on **every** table that has an `organization_id`, derived from the models.

    ## Why this file exists

    Migration `0025_document_control.py` created two tenant-scoped tables,
    `document_versions` and `document_distributions`, and did not enable row-level
    security on either. They had `relrowsecurity = false` and **zero** policies, while
    their ninety-five siblings had `true`, `FORCE`, and one `tenant_isolation` policy
    each. Every other test in the suite passed: the model reconciled, `alembic check`
    reported no drift, and each constraint test proved its rule against a table that
    happened to be readable by everybody.

    The reason nothing caught it is stated in this repository's own words, in
    `test_construction_domain.py`: *"a whole-schema check passes while one new table is
    unprotected, because the 44 that are protected drown out the one that is not."* Both
    existing checks were per-tranche, against a **hand-kept tuple of names** — so a table
    added to the schema and not to a tuple was invisible to them, which is exactly what
    happened, twice over.

    ## Derived from the models, not from a list

    The set of tables under test is computed: every table in `Base.metadata` with an
    `organization_id` column, minus `GLOBAL_TABLES`. A new tenant table is therefore
    covered the moment it is declared, with no list to remember to update — and a table
    added to a list but not to the schema fails too, because the assertion is an equality
    between what the models say and what the database protects.

    ## All three properties, not one

    `ENABLE` without `FORCE` leaves the table open to the *owner*, and the owner is the
    role that runs migrations. A policy without `FORCE` is a policy that the migration
    path does not obey, so asserting only the policy would pass on exactly the connection
    that most needs the protection.
    """

    async def test_every_tenant_table_is_enabled_forced_and_has_a_policy(
        self, db: Database
    ) -> None:
        expected = {
            name
            for name, table in Base.metadata.tables.items()
            if "organization_id" in table.columns and name not in GLOBAL_TABLES
        }
        assert expected, "the models declare no tenant tables at all; the check is vacuous"

        async with db.engine.connect() as conn:
            protected = {
                str(row[0])
                for row in (
                    await conn.execute(
                        text(
                            "SELECT c.relname FROM pg_class c "
                            "JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'public' AND c.relrowsecurity "
                            "  AND c.relforcerowsecurity "
                            "  AND EXISTS (SELECT 1 FROM pg_policies p "
                            "              WHERE p.tablename = c.relname)"
                        )
                    )
                ).all()
            }

        unprotected = sorted(expected - protected)
        assert not unprotected, (
            "Every table with an `organization_id` must have RLS enabled, FORCED, and a "
            "policy. Unprotected: " + ", ".join(unprotected) + ". A table missing this is "
            "readable by every tenant, and no other test in the suite will say so — the "
            "per-tranche checks compare against hand-kept name lists, so a table that was "
            "never added to one is invisible to them. Call `_protect()` from the migration, "
            "the same way 0002, 0007 and 0010 do."
        )

    async def test_the_two_document_control_tables_are_among_them(self, db: Database) -> None:
        """Named, so the pair that was missed is a specific regression rather than a
        general one, and so a future reader can see which tables this test is about.

        A whole-schema assertion names whatever is unprotected *today*. That is the right
        default, but it means that fixing the pair silently makes this test vacuous, and
        the record of what happened here would be gone. This keeps it.
        """
        async with db.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                        "  (SELECT count(*) FROM pg_policies p "
                        "     WHERE p.tablename = c.relname) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' "
                        "  AND c.relname IN ('document_versions', 'document_distributions') "
                        "ORDER BY 1"
                    )
                )
            ).all()
        assert len(rows) == 2, f"the document control tables are missing: {rows}"
        for name, rls, force, policies in rows:
            assert rls and force and policies >= 1, (
                f"{name}: rls={rls} force={force} policies={policies}. Migration 0025 "
                "created this table without calling `_protect()`, so every tenant could "
                "read every other tenant's document versions."
            )
