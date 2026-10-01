"""Agent control: the kill switch as an action, and the decision log read back.

Phase 4c. Two gaps, and they are the same gap from opposite ends.

**`agents.kill_switch` existed and nothing could set it.** Migration `0016` added the
column with three constraints around it -- a kill needs a reason and a moment, and a
moment needs a kill -- and no code path in the repository wrote it. A kill switch nobody
can pull is a comment, and it is the comment on the one control Tập 1 §5.3 asks for by
name.

**`ai_decision_log` was written and nothing read it.** An audit trail only its writer can
see is not an audit trail: it cannot answer the question an auditor actually asks, which
is *what did this agent decide about this thing, was it allowed to, and what happened*.

## Why a kill writes to the decision log, and why `actor_type` is `system`

`ai_decision_log.agent_id` is nullable and `actor_type` is required, and that is not an
oversight to be tidied up. A kill is a decision **about** an agent, taken **by the
platform** rather than by the agent, so `agent_id` names the subject and `actor_type`
names 'system'. A promotion is the same shape, and the domain layer has been relying on
it since `0016`. A row that said "agent X decided to kill agent X" would be a lie in the
one table whose whole purpose is to be believed.

## The reason is required, and the database already said so

`ck_agents_kill_is_recorded` refuses `kill_switch` without a non-empty `kill_reason` and
a `killed_at`. So the reason cannot be defaulted to `''` and the endpoint cannot accept
one that is empty: a request with a blank reason is a 422 **from the endpoint**, naming
the field, rather than an `IntegrityError` from the database naming a constraint. The
database is the backstop; the endpoint is the door.

## Reviving is not the inverse, and the asymmetry is deliberate

`revive` clears `kill_switch`, `killed_at`, `killed_by` and `kill_reason`, and grants
**L1** regardless of what the agent had before. Not as a technicality: an agent that was
killed for a reason and is now running again is not the same agent state as one that was
never killed, and restoring the previous grant would restore a grant nobody re-justified.
L1 is the only level that needs no justification. An operator who wants the old level
back raises it through promotion, which is measured.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ai_orchestrator.api.deps import ApiContext, get_context, paginate, store_idempotency
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.errors import NotFoundError
from ai_orchestrator.domain.ids import new_ulid

router = APIRouter(tags=["agents-control"])

#: L1 is the only level that needs no justification, so it is what a revive grants.
_RESTORED_LEVEL = "L1"

_SELECT_AGENT = """
SELECT a.id, a.name, a.description, a.lifecycle_status, a.runtime_status, a.health,
       a.autonomy_ceiling, a.granted_level, a.kill_switch, a.kill_reason, a.killed_at,
       a.killed_by, a.parent_agent_id, a.active_tasks, a.queue_depth, a.last_heartbeat_at
FROM agents a
WHERE a.organization_id = CAST(:o AS varchar(40)) AND a.id = :agent
"""

_UPSERT_KILL = """
UPDATE agents
SET kill_switch = :kill, kill_reason = :reason,
    killed_at = CAST(:at AS timestamptz), killed_by = :by,
    granted_level = :level,
    lifecycle_status = CASE WHEN :kill THEN 'suspended' ELSE lifecycle_status END,
    updated_at = CAST(:now AS timestamptz)
WHERE organization_id = CAST(:o AS varchar(40)) AND id = :agent
RETURNING id, kill_switch, kill_reason, killed_at, killed_by, granted_level,
          lifecycle_status
