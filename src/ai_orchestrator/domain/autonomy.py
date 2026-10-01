"""# Autonomy: the two scales, and the one module that knows both

Pure rules, no database and no clock.

## Why this module exists

The database holds two spellings of the same idea, and they were both there before
this file:

    autonomy_policies.max_level   'L1' 'L2' 'L3' 'L4'          <- the dossier's form
    agents.autonomy_level         'l1_low_risk_autonomous' ...  <- the substrate's enum
    domain/enums.AutonomyLevel    L0_SUGGEST .. L4_BOUNDED_AUTONOMOUS

The first comes from Tập 1 §5.3 and is seeded by `make seed-process`. The second is the
substrate's behavioural enum and is what `task_execution.py` resolves against a role's
`max_autonomy`. They agree, they are not the same, and **nothing in the code converts
between them.**

Left alone, every comparison across that gap is a string comparison. `'L1' <= 'L4'` is
true for the right reason today, and it stays true by accident rather than by decision:
the codes happen to be single digits in ascending order, in the same case, in the same
alphabet. Change the dossier to `L1a`/`L1b` for a graduated L1 and the comparison
silently inverts. Change `AutonomyLevel` to `L1_LOW_RISK` and a ceiling that used to
bind stops binding. Neither raises anything.

So the mapping lives here, once, and everything that needs to cross the gap calls
`parse_level` or `rank`.

## What is used today, and what is ahead of its caller

`rank`, `may_act` and `ceiling_for_all` are called by `domain/promotion.py`, and that is
the whole production use. `to_dossier` and `parse_level` are currently called by **tests
only** — `procedure_operations` stores and compares the dossier's spelling throughout and
never converts.

That is stated here rather than left for a reader to discover, because a conversion
function with a confident docstring and no caller is the same defect as an enum member
nothing produces: it advertises a capability the module is not yet exercising. The
first real caller will be the agent runtime resolving a granted level against
`agents.autonomy_level`, which is still the substrate's prose spelling — that work is
Phase 4c, and until it happens these two functions are vocabulary held in one place
rather than a bridge in use.

## L0 is in the substrate and not in the dossier

`AutonomyLevel` has `L0_SUGGEST`; the dossier's scale starts at L1. That is not a
discrepancy to paper over — "suggest only, do nothing" is a real and useful state for
an agent, and the dossier's own Phase-1 arrangement ("propose and wait") *is* L0
described in the other vocabulary.

So `rank` accepts L0 and it ranks below everything, `parse_level` accepts `L0` as
input, and `dossier_ceiling` refuses to *return* it — because a `procedure_versions`
column constrained to `L1..L4` cannot store it, and a rule that produces a value its
own store cannot hold is a rule that fails at the boundary instead of at the decision.

## The hard block is not a level

`autonomy_policies.is_hard_block` is true for exactly the two action classes Tập 1
§5.3 lists as "vùng cấm tuyệt đối" — labour-safety conclusions and dealings with a
flagged supplier. Those carry `max_level = 'L1'` in the seed data, which is
deliberately misleading if read as a ceiling: it reads as "an L1 agent may take this
action", and what the dossier means is closer to "no agent takes this action; a person
does, every time".

`may_act` therefore checks `is_hard_block` *before* it compares levels, and refuses at
any level including L4. A policy with `is_hard_block = True` and a generous
`max_level` is refused the same way as one with `max_level = 'L1'`, because the flag is
the stronger statement and a comparison cannot express it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ai_orchestrator.domain.enums import AutonomyLevel

#: The dossier's spelling, in ascending order. Index in this tuple *is* the rank, so
#: adding a level means adding it here and nowhere else.
DOSSIER_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4")

#: Substrate enum to dossier code. The only place this mapping is written down.
_TO_DOSSIER: dict[AutonomyLevel, str] = {
    AutonomyLevel.L0_SUGGEST: "L0",
    AutonomyLevel.L1_LOW_RISK_AUTONOMOUS: "L1",
    AutonomyLevel.L2_PARENT_REVIEW: "L2",
    AutonomyLevel.L3_HUMAN_APPROVAL: "L3",
    AutonomyLevel.L4_BOUNDED_AUTONOMOUS: "L4",
}

#: And back. Built from the table above rather than written out, so the two
#: directions cannot disagree — a hand-written reverse table is a second thing to
#: forget, and it would be tested only in the direction somebody remembered.
_FROM_DOSSIER: dict[str, AutonomyLevel] = {v: k for k, v in _TO_DOSSIER.items()}


class LevelError(ValueError):
    """A level string that is in neither scale, or is in the wrong one."""


def to_dossier(level: AutonomyLevel) -> str:
    """`AutonomyLevel` to the dossier's `L1`-style code, for a database column."""
    try:
        return _TO_DOSSIER[level]
    except KeyError as exc:  # pragma: no cover -- the enum is closed
        raise LevelError(f"{level!r} has no dossier spelling") from exc


