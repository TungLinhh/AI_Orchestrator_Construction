"""Task endpoints.

Task creation is the platform's main entry point, so it carries the two
properties that matter: it is idempotent on a client-supplied key, and it
returns immediately. The work is not done here — a workflow does that — because
an HTTP request must not be held open for work that can take minutes and wait on
a human.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ai_orchestrator.api.deps import (
    ApiContext,
    check_idempotency,
    get_context,
    idem_key,
    page_params,
    paginate,
    require_body_tenant,
    store_idempotency,
)
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.domain.errors import ValidationError
from ai_orchestrator.domain.ids import AgentId, TaskId
from ai_orchestrator.persistence.repositories.task import (
    DelegationRepository,
    ExecutionRepository,
    TaskRepository,
)

router = APIRouter(tags=["tasks"])


class CreateTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    goal: str = Field(min_length=1, max_length=20_000)
    task_type: str = "execution"
    owner_agent_id: str | None = None
    org_unit_id: str | None = None
    parent_task_id: str | None = None
    priority: str = "normal"
    input: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    expected_output_schema: dict[str, Any] | None = None
    budget_limit_usd: float | None = Field(default=None, ge=0)
    budget_limit_tokens: int | None = Field(default=None, ge=0)
    deadline_at: str | None = None
    # Two agents asking for the same work is refused unless it is explicit.
    allow_parallel: bool = False
    # Start a durable workflow. Off for a dry run or a test.
    start_workflow: bool = True
    organization_id: str | None = None


class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str
    objective: str = Field(min_length=1, max_length=20_000)
    input: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    budget_limit_usd: float | None = Field(default=None, ge=0)
    budget_limit_tokens: int | None = Field(default=None, ge=0)
    organization_id: str | None = None


class AddDependencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    depends_on_task_id: str
    dependency_type: str = "finish_to_start"


def _task_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "goal": task.goal,
        "task_type": task.task_type,
        "status": task.status,
        "priority": task.priority,
        "requester_id": task.requester_id,
        "requester_type": task.requester_type,
        "owner_agent_id": task.owner_agent_id,
        "org_unit_id": task.org_unit_id,
        "parent_task_id": task.parent_task_id,
        "fingerprint": task.fingerprint,
        "input": task.input,
        "output": task.output,
        "constraints": task.constraints,
        "budget_limit_usd": (
            float(task.budget_limit_usd) if task.budget_limit_usd is not None else None
        ),
        "spent_usd": float(task.spent_usd),
        "spent_tokens": task.spent_tokens,
        "attempt_count": task.attempt_count,
        "deadline_at": task.deadline_at.isoformat() if task.deadline_at else None,
        "workflow_id": task.workflow_id,
        "workflow_run_id": task.workflow_run_id,
        "last_error": task.last_error,
        "failure_category": task.failure_category,
        "created_at": task.created_at.isoformat(),
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


async def _requester_id(ctx: ApiContext) -> str | None:
    """The `users.id` this request should be attributed to, or `None`.

    `tasks.requester_id` is a foreign key to `users.id`, so it can only ever hold a
    real user. The control plane authenticates with a *service* principal
    (`svc:control-plane`) and agents have rows in `agents`, not `users` — so writing
    the actor's id into this column produced a foreign-key violation, which the
    repository then reported as a deduplication conflict. Every task created over
    HTTP came back as `409 an equivalent task was created concurrently`, which sends
    the caller to look for a duplicate they never made.

    `None` is the right answer, not a placeholder: the column is nullable precisely
    because work can be requested by something that is not a person. The actor is
    still recorded — in `requester_type`, and in every event this create emits — so
    nothing about who asked is lost.

    **And `ActorType.HUMAN` is not sufficient either**, which is the second half of
    the same lesson. With authentication off the principal is a human *stand-in*:
    `no_auth_principal` builds `ActorType.HUMAN` deliberately, because the Approve
    button has to work for a person at a browser. Its id is `dev:no-auth`, and a
    `users` row for it is provisioned by `scripts/seed_local_operator.py` into
    **one** organisation — the one holding the dossier catalogue. So in every other
    tenant the id is a human id with no `users` row, and the insert failed
    `fk_tasks_requester_id_users`.

    Measured as: every task created from the page in a tenant other than the
    provisioned one came back `VALIDATION ... a database integrity rule was
    violated`, with a hint about a column the reader never touched. The foreign key
    is the authority on who exists, so it is what is asked. One `SELECT` on the
    create path, and the page works in any organisation rather than one.
    """
    if ctx.actor.kind is not ActorType.HUMAN:
        return None

    from sqlalchemy import select

    from ai_orchestrator.persistence.models import User

    actor_id = str(ctx.actor.id)
    exists = await ctx.session.execute(
        select(User.id).where(
            User.organization_id == ctx.organization_id,
            User.id == actor_id,
        )
    )
    return actor_id if exists.scalar_one_or_none() is not None else None


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskRequest, request: Request, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Create a task and return immediately.

    The event is written in the same transaction as the task, so a committed task
    is always announced. The workflow is started after the commit: starting it
    inside the transaction would let a workflow observe a task that then rolls
    back.
    """
    require_body_tenant(body.model_dump(), ctx)
    payload = body.model_dump(exclude={"start_workflow", "organization_id"})
    key = idem_key(request)

    replayed = await check_idempotency(ctx, key=key, operation="create_task", payload=payload)
    if replayed is not None:
        return {**replayed["response"], "idempotent_replay": True}

    repo = TaskRepository(ctx.session, ctx.organization_id)
    deadline = None
    if body.deadline_at:
        from datetime import datetime

        try:
            deadline = datetime.fromisoformat(body.deadline_at)
        except ValueError as exc:
            msg = f"deadline_at is not an ISO 8601 timestamp: {body.deadline_at}"
            raise ValidationError(msg, details={"field": "deadline_at"}) from exc

    # **No owner means the chief.** A task with nobody to do it is a row the queue
    # will never drain: `POST /tasks/{id}/run` refuses it with "assign one first", and on
    # the page that refusal arrived as "The task exists but nothing is running it" right
    # after the person pressed Run with a roster that had failed to load. Measured, not
    # hypothetical — the picker shipped as "loading agents…" with nothing behind it (F266),
    # and every such press made an orphan.
    #
    # The chief is the root unit's head, not a name: a name is a second definition of who
    # is on top, and two definitions of that is how the page and the platform disagree.
    owner_agent_id = body.owner_agent_id or await _chief_agent_id(ctx)
    task = await repo.create(
        title=body.title,
        goal=body.goal,
        task_type=body.task_type,
        requester_id=await _requester_id(ctx),
        requester_type=ctx.actor.kind.value,
        owner_agent_id=owner_agent_id,
        org_unit_id=body.org_unit_id,
        parent_task_id=body.parent_task_id,
        priority=body.priority,
        input=body.input,
        constraints=body.constraints,
        expected_output_schema=body.expected_output_schema,
        budget_limit_usd=body.budget_limit_usd,
        budget_limit_tokens=body.budget_limit_tokens,
        deadline_at=deadline,
        allow_parallel=body.allow_parallel,
    )
    bump("tasks_created_total")

    response = _task_dict(task)
    if not body.owner_agent_id:
        response["owner_defaulted_to_chief"] = True
    await store_idempotency(
        ctx,
        key=key,
        operation="create_task",
        payload=payload,
        resource_id=task.id,
        response=response,
    )

    if body.start_workflow:
        # Started *after* the transaction commits, and this handler's session is
        # still open — so a workflow that observes the task must not be able to
        # observe a task that then rolls back. Temporal is the durable queue; the
        # commit is what makes the row real.
        #
        # `start_workflow` was a declared field that nothing read. Every task created
        # over HTTP sat in `created` forever, and the caller got a `201` — a
        # successful-looking response to a request that quietly did nothing. That is
        # worse than refusing the request, because the caller has no way to tell.
        started = await _start_workflow_for(ctx, task)
        response["workflow"] = started

    return response


