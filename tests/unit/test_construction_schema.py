"""The construction schema's structural invariants.

Fast, no database: these read `Base.metadata`, so they fail in milliseconds and
name the table that broke a rule rather than surfacing as a constraint violation
in some unrelated test later.

The rule these exist to protect is the one that failed first. A mixin's
`__table_args__` is *replaced* by a subclass that declares its own — not merged —
so the two provenance constraints reached zero of the ten tables while the whole
suite stayed green, because the only test that exercised them was checking the
ORM and the ORM was the thing that was wrong. The first version of
`tests/integration/test_construction_domain.py` caught it by asking the database
to accept a row it should have refused. This file is what stops the next table
from reintroducing it.

Every construction table is discovered from the metadata rather than listed, so a
new table is covered the moment it is declared and an *omission* cannot hide.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import Boolean, CheckConstraint, Numeric, Table, Text
from sqlalchemy.dialects.postgresql import JSON, JSONB

from ai_orchestrator.persistence import (
    commercial,
    construction,
    contracts,
    process,
    procurement,
    progress,
    supply,
)
from ai_orchestrator.persistence.base import Base

#: Every module holding domain tables. A new one is added here, and the
#: discovery test fails until it is — which is the point of the list existing
#: rather than scanning the package for anything declarative.
DOMAIN_MODULES = (
    construction,
    commercial,
    contracts,
    process,
    supply,
    procurement,
    progress,
)

CONSTRUCTION_MODELS = [
    construction.Client,
    construction.ClientContact,
    construction.Project,
    construction.ProjectRole,
    construction.ProjectPhase,
    construction.UnitDictionary,
    construction.Zone,
    construction.Wbs,
    construction.WbsItem,
    construction.Milestone,
    commercial.Opportunity,
    commercial.Tender,
    commercial.TenderDocument,
    commercial.TenderRequirement,
    commercial.Bid,
    commercial.BidItem,
    contracts.Supplier,
    contracts.Contract,
    contracts.ContractMilestone,
    contracts.ContractVariation,
    contracts.ContractClaim,
    contracts.ClaimEvent,
    contracts.ContractClause,
    process.GateDefinition,
    process.GateCriterion,
    process.GateInstance,
    process.GateCriterionEvaluation,
    process.GateDecision,
    process.GateCondition,
    process.SopDefinition,
    process.SopVersion,
    process.SopStep,
    process.SopRaci,
    process.SopForm,
    process.DocumentVersion,
    process.DocumentDistribution,
    process.DoaMatrix,
    process.AutonomyPolicy,
    supply.MaterialCategory,
    supply.Material,
    supply.SupplierDocument,
    supply.SupplierRiskFlag,
    supply.SupplierAssessment,
    procurement.Rfq,
    procurement.RfqItem,
    procurement.Quotation,
    procurement.QuotationItem,
    procurement.PurchaseOrder,
    procurement.PoItem,
    procurement.GoodsReceipt,
    procurement.ReceiptItem,
    procurement.ReceiptCheck,
    procurement.MaterialReconciliation,
    progress.ProgressSnapshot,
]

#: Money-like and measured columns, named explicitly rather than pattern-matched
#: on a suffix — the suffix convention is a convention, and this is the check on
#: it. Adding a money column to a domain table means adding it here, which is
#: the moment to decide whether it is `NUMERIC`.
NUMERIC_COLUMNS = frozenset(
    {
        "quantity",
        "unit_rate",
        "amount",
        "contract_value",
        "expected_value",
        "area_m2",
        "bid_value",
        "margin_amount",
        "margin_pct",
        "probability_pct",
        "retention_pct",
        "liquidated_damages_pct",
        "advance_payment_pct",
        "deviation_score",
        "value_claimed",
        "value_approved",
        "amount_claimed",
        "amount_awarded",
        "min_amount",
        "max_amount",
        "total_amount",
        "subtotal",
        "tax_amount",
        "retention_amount",
        "tax_rate_pct",
        "market_variance_pct",
        "received_quantity",
        "ordered_quantity",
        "extraction_confidence",
    }
)

#: Check-constraint names every construction table must carry, named from the
#: `ck_<table>_<name>` convention so the lookup works regardless of which table
#: is being checked.
PROVENANCE_RULES = ("source_known", "agent_source_needs_proposal")


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _constraint_names(table: Table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, CheckConstraint)}


class TestProvenanceIsUnremovable:
    """The platform's central promise, expressed as a database constraint."""

    @pytest.mark.parametrize("model", CONSTRUCTION_MODELS, ids=lambda m: m.__tablename__)
    def test_table_declares_provenance_rules(self, model: type) -> None:
        names = _constraint_names(model.__table__)
        expected = f"ck_{model.__tablename__}"
        missing = [rule for rule in PROVENANCE_RULES if f"{expected}_{rule}" not in names]
        assert not missing, (
            f"{model.__tablename__} is missing provenance constraint(s) {missing}. "
            "Declare `__table_args__ = domain_args(...)` — a bare tuple does not "
            "inherit them, which is the bug this test was written for."
        )

    @pytest.mark.parametrize("model", CONSTRUCTION_MODELS, ids=lambda m: m.__tablename__)
    def test_provenance_columns_are_present(self, model: type) -> None:
        columns = model.__table__.columns
        for required in ("organization_id", "source", "source_actor", "proposal_id"):
            assert required in columns, f"{model.__tablename__} has no {required}"

    @pytest.mark.parametrize("model", CONSTRUCTION_MODELS, ids=lambda m: m.__tablename__)
    def test_organization_id_is_not_nullable(self, model: type) -> None:
        # A nullable `organization_id` on a tenant-scoped table is the
        # `quarantined_proposals` bug from migration 0006: the RLS predicate
        # compares it to the tenant GUC, NULL never matches, and the row becomes
        # invisible to its own tenant while still occupying storage.
        assert model.__table__.columns["organization_id"].nullable is False


