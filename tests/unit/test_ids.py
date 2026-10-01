"""Every id type must be able to mint an id.

This file exists because of a specific failure. `A2AAgentId._PREFIX` was `"a2a"`
and `make_id` validated prefixes with `[a-z]{2,5}` — so `A2AAgentId.create()`
raised, and the entire agent-to-agent id family was unusable. Nothing had caught
it because a type that cannot be constructed is invisible to every test that
does not construct it.

Walking the subclasses is the structural fix. Adding a new id type now gets it
checked for free, and a prefix that does not match the validator fails here
rather than in a request three layers away.
"""

from __future__ import annotations

import inspect
import re

import pytest

from ai_orchestrator.domain import ids as ids_module
from ai_orchestrator.domain.ids import BrandedId, make_id

pytestmark = pytest.mark.unit


def _branded_subclasses() -> list[type[BrandedId]]:
    found = []
    for _, obj in inspect.getmembers(ids_module, inspect.isclass):
        if issubclass(obj, BrandedId) and obj is not BrandedId:
            found.append(obj)
    return sorted(found, key=lambda c: c.__name__)


BRANDED = _branded_subclasses()


def test_the_walk_found_the_types() -> None:
    """A test that passes because the walk found nothing is worse than no test."""
    assert len(BRANDED) >= 20, f"only found {len(BRANDED)} id types"


@pytest.mark.parametrize("cls", BRANDED, ids=lambda c: c.__name__)
def test_every_id_type_can_mint_an_id(cls: type[BrandedId]) -> None:
    """The regression this file was written for.

    A prefix the validator rejects makes the type unusable, and nothing else
    notices until something tries to use it.
    """
    value = cls.create()
    assert str(value).startswith(f"{cls._PREFIX}_"), (
        f"{cls.__name__} produced {value!r}, which does not carry its own prefix"
    )


@pytest.mark.parametrize("cls", BRANDED, ids=lambda c: c.__name__)
def test_every_prefix_matches_the_validator(cls: type[BrandedId]) -> None:
    assert re.fullmatch(r"[a-z0-9]{2,5}", cls._PREFIX), (
        f"{cls.__name__} has prefix {cls._PREFIX!r}, which make_id would reject"
    )


def test_a_bad_prefix_is_rejected() -> None:
    """The validator is still a validator: a prefix it would accept is the bug."""
    for bad in ("", "A", "toolongprefix", "has_underscore", "UPPER"):
        with pytest.raises(ValueError, match="invalid id prefix"):
            make_id(bad)


def test_ids_are_unique_within_a_millisecond() -> None:
    """Naive randomness re-randomises inside the same millisecond and the ids
    stop ordering, which breaks every `ORDER BY id` that assumes creation order."""
    minted = [make_id("tst") for _ in range(500)]
    assert len(set(minted)) == 500
    assert minted == sorted(minted), "ids minted in one burst are not monotonic"
