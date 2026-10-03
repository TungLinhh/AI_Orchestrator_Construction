"""Who may read what, across the three tiers.

## The problem this solves

The platform has a hierarchy — a chief, three offices, seven departments — and one
tenant boundary enforced by row-level security. It had **no unit boundary**. An agent
running `internal_database_query` could read any row belonging to any agent in the
tenant, because "organisation_id matches" was the only rule, and the whole organisation
is one organisation.

That is not a hypothetical. The tool refuses writes, refuses multi-statement queries and
refuses catalog reads — three careful controls — and then hands a department the entire
finance ledger. The controls were all about *safety* and none of them were about
*separation*.

## The rule

Depth is 0 for the company, 1 for an office, 2 for a department. Higher tier means
**smaller** depth, and this module reads "higher" as smaller throughout.

| The reader | Its own unit | Its descendants | Its ancestors | A peer's branch |
|---|---|---|---|---|
| chief (0) | full | full | — | full (it is the top) |
| office (1) | full | full | full (the chief) | **status only** |
| department (2) | full | full | full (its office, the chief) | **status only** |

Three answers, because "can they read it" is not one question:

* :func:`may_read_content` — the substance: task goals, run summaries, decisions,
  costs. An office reads its departments'. A department does not read a peer's.
* :func:`may_read_status` — existence, lifecycle, health, autonomy level, run count.
  A department may see that Finance exists and is busy: it has to, or it cannot route to
  it and it cannot tell a colleague from a vacancy. It may not read Finance's work.
* :func:`may_delegate_to` — who this agent may hand work to. Downward only. Nobody
  delegates sideways or up, and this is a different question from reading.

**Ancestors are readable, and that is deliberate.** A department that cannot see its own
office cannot know who it reports to, cannot tell which escalations are legitimate, and
cannot say whether its own escalation is going anywhere. Escalating upward is the
product's safety valve; hiding the way up disables it.

**Peers are status-only, and that is the whole point.** Two departments working the same
handover should be able to see that each other is alive and busy. They should not be
able to read each other's work, because then the review that is supposed to judge a
result is judging something the reviewer was handed.

## Why the policy is here and not in the query

It is pure, it takes two unit paths, and it has no session, no SQL and no clock. The
first version of this idea was a wrapper around `internal_database_query` that rewrote
the model's `SELECT` to add a predicate. That was abandoned: rewriting SQL written by a
model is how you get an injection you did not write, and a policy that can be defeated
by a syntax error is not a boundary. A decision function that returns `yes`/`no` can be
tested exhaustively, and can be used from as many call sites as need it.

The database boundary that already exists — `app.current_tenant` and RLS — stays as it
is. This module governs *unit* visibility on top of tenant visibility, not instead of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ai_orchestrator.domain.errors import ValidationError

#: Depth of the company root, which is the chief's unit.
ROOT_DEPTH = 0


class Visibility(StrEnum):
    """How much of another unit an agent may see.

    Ordered, so a caller can ask for a level and take everything at or below it:
    ``NONE < STATUS < CONTENT``.
    """

    NONE = "none"
    STATUS = "status"
    CONTENT = "content"


def _at_least(wanted: Visibility, available: Visibility) -> bool:
    return available != Visibility.NONE and (
        wanted == Visibility.STATUS or available == Visibility.CONTENT
    )


@dataclass(frozen=True, slots=True)
class UnitRef:
    """One unit, as far as this policy cares: where it sits and which branch it is in.

    `path` is the organisation's own materialised path — the root is `/`, a child of the
    root is `/<id>/` — and it is the *only* thing that decides branch membership. Depth
    alone is not enough: two departments both sit at depth 2 and neither is under the
    other, and a policy that compared depths would hand Finance the whole organisation.

    `slug` is carried for messages. Nothing here reads it.
    """

    id: str
    slug: str
    depth: int
    path: str

    def __post_init__(self) -> None:
        if self.depth < 0:
            msg = "a unit cannot sit above the root"
            raise ValidationError(msg, details={"depth": self.depth})
        if not self.path.startswith("/"):
            msg = "path must start with /"
            raise ValidationError(msg, details={"path": self.path})

    @property
    def is_root(self) -> bool:
        return self.depth == ROOT_DEPTH


def is_ancestor_or_self(upper: UnitRef, lower: UnitRef) -> bool:
    """Is `upper` on `lower`'s path to the root, or is it `lower` itself?

    The test is `upper.path` being a prefix of `lower.path` **with a `/` boundary**,
    and the boundary is not decoration. The seed writes the root's path as `/root` with
    no trailing slash and every other unit's with one, so a bare prefix test would make
    a hypothetical `/root-x` a child of `/root` and hand it the root's content. Measured
    against the live seed, which is where the asymmetry was found:

        root           company     depth=0  path='/root'
        back-office    office      depth=1  path='/root/back-office'
        hr             department  depth=2  path='/root/back-office/hr'

    Normalising to a trailing slash makes `/root` and `/root-x` siblings rather than
    parent and child, and leaves every real relationship untouched.
    """
    if upper.id == lower.id:
        return True
    if upper.depth >= lower.depth:
        return False
    prefix = upper.path if upper.path.endswith("/") else upper.path + "/"
    return lower.path.startswith(prefix)


def visibility_of(reader: UnitRef, target: UnitRef) -> Visibility:
    """What `reader` may see of `target`.

    The decision, in one place, for both :func:`may_read_content` and
    :func:`may_read_status`. Callers ask for a level rather than re-deriving the rules,
    because two implementations of one rule is how they come to disagree.
    """
    if reader.id == target.id:
        return Visibility.CONTENT
    if is_ancestor_or_self(reader, target):
        # Downward: the reader is above the target.
        return Visibility.CONTENT
    if is_ancestor_or_self(target, reader):
        # Upward: the reader is below the target, and the target is on its way to the
        # chief. Reading upward is the escalation path and stays open.
        return Visibility.CONTENT
    # Neither is above the other. Same branch or not, they are peers, and a peer gets
    # enough to route and not enough to audit.
    return Visibility.STATUS


def may_read_content(reader: UnitRef, target: UnitRef) -> bool:
    """May `reader` read the substance of `target`'s work?"""
    return _at_least(Visibility.CONTENT, visibility_of(reader, target))


