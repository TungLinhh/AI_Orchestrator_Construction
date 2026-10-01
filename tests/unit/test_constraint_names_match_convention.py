"""Every constraint's name is the name the naming convention composes.

## Why this file exists

Four times in one session, the same defect: a check constraint was written with a name that
**already carried** the convention's prefix, and the convention composed it again.

* `name="ck_sop_steps_status_known"` on `sop_steps` reached Postgres as
  `ck_sop_steps_ck_sop_steps_status_known` — a name in neither the model nor the schema,
  which the drift test reported as a paired remove/add forever. (F138.)
* The same, in `rebuild_model_table_args.py`, across 56 tables at once.
* `migration 0024`: `op.create_check_constraint("ck_wbs_items_agent_row_needs_a_confidence")`
  produced a doubled name, and `downgrade` then failed with `UndefinedObjectError` because
  it dropped the name the migration had *asked* for.
* `migration 0025`, the same, twice, on `documents`.

Four is a pattern. Each occurrence was found by running something, and each fix was a
manual edit the next author would have had to rediscover. **This file is the instrument.**

## The rule, and it is asymmetric — read the convention, do not remember it

The convention in this repository is five keys, and only one of them composes a *name you
supply*:

| key | pattern | what it does with an explicit `name=` |
|---|---|---|
| `ck` | `ck_%(table_name)s_%(constraint_name)s` | **composes** — the prefix is added to your name |
| `uq` | `uq_%(table_name)s_%(column_0_name)s` | does not compose — your name is used as given |
| `ix` | `ix_%(table_name)s_%(column_0_N_name)s` | does not compose |
| `fk` | `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` | built from the columns |
| `pk` | `pk_%(table_name)s` | n/a |

So a **`CheckConstraint`** carries the **suffix** — `name="status_known"` becomes
`ck_sop_steps_status_known` — and a **`UniqueConstraint`** carries the **full name**,
because `uq` has no `%(constraint_name)s` token to fill.

`uq_consumer_offsets_consumer` is therefore *correct*: it is what
`uq_%(table_name)s_%(column_0_name)s` produces for a unique constraint on
`(organization_id, consumer)`. An earlier version of this file reported it as doubled,
which is what prompted reading the convention instead of inferring it a fourth time.

The other half of the naming problem -- a name longer than Postgres's 63 bytes, which
comes back truncated with a hash appended -- has its own instrument in
`test_construction_schema.py::TestIdentifiersFitPostgres`. It belongs there and not here:
that one scans all of `Base.metadata`, covers indexes as well as constraints, measures
bytes, and includes a test proving its own limit is live. An earlier version of this file
carried a second copy, which is the same rule in two places.

## Why it needs no database

Everything here is `Base.metadata` and the convention, so it runs in milliseconds and needs
no schema. The counterpart — that the *schema* agrees — is `test_there_is_no_drift`; this
file is the one that says *which* object is wrong rather than that something is.
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from ai_orchestrator.persistence.base import Base

#: Only `CheckConstraint` composes the name you give it. `UniqueConstraint` and `Index` do
#: not, and treating them as though they did is how a real index got deleted from a model
#: once (F138).
COMPOSES_THE_NAME = ("CheckConstraint",)

_PREFIX = re.compile(r"^ck_([a-z0-9_]+)_")


def _load_every_model() -> None:
    import importlib
    import pathlib

    persistence = pathlib.Path(__file__).resolve().parents[2] / "src/ai_orchestrator/persistence"
    for path in sorted(persistence.glob("*.py")):
        if path.stem != "__init__":
            importlib.import_module(f"ai_orchestrator.persistence.{path.stem}")


@pytest.fixture(scope="module", autouse=True)
def _metadata() -> None:
    _load_every_model()


def test_the_convention_really_does_compose_a_check_name() -> None:
    """The premise of the assertion below, checked so it cannot be vacuous.

    If the convention stopped composing, the test below would find nothing to complain
    about and pass with a green tick — the failure mode of a test whose premise silently
    became false. So the premise is read out of the metadata and asserted.
    """
    convention = Base.metadata.naming_convention
    assert convention, "no naming convention is configured; the rule below is untestable"
    pattern = convention.get("ck")
    assert pattern == "ck_%(table_name)s_%(constraint_name)s", (
        f"the check-constraint convention is {pattern!r}, not the "
        "`ck_%(table_name)s_%(constraint_name)s` this file's rule assumes. Read it and "
        "correct the rule rather than trusting a green test."
    )


def test_no_check_constraint_name_already_carries_the_table_prefix() -> None:
    offenders: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for constraint in table.constraints:
            if constraint.__class__.__name__ not in COMPOSES_THE_NAME:
                continue
            name = str(constraint.name or "")
            match = _PREFIX.match(name)
            if match and match.group(1) == table_name:
                offenders.append(f"{table_name}.{name}")
    assert not offenders, (
        "A check constraint's name already carries `ck_<table>_`, so the convention "
        "composes it again and the schema holds a name the model does not. Write the "
        "suffix. Offenders:\n  " + "\n  ".join(sorted(offenders))
    )


def test_every_check_constraint_is_uniquely_named_per_table() -> None:
    """Two check constraints composing to one name is a `CREATE TABLE` Postgres refuses."""
    offenders: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        names = [
            str(c.name)
            for c in table.constraints
            if c.__class__.__name__ in COMPOSES_THE_NAME and c.name
        ]
        for name, count in Counter(names).items():
            if count > 1:
                offenders.append(f"{table_name}.{name} x{count}")
    assert not offenders, f"Two check constraints compose to one name: {sorted(offenders)}"


def test_a_unique_constraint_keeps_its_prefixed_name() -> None:
    """The other half of the rule, asserted so it is not "fixed" later.

    `uq` composes the *first column*, not a name you supply, so a prefixed name is correct
    there and stripping it would be a deletion.
    """
    from ai_orchestrator.persistence.models import ConsumerOffset

    names = {
        str(c.name)
        for c in ConsumerOffset.__table__.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    }
    assert "uq_consumer_offsets_consumer" in names, (
        f"the unique constraint on `consumer_offsets` lost its name; names: {sorted(names)}"
    )


def test_an_index_name_is_not_stripped() -> None:
    """`Index` names are verbatim. Stripping one is a deletion, not a fix."""
    from ai_orchestrator.persistence.supply import Material

    names = {str(i.name) for i in Material.__table__.indexes}
    assert "uq_materials_org_id" in names, (
        f"the tenant key is missing from `materials`; names: {sorted(n for n in names if n)}"
    )
