"""Shadow mode: run the model beside the person, and say how often they agreed.

The dossier makes this the precondition for go-live — four weeks of parallel running
at >=95% agreement, reconciled weekly. `docs/ARCHITECTURE_DECISIONS.md` §D6 records
that this project did not attempt it, and the reason given was that it needs four
weeks of real traffic.

**That reason was half right, and this module is the half that was wrong.** Four weeks
of traffic cannot be manufactured. The machinery that *consumes* the evidence already
existed — `domain/promotion.py` gates on `shadow_runs` and `shadow_agreements`, and
`agent_shadow_runs` has columns for what the model would have decided and what
actually happened, with a `divergence` that is required whenever they differ. So the
gate, the table and the thresholds were all in place, and nothing wrote to any of
them. That is the same shape as the DOA matrix: a control fully specified and never
consulted.

So this module produces what the existing gate consumes, and it produces it honestly:
the agreement rate is whatever the record says, the elapsed-time requirement is
checked, and a shortfall is reported as a shortfall rather than as a metric that is
merely early.

**Why the comparison is a pure function with no model in it.**

"Does the model's verdict match the person's" is a question about two values, and the
part that keeps going wrong is not the arithmetic — it is deciding what counts as the
same answer. `APPROVE`, `approve`, `đồng ý` and `Accept` are one answer written four
ways, and a comparison that treats them as four different ones will report a
model as disagreeing with people who agreed with it. Equally, a comparison loose
enough to accept all of those will also accept `reject` written as `không đồng ý`, and
then it reports agreement on the cases that matter most.

The normalisation is therefore explicit and narrow, and every equivalence it claims is
a test.

**What is deliberately not here.** No verdict is ever *derived* from the model. If the
model produced nothing comparable, the run is recorded as a divergence with the reason
"the model's answer was not comparable", never as an agreement — an unmeasured
comparison counted as agreement is how a 95% rate is reached with no evidence behind
it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

#: The dossier's go-live precondition: four weeks of parallel running at >=95%
#: agreement, reconciled weekly (`ARCHITECTURE_DECISIONS.md` §D6).
#:
#: **These are not `promotion.PromotionPolicy`'s defaults, and the difference is
#: deliberate.** That policy answers "may this procedure version replace the live one
#: for a rollout", and it is engineered small: 5 runs, 80%, so the gate can be
#: exercised on a corpus that contains no real traffic. These answer "has this
#: deployment earned the right to go live at all", which is a business decision the
#: dossier states and an engineer does not get to soften. Using 80% and 5 runs here
#: would have made a 4-week, 95% precondition into a five-sample smoke test.
GO_LIVE_MIN_WEEKS = 4
GO_LIVE_MIN_AGREEMENT = 0.95

#: Reconciled weekly, so a report is about the last week rather than about all of it.
RECONCILE_EVERY_DAYS = 7

#: Answer words that mean the same decision, across the vocabularies the corpus and
#: the dossier actually use. **Membership, not replacement** — an unrecognised answer is
#: left as it is, so `phê duyệt` and `approve` are known to agree while an invented
#: word is reported verbatim as a divergence. Guessing at synonyms is how a comparison
#: starts agreeing with itself.
#:
#: Checked by tests in both directions: every pair in a group agrees, and a word that
#: is *not* in the table is never mapped onto one that is.
_APPROVE_RAW = {
    "approve",
    "approved",
    "accept",
    "accepted",
    "dong y",
    "đồng ý",
    "phe duyet",
    "phê duyệt",
    "chấp nhận",
    "chap nhan",
}
_REJECT_RAW = {
    "reject",
    "rejected",
    "refuse",
    "refused",
    "tu choi",
    "từ chối",
    "khong dong y",
    "không đồng ý",
}
_ESCALATE_RAW = {
    "conditional",
    "escalate",
    "escalated",
    "defer",
    "deferred",
    "chuyen len",
    "chuyển lên",
    "trinh sep",
    "trình sếp",
    "canh tac",
    "cần tác",
    "chua quyet dinh",
    "chưa quyết định",
}


_WHITESPACE = re.compile(r"\s+")
#: Vietnamese tone marks and diacritics are folded, not stripped, so `đồng ý` and `dong
#: y` meet. A comparison that strips rather than folds turns `d` and `đ` into
#: different characters and then reports a disagreement between two spellings of one
#: word.
_FOLD = str.maketrans(
    "đĐ",
    "dd",
)


class Uncomparable(Exception):
    """The model's answer cannot be compared, so there is nothing to record.

    Raised rather than returned, for the same reason `DoaRefusal` is: every caller
    that swallowed it would have recorded a comparison it did not make.
    """


def _fold(text: str) -> str:
    return text.translate(_FOLD).strip().lower()


#: The vocabulary, folded the same way an answer is.
#:
#: **This had to be folded too, and the first version was not.** Folding turned `ĐỒNG
#: Ý` into `dồng ý`, which matched nothing in a table written with `đồng ý`, so a model
#: that said "đồng ý" was recorded as disagreeing with a person who said "approve".
#: The same two strings differ by exactly one character and the disagreement was total.
#:
#: A normalisation that only folds one side of the comparison is a normalisation that
#: reports spelling as disagreement.
_APPROVE = frozenset(_fold(w) for w in _APPROVE_RAW)
_REJECT = frozenset(_fold(w) for w in _REJECT_RAW)
_ESCALATE = frozenset(_fold(w) for w in _ESCALATE_RAW)

#: Group → canonical word, so a lookup is one dict and the three sets cannot drift
#: apart from each other.
_VERDICTS: dict[str, str] = {
    **{word: "approve" for word in _APPROVE},
    **{word: "reject" for word in _REJECT},
    **{word: "escalate" for word in _ESCALATE},
}


def normalise(value: Any) -> str:
    """One canonical form for a written answer.

    Case, accents and repeated whitespace are folded; a number written either as
    `3.600.000` or `3,600,000` becomes the same thing, because the dossier's own
    language uses the first and every tool output tends to use the second, and a
    shadow run that disagreed with itself over punctuation would be measuring the
    formatter.

    A verdict word is reduced to its group so that synonyms meet, and **only** if it
    is a whole answer: `approve all three` is prose, and reducing it to `approve`
    would make a hedged answer look like a clean one.
    """
    if value is None:
        raise Uncomparable("the model's answer is absent")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        raise Uncomparable(
            f"a float is not an answer worth recording ({value!r}); numbers must be "
            f"Decimal or str so the comparison is exact"
        )
    text = _WHITESPACE.sub(" ", _fold(str(value))).strip()
    if not text:
        raise Uncomparable("the answer is empty")

    bare = text.replace(".", "").replace(" ", "").replace(",", "")
    if bare.isdigit():
        try:
            return str(Decimal(bare))
        except InvalidOperation:
            return text

    return _VERDICTS.get(text, text)


@dataclass(frozen=True, slots=True)
class KeyAgreement:
    """One decision key, and whether the two answers agree on it."""

    key: str
    model: str
    human: str
    agreed: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "model": self.model,
            "human": self.human,
            "agreed": self.agreed,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    """The whole comparison for one shadow run.

    `agreed` is **all** keys agreeing, not a majority and not a mean. A run over three
    expense claims where the model is right about two is a model that is wrong about a
    payment, and averaging it into 67% agreement makes the specific failure invisible
    at the only place it can be acted on.
    """

    keys: tuple[KeyAgreement, ...]
    agreed: bool
    divergence: str

    @property
    def compared(self) -> int:
        return len(self.keys)

    @property
    def matching(self) -> int:
        return sum(1 for k in self.keys if k.agreed)

    def as_dict(self) -> dict[str, object]:
        return {
            "agreed": self.agreed,
            "compared": self.compared,
            "matching": self.matching,
            "divergence": self.divergence,
            "keys": [k.as_dict() for k in self.keys],
        }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Every scalar in a nested answer, keyed by its path.

    `{"verdicts": {"Minh Châu": "approve"}}` becomes
    `{"verdicts.Minh Châu": "approve"}`, so a disagreement is reported against the
    claim it is about rather than against the object as a whole.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, inner in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(inner, path))
        return out
    if isinstance(value, (list, tuple)):
        out = {}
        for index, inner in enumerate(value):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            out.update(_flatten(inner, path))
        return out
    return {prefix or "value": value}


def compare(model_answer: Any, human_answer: Any) -> Comparison:
    """Compare the two answers key by key, and say where they differ.

    Three situations are refused rather than guessed, because each of them would
    otherwise be recorded as agreement on evidence that does not exist:

    * the model's answer is absent, empty or not comparable;
    * the two answers describe **different sets of keys** — comparing a three-claim
      answer with a one-claim answer and counting the match would score the model on
      claims nobody asked it;
    * a key present on both sides whose value cannot be normalised.
    """
    if model_answer is None or human_answer is None:
        raise Uncomparable("a shadow run needs both an answer and the person's answer")

    model_flat = _flatten(model_answer)
    human_flat = _flatten(human_answer)

    if not model_flat:
        raise Uncomparable("the model's answer held no comparable values")
    if not human_flat:
        raise Uncomparable("the person's answer held no comparable values")

    model_keys, human_keys = set(model_flat), set(human_flat)
    if model_keys != human_keys:
        only_model = sorted(model_keys - human_keys)
        only_human = sorted(human_keys - model_keys)
        raise Uncomparable(
            "the two answers do not describe the same decisions, so there is nothing "
            f"to agree or disagree about. Only the model answered: {only_model or '—'}. "
            f"Only the person answered: {only_human or '—'}"
        )

    keys: list[KeyAgreement] = []
    for key in sorted(model_keys):
        raw_model, raw_human = model_flat[key], human_flat[key]
        try:
            left, right = normalise(raw_model), normalise(raw_human)
        except Uncomparable as exc:
            raise Uncomparable(f"'{key}': {exc}") from exc
        keys.append(KeyAgreement(key=key, model=left, human=right, agreed=left == right))

    disagreeing = [k for k in keys if not k.agreed]
    if disagreeing:
        parts = ", ".join(f"{k.key}: model {k.model!r} vs person {k.human!r}" for k in disagreeing)
        divergence = f"{len(disagreeing)} of {len(keys)} decision(s) differed — {parts}"
    else:
        divergence = ""

    return Comparison(
        keys=tuple(keys),
        agreed=not disagreeing,
        divergence=divergence,
    )


@dataclass(frozen=True, slots=True)
class GoLiveReadiness:
    """Whether the dossier's go-live precondition is met. Every reason, not one."""

    #: Individual comparisons recorded, and how many of them agreed.
    runs: int
    agreements: int
    #: The span the recorded runs actually cover.
    observed_days: int
    required_weeks: int = GO_LIVE_MIN_WEEKS
    required_agreement: float = GO_LIVE_MIN_AGREEMENT
    #: The last whole week, reported separately because the dossier asks for weekly
    #: reconciliation and a single cumulative rate cannot show whether agreement is
    #: holding or decaying.
    last_week_runs: int = 0
    last_week_agreements: int = 0
    missing: tuple[str, ...] = ()

    @property
    def agreement(self) -> float:
        return self.agreements / self.runs if self.runs else 0.0

    @property
    def last_week_agreement(self) -> float:
        return self.last_week_agreements / self.last_week_runs if self.last_week_runs else 0.0

    @property
    def ready(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "runs": self.runs,
            "agreements": self.agreements,
            "agreement": round(self.agreement, 4),
            "last_week_runs": self.last_week_runs,
            "last_week_agreements": self.last_week_agreements,
            "last_week_agreement": round(self.last_week_agreement, 4),
            "observed_days": self.observed_days,
            "required_weeks": self.required_weeks,
            "required_agreement": self.required_agreement,
            "missing": list(self.missing),
        }