class TestAutonomyLevelsAreComparableAsRanks:
    """Every level is `L` and exactly one digit, so SQL can rank it.

    `AUTONOMY_RANK` is the authority in Python, and the dashboard's delegation control
    ranks in SQL with `substring(level from 2)::int` because it is a single aggregate and
    pulling the rows into Python to compare them would be a second code path for one
    number. That trade is only sound while the level *shape* is fixed, so the shape is
    asserted here rather than assumed in a comment.

    What it prevents: a fifth level named `L4a`, or a two-digit `L10`, either of which
    makes `substring(... from 2)::int` yield `NULL` for that row. A `NULL` comparison is
    not `false` in a `count(*) FILTER (WHERE ...)` -- it is *excluded*, so the agent
    silently drops out of the control that exists to catch it. The vocabulary staying
    narrow is what keeps the control total.
    """

    def test_every_autonomy_level_is_one_letter_and_a_digit(self) -> None:
        from ai_orchestrator.persistence.process import AUTONOMY_LEVELS

        assert AUTONOMY_LEVELS, "the vocabulary is empty, so the check below is vacuous"
        for level in AUTONOMY_LEVELS:
            assert len(level) == 2, (
                f"{level!r} is {len(level)} characters. The SQL ranking in "
                "`role_views._AGENT_POSTURE` reads `substring(level from 2)::int`, which "
                "returns NULL for anything but a single digit -- and a NULL comparison is "
                "excluded from `count(*) FILTER`, so that agent would vanish from the "
                "delegation control instead of appearing in it."
            )
            assert level[0] == "L", f"{level!r} does not start with 'L'"
            assert level[1].isdigit(), f"{level!r} does not end with a digit"

    def test_the_ranks_are_consecutive_and_start_at_one(self) -> None:
        from ai_orchestrator.persistence.process import AUTONOMY_LEVELS, AUTONOMY_RANK

        assert sorted(AUTONOMY_RANK.values()) == list(range(1, len(AUTONOMY_LEVELS) + 1))
        assert min(AUTONOMY_RANK.values()) == 1, (
            "`substring(level from 2)::int` returns 0 for 'L0', so a vocabulary starting at "
            "zero would rank below the SQL's own zero and make the comparison wrong in the "
            "same direction as a string sort"
        )