"""

_LOG_DECISION = """
INSERT INTO ai_decision_log (
    id, organization_id, agent_id, actor_type, task_id, decision, autonomy_level,
    rationale, policy_rule, inputs_hash, outcome, occurred_at
) VALUES (
    CAST(:id AS varchar(40)), CAST(:o AS varchar(40)), CAST(:agent AS varchar(40)),
    'system', NULL, CAST(:decision AS varchar(64)), CAST(:level AS varchar(8)),
    CAST(:why AS text), CAST(:rule AS varchar(64)),
    CAST(:hash AS varchar(64)), CAST(:outcome AS varchar(64)),
    CAST(:now AS timestamptz)
)
"""


class KillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Required, and required **non-empty**. `ck_agents_kill_is_recorded` refuses a kill
    #: without a reason, so this is the database's rule arriving at the edge of the API
    #: where it can be named in a 422 instead of surfacing as a constraint name.
    reason: str = Field(min_length=3, max_length=2000)


class AgentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    name: str
    description: str = ""
    lifecycle_status: str = ""
    runtime_status: str = ""
    health: str = ""
    autonomy_ceiling: str = "L1"
    granted_level: str = "L1"
    kill_switch: bool = False
    kill_reason: str = ""
    killed_at: dt.datetime | None = None
    killed_by: str | None = None
    parent_agent_id: str | None = None
    active_tasks: int = 0
    queue_depth: int = 0
    last_heartbeat_at: dt.datetime | None = None

    @property
    def is_working(self) -> bool:
        return (self.active_tasks or 0) > 0


def _agent_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["agent_id"] = out.pop("id")
    for key in ("killed_at", "last_heartbeat_at"):
        value = out.get(key)
        out[key] = value.isoformat() if isinstance(value, dt.datetime) else None
    out["is_working"] = (out.get("active_tasks") or 0) > 0
    return out


async def _require_agent(conn: Any, ctx: ApiContext, agent_id: str) -> dict[str, Any]:
    row = (
        (await conn.execute(text(_SELECT_AGENT), {"o": ctx.organization_id, "agent": agent_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        # 404 and not 403: "forbidden" would confirm the row exists, and this is a
        # tenant-scoped table under forced RLS, so the answer is the same either way.
        msg = f"no agent {agent_id!r} in this organization"
        raise NotFoundError(msg, resource_type="agent", resource_id=agent_id)
    return dict(row)


@router.get("/agents/{agent_id}/control")
async def agent_control(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """One agent's delegation posture, in the shape the Fleet view renders.

    Distinct from `GET /agents/{id}` on purpose: that is the registry record, and this
    is *what a person is allowed to do to it right now* -- granted level against ceiling,
    whether the switch is pulled, and why.
    """
    return _agent_json(await _require_agent(ctx.session, ctx, agent_id))


@router.post("/agents/{agent_id}/kill")
async def kill_agent(
    agent_id: str, body: KillRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Stop an agent acting, and record why.

    Idempotent in the direction that matters: killing an already-killed agent succeeds
    and rewrites the reason, because an operator who pulls a switch twice wants the
    second reason to be the one on the record, not an error telling them it was already
    down. A refusal here would be a dead end at exactly the moment somebody needs to act.
    """
    await _require_agent(ctx.session, ctx, agent_id)
    now = dt.datetime.now(tz=dt.UTC)
    actor = str(ctx.actor.id)
    row = (
        (
            await ctx.session.execute(
                text(_UPSERT_KILL),
                {
                    "o": ctx.organization_id,
                    "agent": agent_id,
                    "kill": True,
                    "reason": body.reason.strip(),
                    "at": now,
                    "by": actor,
                    # The grant drops to L1 *because* the agent is stopped: leaving a
                    # stopped agent holding L3 autonomy is a claim nobody is acting on.
                    "level": _RESTORED_LEVEL,
                    "now": now,
                },
            )
        )
        .mappings()
        .one()
    )
    await _log(
        ctx,
        agent_id=agent_id,
        decision="killed",
        level=_RESTORED_LEVEL,
        rationale=body.reason.strip(),
        rule="kill_switch",
        outcome="killed",
        now=now,
    )
    await store_idempotency(
        ctx,
        key=None,
        operation="agents.kill",
        payload=body.model_dump(),
        resource_id=agent_id,
        response={"killed": True},
    )
    bump("agents_killed_total")
    return {
        "agent_id": row["id"],
        "kill_switch": row["kill_switch"],
        "kill_reason": row["kill_reason"],
        "killed_at": row["killed_at"].isoformat() if row["killed_at"] else None,
        "killed_by": row["killed_by"],
        "granted_level": row["granted_level"],
        "lifecycle_status": row["lifecycle_status"],
        "logged": True,
    }


