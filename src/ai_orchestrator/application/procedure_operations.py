"""Closing the learning loop: propose a version, shadow it, and promote it.

`domain/promotion.py` decides. This module writes, following `progress_operations.py`
exactly: SQL is module constants, timestamps are required parameters rather than clock
reads, and every statement casts `:o` because `organization_id` is `varchar(40)` and an
untyped bind makes Postgres deduce `text` from one context and `varchar` from another.

## The loop, and the hole in it

Before this file existed, the platform could:

    notice a repeated failure -> propose a change -> a human approves it -> ...

and then stop. `load_approved()` had no callers, and no `procedures` table existed to
apply an approved change to. The next run did the same thing again. That is a suggestion
box with an audit trail.

This module supplies the missing three moves, and the order is the design:

1. `record_shadow_run` — what the candidate version *would* have done, and what
   actually happened. Written first, because promotion reads these and a gate over
   evidence nobody recorded is a gate over nothing.
2. `propose_version` — an agent's proposal becomes a `shadow` row citing the approval
   that authorised it. It does **not** change behaviour; the `CHECK` on
   `procedure_versions` refuses an agent-proposed row with no approval.
3. `promote_version` — the one function that makes a version live, and the only one
   that takes `action_classes` and reads `autonomy_policies`, because those are what
   the dossier's L1-L4 limits are expressed in.

## Why promotion is the only function here that reads two tables

`propose_version` reads one row from `approvals` and writes one row. `promote_version`
reads the version, its runs, the proposing agent's kill switch, and the whole autonomy
policy table, then writes three tables in one transaction. That asymmetry is the point:
the cheap operation is cheap because it has to be, and the expensive one is expensive
because it is about to change how the platform behaves.

## Every refusal is recorded as a decision, not just returned

`promote_version` writes a row to `ai_decision_log` whether it promotes or refuses, and
the `CHECK` on that table refuses a `refused` row with an empty rationale. So a refusal
that cannot explain itself is a refusal that cannot be written down, and the two
constraints together mean the log cannot accumulate meaningless entries.

That is the whole point of the dossier's segregation-of-duties principle expressed in
data: the interesting row in an AI decision log is the one where the system declined.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.domain.autonomy import Policy
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.domain.promotion import (
    Block,
    PromotionPolicy,
    Verdict,
    VersionFacts,
    evaluate,
)

#: A shadow run has to belong to somebody, and the two options are an agent and a
#: procedure version. The composite foreign keys are the same as the schema's, so a
#: run cannot be attributed across a tenant boundary even by a hand-written query.
_INSERT_SHADOW_RUN = """
INSERT INTO agent_shadow_runs (
    id, organization_id, agent_id, procedure_version_id, task_id,
    would_have_decided, actually_decided, agreed, divergence, observed_on
)
VALUES (
    :i, CAST(:o AS varchar(40)), :agent, :version, :task,
    :would, :actually, :agreed, :divergence, CAST(:now AS timestamptz)
)
"""

_INSERT_VERSION = """
INSERT INTO procedure_versions (
    id, organization_id, procedure_id, version_no, status, body, body_hash,
    autonomy_ceiling, rationale, source, source_actor, proposal_id, approval_id,
    shadow_runs, shadow_agreements, created_at, updated_at
)
VALUES (
    :i, CAST(:o AS varchar(40)), :procedure, :version_no, 'shadow', :body, :hash,
    CAST(:ceiling AS varchar(2)), :rationale, CAST(:source AS varchar(128)), :actor,
    :proposal, :approval, 0, 0, CAST(:now AS timestamptz), CAST(:now AS timestamptz)
)
RETURNING version_no
"""

_NEXT_VERSION_NO = """
SELECT coalesce(max(version_no), 0) + 1 FROM procedure_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND procedure_id = :procedure
"""

_LOAD_VERSION = """
SELECT v.id, v.status, v.shadow_runs, v.shadow_agreements, v.autonomy_ceiling,
       v.source, v.proposal_id, v.approval_id, v.procedure_id,
       coalesce(a.kill_switch, false) AS author_killed
FROM procedure_versions v
LEFT JOIN agents a ON a.id = v.source_actor AND a.organization_id = v.organization_id
WHERE v.id = :id AND v.organization_id = CAST(:o AS varchar(40))
"""

_LOAD_POLICIES = """
SELECT action_class, max_level, is_hard_block FROM autonomy_policies
WHERE organization_id = CAST(:o AS varchar(40))
"""

_LOAD_RUNS = """
SELECT id, agreed, divergence, would_have_decided, actually_decided
FROM agent_shadow_runs
WHERE organization_id = CAST(:o AS varchar(40)) AND procedure_version_id = :version
ORDER BY observed_on
"""

#: Recount from the runs rather than incrementing the denormalised totals. The totals
#: exist so a promotion query does not aggregate a run table on every row it reads;
#: they are not the source of truth, and a promotion that trusted a counter would
#: promote on a number nobody can reconcile with the rows behind it.
_RECOUNT = """
UPDATE procedure_versions
SET shadow_runs = :runs, shadow_agreements = :agreements,
    updated_at = CAST(:now AS timestamptz)