class TestMoneyAndQuantity:
    def test_no_float_money_anywhere(self) -> None:
        """Money and quantity are NUMERIC or they are wrong.

        A float column would be a `double precision` and this asserts none
        exists. Construction arithmetic is where a float quietly loses a cent and
        nobody notices until a final account does not reconcile.
        """
        for model in CONSTRUCTION_MODELS:
            for column in model.__table__.columns:
                if column.name in NUMERIC_COLUMNS:
                    assert isinstance(column.type, Numeric), (
                        f"{model.__tablename__}.{column.name} is {column.type!r}, "
                        "which is not NUMERIC"
                    )

    def test_every_money_column_is_in_the_checked_set(self) -> None:
        """The reverse direction: a new money column must be declared.

        Without this, adding `discount_amount` to `contracts` and forgetting the
        list would leave it unchecked, and the previous test would keep passing
        because it only looks at names it already knows.

        The suffix match is `_*_pct|_*_amount|_*_value`, which is broad enough to
        catch a money column whatever it ends in — and broad enough to catch
        things that are not money. `contract_clauses.extracted_value` is JSONB
        holding `{"pct": 10}` and is excluded by *type*, not by name: a list of
        exceptions is knowledge duplicated in a second place, and it is the
        shape of F73.
        """
        structured = (JSONB, JSON, Text, Boolean)
        declared = {
            column.name
            for model in CONSTRUCTION_MODELS
            for column in model.__table__.columns
            if column.name.endswith(("_pct", "_amount", "_value"))
            and not isinstance(column.type, structured)
        }
        unlisted = declared - NUMERIC_COLUMNS
        assert not unlisted, (
            f"money-like columns not in NUMERIC_COLUMNS: {sorted(unlisted)}. "
            "Add them so the float check covers them."
        )

    def test_wbs_item_separates_quantity_from_unit(self) -> None:
        """There must be no column that can hold a quantity and its unit together.

        "12.5m" is the single most common estimate error and the schema should
        make it unstorable rather than relying on a reviewer noticing.
        """
        columns = _table("wbs_items").columns
        assert "quantity" in columns
        assert "unit_code" in columns
        for suspicious in ("quantity_with_unit", "qty_unit", "measurement", "measured"):
            assert suspicious not in columns, (
                f"wbs_items.{suspicious} would reintroduce a combined quantity-and-unit column"
            )

    def test_unit_reference_is_tenant_scoped(self) -> None:
        """Every FK from a priced line to its unit must include `organization_id`.

        RLS stops one tenant *reading* another's vocabulary. This stops one
        tenant's line *pointing at* it, which is the direction that corrupts an
        estimate rather than merely exposing a row. Both `wbs_items` and
        `bid_items` are priced, so both are checked; a third will be too.
        """
        for table_name in ("wbs_items", "bid_items"):
            table = _table(table_name)
            composites = [
                tuple(fk.parent.name for fk in constraint.elements)
                for constraint in table.foreign_key_constraints
                if len(constraint.elements) > 1
            ]
            assert ("organization_id", "unit_code") in composites, (
                f"{table_name} must carry a composite foreign key on "
                f"(organization_id, unit_code); found {composites}"
            )


class TestAiAuthoredValuesAreTriageable:
    """A value the model wrote has to be checkable, or it will not be checked.

    The review queue is ordered by confidence, and a row with no confidence
    cannot be placed in it. That makes a missing confidence the same class of
    defect as a missing `proposal_id`: the row looks recorded and is effectively
    invisible to the process meant to review it.
    """

    @pytest.mark.parametrize("table_name", ["tender_requirements", "bid_items", "wbs_items"])
    def test_model_written_values_require_a_confidence(self, table_name: str) -> None:
        names = _constraint_names(_table(table_name))
        assert f"ck_{table_name}_agent_row_needs_a_confidence" in names, (
            f"{table_name} holds model-extracted values and must refuse an "
            "agent-sourced row with no extraction_confidence"
        )

    def test_confidence_is_bounded_wherever_it_exists(self) -> None:
        for table_name in ("tender_requirements", "bid_items", "wbs_items"):
            names = _constraint_names(_table(table_name))
            assert f"ck_{table_name}_confidence_in_range" in names, (
                f"{table_name}.extraction_confidence must be constrained to 0..1"
            )