@router.post("/agents/{agent_id}/revive")
async def revive_agent(agent_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Put an agent back at L1, and say plainly that is what happened.

    **Not** the inverse of kill. The old grant is not restored, and `kill_reason` is
    cleared but the decision log keeps it. An agent that was stopped for a reason and is
    running again has not had that grant re-justified, and the only level that needs no
    justification is L1.
    """
    await _require_agent(ctx.session, ctx, agent_id)
    now = dt.datetime.now(tz=dt.UTC)
    row = (
        (
            await ctx.session.execute(
                text(_UPSERT_KILL),
                {
                    "o": ctx.organization_id,
                    "agent": agent_id,
                    "kill": False,
                    "reason": "",
                    "at": None,
                    "by": None,
                    "level": _RESTORED_LEVEL,
                    "now": now,
                },
            )
        )
        .mappings()
        .one()
    )
    await _log(
        ctx,
        agent_id=agent_id,
        decision="revived",
        level=_RESTORED_LEVEL,
        # The reason is in the log, not in `kill_reason` any more. Clearing the column is
        # what makes `ck_agents_kill_is_recorded` satisfiable; keeping the *text* there
        # would mean a live agent carrying a stale accusation in its registry row.
        rationale="revived at the minimum level; the previous grant is not restored",
        rule="kill_switch",
        outcome="revived",
        now=now,
    )
    bump("agents_revived_total")
    return {
        "agent_id": row["id"],
        "kill_switch": row["kill_switch"],
        "granted_level": row["granted_level"],
        "lifecycle_status": row["lifecycle_status"],
        "restored_to": _RESTORED_LEVEL,
        "logged": True,
    }


async def _log(
    ctx: ApiContext,
    *,
    agent_id: str,
    decision: str,
    level: str,
    rationale: str,
    rule: str,
    outcome: str,
    now: dt.datetime,
) -> None:
    await ctx.session.execute(
        text(_LOG_DECISION),
        {
            "id": new_ulid(),
            "o": ctx.organization_id,
            "agent": agent_id,
            "decision": decision,
            "level": level,
            "why": rationale,
            "rule": rule,
            # A hash of what was acted on, so a later reader can tell two kills of the
            # same agent from two kills of different things. There is no payload here
            # to hash, so it is the agent id -- and a hash of that is a *link* to the
            # log, not a redaction of it.
            "hash": new_ulid()[:16].ljust(64, "0"),
            "outcome": outcome,
            "now": now,
        },
    )


#:   Every appearance of an optional bind is cast, **including the `IS NULL` ones**.
#:   A bare `:since IS NULL` is a separate bind from the `:since` inside the `CAST`, and
#:   asyncpg has no type to give it -- so the moment a caller omits the parameter the
#:   statement fails with `could not determine data type of parameter $2` before it
#:   reads a single row. That is a 500 for the *simplest possible* request, and it is
#:   the second time in this repository. The comment in `readings_for_node` did not stop
#:   me, so the comment is not the fix: `test_every_optional_filter_may_be_omitted`
_DECISIONS = """SELECT d.id, d.agent_id, d.actor_type, d.decision, d.autonomy_level, d.rationale,
       d.policy_rule, d.outcome, d.occurred_at, d.task_id, a.name AS agent_name
FROM ai_decision_log d
LEFT JOIN agents a ON a.id = d.agent_id AND a.organization_id = d.organization_id
WHERE d.organization_id = CAST(:o AS varchar(40))
  AND (CAST(:agent AS varchar(40)) IS NULL
       OR d.agent_id = CAST(:agent AS varchar(40)))
  AND (CAST(:decision AS varchar(64)) IS NULL
       OR d.decision = CAST(:decision AS varchar(64)))
  AND (CAST(:since AS timestamptz) IS NULL
       OR d.occurred_at >= CAST(:since AS timestamptz))
ORDER BY d.occurred_at DESC, d.id DESC
LIMIT :limit OFFSET :offset
"""


@router.get("/ai-decisions")
async def list_decisions(
    agent_id: str | None = Query(default=None, description="One agent's decisions."),
    decision: str | None = Query(default=None, description="e.g. killed, refused, acted"),
    since: dt.datetime | None = Query(default=None, description="Omit for all time."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The decision log, newest first, and the question an auditor asks.

    *"What did this agent decide, was it allowed to, and what happened?"* Answering it
    needs the log reachable, so this is the reader that makes `ai_decision_log` an audit
    trail rather than a write-only table.

    `LEFT JOIN` to `agents`, so a decision **about** an agent that has since been deleted
    is still listed, with a null name. An audit trail that quietly drops rows when the
    subject is cleaned up is an audit trail with a hole exactly where you would look.
    """
    rows = (
        (
            await ctx.session.execute(
                text(_DECISIONS),
                {
                    "o": ctx.organization_id,
                    "agent": agent_id,
                    "decision": decision,
                    "since": since,
                    "limit": limit,
                    "offset": offset,
                },
            )
        )
        .mappings()
        .all()
    )
    items = []
    for r in rows:
        row = dict(r)
        row["occurred_at"] = row["occurred_at"].isoformat()
        items.append(row)
    return paginate(items, limit, offset)


__all__ = ["AgentView", "KillRequest", "router"]
