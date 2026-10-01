"""Emit migration 0020 from the live metadata, so no column or name is hand-typed.

The output is a migration file that gets *reviewed* before it is run — this is a
generator, not a substitute for reading. Hand-writing 20 foreign-key swaps means 20
chances to transpose a column name, and a transposed `ForeignKeyConstraint` does not
fail loudly: it references a column that exists, so the migration succeeds and enforces
the wrong thing.
"""

from __future__ import annotations

import importlib
import warnings

from ai_orchestrator.persistence.base import Base

# Before the model modules, or SQLAlchemy's deprecation notices fire on import.
#
# Loaded by name rather than as eleven `import` statements. Each of those exists purely
# for its side effect -- registering tables on `Base.metadata` -- and a linter cannot see
# that, so every one needs a `noqa`. Then the two exemptions fight: the file-level one
# that silences `E402` also silences `F401`, which makes the per-line `F401` "unused" and
# trips `RUF100`, which sends you back to add `E402` again. A loop has neither problem,
# and it says in one place why the modules are loaded at all.
warnings.filterwarnings("ignore")

for _module in (
    "agent_framework",
    "commercial",
    "construction",
    "contracts",
    "models",
    "procurement",
    "process",
    "progress",
    "supply",
):
    importlib.import_module(f"ai_orchestrator.persistence.{_module}")

CHAIN = (
    "rfqs",
    "rfq_items",
    "quotations",
    "quotation_items",
    "purchase_orders",
    "po_items",
    "goods_receipts",
    "receipt_items",
    "receipt_checks",
)


def has_tenant_key(table) -> bool:
    return any(
        set(i.columns.keys()) == {"organization_id", "id"} and i.unique for i in table.indexes
    )


def main() -> None:
    pairs: list[tuple[str, str, str, str, str]] = []  # child, fk name, column, parent, label
    parents: set[str] = set()
    for name in CHAIN:
        table = Base.metadata.tables[name]
        for fk in table.foreign_key_constraints:
            if len(fk.columns) != 1:
                continue
            element = fk.elements[0]
            if element.column.table.name == "organizations":
                continue
            column = next(iter(fk.columns))
            parent = element.column.table.name
            if has_tenant_key(element.column.table):
                continue  # already covered elsewhere; nothing to do
            pairs.append((name, fk.name, column, parent, fk.name))
            parents.add(parent)

    out = ["__FK__ = ["]
    for child, fk_name, column, parent, _ in pairs:
        out.append(f'    ("{child}", "{fk_name}", "{column}", "{parent}"),')
    out.append("]")
    out.append("__INDEXES__ = [")
    for parent in sorted(parents):
        out.append(f'    "{parent}",')
    out.append("]")

    print(f"# {len(pairs)} foreign keys across {len(CHAIN)} tables")
    print(f"# {len(parents)} parents need a unique (organization_id, id) first:")
    print("\n".join(out))


if __name__ == "__main__":
    main()