WHERE id = :id AND organization_id = CAST(:o AS varchar(40))
"""

#: Read the current live version *before* changing anything, so the identifiers that
#: are being superseded are known rather than inferred. A single
#: `UPDATE ... WHERE status = 'active'` cannot report what it touched without
#: `RETURNING`, and mixing `RETURNING` with the subsequent activate-and-repoint steps
#: makes the transaction's failure modes harder to read than three statements.
_SELECT_ACTIVE = """
SELECT id FROM procedure_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND procedure_id = :procedure
  AND status = 'active'
"""

_SUPERSEDE = """
UPDATE procedure_versions
SET status = 'superseded', updated_at = CAST(:now AS timestamptz)
WHERE id = :id AND organization_id = CAST(:o AS varchar(40)) AND status = 'active'
"""

_ACTIVATE = """
UPDATE procedure_versions
SET status = 'active', promoted_at = CAST(:now AS timestamptz), promoted_by = :by,
    updated_at = CAST(:now AS timestamptz)
WHERE id = :id AND organization_id = CAST(:o AS varchar(40)) AND status = 'shadow'
RETURNING id
"""

_POINT_PROCEDURE = """
UPDATE procedures
SET current_version_id = :version
WHERE id = :procedure AND organization_id = CAST(:o AS varchar(40))
"""

_LOG_DECISION = """
INSERT INTO ai_decision_log (
    id, organization_id, agent_id, actor_type, task_id, decision, autonomy_level,
    rationale, policy_rule, inputs_hash, outcome, occurred_at
)
VALUES (
    :i, CAST(:o AS varchar(40)), :agent, CAST(:actor AS varchar(128)), :task,
    CAST(:decision AS varchar(16)),
    CAST(:level AS varchar(2)), :rationale, :rule, :hash, :outcome,
    CAST(:now AS timestamptz)
)
"""


@dataclass(frozen=True, slots=True)
class ShadowRunRecord:
    """One comparison. The two decisions are kept apart because the comparison is the
    whole value of the row — a single `result` column would be asserting the answer
    rather than recording the evidence.
    """

    would_have_decided: str
    actually_decided: str
    agreed: bool
    divergence: str = ""


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    """The verdict, and what the promotion actually did.

    `blocks` non-empty means nothing was written to `procedure_versions` except the
    run counts, which is the important half of the guarantee: a refusal still leaves
    the evidence intact, so the same refusal can be reviewed and the same gates
    re-evaluated once the evidence improves.
    """

    version_id: str
    verdict: Verdict
    decision: str
    recorded_runs: int = 0
    superseded: tuple[str, ...] = field(default_factory=tuple)

    @property
    def promoted(self) -> bool:
        return self.decision == "acted"

    def as_dict(self) -> dict[str, object]:
        return {
            "version_id": self.version_id,
            "promoted": self.promoted,
            "decision": self.decision,
            "recorded_runs": self.recorded_runs,
            "superseded": list(self.superseded),
            **self.verdict.as_dict(),
        }


async def record_shadow_run(
    conn: AsyncConnection,
    *,
    organization_id: str,
    observed_at: dt.datetime,
    version_id: str,
    run: ShadowRunRecord,
    agent_id: str | None = None,
    task_id: str | None = None,
) -> str:
    """Record one shadow comparison and refresh the version's totals.

    `observed_at` is required rather than defaulted, for the reason
    `progress_operations.write_reading` requires `observed_on`: a shadow run's moment
    is part of the record, and the purity test allows a clock read in exactly one file.

    A `CHECK` on the table refuses a disagreement with an empty `divergence`, so the
    failure happens here rather than at promotion time, when the reasons are two weeks
    old and nobody remembers.
    """
    if observed_at.tzinfo is None:
        raise ValueError(
            "observed_at must be timezone-aware; `observed_on` is timestamptz so every "
            "row this compares against is aware, and a naive value raises TypeError "
            "from inside the subtraction rather than here"
        )
    if not run.agreed and not run.divergence.strip():
        raise ValueError(
            "a shadow run that disagreed must say how. Promotion is a rate, and a "
            "rate computed over disagreements nobody explained is not actionable"
        )

    run_id = f"asr_{new_ulid()}"
    async with conn.begin_nested():
        await conn.execute(
            text(_INSERT_SHADOW_RUN),
            {
                "i": run_id,
                "o": organization_id,
                "agent": agent_id,
                "version": version_id,
                "task": task_id,
                "would": run.would_have_decided,
                "actually": run.actually_decided,
                "agreed": run.agreed,
                "divergence": run.divergence,
                "now": observed_at,
            },
        )
        # The counts land in the version row; this caller does not need them.
        await _recount(conn, organization_id, version_id, observed_at)
    return run_id


async def _recount(
    conn: AsyncConnection, organization_id: str, version_id: str, now: dt.datetime
) -> tuple[int, int]:
    """Recount from the rows, never increment a counter.

    A counter that drifts is worse than no counter: it is a number that looks right in
    a dashboard and promotes a version on evidence that does not exist.
    """
    rows = (
        (await conn.execute(text(_LOAD_RUNS), {"o": organization_id, "version": version_id}))
        .mappings()
        .all()
    )
    agreements = sum(1 for r in rows if r["agreed"])
    await conn.execute(
        text(_RECOUNT),
        {
            "o": organization_id,
            "id": version_id,
            "runs": len(rows),
            "agreements": agreements,
            "now": now,
        },
    )
    return len(rows), agreements


async def propose_version(
    conn: AsyncConnection,
    *,
    organization_id: str,
    procedure_id: str,
    proposed_at: dt.datetime,
    body: str,
    autonomy_ceiling: str = "L1",
    rationale: str = "",
    source: str = "human",
    source_actor: str = "",
    proposal_id: str | None = None,
    approval_id: str | None = None,
) -> str:
    """Write a new version in `shadow`. It does not change behaviour.

    `source = 'agent_proposal'` requires `proposal_id` **and** `approval_id`; the two
    `CHECK` constraints on the table refuse either omission. That is the link which
    closes the loop, and it is a constraint rather than a branch here so a second
    writer cannot produce a change nobody agreed to.

    The version number is `max + 1` rather than supplied, so two concurrent proposals
    cannot claim the same number — and the `UNIQUE (organization_id, procedure_id,
    version_no)` is what actually enforces it, with this being the friendly path.
    """
    if proposed_at.tzinfo is None:
        raise ValueError("proposed_at must be timezone-aware; see `record_shadow_run`")
    if source == "agent_proposal" and not approval_id:
        raise ValueError(
            "an agent-proposed version must cite the approval that authorised it. "
            "This is the constraint that stops the learning loop running on "
            "unauthorised changes, and it is enforced in the table as well as here"
        )

    version_id = f"prv_{new_ulid()}"
    next_no: int = (
        await conn.execute(
            text(_NEXT_VERSION_NO),
            {"o": organization_id, "procedure": procedure_id},
        )
    ).scalar_one()
    await conn.execute(
        text(_INSERT_VERSION),
        {
            "i": version_id,
            "o": organization_id,
            "procedure": procedure_id,
            "version_no": next_no,
            "body": body,
            # A content hash rather than a length: two revisions of the same
            # procedure with the same number of characters are different documents,
            # and a hash is the only thing here that can tell them apart.
            "hash": _sha256(body),
            "ceiling": autonomy_ceiling,
            "rationale": rationale,
            "source": source,
            "actor": source_actor,
            "proposal": proposal_id,
            "approval": approval_id,
            "now": proposed_at,
        },
    )
    return version_id


def _sha256(text_value: str) -> str:
    import hashlib

    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()


async def promote_version(
    conn: AsyncConnection,
    *,
    organization_id: str,
    version_id: str,
    decided_at: dt.datetime,
    action_classes: tuple[str, ...],
    promoted_by: str = "human",
    policy: PromotionPolicy | None = None,
) -> PromotionOutcome:
    """Decide, record, and either promote or explain the refusal.

    The only function here that changes behaviour, and the only one that reads the
    autonomy policy table — because the dossier's limits are expressed in that table
    and nowhere else.

    Order of operations, which matters:

    1. Load the version, its runs, and the proposing agent's kill switch.
    2. Recount the runs, so the gate reads rows rather than a counter.
    3. Evaluate. **Every** gate, so the caller learns all the reasons at once.
    4. Log the decision — promoted or refused, always.
    5. Only if it may promote: supersede the current, activate this one, repoint the
       procedure. All inside one savepoint, because a half-applied promotion leaves a
       procedure pointing at a version nothing runs, which is the failure this
       ordering exists to make impossible.
    """
    if decided_at.tzinfo is None:
        raise ValueError("decided_at must be timezone-aware; see `record_shadow_run`")

    row = (
        (await conn.execute(text(_LOAD_VERSION), {"o": organization_id, "id": version_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError(
            f"no version {version_id} in this organization. A missing row is not a "
            f"refusal to promote -- it is a request about nothing"
        )

    runs, agreements = await _recount(conn, organization_id, version_id, decided_at)
    policies = await _load_policies(conn, organization_id)

    verdict = evaluate(
        VersionFacts(
            version_id=version_id,
            status=row["status"],
            shadow_runs=runs,
            shadow_agreements=agreements,
            autonomy_ceiling=row["autonomy_ceiling"],
            action_classes=action_classes,
            author_killed=bool(row["author_killed"]),
        ),
        policies,
        policy=policy,
    )

    decision = "acted" if verdict.may_promote else "refused"
    rationale = (
        "every gate passed and the shadow agreement rate was "
        f"{verdict.permitted_level} or below the claimed ceiling"
        if verdict.may_promote
        else "; ".join(verdict.reasons)
    )
    await _log(
        conn,
        organization_id=organization_id,
        # `agent_id` is null and `actor_type` says `system:promotion`: this decision
        # is *about* an agent rather than taken by one, and a human-written version
        # has no agent to name. `ai_decision_log` carries both columns for exactly
        # this case, the same way `audit_logs` does.
        actor_type="system:promotion",
        agent_id=None,
        decision=decision,
        level=row["autonomy_ceiling"],
        rationale=rationale,
        rule=(
            "promotion.all_gates_passed"
            if verdict.may_promote
            else "promotion." + ",".join(b.value for b in verdict.blocks)
        ),
        now=decided_at,
    )

    if not verdict.may_promote:
        return PromotionOutcome(
            version_id=version_id, verdict=verdict, decision="refused", recorded_runs=runs
        )

    superseded: tuple[str, ...] = ()
    async with conn.begin_nested():
        # Supersede *before* activating. The partial unique index allows one `active`
        # version per procedure, so activating first would be refused -- and being
        # refused there, after the decision was already logged as `acted`, would be
        # the worst possible place to find out.
        current: Sequence[str] = (
            (
                await conn.execute(
                    text(_SELECT_ACTIVE),
                    {"o": organization_id, "procedure": row["procedure_id"]},
                )
            )
            .scalars()
            .all()
        )
        superseded = tuple(current)
        for previous in current:
            await conn.execute(
                text(_SUPERSEDE),
                {"o": organization_id, "id": previous, "now": decided_at},
            )
        activated = await conn.execute(
            text(_ACTIVATE),
            {
                "o": organization_id,
                "id": version_id,
                "by": promoted_by,
                "now": decided_at,
            },
        )
        if activated.rowcount != 1:
            # The version was in `shadow` when it was evaluated and is not now, which
            # means somebody else moved it between the two statements. Refusing here
            # rolls back the savepoint, so the superseded version is restored too and
            # the procedure is never left pointing at nothing.
            raise RuntimeError(
                f"version {version_id} was 'shadow' when evaluated and could not be "
                f"activated; another writer changed it first. The promotion has been "
                f"rolled back and no version is live that should not be"
            )
        await conn.execute(
            text(_POINT_PROCEDURE),
            {
                "o": organization_id,
                "procedure": row["procedure_id"],
                "version": version_id,
            },
        )
    return PromotionOutcome(
        version_id=version_id,
        verdict=verdict,
        decision="acted",
        recorded_runs=runs,
        superseded=superseded,
    )


async def _load_policies(conn: AsyncConnection, organization_id: str) -> dict[str, Policy]:
    """The dossier's ceilings, as domain objects.

    `domain/autonomy.py` does not read the database, so this is the only place the
    mapping from row to rule happens, and it is three fields.
    """
    rows = (await conn.execute(text(_LOAD_POLICIES), {"o": organization_id})).mappings().all()
    return {
        r["action_class"]: Policy(
            action_class=r["action_class"],
            max_level=r["max_level"],
            is_hard_block=bool(r["is_hard_block"]),
        )
        for r in rows
    }


async def _log(
    conn: AsyncConnection,
    *,
    organization_id: str,
    actor_type: str,
    agent_id: str | None,
    decision: str,
    level: str,
    rationale: str,
    rule: str,
    now: dt.datetime,
) -> None:
    """One row per decision, refused ones included.

    A `CHECK` on `ai_decision_log` refuses a `refused` row with an empty rationale, so
    the two constraints together mean this cannot silently write a meaningless entry.
    """
    await conn.execute(
        text(_LOG_DECISION),
        {
            "i": f"adl_{new_ulid()}",
            "o": organization_id,
            "agent": agent_id,
            "actor": actor_type,
            "task": None,
            "decision": decision,
            "level": level,
            "rationale": rationale,
            "rule": rule,
            "hash": "",
            "outcome": "",
            "now": now,
        },
    )


__all__ = [
    "Block",
    "PromotionOutcome",
    "ShadowRunRecord",
    "promote_version",
    "propose_version",
    "record_shadow_run",
]