async def _chief_agent_id(ctx: ApiContext) -> str | None:
    """The root unit's head agent, or `None` when the tree has no head.

    `None` is a real answer, not a failure: a tenant mid-seed has units without heads,
    and the task is then created ownerless exactly as before. The refusal at run time
    ("assign one first") is what protects that case, and it still does.
    """
    from sqlalchemy import select as _select

    from ai_orchestrator.persistence.models import Agent as _Agent
    from ai_orchestrator.persistence.models import OrgUnit as _OrgUnit

    root = (
        (
            await ctx.session.execute(
                _select(_OrgUnit).where(
                    _OrgUnit.organization_id == ctx.organization_id,
                    _OrgUnit.parent_id.is_(None),
                )
            )
        )
        .scalars()
        .first()
    )
    if root is None or not root.head_agent_id:
        return None
    agent = (
        (
            await ctx.session.execute(
                _select(_Agent).where(
                    _Agent.organization_id == ctx.organization_id,
                    _Agent.id == root.head_agent_id,
                    _Agent.lifecycle_status == "active",
                )
            )
        )
        .scalars()
        .first()
    )
    return str(agent.id) if agent is not None else None


async def _start_workflow_for(ctx: ApiContext, task: Any) -> dict[str, Any]:
    """Start the task's workflow, and report honestly if it did not start.

    The result is returned *in the response* rather than logged and forgotten. A
    caller that asked for work to begin and got a refusal needs to know which it was,
    and a boolean in a log line six frames deep is not something a browser tab can
    act on.
    """
    from ai_orchestrator.workflows.client import start_task_workflow

    result = await start_task_workflow(
        {"task_id": str(task.id), "organization_id": ctx.organization_id}
    )
    if result.started:
        return {
            "started": True,
            "workflow_id": result.workflow_id,
            "note": result.reason or "",
        }
    # Not an exception. The task exists and is committed; what failed is the
    # dispatch. Raising here would roll the task back and lose work the caller
    # legitimately asked for, so the row stays and the reason travels with it.
    return {
        "started": False,
        "reason": result.reason or "the workflow engine did not accept the task",
        "note": "the task exists but nothing is running it; start it from the task",
    }