def readiness(
    observations: Any,
    *,
    now: datetime,
    required_weeks: int = GO_LIVE_MIN_WEEKS,
    required_agreement: float = GO_LIVE_MIN_AGREEMENT,
) -> GoLiveReadiness:
    """Judge a set of recorded shadow runs against the dossier's precondition.

    `observations` is anything iterable of `(observed_on, agreed)` — or of anything
    with those two attributes, so the caller does not have to build tuples from ORM
    rows and then lose the clock by rebuilding them.

    **Both gates, and the time one is checked first.** Four runs agreeing 100% is one
    day of evidence, and reporting it as a rate that "meets 95%" is the F101 shape
    again: a statistic that reads as excellent because it was measured over nothing.
    So the span is checked first and named, and a run set too young is reported as too
    young however well it agreed.
    """
    # `now` is required, not defaulted: `domain/` may not read the clock, and
    # `tests/unit/test_domain_purity.py` asserts it. It also forces the caller to say
    # whose clock the span is measured against, which matters when replaying history.
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    seen: list[tuple[datetime, bool]] = []
    for observation in observations:
        when = observation.observed_on if hasattr(observation, "observed_on") else observation[0]
        ok = bool(observation.agreed) if hasattr(observation, "agreed") else bool(observation[1])
        # The observation may be naive even when `now` is not: a replayed window read
        # out of a column, or a caller that normalised one end and not the other.
        # Subtracting a naive from an aware raises, and it raised here — on the one
        # path whose entire job is to survive odd inputs.
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seen.append((when, ok))
    seen.sort()
    runs = len(seen)
    agreements = sum(1 for _, ok in seen if ok)

    missing: list[str] = []
    observed_days = 0
    if runs:
        span = moment - seen[0][0]
        observed_days = max(0, span.days)
        required_days = required_weeks * 7
        if observed_days < required_days:
            missing.append(
                f"the record covers {observed_days} day(s) of parallel running, needs "
                f"{required_days} ({required_weeks} weeks). It has to be waited out: "
                f"this cannot be shortened by collecting more runs today"
            )
    else:
        missing.append("no shadow runs have been recorded at all")

    if runs:
        rate = agreements / runs
        if rate < required_agreement:
            missing.append(
                f"agreed {rate:.1%} of {runs} run(s), needs {required_agreement:.0%}. "
                f"A disagreement on a payment is not a rounding error"
            )
        if observed_days < required_weeks * 7:
            missing.append(
                "not enough elapsed time to reconcile weekly, so a trend cannot be "
                "read from a single cumulative rate"
            )

    cutoff = moment - timedelta(days=RECONCILE_EVERY_DAYS)
    recent = [(when, ok) for when, ok in seen if when >= cutoff]

    return GoLiveReadiness(
        runs=runs,
        agreements=agreements,
        observed_days=observed_days,
        required_weeks=required_weeks,
        required_agreement=required_agreement,
        last_week_runs=len(recent),
        last_week_agreements=sum(1 for _, ok in recent if ok),
        missing=tuple(missing),
    )


__all__ = [
    "GO_LIVE_MIN_AGREEMENT",
    "GO_LIVE_MIN_WEEKS",
    "RECONCILE_EVERY_DAYS",
    "Comparison",
    "GoLiveReadiness",
    "KeyAgreement",
    "Uncomparable",
    "compare",
    "normalise",
    "readiness",
]
