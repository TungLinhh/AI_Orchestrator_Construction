"""No table may declare the same foreign-key name twice, or a column twice.

The checks that exist in this repository caught most of F133. None of them caught *this*:
a table carrying a composite key and a bare key under one name, because both are valid
declarations, both are in the metadata, and the drift test only failed once alembic
compared them and reported a single `add_fk` for a constraint that looked correct.

A duplicate name is not a subtle problem. It is a `CREATE TABLE` that Postgres refuses
with a duplicate-object error, and until that migration is run the schema is a thing that
only exists in Python.

## Why the test is about the *model* and not the database

The database cannot have a duplicate — it would not accept one. So the only place this can
be wrong is the ORM, and the test reads `Base.metadata` directly. No database, no
migration, no fixture: it is a unit test wearing an integration test's name in spirit, and
it runs in milliseconds.

## What each assertion is for

* **duplicate foreign-key names** — one relationship, two declarations. The naming
  convention gives a column-level key and a table-level key the same name, so adding a
  composite key without removing the bare one produces this, and the model imports
  cleanly.
* **duplicate indexes by name** — the same failure with a different object, and the one
  that cost six indexes in F133 when a second `__table_args__` silently replaced the first.
* **a `__table_args__` that is not a tuple or a call** — a leftover opener with no
  arguments, which declares no constraints while reading as though it declares some.
"""

from __future__ import annotations

import ast
import collections
import importlib
import pathlib
import warnings
from collections.abc import Iterator

import pytest

warnings.filterwarnings("ignore")

PERSISTENCE = pathlib.Path(__file__).resolve().parents[2] / "src/ai_orchestrator/persistence"


def _load_every_model() -> None:
    """Import every model module, so `Base.metadata` is complete.

    `Base` on its own registers nothing, and a metadata check against nine of a hundred
    tables passes vacuously — which is the failure mode of a test that measures the wrong
    population.
    """
    for module in sorted(PERSISTENCE.glob("*.py")):
        if module.stem != "__init__":
            importlib.import_module(f"ai_orchestrator.persistence.{module.stem}")


@pytest.fixture(scope="module")
def metadata() -> Iterator[object]:
    _load_every_model()
    from ai_orchestrator.persistence.base import Base

    yield Base.metadata


def _duplicates(names: list[str]) -> list[str]:
    return sorted(name for name, count in collections.Counter(names).items() if count > 1)


class TestNoDuplicateConstraintNames:
    def test_no_table_declares_one_foreign_key_name_twice(self, metadata) -> None:
        offenders: list[str] = []
        for name, table in sorted(metadata.tables.items()):
            for duplicate in _duplicates([fk.name for fk in table.foreign_key_constraints]):
                offenders.append(f"{name}.{duplicate}")
        assert not offenders, (
            "A table declares the same foreign key twice. The naming convention gives a "
            "column-level key and a composite key the same name, so adding one without "
            f"removing the other produces this: {offenders}"
        )

    def test_no_table_declares_one_index_name_twice(self, metadata) -> None:
        offenders: list[str] = []
        for name, table in sorted(metadata.tables.items()):
            for duplicate in _duplicates([index.name for index in table.indexes]):
                offenders.append(f"{name}.{duplicate}")
        assert not offenders, f"Duplicate index names: {offenders}"

    def test_no_index_and_constraint_share_a_name(self, metadata) -> None:
        """A unique index and a constraint on the same pair are two ways to say one thing.

        Both are accepted by Postgres, and the second is refused, so this is the shape
        that a copy-paste of a constraint into `__table_args__` produces.
        """
        offenders: list[str] = []
        for name, table in sorted(metadata.tables.items()):
            index_names = {index.name for index in table.indexes}
            for fk in table.foreign_key_constraints:
                if fk.name in index_names:
                    offenders.append(f"{name}.{fk.name}")
        assert not offenders, f"An index and a foreign key share a name: {offenders}"


