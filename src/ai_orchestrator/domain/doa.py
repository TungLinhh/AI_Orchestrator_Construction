"""What the delegation-of-authority matrix means, decided without a database.

The dossier makes this matrix the spine of financial control: an amount decides who
signs. The platform loaded all eight bands and never asked it anything, so authority
was an `AutonomyLevel` -- an ordinal letter -- and a payment of thirty billion was
no more out of reach than one of thirty thousand. `CURRENT_STATE.md` said so, and the
seed did not care.

This module is the missing half: given the bands, say who must approve an amount,
and whether an agent at a given autonomy may act on its own.

**Why it is a pure function and not a query.**

The interesting part is not the SELECT. It is what happens at a band edge and when
no band covers the amount at all, and those are decisions with no I/O in them:
whether `100.000.000` belongs to the band ending there or the one starting there,
whether a gap is a refusal or a pass, and whether an unbounded band above the top
one is an oversight or the top band. Every one of those was decided by asking the
database a slightly different question in a different order, and the answers were
not consistent between them.

**The two refusals, which are the whole point.**

*No band covers the amount.* An amount nobody wrote a rule for is the case that must
stop. Defaulting to the nearest band would mean a gap silently inherits authority
from whatever sits next to it, and a matrix is a list of refusals as much as a list of
grants.

*Above the highest band.* A `max_amount IS NULL` band means "and everything above".
If the highest band has a ceiling and the amount is past it, the honest answer is that
nobody is authorised, not the nearest person down.

An agent may never be its own approver, whatever the band says -- the same structural
rule the approval path already enforces, kept here so a band cannot be read as a
permission slip.

No I/O, no clock, no sibling imports. An AST test asserts it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

#: What each autonomy level actually *permits*, and whether an agent may act on its
#: own within it.
#:
#: **These are not a ladder, and treating them as one is the trap in this module.**
#: `L3_HUMAN_APPROVAL` sorts after `L2_PARENT_REVIEW` and before
#: `L4_BOUNDED_AUTONOMOUS`, so "L4 is more autonomy than L3" reads as obvious and is
#: meaningless: L3 is the level where a *human* must sign, and L4 is the level where
#: an agent acts alone inside a stated bound. An ordinal comparison over these
#: strings says an L4 agent outranks an L3 one, and then lets the agent with the
#: human-approval posture spend without a human.
#:
#: So each level is read for what it says. Ordered by *how much the agent may do by
#: itself*, the useful fact is a partition, not a scale:
AGENT_MAY_ACT: frozenset[str] = frozenset(
    {
        "L1_LOW_RISK_AUTONOMOUS",
        "L2_PARENT_REVIEW",
        "L4_BOUNDED_AUTONOMOUS",
    }
)
#: Levels where a human is in the loop by definition. A band naming one of these as
#: its ceiling is a band where an agent may not settle anything alone.
NEEDS_HUMAN: frozenset[str] = frozenset({"L3_HUMAN_APPROVAL"})
#: The agent may only propose.
SUGGEST_ONLY: frozenset[str] = frozenset({"L0_SUGGEST"})

_LEVELS: dict[str, str] = {
    "L0": "L0_SUGGEST",
    "L1": "L1_LOW_RISK_AUTONOMOUS",
    "L2": "L2_PARENT_REVIEW",
    "L3": "L3_HUMAN_APPROVAL",
    "L4": "L4_BOUNDED_AUTONOMOUS",
}

#: (thousands separator, decimal point) per locale. The project's working language
#: writes `3.600.000`; English writes `3,600,000`. Neither is inferable from the
#: string, so the caller says which.
_GROUPING: dict[str, tuple[str, str]] = {"vi_VN": (".", ","), "en_US": (",", ".")}


class DoaRefusal(Exception):
    """No band authorises this. Not a warning, and not a fallback.

    Raised rather than returned because every caller that swallowed it would have
    become the bug this module exists to prevent: a warning in a log that nobody
    reads is not a refusal, and a matrix whose gaps are warnings is not a control.
    """


@dataclass(frozen=True, slots=True)
class DoaBand:
    """One row of the matrix: a subject, an amount band, and who signs inside it."""

    code: str
    subject_kind: str
    min_amount: Decimal
    max_amount: Decimal | None
    approver_role_key: str
    fallback_role_key: str = ""
    max_agent_autonomy: str = "L3"

    def covers(self, amount: Decimal) -> bool:
        """Whether this band owns `amount`.

        Lower bound inclusive, upper **exclusive**, with `None` meaning unbounded.
        Exclusive upper is what makes the seeded bands partition cleanly:
        `payment` has `0..100.000.000` and `100.000.000..1.000.000.000`, and at
        exactly 100.000.000 one of them must win. Inclusive upper would put the
        amount in two bands, and a lookup that returns whichever the database
        happened to order first is a lookup whose answer depends on a plan.
        """
        if amount < self.min_amount:
            return False
        return self.max_amount is None or amount < self.max_amount


@dataclass(frozen=True, slots=True)
class DoaDecision:
    """What the matrix says about one amount, and whether an agent may act."""

    band: DoaBand
    amount: Decimal
    subject_kind: str
    #: Whether the agent may proceed on its own, within the band's own ceiling.
    may_act_alone: bool
    #: Why, in words fit to put in a rejection message or an approval's context.
    reason: str

    @property
    def approver_role_key(self) -> str:
        return self.band.approver_role_key

    @property
    def fallback_role_key(self) -> str:
        return self.band.fallback_role_key


def _strip_groups(text: str, separator: str) -> str | None:
    """Remove thousands separators, or return `None` if they are not well formed.

    Well formed means groups of exactly three after the first, which is what makes the
    two conventions tellable apart at all: `3.600.000` is three groups and is
    Vietnamese thousands; `3.6` is not a group pattern and is a decimal point.

    A plain digit string carries no separator and is already a number, so it passes
    through untouched. Refusing `100000000` would have been a fourth way for this
    function to be wrong about a value it could have read exactly.
    """
    if not text:
        return None
    if separator not in text:
        # No separator: it has to already be a plain digit string.
        return text if text.isdigit() else None
    if not text.replace(separator, "").isdigit():
        return None
    parts = text.split(separator)
    if not parts[-1] or len(parts[-1]) != 3:
        return None
    head = parts[0]
    if len(head) > 1 and head.startswith("0"):
        return None
    return "".join(parts)


def _decimal(value: Any, *, what: str, locale: str = "vi_VN") -> Decimal:
    """Money in, `Decimal` out, or a refusal naming the field and the expected format.

    Three ways this went wrong, all of them a control reading the wrong number:

    * **`float` was coerced.** `Decimal(0.1)` from a float is
      `0.1000000000000000055511151231257827...`, so a band edge of `100.000.000` could
      be missed by a fraction of a dong because the amount arrived from JSON.
    * **Stripping every separator.** `Decimal("3.600.000")` raises, so the very first
      claim in this project's own scenario catalogue — Minh Châu, `3.600.000 VND` —
      could not be resolved at all, and neither could any of the other amounts written
      in the dossier's own format. The strip was aimed at commas, which are not the
      grouping character in this language.
    * **Guessing the locale.** `.` groups thousands in this project's working language
      and is a decimal point in English, and the same string is both. A control that
      guesses picks an authority band on a coin flip, so the locale is stated by the
      caller instead, and an unparseable string is refused rather than reinterpreted.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise DoaRefusal(f"{what} is a boolean, which is not an amount")
    if isinstance(value, float):
        raise DoaRefusal(
            f"{what} arrived as a float ({value!r}); pass a string or Decimal so the "
            f"band edges are exact"
        )
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, str):
        raise DoaRefusal(f"{what} is not a number: {value!r}")

    # Every space a Vietnamese amount may carry: the narrow no-break space used as a
    # group separator, and the plain one. Written as escapes because the first is
    # invisible in source, and a reader who cannot see it cannot review it.
    text = value.strip().replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    if not text:
        raise DoaRefusal(f"{what} is empty")

    if locale not in _GROUPING:
        raise DoaRefusal(f"unknown locale {locale!r}; use 'vi_VN' or 'en_US'")
    group, decimal_point = _GROUPING[locale]

    sign = ""
    if text[0] in "+-":
        sign, text = text[0], text[1:]

    # A single `decimal_point` with 1-2 digits after it is a fractional amount in
    # both locales, which is the one form where they agree.
    if decimal_point in text:
        whole, _, frac = text.rpartition(decimal_point)
        if decimal_point in whole or not frac.isdigit() or not 1 <= len(frac) <= 2:
            raise DoaRefusal(
                f"{what} is not a number in {locale}: {value!r} "
                f"(write it as 3,600,000 for en_US or 3.600.000 for vi_VN)"
            )
        stripped = _strip_groups(whole, group)
        if stripped is None:
            raise DoaRefusal(f"{what} has malformed groups for {locale}: {value!r}")
        return Decimal(f"{sign}{stripped}.{frac}")

    # Integer part only. Both conventions use the same digits here; the separator
    # decides whether it was grouping or a stray character.
    stripped = _strip_groups(text, group)
    if stripped is None:
        if group in text or decimal_point in text:
            raise DoaRefusal(
                f"{what} is not a number in {locale}: {value!r} "
                f"(write it as 3,600,000 for en_US or 3.600.000 for vi_VN)"
            )
        raise DoaRefusal(f"{what} is not a number: {value!r}")
    return Decimal(f"{sign}{stripped}")