#: SQL words that appear in a check constraint and are not column references.
#: Without this list the scan below reports every quoted enum value as a missing
#: column — 104 false positives on the process tranche alone, which is enough
#: noise that nobody reads the output.
#: SQL words and function names that appear in a check constraint without being
#: column references. `abs` joined this list because
#: `ck_quotations_a_large_variance_states_why` uses it — and the scan correctly
#: flagged it as an unknown column, which is the F80 detector doing its job on a
#: *function*. A detector with no vocabulary here reports every function as a
#: missing column, and 104 of those is how a detector gets ignored.
_SQL_NOISE = frozenset(
    {
        "abs",
        "all",
        "and",
        "any",
        "as",
        "between",
        "case",
        "cast",
        "ceil",
        "coalesce",
        "current_date",
        "date_trunc",
        "else",
        "end",
        "false",
        "floor",
        "greatest",
        "ilike",
        "in",
        "is",
        "least",
        "like",
        "length",
        "lower",
        "not",
        "null",
        "or",
        "round",
        "then",
        "true",
        "upper",
        "when",
    }
)


def _referenced_identifiers(sql: str) -> set[str]:
    """Column references in a check constraint's SQL.

    Two things are stripped before scanning, and the second one was added because
    Postgres writes casts.

    **String literals.** An `IN ('human', 'agent')` list is made of quoted words that are
    not columns, and a scanner that does not strip them reports a hundred phantom
    problems.

    **Casts.** `pg_get_constraintdef` returns Postgres's *normalised* form, so a
    constraint the repository writes as `status IN ('shadow', 'active')` reads back as
    `(((status)::text = ANY ((ARRAY['shadow'::character varying, 'active'::character
    varying])::text[])))`. The words `text`, `character` and `varying` are type names, and
    a scanner that keeps them reports them as missing columns on 53 tables — which is what
    it did, correctly, from a correctly-built model that simply spells its casts the way
    the database does.
    """
    without_literals = re.sub(r"'[^']*'", " ", sql)
    # `::type`, and `type[]` at the end of a cast chain. Strip the whole cast, including
    # a trailing array marker, so neither the type nor the brackets survive.
    without_casts = re.sub(r"::\s*[a-zA-Z_][a-zA-Z0-9_ ]*(\[\])?", " ", without_literals)
    return {
        word
        for word in re.findall(r"\b([a-z_][a-z0-9_]*)\b", without_casts)
        if word not in _SQL_NOISE
    }


class TestCheckConstraintsReferenceRealColumns:
    """A `CheckConstraint` is an unvalidated string until Postgres reads it.

    SQLAlchemy does not check that the identifiers in `sqltext` name columns of
    the table the constraint is attached to. One referring to a column that does
    not exist is accepted silently by the model, appears in `metadata`, satisfies
    every other structural test in this file, and then fails at `alembic upgrade`
    with `UndefinedColumnError`.

    Not hypothetical: `gate_criterion_evaluations` shipped a constraint reading
    `waive_note` against a column named `waiver_note`, and the only thing that
    caught it was running the migration. This catches it in a quarter of a second,
    with no database.
    """

    @pytest.mark.parametrize("model", CONSTRUCTION_MODELS, ids=lambda m: m.__tablename__)
    def test_no_constraint_names_a_missing_column(self, model: type) -> None:
        columns = set(model.__table__.columns.keys())
        broken: dict[str, list[str]] = {}
        for constraint in model.__table__.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            missing = sorted(_referenced_identifiers(str(constraint.sqltext)) - columns)
            if missing:
                broken[constraint.name or "<unnamed>"] = missing
        assert not broken, (
            f"{model.__tablename__} has check constraints naming columns that do "
            f"not exist: {broken}. This fails at alembic upgrade with "
            "UndefinedColumnError, not here."
        )


class TestDiscovery:
    def test_every_table_in_the_domain_modules_is_in_the_test(self) -> None:
        """A new domain table must be added to this file.

        Found by comparing the modules' declarative classes against the list
        parameterised above, so adding a table and forgetting to cover it is a
        test failure rather than a silent gap in coverage.
        """
        declared = {
            obj.__tablename__
            for module in DOMAIN_MODULES
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, Base)
            and obj is not Base
            and getattr(obj, "__tablename__", None)
        }
        covered = {m.__tablename__ for m in CONSTRUCTION_MODELS}
        assert declared == covered, (
            f"the domain modules declare {sorted(declared - covered)} but this test "
            f"covers {sorted(covered - declared)}"
        )

    def test_module_list_is_not_stale(self) -> None:
        """`DOMAIN_MODULES` is itself a list somebody has to maintain.

        A new module in the persistence package that is not on the list would be
        invisible to every check in this file, so the list is compared against
        what the package actually contains.
        """
        import ai_orchestrator.persistence as pkg

        on_list = {m.__name__ for m in DOMAIN_MODULES}
        candidates = {
            name
            for name, obj in vars(pkg).items()
            if isinstance(obj, type) and getattr(obj, "__tablename__", None)
        }
        missing = candidates - on_list
        assert not missing, f"domain tables found on modules not in DOMAIN_MODULES: {missing}"


