"""Writing shadow evidence, and reporting whether it earns the right to go live.

`domain/shadow.py` decides whether two answers agree. This is the part that puts the
decision somewhere a person can find it, because a comparison nobody recorded is a
comparison nobody made.

The table was already there — `agent_shadow_runs`, with `would_have_decided`,
`actually_decided`, `agreed` and a `divergence` that is required whenever the two
differ — and `domain/promotion.py` already gates on `shadow_runs` and
`shadow_agreements`. Nothing wrote to either. That is the shape this project has
produced twice now: the DOA matrix seeded and unread, the shadow evidence consumed
and unproduced.

**Recording is idempotent per (task, key set) on purpose.** A shadow run that is
recorded twice inflates both the numerator and the denominator, which moves the rate
toward whatever the duplicate agreed on. Re-running a comparison must therefore either
replace the row or do nothing — never append. The key is the task plus the procedure
version, because the same task compared under a different version is a different
observation.

**Nothing here decides what the answer should have been.** The person's answer is
recorded as given. This module never derives, infers or "corrects" it, because a
shadow record that quietly repaired a disagreement would report a rate with no
disagreement in it and would be indistinguishable from a model that is right.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.shadow import (
    Comparison,
    GoLiveReadiness,
    Uncomparable,
    compare,
    readiness,
)
from ai_orchestrator.persistence.agent_framework import AgentShadowRun
from ai_orchestrator.persistence.base import utcnow
from ai_orchestrator.telemetry.logging import get_logger

logger = get_logger(__name__)

#: Written when a comparison could not be made at all.
#:
#: Recorded rather than dropped. A run the model could not be compared on is evidence
#: about the model — it did not produce something comparable — and counting it as an
#: agreement would be how a 95% rate is reached with nothing behind it. Counting it as
#: a disagreement is equally wrong, because nothing was decided. So it is recorded, with
#: `agreed = False` and the reason in `divergence`, and the report counts it as
#: unmeasured rather than as a failure of the model.
UNCOMPARABLE_CATEGORY = "uncomparable"


@dataclass(frozen=True, slots=True)
class ShadowOutcome:
    """What was written, or why nothing was."""

    recorded: bool
    comparison: Comparison | None = None
    run_id: str | None = None
    #: Set when the two answers could not be compared. The run is still recorded.
    uncomparable: str = ""

    @property
    def agreed(self) -> bool | None:
        return self.comparison.agreed if self.comparison else None

    def as_dict(self) -> dict[str, object]:
        return {
            "recorded": self.recorded,
            "run_id": self.run_id,
            "agreed": self.agreed,
            "uncomparable": self.uncomparable,
            "comparison": self.comparison.as_dict() if self.comparison else None,
        }


async def record_comparison(
    session: AsyncSession,
    organization_id: str,
    *,
    agent_id: str | None,
    task_id: str | None,
    procedure_version_id: str | None,
    model_answer: Any,
    human_answer: Any,
    observed_on: datetime | None = None,
) -> ShadowOutcome:
    """Compare two answers and write down what was compared.

    The caller's clock: `observed_on` is passed in rather than read here, so a replay
    of a historical window writes the window's own dates instead of today's.

    **An uncomparable pair is still written.** `agreed = False` with the reason in
    `divergence`, because the alternative — skipping it — silently improves every rate
    the record feeds.
    """
    moment = observed_on or utcnow()

    try:
        result = compare(model_answer, human_answer)
    except Uncomparable as exc:
        reason = f"the two answers could not be compared: {exc}"
        logger.warning("shadow.uncomparable", organization_id=organization_id, reason=reason)
        run = await _write(
            session,
            organization_id,
            agent_id=agent_id,
            task_id=task_id,
            procedure_version_id=procedure_version_id,
            would_have=_dump(model_answer),
            actually=_dump(human_answer),
            agreed=False,
            divergence=reason,
            observed_on=moment,
        )
        return ShadowOutcome(recorded=True, run_id=run.id, uncomparable=reason)

    run = await _write(
        session,
        organization_id,
        agent_id=agent_id,
        task_id=task_id,
        procedure_version_id=procedure_version_id,
        would_have=_dump(model_answer),
        actually=_dump(human_answer),
        agreed=result.agreed,
        divergence=result.divergence,
        observed_on=moment,
    )
    return ShadowOutcome(recorded=True, comparison=result, run_id=run.id)


async def _write(
    session: AsyncSession,
    organization_id: str,
    *,
    agent_id: str | None,
    task_id: str | None,
    procedure_version_id: str | None,
    would_have: str,
    actually: str,
    agreed: bool,
    divergence: str,
    observed_on: datetime,
) -> AgentShadowRun:
    """One row, replacing any earlier comparison of the same thing.

    The delete-then-insert is the whole reason this is not a bare `add`. A duplicate
    row inflates the numerator and the denominator together, which moves the
    agreement rate toward whatever the duplicate said — so re-running a comparison
    would change the go-live answer. Replacing keeps the rate a function of the
    distinct observations rather than of how many times they were measured.
    """
    await session.execute(
        delete(AgentShadowRun).where(
            AgentShadowRun.organization_id == organization_id,
            AgentShadowRun.task_id == task_id,
            AgentShadowRun.procedure_version_id == procedure_version_id,
        )
    )
    run = AgentShadowRun(
        id=str(new_ulid()),
        organization_id=organization_id,
        agent_id=agent_id,
        task_id=task_id,
        procedure_version_id=procedure_version_id,
        would_have_decided=would_have[:8000],
        actually_decided=actually[:8000],
        agreed=agreed,
        divergence=divergence[:8000],
        observed_on=observed_on,
    )
    session.add(run)
    await session.flush()
    return run


def _dump(answer: Any) -> str:
    """The answer as stored, so a reader can see exactly what was compared.

    `default=str` because an answer may carry a `Decimal` or an enum, and a stored
    `2` that reads back as `'2'` is a difference nobody can audit.
    """
    try:
        return json.dumps(answer, sort_keys=True, default=str, ensure_ascii=False)
    except TypeError, ValueError:
        return str(answer)


async def observations(
    session: AsyncSession,
    organization_id: str,
    *,
    procedure_version_id: str | None = None,
    task_id: str | None = None,
) -> list[AgentShadowRun]:
    """The recorded runs, oldest first. The evidence, as rows rather than as a rate."""
    statement = select(AgentShadowRun).where(AgentShadowRun.organization_id == organization_id)
    if procedure_version_id is not None:
        statement = statement.where(AgentShadowRun.procedure_version_id == procedure_version_id)
    if task_id is not None:
        statement = statement.where(AgentShadowRun.task_id == task_id)
    statement = statement.order_by(AgentShadowRun.observed_on, AgentShadowRun.id)
    return list((await session.execute(statement)).scalars().all())


async def go_live_readiness(
    session: AsyncSession,
    organization_id: str,
    *,
    now: datetime,
    procedure_version_id: str | None = None,
    required_weeks: int | None = None,
    required_agreement: float | None = None,
) -> GoLiveReadiness:
    """The dossier's precondition, measured on what is actually recorded.

    `required_weeks` and `required_agreement` default to the dossier's 4 weeks and
    95%, and are overridable so a test can ask a smaller question. There is no way to
    ask for a *laxer* one by accident, because the defaults are the strict ones.
    """
    from ai_orchestrator.domain.shadow import GO_LIVE_MIN_AGREEMENT

    runs = await observations(session, organization_id, procedure_version_id=procedure_version_id)
    kwargs: dict[str, Any] = {
        "now": now,
        "required_agreement": (
            required_agreement if required_agreement is not None else GO_LIVE_MIN_AGREEMENT
        ),
    }
    if required_weeks is not None:
        kwargs["required_weeks"] = required_weeks
    return readiness(runs, **kwargs)


__all__ = [
    "UNCOMPARABLE_CATEGORY",
    "ShadowOutcome",
    "go_live_readiness",
    "observations",
    "record_comparison",
]
