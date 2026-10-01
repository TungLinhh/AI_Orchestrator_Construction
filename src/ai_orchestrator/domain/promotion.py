"""Whether a shadow procedure version may become the one agents actually run.

Pure rules. This is the last gate before the platform changes its own behaviour, and
the design principle is the same one `domain/reaper.py` uses: **the default is to
refuse, and every refusal has to name itself.**

## The order of the checks, which is the design

1. **Is the version in `shadow`?** Anything else is not a promotion candidate. A
   `rejected` version being promoted is a bug wearing a costume, and promoting an
   `active` one is a no-op that would still write `promoted_at`.
2. **Is the author still an agent?** A version proposed by an agent whose kill switch
   is thrown is a version nobody is maintaining. It is not promoted; it is not deleted
   either, because "we learned this and then stopped the agent" is worth keeping.
3. **Does the autonomy ceiling fit the action classes it touches?** The version may
   claim a level the dossier refuses for the actions it performs. See
   `domain/autonomy.py`.
4. **Is there enough shadow evidence?** A minimum run count, and an agreement rate
   below a floor.

Only then does it promote.

## Why the agreement rate is a floor and not a target

A version with 3 runs and 3 agreements has an agreement rate of 100% and knows almost
nothing. That is why `min_runs` is a separate gate and is checked *first* among the
evidence gates: a rate computed over too few samples is not a weak signal, it is a
number that will look excellent in every dashboard. This is F101's shape again — a
statistic designed from a fixture rather than from the sample it is measured on — and
the guard is a run count the rate cannot launder.

## Why a hard-blocked action class refuses promotion outright

`ceiling_for_all` returns `None` for a set containing one, because there is no level at
which the whole set is permitted. The tempting repair is to return the *lowest* ceiling
of the survivors — "everything else is fine, so cap it there" — and that repair turns
a prohibition into a setting. A version that concludes a labour-safety matter is
exactly the version Tập 1 §5.3 says must never be automated, and capping it at L1 does
not make automating it safe, it just makes it slower.

## `is_hard_block` beats `max_level` in the policy itself too

Same reasoning one layer up, and recorded in `domain/autonomy.py` as well: a policy
with `is_hard_block = True` and `max_level = 'L1'` is the seed data's shape, and read
as a ceiling it says "an L1 agent may do this". Read as a flag it says "no agent may do
this". The second reading is the dossier's, so `may_act` checks the flag first.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ai_orchestrator.domain.autonomy import (
    Policy,
    Refusal,
    ceiling_for_all,
    may_act,
    rank,
)

#: Runs before an agreement rate means anything. Three is not a number derived from
#: anything — no corpus here contains shadow runs yet, because there are no agents to
#: shadow — and it is deliberately small so the gate can be exercised, and small
#: enough that raising it later is not a schema change.
DEFAULT_MIN_RUNS = 5

#: The rate below which a version stays in shadow, as a fraction in `0..1`. Compared as
#: a fraction and named `_RATE` rather than a percentage, following the rule
#: `persistence/progress.py` records: the corpus's own `% Hoàn thành` header holds a
#: fraction, and a column called a percentage that holds `0.9` is the F95 shape.
DEFAULT_MIN_AGREEMENT = 0.80


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """The thresholds, in one place, so they are a decision rather than six literals.

    Deliberately *not* in the database. These are engineering thresholds, not business
    data, and a threshold in a table is one somebody will eventually edit in a
    transaction during an incident. The dossier's own limits — the per-action-class
    ceilings — *are* in the database, because they are Tập 1 §5.3 and belong to the
    business.
    """

    min_runs: int = DEFAULT_MIN_RUNS
    min_agreement: float = DEFAULT_MIN_AGREEMENT

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_agreement <= 1.0:
            # A rate outside `0..1` makes every comparison below trivially pass or
            # fail, and the failure looks like a data problem rather than a
            # configuration one. Refused here, at the boundary, where the cause is.
            raise ValueError(
                f"min_agreement must be a fraction in 0..1, got {self.min_agreement!r}"
            )


class VersionState(StrEnum):
    """The states `procedure_versions.status` can be in."""

    SHADOW = "shadow"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class Block(StrEnum):
    """Why a promotion was refused.

    Named, not numbered, for the reason every other output in this codebase is named:
    these strings are read by a person deciding whether to override, and `REASON_3` is
    a lookup while `NOT_ENOUGH_SHADOW_RUNS` is a decision.
    """

    NOT_IN_SHADOW = "not_in_shadow"
    AUTHOR_KILLED = "author_killed"
    AUTONOMY_REFUSED = "autonomy_refused"
    UNCLASSIFIED_ACTION = "unclassified_action"
    NOT_ENOUGH_RUNS = "not_enough_runs"
    AGREEMENT_TOO_LOW = "agreement_too_low"
    ALREADY_PROMOTED = "already_promoted"


@dataclass(frozen=True, slots=True)
class VersionFacts:
    """Everything the rule needs about one version, and nothing else.

    A deliberately narrow surface, like `reaper.TaskSnapshot`. A promotion rule that
    could read `procedure_versions.body` would one day start branching on the text of
    a procedure, and a promotion gate that depends on prose is a gate that changes when
    somebody rewords an SOP.
    """

    version_id: str
    status: str
    shadow_runs: int
    shadow_agreements: int
    #: What the version claims it may do, as a dossier code.
    autonomy_ceiling: str
    #: The action classes its procedure performs. A version that touches none is
    #: refused: a procedure that names no action class has not been classified, and
    #: the same argument as an unclassified policy applies.
    action_classes: tuple[str, ...] = ()
    #: Whether the agent that proposed it has been killed.
    author_killed: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    """A decision, and every reason behind it.

    `blocks` is a tuple rather than a single value because the gates are independent
    and a person fixing one problem at a time is a slower route to the same place —
    the argument `ProposalGate.evaluate` already makes, and for the same reason. All
    of them are reported, including after the first failure.
    """

    version_id: str
    blocks: tuple[Block, ...] = ()
    #: The human-readable reasons, in gate order. An override decision is made against
    #: these, so they are the output rather than the enum.
    reasons: tuple[str, ...] = ()
    #: The level this version *would* be promoted at, when it is blocked on evidence
    #: rather than on prohibition. `None` when even L1 would be wrong.
    permitted_level: str | None = None

    @property
    def may_promote(self) -> bool:
        return not self.blocks

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "may_promote": self.may_promote,
            "blocks": [b.value for b in self.blocks],
            "reasons": list(self.reasons),
            "permitted_level": self.permitted_level,
        }


def evaluate(
    facts: VersionFacts,
    policies: dict[str, Policy],
    *,
    policy: PromotionPolicy | None = None,
) -> Verdict:
    """Decide whether one version may be promoted. Every gate, every reason.

    `policies` is the `autonomy_policies` table as a dictionary keyed by
    `action_class`, passed in rather than read, so this stays pure and so a caller
    uses one consistent snapshot for a whole batch of versions.
    """
    p = policy or PromotionPolicy()
    blocks: list[Block] = []
    reasons: list[str] = []

    # --- 1. state -----------------------------------------------------------
    if facts.status == VersionState.ACTIVE.value:
        blocks.append(Block.ALREADY_PROMOTED)
        reasons.append(
            "this version is already the live one; promoting it again would only "
            "rewrite promoted_at and make the change look later than it was"
        )
    elif facts.status != VersionState.SHADOW.value:
        blocks.append(Block.NOT_IN_SHADOW)
        reasons.append(
            f"status is '{facts.status}'; only a version in 'shadow' is a promotion "
            f"candidate. A rejected version coming back is a bug, not a promotion"
        )

    # --- 2. the author ------------------------------------------------------
    if facts.author_killed:
        blocks.append(Block.AUTHOR_KILLED)
        reasons.append(
            "the agent that proposed this has its kill switch thrown, so nobody is "
            "maintaining it. Not deleted, because what was learned before the switch "
            "was thrown is still worth reading"
        )

    # --- 3. autonomy --------------------------------------------------------
    permitted: str | None = None
    if not facts.action_classes:
        # Same argument as an unclassified policy: not naming an action class is not
        # the same as being permitted under every one of them.
        blocks.append(Block.UNCLASSIFIED_ACTION)
        reasons.append(
            "the version names no action class, so nothing can check it against the "
            "dossier's autonomy policies. A procedure that has not been classified "
            "cannot be shown to be permitted"
        )
    else:
        permitted = ceiling_for_all(policies, facts.action_classes)
        if permitted is None:
            hard = [c for c in facts.action_classes if c in policies and policies[c].is_hard_block]
            missing = [c for c in facts.action_classes if c not in policies]
            if hard:
                blocks.append(Block.AUTONOMY_REFUSED)
                reasons.append(
                    f"touches {', '.join(hard)}, which Tập 1 §5.3 lists as an absolute "
                    f"prohibition. There is no level at which automating it is "
                    f"permitted, so there is no level to cap it at"
                )
            if missing:
                blocks.append(Block.UNCLASSIFIED_ACTION)
                reasons.append(
                    f"touches {', '.join(missing)}, which is not in the autonomy "
                    f"policy table, so nobody has decided how autonomous to be about "
                    f"it"
                )
        else:
            # The version's *claim* has to be within what the dossier permits. A
            # version asking for more than the actions allow is refused even though a
            # lower level would have been fine -- silently lowering it would promote
            # a document nobody approved at the level it was written for.
            try:
                claimed = rank(facts.autonomy_ceiling)
            except ValueError as exc:
                blocks.append(Block.UNCLASSIFIED_ACTION)
                reasons.append(str(exc))
            else:
                if claimed > rank(permitted):
                    blocks.append(Block.AUTONOMY_REFUSED)
                    reasons.append(
                        f"claims {facts.autonomy_ceiling} but the action classes it "
                        f"touches permit at most {permitted}"
                    )
                else:
                    # A single refusal per action class, so a version touching three
                    # hard-blocked classes reports three and not one.
                    for action_class in facts.action_classes:
                        refusal: Refusal | None = may_act(
                            action_class, facts.autonomy_ceiling, policies
                        )
                        if refusal is not None:
                            blocks.append(Block.AUTONOMY_REFUSED)
                            reasons.append(refusal.reason)

    # --- 4. shadow evidence -------------------------------------------------
    if facts.shadow_runs < p.min_runs:
        blocks.append(Block.NOT_ENOUGH_RUNS)
        reasons.append(
            f"{facts.shadow_runs} shadow run(s), needs {p.min_runs}. Checked before "
            f"the agreement rate deliberately: {facts.shadow_agreements}/"
            f"{facts.shadow_runs} is a rate computed over too few samples to mean "
            f"anything, and it will read as excellent in every dashboard"
        )
    else:
        rate = facts.shadow_agreements / facts.shadow_runs
        if rate < p.min_agreement:
            blocks.append(Block.AGREEMENT_TOO_LOW)
            reasons.append(
                f"agreed {rate:.0%} of the time ({facts.shadow_agreements}/"
                f"{facts.shadow_runs}), needs {p.min_agreement:.0%}. A version that "
                f"disagrees with reality a fifth of the time is not an improvement"
            )

    return Verdict(
        version_id=facts.version_id,
        blocks=tuple(blocks),
        reasons=tuple(reasons),
        permitted_level=permitted,
    )


__all__ = [
    "DEFAULT_MIN_AGREEMENT",
    "DEFAULT_MIN_RUNS",
    "Block",
    "PromotionPolicy",
    "Verdict",
    "VersionFacts",
    "VersionState",
    "evaluate",
]