def may_read_status(reader: UnitRef, target: UnitRef) -> bool:
    """May `reader` see that `target` exists, and whether it is busy?"""
    return _at_least(Visibility.STATUS, visibility_of(reader, target))


def may_delegate_to(reader: UnitRef, target: UnitRef) -> bool:
    """May `reader` hand work to `target`?

    **Downward only**, and this is not a reading rule. An agent delegating upward is a
    chain with a loop in it, and delegating sideways is a peer reviewing itself — which
    is the failure `no_delegation` was built to stop, reached from the other direction.

    The chief is the exception that makes it work: the root delegates to everything, and
    `is_root` is what says so, because there is nothing above the root to delegate to.
    """
    if reader.id == target.id:
        return False
    if reader.is_root:
        return True
    return is_ancestor_or_self(reader, target) and not is_ancestor_or_self(target, reader)


def scope_of(reader: UnitRef, units: list[UnitRef]) -> dict[str, Visibility]:
    """Every unit's visibility to `reader`, in one call.

    The shape an endpoint or a tool wants, and the shape a test can assert without a
    database. Exposed because "which units can this agent see" is asked by more than one
    caller and re-deriving it per call site is how the answers diverge.
    """
    return {unit.slug: visibility_of(reader, unit) for unit in units}


def describe(visibilities: dict[str, Visibility]) -> str:
    """One line a person can read, for a CLI or a log line."""
    order = {Visibility.CONTENT: 0, Visibility.STATUS: 1, Visibility.NONE: 2}
    parts = [
        f"{slug}={level.value}"
        for slug, level in sorted(visibilities.items(), key=lambda kv: (order[kv[1]], kv[0]))
    ]
    return " ".join(parts) or "nothing"


__all__ = [
    "ROOT_DEPTH",
    "UnitRef",
    "Visibility",
    "describe",
    "is_ancestor_or_self",
    "may_delegate_to",
    "may_read_content",
    "may_read_status",
    "scope_of",
    "visibility_of",
]