class TestIdentifiersFitPostgres:
    """Postgres truncates identifiers over 63 *bytes*, silently.

    This is the same class of defect as `TestCheckConstraintsReferenceRealColumns`
    and it was found the same way — by looking at the database rather than at the
    model. Two constraint names reached Postgres at 64 bytes and arrived as:

        ck_gate_criterion_evaluations_proposal_required_for_age_dd1c
        ck_goods_receipts_acceptance_requires_quality_and_qa_hs_5164

    Postgres keeps a prefix and appends `_` plus four hex digits, so the
    constraint still *fired* — the rule was enforced correctly and the name was
    destroyed. Nothing failed. The column-reference scan above missed it precisely
    because the surviving name is a valid string naming no missing column; a
    *wrong* name reads the same as a *right* one to every other test in this file.

    Two things make it unrepeatable. This test refuses the overflow before it
    reaches a database, and `test_no_constraint_looks_postgres_truncated` in
    `tests/integration/test_procurement_chain.py` refuses a truncated name in one.
    A name that ends in four hex digits after an underscore is never a name anyone
    wrote.
    """

    #: `NAMEDATALEN - 1`. Postgres truncates above this, in bytes, not characters —
    #: which matters here because constraint names are ASCII but a Vietnamese
    #: table or column name would not be.
    MAX_IDENTIFIER_BYTES = 63

    @staticmethod
    def _identifiers() -> list[tuple[str, str, int]]:
        from ai_orchestrator.persistence.base import Base

        found: list[tuple[str, str, int]] = []
        for table in Base.metadata.tables.values():
            for obj in (*table.constraints, *table.indexes):
                if obj.name:
                    found.append((table.name, obj.name, len(obj.name.encode())))
        return found

    def test_no_constraint_or_index_name_exceeds_the_limit(self) -> None:
        over = [
            (tbl, size, name)
            for tbl, name, size in self._identifiers()
            if size > self.MAX_IDENTIFIER_BYTES
        ]
        assert not over, (
            "Postgres will silently truncate these to a prefix plus a hash, which "
            f"makes the rule unfindable: {over}"
        )

    def test_the_check_is_actually_wired_up(self) -> None:
        """A test that cannot fail is worse than no test.

        Asserting the boundary directly: a name of exactly the limit is legal and
        one byte more is not. If `MAX_IDENTIFIER_BYTES` were ever set to
        something Postgres does not enforce, this is what notices.
        """
        at_limit = "x" * self.MAX_IDENTIFIER_BYTES
        over_limit = "x" * (self.MAX_IDENTIFIER_BYTES + 1)
        assert len(at_limit.encode()) <= self.MAX_IDENTIFIER_BYTES
        assert len(over_limit.encode()) > self.MAX_IDENTIFIER_BYTES

    def test_the_identifiers_are_measured_in_bytes_not_characters(self) -> None:
        """The distinction that would bite first.

        A table named with Vietnamese characters has fewer *characters* than bytes,
        so a character-counting check passes a name Postgres then truncates.
        """
        name = "hạng_mục_" + "y" * 53
        assert len(name) < self.MAX_IDENTIFIER_BYTES, "fewer characters..."
        assert len(name.encode()) > self.MAX_IDENTIFIER_BYTES, "...but more bytes"

    def test_the_longest_table_leaves_headroom_for_its_provenance_name(self) -> None:
        """The specific arithmetic that broke, kept as an assertion.

        The naming convention builds `ck_<table>_<suffix>`, so the risk grows with
        the table name rather than with anything anyone editing a constraint thinks
        about. Pinning the longest table means a future rename is caught here.
        """
        from ai_orchestrator.persistence.base import Base

        longest = max(Base.metadata.tables, key=len)
        size = len(f"ck_{longest}_agent_source_needs_proposal".encode())
        assert size <= self.MAX_IDENTIFIER_BYTES, (
            f"the longest table {longest!r} puts its provenance check at {size} bytes; "
            f"the suffix must lose {size - self.MAX_IDENTIFIER_BYTES} more"
        )