class TestNoDuplicateColumnNames:
    def test_no_class_declares_one_column_twice(self) -> None:
        """`sqlalchemy` keeps the last assignment; mypy is what complains.

        F133 duplicated three class bodies with a text edit. Each one imported cleanly and
        declared the right table, and the only signal was `Name "id" already defined` from
        a different tool entirely.
        """
        offenders: list[str] = []
        for module in sorted(PERSISTENCE.glob("*.py")):
            tree = ast.parse(module.read_text(), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = [
                    stmt.target.id
                    for stmt in node.body
                    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
                ]
                for duplicate in _duplicates(names):
                    offenders.append(f"{module.name}:{node.name}.{duplicate}")
        assert not offenders, f"A class declares one column twice: {offenders}"


class TestTableArgsIsWellFormed:
    def test_every_table_has_at_most_one_table_args(self) -> None:
        """`__table_args__` is a class attribute: a second assignment silently wins.

        This is the original F133 mistake and the one that cost six indexes with no error
        at all. The check is on the *source*, because the metadata cannot show it — by the
        time SQLAlchemy has read the class, the first assignment is already gone.
        """
        offenders: list[str] = []
        for module in sorted(PERSISTENCE.glob("*.py")):
            tree = ast.parse(module.read_text(), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not any(
                    isinstance(b, ast.Assign)
                    and any(getattr(t, "id", None) == "__tablename__" for t in b.targets)
                    for b in node.body
                ):
                    continue
                count = sum(
                    1
                    for b in node.body
                    if isinstance(b, ast.Assign)
                    and any(getattr(t, "id", None) == "__table_args__" for t in b.targets)
                )
                if count > 1:
                    offenders.append(f"{module.name}:{node.name} x{count}")
        assert not offenders, (
            "A class declares __table_args__ more than once, and only the last one "
            f"survives: {offenders}"
        )

    def test_every_table_args_is_a_tuple_or_a_call(self, metadata) -> None:
        """A `domain_args(` with no arguments declares nothing and reads as though it does.

        Three of these were left behind by F133's repair scripts, and the class they
        belonged to had lost every index it had.
        """
        offenders: list[str] = []
        for module in sorted(PERSISTENCE.glob("*.py")):
            tree = ast.parse(module.read_text(), filename=str(module))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for stmt in node.body:
                    if not (
                        isinstance(stmt, ast.Assign)
                        and any(getattr(t, "id", None) == "__table_args__" for t in stmt.targets)
                    ):
                        continue
                    value = stmt.value
                    if isinstance(value, ast.Tuple) and value.elts:
                        continue
                    if isinstance(value, ast.Call) and value.args:
                        continue
                    offenders.append(f"{module.name}:{node.name} line {stmt.lineno}")
        assert not offenders, f"An empty or malformed __table_args__: {offenders}"


class TestTheTenantPairIsDeclaredWhereAForeignKeyNeedsIt:
    def test_every_composite_foreign_key_references_something_unique(self, metadata) -> None:
        """A composite key needs its **referenced columns** to be unique on the parent.

        Migration `0020` adds `UNIQUE (organization_id, id)` to ten parents for exactly
        this reason. A composite key added to a parent that lacks the pair is refused by
        Postgres at `CREATE TABLE` time, which is the loud version of the problem.

        The first version of this check assumed the pair is always
        `(organization_id, id)`, and reported `units_dictionary` four times. That table has
        no `id` column: its primary key is `(organization_id, code)`, and the four keys
        pointing at it reference *that*. The assumption was the bug, not the schema — so
        the rule below asks about the referenced columns rather than about a name.
        """
        offenders: list[str] = []
        for name, table in sorted(metadata.tables.items()):
            for fk in table.foreign_key_constraints:
                locals_ = list(fk.columns.keys())
                # A key on `organization_id` *alone* is not composite -- it is the ordinary
                # tenant key every table carries, and it needs no unique pair.
                if len(locals_) < 2:
                    continue
                parent = fk.elements[0].column.table
                remote = {element.column.name for element in fk.elements}
                covered = (
                    any(
                        set(index.columns.keys()) == remote and index.unique
                        for index in parent.indexes
                    )
                    or set(parent.primary_key.columns.keys()) == remote
                )
                if not covered:
                    offenders.append(f"{name}.{fk.name} -> {parent.name}{sorted(remote)}")
        assert not offenders, (
            "A composite foreign key references columns that are neither a unique index "
            f"nor the primary key on the parent: {offenders}"
        )