@router.get("/tasks")
async def list_tasks(
    status_filter: str | None = None,
    owner_agent_id: str | None = None,
    parent_task_id: str | None = None,
    page: tuple[int, int] = Depends(page_params),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    repo = TaskRepository(ctx.session, ctx.organization_id)
    tasks = await repo.all(
        status=status_filter,
        owner_agent_id=owner_agent_id,
        parent_task_id=parent_task_id,
        limit=500,
    )
    limit, offset = page
    body = paginate([_task_dict(t) for t in tasks], limit, offset)
    body["counts_by_status"] = await repo.count_by_status()
    return body


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = TaskRepository(ctx.session, ctx.organization_id)
    task = await repo.get(task_id)
    return _task_dict(task)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Cancel a task.

    A cancel also signals the running workflow, so the work actually stops rather
    than finishing in the background and reporting success for a task nobody
    wants any more.
    """
    from ai_orchestrator.domain.state_machines import Transition

    repo = TaskRepository(ctx.session, ctx.organization_id)
    task = await repo.get(task_id)
    task = await repo.transition(task_id, Transition.CANCEL)
    bump("tasks_canceled_total")

    if task.workflow_id:
        from ai_orchestrator.workflows.client import cancel_workflow

        await cancel_workflow(task.workflow_id, reason="canceled by a human")
    return _task_dict(task)


@router.post("/tasks/{task_id}/dependencies", status_code=status.HTTP_201_CREATED)
async def add_dependency(
    task_id: str, body: AddDependencyRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    repo = TaskRepository(ctx.session, ctx.organization_id)
    dep = await repo.add_dependency(
        task_id=task_id,
        depends_on_task_id=body.depends_on_task_id,
        dependency_type=body.dependency_type,
    )
    return {"task_id": dep.task_id, "depends_on_task_id": dep.depends_on_task_id}


@router.post("/tasks/{task_id}/delegate", status_code=status.HTTP_201_CREATED)
async def delegate_task(
    task_id: str,
    body: CreateDelegationRequest,
    request: Request,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Delegate to another agent.

    Default-deny: with no `owner_agent_id` on the task there is no source agent, so
    the delegation is refused rather than attributed to whoever happened to call
    the API. A delegation with no source is unattributable work.
    """
    from ai_orchestrator.domain.delegation import DelegationLimits, DelegationPath
    from ai_orchestrator.domain.errors import PreconditionError

    require_body_tenant(body.model_dump(), ctx)
    tasks = TaskRepository(ctx.session, ctx.organization_id)
    task = await tasks.get(task_id)

    if not task.owner_agent_id:
        msg = (
            f"task {task_id} has no owner agent, so the delegation has no source; "
            f"assign an agent before delegating"
        )
        raise PreconditionError(msg, details={"task_id": task_id})

    platform = DelegationLimits.platform_default()
    delegations = DelegationRepository(ctx.session, ctx.organization_id)
    delegation = await delegations.record(
        parent_task_id=task_id,
        source_agent_id=task.owner_agent_id,
        target_agent_id=body.target_agent_id,
        objective=body.objective,
        # Branded, not `str`: the path is what the cycle check walks, and
        # `AgentId`/`TaskId` exist so an agent id cannot be passed where a task
        # id belongs. Plain strings here defeat the whole mechanism.
        path=DelegationPath.root(AgentId(task.owner_agent_id), TaskId(task_id)),
        platform_limits=platform,
        parent_limits=platform,
        input=body.input,
        constraints=body.constraints,
        budget_limit_usd=body.budget_limit_usd,
        budget_limit_tokens=body.budget_limit_tokens,
    )
    bump("delegations_created_total")

    return {
        "id": delegation.id,
        "parent_task_id": delegation.parent_task_id,
        "source_agent_id": delegation.source_agent_id,
        "target_agent_id": delegation.target_agent_id,
        "status": delegation.status,
        "depth": delegation.depth,
        "delegation_path": delegation.delegation_path,
    }


@router.get("/tasks/{task_id}/delegations")
async def list_delegations(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = DelegationRepository(ctx.session, ctx.organization_id)
    items = await repo.list_for_task(task_id)
    return {
        "items": [
            {
                "id": d.id,
                "source_agent_id": d.source_agent_id,
                "target_agent_id": d.target_agent_id,
                "status": d.status,
                "depth": d.depth,
                "objective": d.objective,
                "delegation_path": d.delegation_path,
                "denial_reason": d.denial_reason,
                "created_at": d.created_at.isoformat(),
            }
            for d in items
        ],
        "count": len(items),
    }


@router.get("/executions/{execution_id}")
async def get_execution(
    execution_id: str, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """The record of one attempt: what ran, on what, at what cost, and why.

    This is the artifact that answers "why did this run behave this way",
    deliberately without the model's private reasoning.
    """
    repo = ExecutionRepository(ctx.session, ctx.organization_id)
    execution = await repo.get(execution_id)
    return {
        "id": execution.id,
        "task_id": execution.task_id,
        "agent_id": execution.agent_id,
        "delegation_id": execution.delegation_id,
        "attempt": execution.attempt,
        "status": execution.status,
        "runtime_adapter": execution.runtime_adapter,
        "model_profile": execution.model_profile,
        "model_used": execution.model_used,
        "agent_definition_version": execution.agent_definition_version,
        "policy_version": execution.policy_version,
        "workflow_id": execution.workflow_id,
        "trace_id": execution.trace_id,
        "summary": execution.summary,
        "decision_record": execution.decision_record,
        "artifacts": execution.artifacts,
        "error_kind": execution.error_kind,
        "error_category": execution.error_category,
        "error_message": execution.error_message,
        "input_tokens": execution.input_tokens,
        "output_tokens": execution.output_tokens,
        "reasoning_tokens": execution.reasoning_tokens,
        "cost_usd": float(execution.cost_usd),
        "duration_ms": execution.duration_ms,
        "retry_count": execution.retry_count,
        "started_at": execution.started_at.isoformat(),
        "finished_at": execution.finished_at.isoformat() if execution.finished_at else None,
    }


@router.get("/tasks/{task_id}/executions")
async def list_executions(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = ExecutionRepository(ctx.session, ctx.organization_id)
    items = await repo.list_for_task(task_id)
    return {
        "items": [
            {
                "id": e.id,
                "attempt": e.attempt,
                "status": e.status,
                "agent_id": e.agent_id,
                "model_used": e.model_used,
                "cost_usd": float(e.cost_usd),
                "duration_ms": e.duration_ms,
                "error_category": e.error_category,
                "started_at": e.started_at.isoformat(),
            }
            for e in items
        ],
        "count": len(items),
    }


@router.post("/tasks/{task_id}/run")
async def run_task_now(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Run this task **here**, now, and return at once.

    The workflow engine is the right path and it is not running, so `POST /tasks` commits
    the task and `_start_workflow_for` reports `started: false` with the reason. That is
    honest and it is also a dead end when nothing else will pick the row up -- which is
    what a page with a Run button and no worker looks like.

    So this drives `TaskExecutionService.execute_task`, the same call the Temporal activity
    makes, on a background task. The request returns immediately with a handle.

    **The run is long.** Measured on the free model for a three-way coordination
    delegation: **644 seconds and 67,136 tokens**. Both numbers are in the response, and
    the page states them on the button -- a person who presses Run deserves to know the
    cost before, not after. A synchronous run would time out in every reverse proxy worth
    the name.

    Refuses a second run of a task already in flight. That refusal is the cheapest thing
    here: 67,000 tokens is a real bill and a duplicate is a real one.
    """
    from ai_orchestrator.application.local_runner import start

    return (
        await start(
            ctx.session,
            organization_id=ctx.organization_id,
            task_id=task_id,
        )
    ).as_dict()


@router.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """Run this work again, as a **new** task that remembers the first attempt.

    ## Why this is not a reset

    A `failed` task is terminal and the state machine says so on purpose: nothing leaves it,
    because reusing the row would make the audit trail lie about what was attempted. That
    reasoning is right and it is also where the platform stopped: the design said a retry is
    a *new* task linked by `task_dependencies`, and **nobody built it**. Measured: 58 failed
    tasks in the development tenant, every one of which the platform could explain and none
    of which it would let anybody do anything about. A work queue you can only read is not a
    work queue.

    So this creates the new task rather than reopening the old one, and the failed row keeps
    its failure and its `last_error` — those are facts about an attempt that happened.

    ## What it refuses, and why that is not pedantry

    * A task that has **not** failed gets a 409. A `created` task needs running, a
      `completed` one needs nothing, and a `running` one is already working. Copying any of
      them would manufacture work.
    * A retry whose work is **already active** is refused by the same duplicate check every
      other task insert goes through, so pressing this twice cannot buy two runs. The refusal
      names the task that is already on it.

    Returns the new task. The caller starts it -- retrying and running are separate decisions,
    and a person who creates a retry by accident should not immediately spend 67,000 tokens
    on it.
    """
    import datetime as dt

    from ai_orchestrator.application.task_retry import retry_failed

    return await retry_failed(
        ctx.session,
        organization_id=ctx.organization_id,
        task_id=task_id,
        now=dt.datetime.now(tz=dt.UTC),
    )


@router.get("/tasks/{task_id}/report")
async def get_task_report(
    task_id: str,
    event_limit: int = Query(200, ge=1, le=1000),
    event_offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The account of a task: what was asked, and everything that happened to it.

    Separate from `GET /tasks/{task_id}` on purpose. That one returns the row; this
    returns the **account** -- the delegation tree, every execution, every approval and its
    decision, and the ordered record. An audit asks those together, and assembling them
    from five calls is how an auditor ends up describing a system that has already moved on.

    It is a read. Nothing here decides, and it does not say the work was good: quality is
    the approval's subject, and a report that implied a verdict would be doing an
    approval's job with a `SELECT`.
    """
    from ai_orchestrator.application.coordination import task_report

    return await task_report(
        ctx.session,
        organization_id=ctx.organization_id,
        task_id=task_id,
        event_limit=event_limit,
        event_offset=event_offset,
    )


@router.get("/tasks/{task_id}/timeline")
async def get_task_timeline(task_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The task's story, from the audit log.

    Ordered by a monotonic sequence rather than by wall clock, and structured
    rather than a chat transcript. This is what an operator reads to understand a
    task; a conversation would be a lossy, unsortable rendering of the same facts.
    """
    from ai_orchestrator.audit import AuditService

    audit = AuditService(ctx.session, ctx.organization_id)
    events = [
        {
            "sequence": r.sequence,
            "at": r.created_at.isoformat(),
            "actor_type": r.actor_type,
            "actor_id": r.actor_id,
            "action": r.action,
            "resource": f"{r.resource_type}:{r.resource_id or '-'}",
            "outcome": r.outcome,
            "policy_rule_id": r.policy_rule_id,
            "approval_id": r.approval_id,
        }
        for r in sorted(await audit.query(task_id=task_id, limit=1000), key=lambda r: r.sequence)
    ]
    return {"task_id": task_id, "timeline": events, "count": len(events)}


__all__ = ["router"]