def _level(name: str) -> str:
    """The canonical autonomy level for a label, refusing an unknown one.

    Accepts `L3` and `l3_human_approval` alike, because the matrix stores one and the
    agents carry the other, and a lookup that only understands one of them fails on
    real data with a `KeyError` from the middle of a payment.
    """
    text = str(name).strip().upper().replace("-", "_")
    if text in _LEVELS:
        return _LEVELS[text]
    if text in set(_LEVELS.values()):
        return text
    raise DoaRefusal(f"unknown autonomy level {name!r}; the bands use L0..L4")


def bands_of(subject_kind: str, rows: Any) -> list[DoaBand]:
    """The bands for one subject kind, ascending.

    Sorted here rather than assumed from the caller: a matrix read in insertion order
    answers "which band" differently depending on who wrote the rows, and the tests
    pass them in an order chosen to catch it.
    """
    wanted = subject_kind.strip().lower()
    bands = [row for row in rows if str(getattr(row, "subject_kind", "")).strip().lower() == wanted]
    return sorted(bands, key=lambda b: (b.min_amount, b.code))


def resolve(
    rows: Any,
    subject_kind: str,
    amount: Any,
    *,
    agent_autonomy: str | None = None,
    locale: str = "vi_VN",
) -> DoaDecision:
    """Who must approve this amount, and may an agent at this level do it alone?

    Refuses rather than approximating when no band owns the amount. The two refusals
    are the module's value: a matrix that guesses is not a matrix.
    """
    money = _decimal(amount, what="amount", locale=locale)
    if money < 0:
        raise DoaRefusal(f"amount is negative: {money}")

    bands = bands_of(subject_kind, rows)
    if not bands:
        raise DoaRefusal(
            f"no DOA band covers subject kind {subject_kind!r}; there is no rule for "
            f"what may be done about a {money} {subject_kind}"
        )

    match = next((band for band in bands if band.covers(money)), None)
    if match is None:
        # Two different situations that must not be reported the same way. A gap
        # between bands is an oversight; an amount past every ceiling is a decision
        # somebody has not made yet. Both stop, and they stop with different words.
        above = money > max(b.min_amount for b in bands)
        if above:
            raise DoaRefusal(
                f"amount {money} is above every {subject_kind} band in the matrix; "
                f"the highest band is {bands[-1].code} and it does not extend this far"
            )
        raise DoaRefusal(
            f"amount {money} falls in a gap between the {subject_kind} bands; the "
            f"matrix does not say who may approve it"
        )

    ceiling = _level(match.max_agent_autonomy)
    if agent_autonomy is None:
        return DoaDecision(
            band=match,
            amount=money,
            subject_kind=subject_kind,
            may_act_alone=False,
            reason=(
                f"{match.code}: a {money} {subject_kind} needs "
                f"{match.approver_role_key}, and no autonomy level was stated"
            ),
        )

    level = _level(agent_autonomy)

    # The band's own posture is read first, and it can refuse on its own. The seeded
    # matrix carries `L3` on every band, which under this reading means *a human signs
    # for every amount* -- the conservative default, and the one the dossier's own
    # control intent asks for. Before this, the inert string `L3` was compared
    # ordinally and let an agent settle any amount at any level.
    if ceiling in NEEDS_HUMAN:
        reason = (
            f"{match.code}: {money} {subject_kind} is banded to "
            f"{match.approver_role_key}, and the band's posture is "
            f"{match.max_agent_autonomy} -- a human signs, so the agent may not settle "
            f"it alone whatever level it holds"
        )
        may = False
    elif ceiling in SUGGEST_ONLY:
        may = False
        reason = (
            f"{match.code}: the band's posture is {match.max_agent_autonomy}, so the "
            f"agent may only propose on this {money} {subject_kind}"
        )
    elif level in NEEDS_HUMAN or level in SUGGEST_ONLY:
        may = False
        reason = (
            f"{match.code}: {money} {subject_kind} falls in {match.approver_role_key}'s "
            f"band and the band permits an agent to act, but this agent runs at "
            f"{level}, which puts a human in the loop"
        )
    elif level in AGENT_MAY_ACT:
        may = True
        reason = (
            f"{match.code}: {money} {subject_kind} is within {match.approver_role_key}'s "
            f"band and this agent runs at {level}, which the band permits"
        )
    else:
        may = False
        reason = f"{match.code}: this agent's level {level} is not recognised"

    return DoaDecision(
        band=match,
        amount=money,
        subject_kind=subject_kind,
        may_act_alone=may,
        reason=reason,
    )


__all__ = [
    "AGENT_MAY_ACT",
    "NEEDS_HUMAN",
    "SUGGEST_ONLY",
    "DoaBand",
    "DoaDecision",
    "DoaRefusal",
    "bands_of",
    "resolve",
]