def parse_level(code: str) -> AutonomyLevel:
    """A dossier code to the substrate's `AutonomyLevel`.

    Case-insensitive, because the two scales in the database differ in case and a
    caller that has to remember which is which is a caller that will get it wrong
    once. Trailing and leading whitespace is stripped for the same reason — these
    values arrive in spreadsheets and in seeded SQL, not from a type-checked call
    site.
    """
    normalised = code.strip().upper()
    try:
        return _FROM_DOSSIER[normalised]
    except KeyError as exc:
        raise LevelError(
            f"{code!r} is not an autonomy level; expected one of {', '.join(DOSSIER_LEVELS)}"
        ) from exc


def rank(code: str) -> int:
    """How autonomous a dossier code is. Higher is more autonomous.

    By position, not by string comparison. `'L10'` sorting below `'L2'` is exactly the
    accident this avoids.
    """
    normalised = code.strip().upper()
    if normalised not in DOSSIER_LEVELS:
        raise LevelError(
            f"{code!r} is not an autonomy level; expected one of {', '.join(DOSSIER_LEVELS)}"
        )
    return DOSSIER_LEVELS.index(normalised)


@dataclass(frozen=True, slots=True)
class Policy:
    """One row of `autonomy_policies`: the dossier's ceiling for an action class.

    A dataclass rather than a read of the table so the promotion rules stay pure and
    testable, exactly as `domain/gates.py` is.
    """

    action_class: str
    max_level: str
    is_hard_block: bool = False

    @property
    def ceiling(self) -> int:
        return rank(self.max_level)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A decision not to act, and the sentence explaining it."""

    action_class: str
    reason: str
    rule_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "action_class": self.action_class,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


def may_act(action_class: str, at_level: str, policies: dict[str, Policy]) -> Refusal | None:
    """Whether an agent may take this action at this level. `None` means yes.

    A missing policy **refuses**. That is the conservative direction and it is
    deliberate: an action class nobody has classified is one nobody has decided how
    autonomous to be about, and defaulting to "allowed" would make the policy table
    a blocklist rather than a specification. Six classes are seeded; everything else
    is unclassified, and unclassified is not permitted.

    The hard block is checked first and refuses at every level, including L4. A
    policy with `is_hard_block = True` and `max_level = 'L1'` would otherwise read as
    "an L1 agent may do this", which is not what Tập 1 §5.3 means by
    *vùng cấm tuyệt đối*.
    """
    policy = policies.get(action_class)
    if policy is None:
        return Refusal(
            action_class=action_class,
            rule_id="autonomy.unclassified",
            reason=(
                f"'{action_class}' is not in the autonomy policy table, so nobody has "
                f"decided how autonomous to be about it. Unclassified is not "
                f"permitted; classify it first."
            ),
        )
    if policy.is_hard_block:
        return Refusal(
            action_class=action_class,
            rule_id="autonomy.hard_block",
            reason=(
                f"'{action_class}' is an absolute prohibition (Tập 1 §5.3). No level "
                f"permits it, including L4 -- a person decides, every time."
            ),
        )
    try:
        at = rank(at_level)
    except LevelError as exc:
        return Refusal(
            action_class=action_class,
            rule_id="autonomy.unknown_level",
            reason=str(exc),
        )
    if at > policy.ceiling:
        return Refusal(
            action_class=action_class,
            rule_id="autonomy.over_ceiling",
            reason=(
                f"acting at {at_level.strip().upper()} exceeds the {policy.max_level} "
                f"permitted for '{action_class}'"
            ),
        )
    return None


def ceiling_for_all(policies: dict[str, Policy], action_classes: Iterable[str]) -> str | None:
    """The highest level at which **every** listed action class may be acted on.

    `None` if any one of them is hard-blocked or unclassified, because then there is
    no level at which the whole set is permitted, and returning the *lowest* ceiling
    of the survivors would be a way of saying yes to a set that includes a
    prohibition.

    This is the function the promotion rule uses: a procedure version that touches two
    action classes can only be promoted to the lower of the two ceilings, or refused.
    """
    classes = list(action_classes)
    if not classes:
        return None
    lowest: str | None = None
    for action_class in classes:
        policy = policies.get(action_class)
        if policy is None or policy.is_hard_block:
            return None
        if lowest is None or policy.ceiling < rank(lowest):
            lowest = policy.max_level
    return lowest


__all__ = [
    "DOSSIER_LEVELS",
    "LevelError",
    "Policy",
    "Refusal",
    "ceiling_for_all",
    "may_act",
    "parse_level",
    "rank",
    "to_dossier",
]
