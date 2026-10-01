"""Task execution: the application service that actually runs a task.

This is where the pieces meet. It receives a task id and drives it through the
lifecycle, and it is deliberately the only place that knows the *order* of
operations. Everything it calls is independently testable; the ordering is what
lives here and nowhere else.

The order is the design:

    load task -> check dependencies -> resolve the owner agent
      -> assemble the authorised context -> run the runtime
      -> persist the decision record -> advance the state machine

Each step is allowed to stop the task, and each way of stopping is different:
a missing dependency blocks it, a policy denial fails it with a category, a
needs-approval pauses it. Conflating those three is what produces a platform
where "blocked" and "denied" are the same state and operators cannot tell a
waiting task from a refused one.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_orchestrator.agent_runtime.context import (
    ContextBuilder,
    ContextBuilderInput,
    actor_for_agent,
)
from ai_orchestrator.application.delegation_executor import (
    DelegationExecutor,
    DelegationOutcome,
)
from ai_orchestrator.application.peer_consultation import ConsultationLedger
from ai_orchestrator.audit import AuditService
from ai_orchestrator.domain.authority import (
    AuthorityAction,
    AuthorityGrant,
    AuthorityProfile,
)
from ai_orchestrator.domain.budget import BudgetState, Money
from ai_orchestrator.domain.contracts import (
    ActionProposal,
    AgentContext,
    AgentResult,
    AgentResultStatus,
    AgentRuntime,
    DelegateOption,
    MemoryScope,
    SkillContract,
    ToolContract,
)
from ai_orchestrator.domain.enums import (
    AgentLifecycleStatus,
    AutonomyLevel,
    DataClassification,
    EffectClass,
    RiskLevel,
    RunMode,
    TaskStatus,
    TaskType,
    ToolRisk,
)
from ai_orchestrator.domain.errors import (
    AgentUnavailable,
    PlatformError,
    PreconditionError,
)
from ai_orchestrator.domain.ids import (
    ExecutionId,
    ModelUsageId,
    OrganizationId,
    SkillId,
    TaskId,
    ToolId,
)
from ai_orchestrator.domain.output_contract import describe_mismatch, missing_keys
from ai_orchestrator.domain.procedure import describe
from ai_orchestrator.domain.state_machines import Transition
from ai_orchestrator.persistence.models import Agent, AgentDefinition, Role, Task
from ai_orchestrator.persistence.repositories.organization import (
    AgentCapabilityRepository,
    AgentDefinitionRepository,
    AgentRepository,
    OrgUnitRepository,
    RoleRepository,
)
from ai_orchestrator.persistence.repositories.task import (
    DelegationRepository,
    ExecutionRepository,
    TaskRepository,
)
from ai_orchestrator.telemetry.logging import get_logger
from ai_orchestrator.tools.registry import ToolResult

logger = get_logger(__name__)

#: Fallback approver id, used only when the organisation has no user at all --
#: a tenant created seconds ago that nobody has been added to.
#:
#: It is *not* the general answer, and treating it as one is a mistake this
#: repository has now paid for twice. `approvals.decided_by` is a **composite**
#: foreign key -- `(organization_id, decided_by) REFERENCES
#: users(organization_id, id)` -- while `users` is keyed on `id` alone. One id
#: therefore belongs to exactly one tenant: the seeded `dev:no-auth` row lives
#: in whichever tenant was created first, and a decision recorded in any other
#: tenant fails the foreign key. The write aborts the transaction, the decision
#: is lost, and the approval stays pending while its own note claims it was
#: answered.
#:
#: So the approver is resolved per organisation at call time. An id from another
#: tenant is the failure this avoids: it looks correct in the log and fails the
#: write.
AUTO_APPROVER_ID = "dev:no-auth"

#: Provisions a user for a tenant that has none. `password_hash` is a fixed
#: string that is not a hash of anything: the only path to this row is
#: `api_auth_disabled`, which cannot start in production or in tests (F182).
_ENSURE_APPROVER = """
INSERT INTO users (
    id, organization_id, email, display_name, password_hash, role,
    is_org_admin, is_privileged, mfa_enabled, token_version, is_active)
VALUES (
    CAST(:approver AS varchar(40)), CAST(:org AS varchar(40)), :email,
    'Local operator', 'authentication-is-off-there-is-no-password', 'org_admin',
    true, true, false, 1, true)
ON CONFLICT (id, organization_id) DO NOTHING
"""


async def _resolve_auto_approver(session: Any, organization_id: str) -> str:
    """A `users` id in *this* organisation that is allowed to record a decision.

    Prefers an existing privileged human, because that is who a real decision
    would be recorded against, and provisions one only when the organisation has
    nobody. Both paths end with an id the composite foreign key accepts, which
    is the only property that matters here.

    A failure to provision is logged rather than raised: the caller is mid-run
    and the decision itself will fail loudly on its own, which is the honest
    outcome. Swallowing it silently would leave an approval pending with a note
    claiming it was answered.
    """
    from sqlalchemy import text

    found = (
        (
            await session.execute(
                text(
                    "SELECT id FROM users WHERE organization_id = CAST(:org AS varchar(40)) "
                    "AND is_privileged ORDER BY created_at LIMIT 1"
                ),
                {"org": organization_id},
            )
        )
        .scalars()
        .first()
    )
    if found:
        return str(found)

    # Derived from the organisation, so it is unique across tenants -- the same
    # literal id in two tenants is precisely the failure being avoided.
    approver = f"auto:{organization_id[-12:]}"
    try:
        await session.execute(
            text(_ENSURE_APPROVER),
            {
                "approver": approver,
                "org": organization_id,
                "email": f"auto-approver@{organization_id[-12:]}.example.invalid",
            },
        )
    except Exception as exc:
        logger.info("approval.approver_row_unavailable", error=str(exc))
    return approver


@dataclass(slots=True)
class ExecutionOutcome:
    """What happened, in a form a workflow can branch on."""

    task_id: str
    status: TaskStatus
    summary: str = ""
    execution_id: str | None = None
    agent_id: str | None = None
    needs_approval: bool = False
    blocked_reason: str | None = None
    failure_category: str | None = None
    result: AgentResult | None = None
    proposed_actions: tuple[ActionProposal, ...] = ()
    cost_usd: Money = field(default_factory=lambda: Money("0"))
    tokens: int = 0
    duration_ms: int = 0
    retryable: bool = False
    #: The shape of the work this run performed, and how many times the agent had
    #: done it before. `None` when the run left no trace.
    #:
    #: Carried on the outcome rather than only logged, because the count is the
    #: input to the repetition gate and a number only two log files away is not
    #: something anything can act on. `prior_repetitions == 0` is the ordinary case
    #: and means nothing has repeated *yet*; it is not evidence that nothing ever
    #: will.
    procedure_fingerprint: str | None = None
    prior_repetitions: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status is TaskStatus.COMPLETED


@dataclass(slots=True)
class ResolvedAgent:
    """Everything about the agent that will run, resolved once."""

    agent: Agent
    definition: AgentDefinition
    role: Role
    profile: AuthorityProfile
    org_unit_id: str | None


#: Statement keywords that must never appear in a statement the tenant reader will
#: run. Only consulted for a `WITH`, because a plain `SELECT` is allowed on its
#: leading keyword alone — scanning a `SELECT` body for these words would refuse
#: legitimate reads whose column names or string literals happen to contain them,
#: and a read tool that refuses valid queries gets worked around.
_WRITE_KEYWORDS = frozenset(
    {
        "alter",
        "call",
        "cluster",
        "comment",
        "copy",
        "create",
        "delete",
        "do",
        "drop",
        "execute",
        "grant",
        "insert",
        "lock",
        "merge",
        "prepare",
        "reindex",
        "refresh",
        "revoke",
        "rollback",
        "savepoint",
        "set",
        "truncate",
        "update",
        "vacuum",
    }
)

#: Leading keywords that can produce rows without writing.
_READ_LEADERS = frozenset({"select", "table", "values", "with"})


def _refuse_if_not_a_read(sql: str) -> None:
    """Raise unless `sql` is a single statement that can only read.

    The rule is deliberately shaped around false *positives* rather than false
    negatives. A leading `SELECT` is accepted without looking at the rest of the
    statement, because that is what almost every legitimate query looks like and a
    read tool that refuses valid SQL is a tool whose refusals get routed around. A
    leading `WITH` is the one interesting case: a CTE can end in a write
    (`WITH gone AS (SELECT ...) DELETE FROM t USING gone`), so the body is scanned.

    This is inspection and not enforcement — a string check can be fooled. It is
    the right layer anyway, because the only real alternative is
    `SET TRANSACTION READ ONLY`, which is transaction-wide and therefore unusable
    here: the reader runs inside the run's long-lived transaction, which
    legitimately writes audit rows. See `_make_tenant_reader`.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise ValueError("empty statement")
    if ";" in statement:
        # A second statement is either a write or a stacked read, and a savepoint
        # around two statements recovers neither cleanly.
        raise ValueError("more than one statement")
    lowered = statement.lower()
    leader = ""
    for token in lowered.replace("(", " ").replace("\n", " ").split():
        if token.strip():
            leader = token.strip(",;")
            break
    if leader not in _READ_LEADERS:
        raise ValueError(f"{leader or 'statement'} is not a read")
    if leader == "with":
        for word in lowered.replace("(", " ").replace(")", " ").replace(",", " ").split():
            if word in _WRITE_KEYWORDS:
                raise ValueError(f"a WITH may not contain {word.upper()}")


def _category_for(error_code: str | None) -> str:
    """Map a runtime `error_code` onto the platform's `ErrorCategory`.

    The runtime already distinguishes its failures -- `budget_exhausted`, `model_error`,
    `tool_failed` and so on, and it writes the right one into `error_code`. The executor
    threw all of them away and recorded `internal_error` for every one.

    That is not a cosmetic label. The category is what the reaper and the retry policy key
    on, so a run that stopped because it **ran out of turn budget** was filed as an
    internal fault, which is both wrong ("the platform is broken") and the one that invites
    a retry. `ErrorCategory.BUDGET` has existed the whole time with nothing pointing at it.

    Found by `scripts/run_fleet.py`, on its first real run:

        Finance Agent  failed  category=internal_error
        reason="the agent exceeded its turn budget and was stopped: The next request
                would exceed the request_limit of 24"

    **Unknown codes still become `internal_error`**, which is the correct default: an
    unrecognised failure is a failure nobody classified, and saying so is better than
    guessing.

    The `24` in the message above is **what it was at the time**, quoted verbatim because a
    record edited to match the present is not a record. The ceiling has since been measured
    and raised to 48; see `BudgetState.max_requests`.
    """
    if not error_code:
        return "internal_error"
    table = {
        "budget_exhausted": "budget_error",
        "budget": "budget_error",
        "max_tokens": "budget_error",
        "model_error": "model_error",
        "model_unavailable": "model_error",
        "no_provider": "model_error",
        "tool_failed": "tool_execution_error",
        "tool_error": "tool_execution_error",
        "invalid_tool_arguments": "tool_selection_error",
        "unknown_tool": "tool_selection_error",
        "context_too_large": "context_error",
        "no_output": "context_error",
        "malformed_output": "context_error",
    }
    return table.get(str(error_code), "internal_error")


class TaskExecutionService:
    _LEASE_SECONDS = 1_800

    """Drives one task through its lifecycle.

    Not a workflow and not an activity. It is the business logic, callable from a
    Temporal activity, from a synchronous API request, or from a test, which is
    what keeps the durable orchestration separate from the domain logic.
    """

    def __init__(
        self,
        session: AsyncSession,
        organization_id: str,
        *,
        runtime: AgentRuntime,
        run_mode: RunMode = RunMode.LIVE,
        default_limits: Any = None,
        auto_approve: bool | None = None,
    ) -> None:
        self._session = session
        self._org = organization_id
        self._runtime = runtime
        self._run_mode = run_mode
        self._default_limits = default_limits
        self._tasks = TaskRepository(session, organization_id)
        self._executions = ExecutionRepository(session, organization_id)
        # The delegation repository is held here so the executor can record a
        # hop without the caller assembling a second repository over the same
        # session — two repositories over one session is two views of one
        # transaction, which is fine, and a reason to be surprised when it is not.
        self._delegations = DelegationRepository(session, organization_id)
        self._agents = AgentRepository(session, organization_id)
        self._definitions = AgentDefinitionRepository(session, organization_id)
        self._roles = RoleRepository(session, organization_id)
        self._units = OrgUnitRepository(session, organization_id)
        self._capabilities = AgentCapabilityRepository(session, organization_id)
        self._audit = AuditService(session, organization_id)
        # The consultation counter for the current run. It lives on the service
        # rather than in the context because the limit is a property of the run,
        # and a run may build several contexts. It is reset per task, not per
        # service, so one long-lived worker cannot accumulate a budget.
        self._consultations: ConsultationLedger | None = None
        # Call counters, reset per run in `execute_task` and declared here so
        # that a path which records a model call without starting a run -- the
        # ledger tests, and anything that records before `execute_task` -- finds
        # a number rather than an AttributeError. An attribute that only exists
        # on one code path is an attribute that will be missing on another.
        self._tool_calls = 0
        self._model_calls = 0
        # Whether an approval request is answered the moment it is raised. Read
        # once here so a run cannot change the rule halfway through itself, and
        # overridable so a test can hold both positions without touching the
        # environment -- a test that mutates global settings is a test that
        # cannot run beside another one doing the same.
        from ai_orchestrator.config.settings import get_settings

        self._auto_approve: bool = (
            get_settings().approval_auto_approve if auto_approve is None else auto_approve
        )

    async def execute_task(
        self,
        task_id: str,
        *,
        agent_id: str | None = None,
        attempt: int = 1,
        input_override: dict[str, Any] | None = None,
        retrieved_context: Sequence[str] = (),
    ) -> ExecutionOutcome:
        """Run one task to a terminal or paused state.

        Never raises for an expected failure. Every refusal, block and error
        becomes a state on the task, because a task stuck in `running` with a
        stack trace in a log is the worst outcome available: the system cannot
        tell anyone what happened and the task cannot be retried.
        """
        started = time.monotonic()
        task = await self._tasks.get(task_id)

        # --- dependencies -------------------------------------------------
        blocking = await self._tasks.unsatisfied_dependencies(task_id)
        if blocking:
            return await self._block(
                task, f"waiting for {len(blocking)} unmet dependency/dependencies"
            )

        # --- resolve the owner --------------------------------------------
        target_agent_id = agent_id or task.owner_agent_id
        if target_agent_id is None:
            return await self._fail(
                task, "no agent is assigned and the task declares no owner", "planning_error"
            )
        try:
            resolved = await self._resolve_agent(target_agent_id)
        except AgentUnavailable as exc:
            return await self._fail(task, exc.message, "planning_error")

        # --- state ---------------------------------------------------------
        if task.status == TaskStatus.CREATED.value:
            task = await self._tasks.transition(task_id, Transition.ASSIGN)
        if task.status == TaskStatus.ASSIGNED.value:
            task = await self._tasks.transition(task_id, Transition.BEGIN_WORK)

        # --- lease ---------------------------------------------------------
        # **Taken here, and this is the only place it is taken.**
        #
        # The reaper reclaims a task with
        # `status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now`.
        # Measured before this line existed: **0 of 129 tasks held a lease.** So the reaper
        # could never reclaim anything, ever -- its condition was unsatisfiable, and it was
        # dead code that looked alive because it ran and reported "nothing found".
        #
        # **What the lease does NOT do, measured rather than argued.** Both callers wrap the
        # run in `Database.tenant_session`, which is `session.begin()` -- one transaction for
        # the whole run -- and there is **no `commit()` anywhere** in the run path: not in
        # `task_execution`, not in the repositories, not in the audit service. Verified by
        # count, not by reading: 0 occurrences across those three packages.
        #
        # So this write lands in an open transaction. No other connection can see it until the
        # run commits, which is the moment a lease stops mattering. The reaper reads through
        # its own connection, so **it cannot observe a run in flight** -- not its status, not
        # its lease. Two consequences, and both are real:
        #
        # * the lease does not yet do the one thing its docstring claims, which is telling
        #   "slow" from "dead";
        # * a task is therefore never reaped *while working* (accidentally safe) and a task
        #   whose worker was killed is also never reaped, because the rollback that removes
        #   the lease removes the `running` status with it.
        #
        # A previous version of this comment claimed 30 minutes was "comfortable" for a run
        # measured at 644 seconds. That was reasoning about a number while ignoring where the
        # number is written. Fixing it properly means the lease has to be taken **and renewed
        # on a connection of its own**, outside the run's transaction -- which is a change to
        # the three call sites (`local_runner`, `worker_runtime`, `task_workflow`), not to
        # this method, because committing here would end the caller's transaction. Recorded as
        # F190 rather than half-done here: a lease that is written where nobody can read it is
        # worse than one that is obviously absent, because it looks like the problem is solved.
        await self._tasks.renew_lease(task_id, self._LEASE_SECONDS)

        execution = await self._executions.start(
            task_id=task_id,
            agent_id=resolved.agent.id,
            runtime_adapter=self._runtime.name,
            model_profile=resolved.agent.model_profile,
            attempt=attempt,
            input_hash=task.fingerprint,
        )

        # --- budget --------------------------------------------------------
        budget = BudgetState(
            max_tokens=task.budget_limit_tokens or self._token_limit(),
            max_cost_usd=Money(str(task.budget_limit_usd or self._cost_limit())),
        )

        # --- context -------------------------------------------------------
        try:
            # Reports from below are added to what the caller retrieved, and
            # never replace it: a caller's own context is something it asked for,
            # and a child's report is something it did not.
            child_reports = await self._reports_from_children(task)
            context = await self._build_context(
                task,
                resolved,
                retrieved_context=(*retrieved_context, *child_reports),
            )
        except PlatformError as exc:
            return await self._fail(
                task, exc.message, exc.category.value, execution_id=execution.id
            )

        await self._audit.record(
            actor=context.actor,
            action="task.execute",
            resource_type="task",
            resource_id=task_id,
            task_id=task_id,
            execution_id=execution.id,
            context={
                "agent_id": resolved.agent.id,
                "runtime": self._runtime.name,
                "model_profile": resolved.agent.model_profile,
                "agent_definition_version": resolved.definition.version,
            },
        )

        # --- run -----------------------------------------------------------
        # The context is held for the duration of the run so the tool executor
        # can see what the agent is authorised to do. A closure has nowhere else
        # to read it from, and re-building the context per tool call would mean
        # re-deciding the answer to "what may this agent do" several times.
        self._active_context = context
        # A new task is a new run: the consultation ceiling is per task, and
        # carrying it across tasks would make the second task unable to ask at
        # all because of work the first one did.
        self._consultations = ConsultationLedger()
        # The real execution row's id, held for the same reason. The id on
        # `context.task.execution_id` is minted per call and has no row behind it,
        # so writing it into an audited record violates the executions foreign key
        # — which is exactly what happened the first time a tool call was recorded.
        self._active_execution_id = execution.id
        # Call counters for this execution, kept in memory and written once when
        # the run finishes (see migration 0029). Counting in memory rather than
        # incrementing a column per call keeps a write off the hot path: the
        # number cannot mean anything different until the run is over, so paying
        # for a write per tool call buys nothing but contention.
        self._tool_calls = 0
        self._model_calls = 0
        # The delegating agent's id, for the same reason: the coordination rule
        # asks "did *this* agent delegate this task?" and only the database can
        # answer it for the tool path.
        self._active_source_agent_id = str(resolved.agent.id)
        agent_task = self._to_agent_task(task, input_override)
        try:
            result = await self._runtime.execute(
                agent_task,
                context,
                execute_tool=self._execute_tool_call,
                record_usage=lambda response: self._record_model_call(
                    response,
                    task_id=task.id,
                    execution_id=execution.id,
                    agent_id=resolved.agent.id,
                ),
            )
        except PlatformError as exc:
            return await self._fail(
                task, exc.message, exc.category.value, execution_id=execution.id
            )
        except Exception as exc:
            # A session that is already poisoned cannot record its own failure.
            # `_fail` writes through the same session, so if the transaction has
            # been rolled back, the attempt to *report* the error raises a second,
            # unrelated one — "Can't operate on closed transaction" — and that is
            # the traceback the operator sees. The real cause is the exception in
            # hand, so it goes in the log and the outcome, and the write is
            # attempted only if the session can still take it.
            return await self._fail_without_a_usable_session(
                task=task,
                message=f"{type(exc).__name__}: {exc}",
                execution_id=execution.id,
                cause=exc,
            )

        return await self._finish(task, resolved, context, result, execution.id, budget, started)

    async def _apply_delegations(
        self,
        *,
        task: Task,
        source_agent_id: str,
        result: Any,
        context: Any,
    ) -> DelegationOutcome:
        """Turn the run's `delegate` proposals into real delegations.

        Refusals never raise: a cycle or a depth breach is a decision, and the
        caller needs to report it rather than unwind. The audit row written by the
        executor is what makes the refusal visible to an operator later.
        """
        executor = DelegationExecutor(
            tasks=self._tasks,
            delegations=self._delegations,
            agents=self._agents,
            audit=self._audit,
            organization_id=self._org,
            platform_limits=context.delegation_limits,
        )
        from ai_orchestrator.domain.delegation import DelegationPath
        from ai_orchestrator.domain.ids import AgentId, TaskId

        path = DelegationPath.root(AgentId(source_agent_id), TaskId(task.id))
        # Replay the ancestors from the stored path so the cycle check can see
        # above this task rather than only this hop.
        if context.delegation_path and context.delegation_path.depth:
            path = context.delegation_path

        outcome = await executor.apply(
            parent=task,
            source_agent_id=source_agent_id,
            proposals=result.follow_up_actions,
            path=path,
        )
        if outcome.summary:
            logger.info("delegation.summary", task_id=task.id, summary=outcome.summary)
        return outcome

    async def _execute_tool_call(
        self, *, tool_name: str, arguments: dict[str, Any], **_: Any
    ) -> Any:
        """Run one authorised tool on the runtime's behalf, through the gateway.

        Every gate applies, because this is the same `ToolGateway.invoke` the
        acceptance tests drive directly — the model gains a way to ask, not a way
        around the checks. A refusal comes back as a `ToolResult` the model can
        read and reason about, not as an exception, because an exception here
        would end the run rather than let the agent try something else.
        """
        from ai_orchestrator.domain.policy import (
            RuleBasedPolicyEngine,
            default_policy_set,
        )
        from ai_orchestrator.tools.builtin import build_default_tools
        from ai_orchestrator.tools.gateway import ToolGateway
        from ai_orchestrator.tools.registry import ToolResult

        gateway = getattr(self, "_tool_gateway", None)
        if gateway is None:
            gateway = ToolGateway(
                build_default_tools(),
                # The platform's own default policy set. A tool call made by the
                # runtime goes through the same engine as one made by an
                # application service, so there is no ungated path by virtue of
                # being called from inside the agent loop.
                policy_engine=RuleBasedPolicyEngine(default_policy_set().rules),
            )
            self._tool_gateway = gateway

        context = getattr(self, "_active_context", None)
        if context is None:
            return ToolResult.failure("NO_CONTEXT", "no authorised context is active for this call")

        self._tool_calls += 1
        invocation = await gateway.invoke(
            actor=context.actor,
            tool_name=tool_name,
            arguments=arguments,
            organization_id=str(context.organization_id),
            task_id=str(context.task.task_id),
            execution_id=str(context.task.execution_id),
            autonomy_level=context.autonomy_level,
            run_mode=context.run_mode,
            context_attributes={
                "delegate": self._make_delegate(context),
                "ask_peer": self._make_ask(context),
                "read_tenant_sql": self._make_tenant_reader(context),
                "a2a_call": self._make_a2a_caller(),
            },
        )
        await self._record_tool_call(
            context=context,
            tool_name=tool_name,
            arguments=arguments,
            invocation=invocation,
        )
        # The caller — and the model — gets the `ToolResult`, not the wrapper. The
        # wrapper is the gateway's record; the result is the answer.
        return (
            invocation.result
            if invocation.result is not None
            else ToolResult.failure("NO_RESULT", "the gateway returned no result for this call")
        )

    async def _record_tool_call(
        self,
        *,
        context: AgentContext,
        tool_name: str,
        arguments: dict[str, Any],
        invocation: Any,
    ) -> None:
        """Write one durable row per tool call.

        Without this the platform cannot answer the only question that matters
        about an agent's behaviour: *what did it actually do?* A run that spun
        through 24 turns left behind a `task.execute` row, a cost ledger entry per
        model call, and nothing whatsoever about the 30-odd tool calls in between.
        The tools offered are logged; the tools used were not recorded anywhere.

        The arguments are redacted before they are stored, because a tool argument
        is the single most likely place for a secret or a customer's data to enter
        a log, and this table is tenant-scoped and queryable by anyone holding the
        tenant.

        Ordering is left to the audit sequence rather than a timestamp column, and
        the arguments are stored whole rather than summarised: a procedure is a
        *sequence* of steps, and the sequence is the thing worth keeping.

        **No flush here.** `AuditService.record` flushes, and this is called from
        inside a tool handler, which is itself running inside a flush of the parent
        task's `create`. SQLAlchemy refuses a nested flush with
        `InvalidRequestError: Session is already flushing`, which killed the run
        and then poisoned every later statement in the session with
        "Can't operate on closed transaction" — one nested flush, two unrelated
        errors, and the second one is what the traceback ends on.

        The row is still written; it joins the transaction that is already open,
        which is the correct lifetime for it anyway. What is wrong is asking for it
        to be durable at a moment when the surrounding transaction is not.
        """
        from ai_orchestrator.telemetry.logging import redact

        # `ToolGateway.invoke` returns a `ToolInvocation`, which *wraps* the
        # `ToolResult` as `.result`. Reaching for `.ok` on the invocation and
        # defaulting to `True` reported 21 successful delegations for a run in
        # which every one of them failed — the trace was confidently wrong, and
        # cost an hour of chasing a delegation that had never happened.
        #
        # Both attribute reads are now explicit and fail loudly rather than
        # optimistically. A `getattr` default on a value whose shape you have not
        # checked is a guess that reads as a fact.
        result = getattr(invocation, "result", None)
        ok = result is not None and bool(result.ok)
        error_kind = result.error_kind if result is not None else "NO_RESULT"
        error_message = result.error_message if result is not None else None
        execution_id = getattr(self, "_active_execution_id", None) or context.task.execution_id
        # `add`, not `record`: no `await` because `add` does not flush, and the
        # absence of `await` next to every other call in this file is the visible
        # consequence of the reason spelled out above.
        self._audit.add(
            actor=context.actor,
            action="tool.invoke",
            resource_type="tool",
            resource_id=tool_name,
            task_id=str(context.task.task_id),
            execution_id=str(execution_id),
            outcome="success" if ok else "failure",
            policy_reason=error_message,
            context=redact(
                {
                    "arguments": arguments,
                    "output": getattr(result, "output", None),
                    "error_kind": error_kind,
                }
            ),
        )

    def _make_tenant_reader(self, context: Any) -> Any:
        """A read-only SQL callable bound to this run's transaction.

        The tool layer is handed a callable rather than a session, so it cannot
        open a connection, cannot commit, and cannot outlive the tenant binding.
        The `organization_id` predicate is also *forced into the text* — RLS would
        catch its absence anyway, but a query that names no tenant returns zero
        rows and reads to a model as "the data is empty", which is a different and
        wrong conclusion from "your query was malformed".
        """

        async def _read(sql: str) -> list[dict[str, Any]]:
            from sqlalchemy import text as _text

            # Read-only is enforced here rather than only in the tool that calls
            # this. `_internal_database_query` does refuse writes by inspecting the
            # SQL for `delete ` and friends, and that is the policy — but this
            # callable is what actually holds the session, and the one guarantee
            # it made in its own docstring ("a read-only SQL callable") was not
            # true. A test that passed it a `DELETE` deleted the row; the only
            # reason the test failed rather than passing quietly is that a write
            # returns no rows and `.mappings()` then raises.
            #
            # This is inspection, not enforcement, and the distinction is worth
            # stating: a string check can be fooled by a comment or an unusual
            # spelling. It is the right layer for it because the alternative —
            # `SET TRANSACTION READ ONLY` — is transaction-wide, and this reader
            # runs inside the run's long-lived transaction, which legitimately
            # writes audit rows. The real boundary is the tool's policy; this is
            # the floor under it, so that a second caller cannot skip the check by
            # reaching for the session instead of the tool.
            _refuse_if_not_a_read(sql)

            # `no_autoflush`, and this is not a micro-optimisation. A plain
            # `session.execute` flushes every pending object first, so a read tool
            # called from inside a tool handler would *begin* a flush — and the
            # audit row the surrounding flush was already writing then hit
            # `Session is already flushing`, rolled the transaction back, and took
            # the run with it. A read must not write, and that includes not
            # triggering someone else's write.
            #
            # `no_autoflush` rather than `execution_options={"autoflush": False}`:
            # the session's autoflush flag is what governs this, and the
            # execution-option form silently did nothing. Which is its own lesson
            # — a read that looked bounded and was not.
            #
            # The savepoint is the other important part. In Postgres a statement
            # that errors **aborts the whole transaction**: every later command on
            # that session fails with `current transaction is aborted` until
            # something issues a `ROLLBACK`. A malformed query is an *expected*
            # outcome here — the model is being allowed to try things and learn from
            # the refusal — so without a savepoint the first bad query killed the
            # run's transaction permanently. It was found by a demo run in which
            # the model wrote `WHERE id = '08017d96' OR run_id = '08017d96'` against
            # `tasks`, a column that does not exist. The tool caught the error and
            # returned `QUERY_FAILED` correctly; the transaction was still dead, the
            # connection sat in `idle in transaction (aborted)` for six minutes, and
            # the process never exited. The tool's error handling was correct and
            # the layer underneath it was not.
            #
            # `ROLLBACK TO SAVEPOINT` rather than a full rollback, because the point
            # is to keep the run's transaction — and everything already written in
            # it — alive.
            #
            # The whole block, savepoint included, is inside `no_autoflush`. The
            # first version issued `SAVEPOINT` outside it, and
            # `test_the_tenant_reader_does_not_autoflush` failed immediately:
            # `session.execute` flushes pending objects, so the savepoint statement
            # was itself the write the test exists to forbid. Two lines of guard
            # around the *query* are not a guard around the *statement that opens
            # the guard*.
            with self._session.no_autoflush:
                await self._session.execute(_text("SAVEPOINT tenant_sql_read"))
                try:
                    result = await self._session.execute(
                        _text(sql), {"__tenant__": str(context.organization_id)}
                    )
                    rows = [dict(row) for row in result.mappings().all()]
                except Exception:
                    # Undo just this statement. If the rollback itself fails the
                    # transaction is unrecoverable whatever we do, so the original
                    # error is the one worth reporting to the model.
                    await self._session.execute(_text("ROLLBACK TO SAVEPOINT tenant_sql_read"))
                    raise
                await self._session.execute(_text("RELEASE SAVEPOINT tenant_sql_read"))
            return rows

        return _read

    async def _open_approval(
        self,
        *,
        task: Task,
        resolved: ResolvedAgent,
        result: Any,
        execution_id: str | None,
    ) -> str | None:
        """Put a real request in front of a real person, or say why not.

        The row is always written, even when the decision is automatic. That is
        deliberate: the audit of a self-approved action is more valuable than the
        convenience, because a demo that silently approves leaves no evidence
        that anyone was ever asked. A record of "this was approved automatically
        because auto-approval is on" is the difference between a demo and a
        claim.

        Auto-approval is a setting, not a mode. It answers a request that was
        genuinely made, and it writes down that it did so, which means turning it
        off changes the behaviour and not the shape of the system.
        """
        from ai_orchestrator.approvals.service import ApprovalRequest, ApprovalService
        from ai_orchestrator.domain.contracts import Actor
        from ai_orchestrator.domain.enums import ActorType, RiskLevel

        what = result.summary or "the agent asked for approval to proceed"
        request = ApprovalRequest(
            organization_id=self._org,
            action_type="task.continue",
            action_payload={
                "task_id": str(task.id),
                "summary": what,
                "proposed_actions": list(result.follow_up_actions or []),
            },
            # The agent that asked, not the person who will answer. An approval
            # whose requester is the platform is an approval nobody could audit.
            requested_by=str(resolved.agent.id),
            requested_by_type=ActorType.AGENT,
            risk_level=RiskLevel.MEDIUM,
            reason=what,
            task_id=str(task.id),
            execution_id=execution_id,
        )
        approvals = ApprovalService(self._session, self._org)
        try:
            approval = await approvals.create(request)
        except PlatformError as exc:
            # A failed request must not lose the reason the task paused. Logged
            # and returned; the task stays waiting, which is honest.
            logger.info("approval.open_failed", task_id=str(task.id), error=str(exc))
            return None

        if not self._auto_approve:
            return str(approval.id)

        # The approver has to be a real `users` row before a decision can be
        # recorded against it. Provisioning it here rather than relying on
        # whatever started the process is the difference between a switch that
        # works and a switch that works in the one entry point that remembered
        # to run a setup script: `decided_by` is a foreign key, so without the
        # row the write fails, the exception is swallowed as a refusal to log,
        # and the approval sits pending with a note claiming it was answered.

        # Resolved here, for this organisation, rather than held as a constant:
        # see `AUTO_APPROVER_ID` for why one fixed id cannot be the answer.
        approver_id = await _resolve_auto_approver(self._session, self._org)

        # The demo switch, and it is narrower than it looks. `decide` refuses a
        # requester approving their own request, so the auto-approver is a
        # distinct principal rather than the agent that asked. Recording the
        # decision through the same service a person uses keeps one code path:
        # switching this off changes when a decision happens, not how it is
        # stored, and the row still shows who answered and why.
        try:
            await approvals.decide(
                approval_id=str(approval.id),
                # `dev:no-auth`, not an invented principal. `approvals.decided_by`
                # is a foreign key to `users`, so any other id would fail the
                # write and the approval would sit pending forever with the note
                # claiming it was answered -- the switch would work in the
                # interface and be a no-op in the database. This is the same
                # principal a person operates as, which is honest: the decision
                # really was made by whoever is driving, without them clicking.
                approver=Actor(
                    id=approver_id,
                    kind=ActorType.HUMAN,
                    display_name="Automatic approver",
                    organization_id=OrganizationId(self._org),
                    is_privileged_human=True,
                ),
                approve=True,
                note="approved automatically: auto-approval is on for this run",
            )
        except PlatformError as exc:
            logger.info("approval.auto_decide_failed", approval_id=str(approval.id), error=str(exc))
        return str(approval.id)

    async def _learn_if_approved(self, task: Task) -> None:
        """A person accepted this work; write down what it teaches.

        Reads the run out of the log and proposes one skill version. The proposal
        is **not published** — publishing is what applies it, and that stays a
        person's decision.

        The agent is resolved from the task rather than taken from the approval,
        because the approval records who *asked* and the lesson belongs to who
        *did*. For the two to be different: a manager that delegated the work and
        an agent that performed it, and a lesson written into the manager's
        instructions from a run it did not perform is how a habit gets attributed
        to somebody who has not got it yet.
        """
        from ai_orchestrator.application.skill_learner import SkillLearner
        from ai_orchestrator.persistence.models import Approval as _Approval

        if not task.owner_agent_id:
            return
        accepted = (
            (
                await self._session.execute(
                    select(_Approval.id).where(
                        _Approval.organization_id == self._org,
                        _Approval.task_id == str(task.id),
                        _Approval.status == "approved",
                    )
                )
            )
            .scalars()
            .first()
        )
        if not accepted:
            logger.info(
                "skill.lesson_skipped",
                task_id=str(task.id),
                reason="the run completed but no person approved it",
            )
            return

        learner = SkillLearner(self._session, self._org)
        outcome = await learner.learn_from(task_id=str(task.id), agent_id=str(task.owner_agent_id))
        logger.info(
            "skill.learned",
            task_id=str(task.id),
            agent_id=str(task.owner_agent_id),
            written=outcome.get("written"),
            skill_id=outcome.get("skill_id"),
            reason=outcome.get("reason"),
        )

    async def _reports_from_children(self, task: Task, *, limit: int = 5) -> tuple[str, ...]:
        """What the agents working below this one said they did.

        Work travels down and reports travel back up, and the second half is the
        half that is usually missing: the parent knows it has six children and
        that five of them finished, which is a queue, not an answer. A manager
        needs "the tender came in 12% over budget and I recommend splitting it",
        and that sentence exists nowhere except in the child's own execution
        summary.

        The summary lives on `executions`, not on `tasks` -- measured while
        building this, and it is the reason a first attempt at this function
        looked for a column that does not exist. Read from the child's latest
        completed execution, so a task that ran twice reports once, from its last
        run, which is the run whose answer the parent would act on.

        Attributed to the child by title and marked untrusted for the same reason
        retrieved documents are: a colleague's report is another agent's output,
        and a parent that treats it as an instruction is a parent that can be
        steered by anyone below it.

        Bounded at five, most recent first, and the truncation says so. A parent
        with thirty closed children does not get a wall of text; it gets the ones
        that finished since it last looked, and a list that admits what it left
        out is honest in a way a silently shortened one is not.
        """

        from ai_orchestrator.persistence.models import Execution

        rows = (
            await self._session.execute(
                select(Task.title, Execution.summary, Task.completed_at)
                .join(
                    Execution,
                    (Execution.task_id == Task.id)
                    & (Execution.organization_id == Task.organization_id),
                )
                .where(
                    Task.organization_id == self._org,
                    Task.parent_task_id == str(task.id),
                    Task.status == TaskStatus.COMPLETED.value,
                    Execution.summary.isnot(None),
                    Execution.summary != "",
                )
                .order_by(Task.completed_at.desc().nullslast())
                .limit(limit)
            )
        ).all()
        if not rows:
            return ()
        seen: set[str] = set()
        reports: list[str] = []
        for title, summary, completed_at in rows:
            # One report per child: a task that ran three times has three
            # execution rows and one answer worth passing up.
            if title in seen:
                continue
            seen.add(title)
            when = completed_at.date().isoformat() if completed_at else "an unrecorded date"
            reports.append(f'"{title}" (finished {when}) reported: {summary}')
        if len(rows) >= limit:
            reports.append(f"(showing at most {limit} reports; earlier ones are not shown)")
        return tuple(reports)

    def _make_ask(self, context: AgentContext) -> Any:
        """The consultation callable the `ask_agent` tool will call.

        Four rules, all enforced here rather than requested in a prompt:

        1. Only somebody in the caller's own roster is addressable.
        2. A per-run counter caps consultations, and the refusal names the cap.
        3. No task row, no delegation row, no owner. The recipient answers; the
           caller keeps the work.
        4. The recipient's tool set is emptied. A consultation is a question,
           and a colleague who could call `delegate_to_agent` from inside an
           answer would be running somebody else's run inside this one.

        The cost is a model call and it is not free to hide: it lands in the same
        budget as the run, so a loop cannot spend without being charged.
        """

        async def _ask(*, agent_name: str, question: str) -> ToolResult:
            from ai_orchestrator.application.peer_consultation import (
                REFUSAL_LIMIT,
                REFUSAL_NO_ANSWER,
                REFUSAL_NOT_ADDRESSABLE,
                REFUSAL_QUESTION_REQUIRED,
                can_consult,
            )

            if not agent_name or not question.strip():
                return ToolResult.failure(
                    REFUSAL_QUESTION_REQUIRED, "agent_name and question are both required"
                )
            roster = context.delegate_options
            if not can_consult(roster, agent_name):
                # Same refusal the roster is for: a name that was never offered is
                # a name that does not exist as far as this run is concerned.
                return ToolResult.failure(
                    REFUSAL_NOT_ADDRESSABLE,
                    f"{agent_name} is not one of the colleagues you may ask; "
                    f"you may ask: "
                    f"{', '.join(o.agent_name for o in roster) or 'nobody'}",
                )
            if self._consultations is None:
                self._consultations = ConsultationLedger()
            if self._consultations.exhausted:
                # The one refusal the model must be able to act on: the ceiling is
                # reached, so the right move is to decide with what is known.
                return ToolResult.failure(
                    REFUSAL_LIMIT,
                    f"you have already asked {self._consultations.limit} colleagues "
                    f"on this task; decide with what you have rather than asking again",
                )

            answer = await self._consult_once(
                context=context, agent_name=agent_name, question=question
            )
            self._consultations.record(str(context.actor.id), agent_name)
            if answer is None:
                return ToolResult.failure(
                    REFUSAL_NO_ANSWER,
                    f"{agent_name} did not return an answer; proceed on what you know",
                )
            return ToolResult(ok=True, output={"agent": agent_name, "answer": answer})

        return _ask

    async def _consult_once(
        self, *, context: AgentContext, agent_name: str, question: str
    ) -> str | None:
        """Run one agent to get one answer. No task, no delegation, no owner.

        A transient failure returns `None` rather than raising: the caller is
        mid-run and asking is an optional courtesy, so a colleague being down
        must not kill the run that asked.
        """
        from sqlalchemy import select as _select

        from ai_orchestrator.application.peer_consultation import build_query
        from ai_orchestrator.persistence.models import Agent as _Agent
        from ai_orchestrator.persistence.models import Task as _Task

        rows = (
            (
                await self._session.execute(
                    _select(_Agent).where(
                        _Agent.organization_id == self._org, _Agent.name == agent_name
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        target = rows[0]
        if target.lifecycle_status != "active":
            return None

        # A throwaway task object. It is never added to the session, so it leaves
        # no row: it exists because `AgentContext` is defined in terms of a task,
        # and inventing a second context type for consultations would fork the
        # contract the runtime is written against.
        task = _Task(
            id=str(context.task.task_id),
            organization_id=self._org,
            title="consultation",
            goal=build_query(str(context.task.goal), question),
            task_type=TaskType.ANALYSIS,
            status=TaskStatus.RUNNING.value,
            # The runtime contract requires both. A persisted task gets them
            # from the database default; a throwaway object gets them from
            # here, and leaving them out fails the peer run with a validation
            # error that reads as a platform fault rather than as a missing
            # default -- which is exactly how this was first found.
            input={},
            constraints={},
        )
        try:
            peer = await self._resolve_agent(target.id)
            peer_context = await self._build_context(task, peer)
            # Empty the peer's reach. An answer is advice; a colleague who can
            # delegate from inside an answer is running a second organisation's
            # work inside this run.
            peer_context = peer_context.model_copy(
                update={"authorized_tools": (), "delegate_targets": (), "delegate_options": ()}
            )
            agent_task = self._to_agent_task(task)
            result = await self._runtime.execute(agent_task, peer_context)
        except Exception as exc:
            logger.info("consultation.failed", target=agent_name, error=str(exc))
            return None
        summary = (result.summary or "").strip()
        return summary or None

    def _make_a2a_caller(self) -> Any:
        """The callable behind `call_a2a_agent`.

        Built once per service rather than per run, and it closes over nothing:
        an A2A call is not part of *this* delegation chain. It has no depth, no
        ancestor path and no fan-out, because the peer is not an agent in this
        organisation and cannot be handed a task. What it does have is the
        gateway's own gates -- `EXTERNAL_SIDE_EFFECT` risk, the run-mode check
        that refuses to send anything in simulation, the audit row -- because the
        call leaves a system this platform does not control.

        Returns a refusal rather than raising when the gateway cannot be opened,
        for the same reason `delegate_to_agent` does: an agent that cannot reach a
        peer needs to be able to do the work itself instead of having its run
        killed.
        """
        from ai_orchestrator.a2a.gateway import A2AGateway

        try:
            gateway = A2AGateway(self._session, self._org)
        except Exception as exc:
            # Bound to a new name: Python deletes an `except ... as exc` binding
            # when the block ends, so a closure over `exc` raises `NameError` at
            # call time instead of returning the message.
            problem = f"{type(exc).__name__}: {exc}"

            async def _unavailable(**_: Any) -> ToolResult:
                return ToolResult.failure(
                    "A2A_UNAVAILABLE", f"the A2A gateway could not be opened: {problem}"
                )

            return _unavailable

        async def _call(*, agent_name: str, prompt: str, skill_id: str | None) -> ToolResult:
            from ai_orchestrator.domain.contracts import Actor
            from ai_orchestrator.domain.enums import ActorType

            try:
                peer_id = await gateway.resolve(organization_id=self._org, name=agent_name)
            except (LookupError, PreconditionError) as exc:
                return ToolResult.failure("A2A_AGENT_UNUSABLE", f"{agent_name}: {exc}")
            outcome = await gateway.call(
                actor=Actor(id=str(self._active_source_agent_id or "system"), kind=ActorType.AGENT),
                a2a_agent_id=peer_id,
                prompt=prompt,
                skill_id=skill_id,
            )
            if not outcome.succeeded:
                return ToolResult.failure(
                    "A2A_CALL_FAILED",
                    f"{agent_name} did not answer: state={outcome.remote_state}",
                )
            # The peer's words are data, not instructions. Flagged, because a
            # remote agent is exactly where a prompt injection would arrive from.
            return ToolResult(
                ok=True,
                output={
                    "text": outcome.text,
                    "remote_task_id": outcome.remote_task_id,
                    "untrusted": True,
                },
            )

        return _call

    def _make_delegate(self, context: Any) -> Any:
        """The delegation callable the `delegate_to_agent` tool will call.

        Built per run and closed over the context, because the tool must not be
        able to reach the database or the ancestor path itself: it asks, and
        this is the code that decides. Every bound the domain layer has — the
        ancestor chain, depth, fan-out, budget narrowing, the cycle check — is
        applied here by `DelegationExecutor`, so a tool cannot route around any
        of them.
        """

        async def _delegate(*, agent_name: str, objective: str) -> Any:
            outcome = await self._delegate_once(
                context=context, agent_name=agent_name, objective=objective
            )
            # `error_kind`/`error_message`, not `error_code`/`message`: those
            # are the field names on `ToolResult`, and a typo here is a TypeError
            # raised *inside* the agent loop, which reads as a tool failure rather
            # than as a constructor mistake.
            return ToolResult(
                ok=outcome is not None,
                output={
                    "accepted": outcome is not None,
                    "agent": agent_name,
                    "objective": objective,
                    "child_task_ids": list(getattr(outcome, "child_task_ids", [])),
                },
                error_kind=None if outcome is not None else "DELEGATION_REFUSED",
                error_message=(
                    "delegated" if outcome is not None else "the platform refused this delegation"
                ),
            )

        return _delegate

    async def _delegate_once(self, *, context: Any, agent_name: str, objective: str) -> Any:
        """Resolve the name, delegate, and report the refusal as text.

        The name is resolved inside the tenant, so a model that hallucinates an
        agent gets a refusal it can read rather than a row in somebody else's
        organisation.
        """
        from sqlalchemy import select as _select

        from ai_orchestrator.application.delegation_executor import DelegationExecutor
        from ai_orchestrator.domain.contracts import ActionProposal
        from ai_orchestrator.domain.delegation import DelegationPath
        from ai_orchestrator.domain.ids import AgentId, TaskId
        from ai_orchestrator.persistence.models import Agent as _Agent
        from ai_orchestrator.persistence.models import Task as _Task

        rows = (
            (
                await self._session.execute(
                    _select(_Agent).where(
                        _Agent.organization_id == self._org, _Agent.name == agent_name
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            logger.info("delegation.unknown_agent", requested=agent_name)
            return None
        target = rows[0]
        if target.lifecycle_status != "active":
            logger.info("delegation.agent_not_active", requested=agent_name)
            return None

        task = (
            await self._session.execute(_select(_Task).where(_Task.id == str(context.task.task_id)))
        ).scalar_one_or_none()
        if task is None:
            return None

        executor = DelegationExecutor(
            tasks=self._tasks,
            delegations=self._delegations,
            agents=self._agents,
            audit=self._audit,
            organization_id=self._org,
            platform_limits=context.delegation_limits,
        )
        path = context.delegation_path
        if not path or path.depth == 0:
            path = DelegationPath.root(AgentId(str(context.actor.id)), TaskId(task.id))

        result = await executor.apply(
            parent=task,
            source_agent_id=str(context.actor.id),
            proposals=(
                ActionProposal(
                    kind="delegate",
                    target_agent_id=str(target.id),
                    objective=objective,
                ),
            ),
            path=path,
        )
        if not result.any_accepted:
            logger.info(
                "delegation.refused_by_platform",
                requested=agent_name,
                reason=result.refused[0][1] if result.refused else "unknown",
            )
            return None
        return result

    async def _count_delegations(self, task_id: str) -> int:
        """How many delegations already exist below this task.

        Read from the database rather than from the run's in-memory outcome,
        because the tool path creates delegations *during* the run and the
        in-memory outcome only knows about the proposal path. A rule that asked
        the wrong object would fail tasks that had delegated correctly — which is
        what happened.
        """
        return await self._delegations.issued_by_for_parent(
            self._active_source_agent_id or "", str(task_id)
        )

    async def _record_procedure(
        self, task: Task, resolved: ResolvedAgent
    ) -> tuple[str, int] | None:
        """Record this run's shape, and how many times it has happened.

        Returns `(fingerprint, prior_count)` or `None` when the run left no trace.
        The count is the repetition gate's input, and it is read *before* this run
        is written, so it means "how many times had this agent done this before
        now" — which is the question a change proposal needs answered.

        Not fatal if this fails. A missing fingerprint costs the ability to detect
        a repeat; failing the run over it would cost the work itself, and a task
        that completed is not made untrue by not being counted.
        """
        from ai_orchestrator.application.procedures import ProcedureReader

        reader = ProcedureReader(self._session, self._org)
        try:
            fingerprint = await reader.fingerprint_for(str(task.id))
            if fingerprint is None:
                return None
            prior = await reader.repetition_count(
                agent_id=str(resolved.agent.id),
                fingerprint=fingerprint,
                exclude_task_id=str(task.id),
            )
            await reader.record_for(str(task.id))
        except Exception as exc:
            logger.warning(
                "procedure.not_recorded",
                task_id=str(task.id),
                detail=f"{type(exc).__name__}: {exc}"[:200],
            )
            return None
        logger.info(
            "procedure.recorded",
            task_id=str(task.id),
            agent_id=str(resolved.agent.id),
            procedure=describe(fingerprint),
            prior_repetitions=prior,
        )
        return fingerprint, prior

    async def _delegate_roster(self, resolved: ResolvedAgent) -> tuple[tuple[str, str], ...]:
        """Who this agent may hand work to, with one line each on what they do.

        Built from the organisation tree, not from the model's imagination. The
        first run of the real model delegated to a department called
        `Procurement`, which this company does not have — the platform refused
        the delegation, correctly, and the goal was lost. A refusal is the right
        behaviour; being unable to name anyone is not.

        An agent that reports to you is not someone you delegate to, so only the
        units *below* this one are candidates — the manager's own subordinates
        are excluded, and so is the agent itself.
        """
        if not resolved.org_unit_id:
            return ()
        child_units = await self._units.list_children(resolved.org_unit_id)
        if not child_units:
            return ()
        purposes = {u.id: str(u.purpose) for u in child_units}
        roster: list[tuple[str, str]] = []
        for unit in child_units:
            for agent in await self._agents.list(lifecycle_status="active", org_unit_id=unit.id):
                if agent.id != resolved.agent.id:
                    roster.append((str(agent.name), purposes[unit.id]))
        return tuple(roster)

    async def _delegate_options(self, resolved: ResolvedAgent) -> tuple[DelegateOption, ...]:
        """The roster, plus what each person can do and what they are carrying.

        The plain roster answers "who may I name". This answers "who should I
        name", which is the question a manager is actually asking. Two facts come
        from the registry rather than the prompt: `capabilities`, so a run can be
        matched to a specialism instead of to a name, and `active_tasks` plus the
        live titles, so a colleague at capacity is visibly at capacity.

        Live titles are the difference between a count and a workload. "3 tasks"
        does not say whether one is a small approval or a stalled negotiation, and
        a model handed only the number will pick a colleague it cannot help.
        """
        if not resolved.org_unit_id:
            return ()
        child_units = await self._units.list_children(resolved.org_unit_id)
        if not child_units:
            return ()
        purposes = {u.id: str(u.purpose) for u in child_units}
        options: list[DelegateOption] = []
        for unit in child_units:
            for agent in await self._agents.list(lifecycle_status="active", org_unit_id=unit.id):
                if agent.id == resolved.agent.id:
                    continue
                options.append(
                    DelegateOption(
                        agent_name=str(agent.name),
                        purpose=purposes[unit.id],
                        capabilities=tuple(str(c) for c in (agent.capabilities or [])),
                        active_tasks=int(agent.active_tasks or 0),
                        current_work=await self._live_titles(agent.id),
                    )
                )
        return tuple(options)

    async def _live_titles(self, agent_id: str, *, limit: int = 3) -> tuple[str, ...]:
        """Titles of the work this agent is holding now, oldest first.

        Bounded at three: a longer list is unreadable in a prompt and the model
        cannot weigh it anyway. A colleague holding twenty tasks is already
        excluded by `active_tasks`.
        """
        rows = await self._session.execute(
            select(Task.title, Task.priority)
            .where(
                Task.organization_id == self._org,
                Task.owner_agent_id == agent_id,
                Task.status.in_(
                    [
                        TaskStatus.ASSIGNED,
                        TaskStatus.RUNNING,
                        TaskStatus.BLOCKED,
                        TaskStatus.WAITING_FOR_INPUT,
                        TaskStatus.WAITING_FOR_APPROVAL,
                    ]
                ),
            )
            .order_by(Task.created_at.asc())
            .limit(limit)
        )
        return tuple(str(r[0]) for r in rows)

    # ------------------------------------------------------------- context --
    async def _resolve_agent(self, agent_id: str) -> ResolvedAgent:
        agent = await self._agents.get(agent_id)
        if agent.lifecycle_status not in {
            AgentLifecycleStatus.ACTIVE.value,
            AgentLifecycleStatus.DEGRADED.value,
        }:
            msg = (
                f"agent {agent.name} is {agent.lifecycle_status}, not active; "
                f"it cannot be given work"
            )
            raise AgentUnavailable(msg, resource_type="agent", resource_id=agent_id)

        definition = await self._definitions.get(agent.definition_id)
        role = await self._roles.get(agent.role_id)
        unit = None
        if agent.org_unit_id:
            try:
                unit = await self._units.get(agent.org_unit_id)
            except PlatformError:
                unit = None

        return ResolvedAgent(
            agent=agent,
            definition=definition,
            role=role,
            profile=_profile_from_role(role),
            org_unit_id=unit.id if unit else agent.org_unit_id,
        )

    async def _build_context(
        self,
        task: Task,
        resolved: ResolvedAgent,
        *,
        retrieved_context: Sequence[str] = (),
    ) -> AgentContext:
        """Assemble what this agent is allowed to see.

        Tools and skills come from bindings, filtered by the role's authority
        profile. A binding the role does not permit is dropped here, so a
        misconfigured grant cannot widen an agent's reach.
        """
        tool_bindings = await self._capabilities.tool_bindings_for(resolved.agent.id)
        tool_contracts: list[ToolContract] = []
        for binding in tool_bindings:
            contract = await self._contract_for_binding(binding)
            if contract is None:
                continue
            # Narrowest-wins. The binding stores a `RiskLevel` (a severity) and
            # the tool carries a `ToolRisk` (a classification), so the ceiling is
            # translated into the tool vocabulary before the two are compared —
            # indexing one ordering with the other's values raises `KeyError`,
            # and quietly comparing the raw strings would get it backwards.
            ceiling = tool_risk_ceiling_for(binding.max_risk)
            effective_risk = (
                ceiling
                if _TOOL_RISK_ORDER[ceiling] < _TOOL_RISK_ORDER[contract.risk.value]
                else contract.risk.value
            )
            tool_contracts.append(
                contract.model_copy(
                    update={
                        "risk": ToolRisk(effective_risk),
                        # An approval requirement is never removed by a narrower
                        # risk ceiling: the two are independent decisions.
                        "requires_approval": (
                            contract.requires_approval or binding.requires_approval
                        ),
                        "timeout_s": min(
                            contract.timeout_s,
                            binding.rate_limit_override or contract.timeout_s,
                        ),
                    }
                )
            )

        skill_contracts = await self._skill_contracts(resolved.agent.id)

        memory_scope = MemoryScope(
            organization_id=OrganizationId(self._org),
            org_unit_ids=(OrganizationId(resolved.org_unit_id),) if resolved.org_unit_id else (),
            agent_ids=(resolved.agent.id,),
            task_ids=(TaskId(task.id),),
            max_classification=DataClassification.INTERNAL,
        )

        roster = await self._delegate_roster(resolved)
        delegate_options = await self._delegate_options(resolved)
        return ContextBuilder().build(
            ContextBuilderInput(
                actor=actor_for_agent(
                    resolved.agent.id,
                    self._org,
                    org_unit_id=resolved.org_unit_id,
                    role_id=resolved.role.id,
                    display_name=resolved.agent.name,
                ),
                task=self._to_agent_task(task),
                system_instructions=resolved.definition.system_instructions,
                role_name=resolved.role.name,
                delegate_targets=roster,
                delegate_options=delegate_options,
                organization_id=self._org,
                org_unit_id=resolved.org_unit_id,
                tools=tool_contracts,
                skills=skill_contracts,
                memory=memory_scope,
                retrieved_context=list(retrieved_context),
                max_tokens=task.budget_limit_tokens or self._token_limit(),
                max_cost_usd=float(task.budget_limit_usd or self._cost_limit()),
                max_runtime_s=self._runtime_limit(),
                max_tool_calls=self._tool_call_limit(),
                autonomy_level=AutonomyLevel(resolved.agent.autonomy_level),
                run_mode=self._run_mode,
                model_profile=resolved.agent.model_profile,
                deadline=task.deadline_at,
                agent_definition_version=resolved.definition.version,
                data_classification=DataClassification.INTERNAL,
            )
        )

    async def _skill_contracts(self, agent_id: str) -> list[SkillContract]:
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import AgentSkillBinding, Skill, SkillVersion

        result = await self._session.execute(
            select(Skill, SkillVersion)
            .join(AgentSkillBinding, AgentSkillBinding.skill_id == Skill.id)
            .outerjoin(SkillVersion, SkillVersion.id == AgentSkillBinding.skill_version_id)
            .where(
                AgentSkillBinding.agent_id == agent_id,
                AgentSkillBinding.is_enabled.is_(True),
            )
        )
        contracts: list[SkillContract] = []
        for skill, version in result.all():
            if skill.governance_state == "blocked":
                continue
            contracts.append(
                SkillContract(
                    skill_id=_as_skill_id(skill.id),
                    name=skill.name,
                    version=version.version if version else "unpinned",
                    description=skill.description,
                    instructions=version.instructions if version else "",
                    input_schema=version.input_schema if version else {},
                    output_schema=version.output_schema if version else None,
                    required_tool_ids=tuple(version.required_tool_ids) if version else (),
                    risk_level=_as_risk(version.risk_level if version else "low"),
                )
            )
        return contracts

    async def _record_model_call(
        self,
        response: Any,
        *,
        task_id: str,
        execution_id: str,
        agent_id: str,
    ) -> None:
        """One row in `model_usage` per provider call.

        The routing decision travels with it, so a call served by a fallback is
        recorded as a fallback. Without this the table stays empty and the cost
        dashboard is a well-formatted zero — which is what it was, until a live
        call showed `SELECT count(*) FROM model_usage` returning 0.

        A refusal is not recorded. The gateway declined before reaching a
        provider, so there is no call, and a row naming no model would be a lie
        in a table whose whole purpose is to say what was actually called.
        """
        from ai_orchestrator.persistence.models import ModelUsage

        # Counted before the early return below. A response with no model name
        # is a provider that failed without answering, and those calls are the
        # ones worth seeing -- a run that made thirty calls and got thirty
        # failures looks identical to a run that made none otherwise.
        self._model_calls += 1
        if not response.model_used:
            return
        decision = response.decision
        usage = response.usage
        self._session.add(
            ModelUsage(
                id=str(ModelUsageId.create()),
                organization_id=self._org,
                task_id=task_id,
                execution_id=execution_id,
                agent_id=agent_id,
                model_profile=decision.profile if decision else "unknown",
                provider=response.provider or "unknown",
                model_used=response.model_used,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                reasoning_tokens=usage.reasoning_tokens if usage else 0,
                cache_read_tokens=usage.cache_read_tokens if usage else 0,
                cost_usd=float(response.cost_usd or 0),
                latency_ms=response.latency_ms,
                routing_reason=decision.routing_reason if decision else "primary",
                status="ok",
                error_kind=None,
            )
        )

    # ------------------------------------------------------------ outcomes --
    async def _finish(
        self,
        task: Task,
        resolved: ResolvedAgent,
        context: AgentContext,
        result: AgentResult,
        execution_id: str,
        budget: BudgetState,
        started: float,
    ) -> ExecutionOutcome:
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = result.usage
        cost = result.cost_usd or Money("0")
        if usage:
            budget.commit(usage, cost)
        if cost > 0 or (usage and usage.total):
            await self._tasks.record_spend(
                task.id, tokens=usage.total if usage else 0, cost_usd=float(cost)
            )

        await self._executions.finish(
            execution_id,
            status=result.status.value,
            summary=result.summary,
            decision_record={
                "steps": [
                    {
                        "action": "runtime.execute",
                        "runtime": self._runtime.name,
                        "status": result.status.value,
                        "model_used": result.model_used,
                        "proposals": [p.kind for p in result.follow_up_actions],
                    }
                ],
                "autonomy_level": context.autonomy_level.value,
                "policy_version": context.policy_version,
            },
            artifacts=[a.model_dump(mode="json") for a in result.artifacts],
            error_kind=result.error_code,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            reasoning_tokens=usage.reasoning_tokens if usage else 0,
            cost_usd=float(cost),
            duration_ms=duration_ms,
            model_used=result.model_used,
        )

        # --- delegation: before the status, never after -----------------
        # Applied here because this is the only place that knows the ordering.
        # A proposal to delegate is data until `authorize_delegation` says
        # otherwise, and the child task has to exist before the parent is
        # allowed to look finished — otherwise a goal with outstanding work is
        # reported as complete.
        outcome = await self._apply_delegations(
            task=task, source_agent_id=resolved.agent.id, result=result, context=context
        )

        # --- the shape of the work, recorded ---------------------------
        # After the delegations, so the fingerprint covers the tool calls the run
        # actually made *including* the delegation it proposed — and before the
        # status transition, because a completed task with no recorded procedure is
        # a task the repetition count can never see.
        #
        # Recording it here rather than in a scheduler means the count is never
        # behind: a procedure that happened is counted the moment it finished. A
        # nightly job would make the gate open a day late every time, and a gate
        # that opens late is a gate nobody trusts.
        procedure = await self._record_procedure(task, resolved)

        # Persist the call counts, once, before any branch can return. Placed here
        # because it is the first point after the run that no early return can
        # skip: a task that failed at the model, or was refused at a gate, still
        # made the calls it made, and a run that only counts its successes is a
        # run whose failures are invisible -- which is the half worth counting.
        await self._record_call_counts()

        # `task` stays a `Task` throughout. `_fail` returns an `ExecutionOutcome`,
        # and assigning that back to `task` meant the summary below read
        # `task.id` from an object that has `task_id` — so every task failure
        # raised `AttributeError` while reporting the failure, turning a recorded
        # failure into a 500 with the cause lost. `_block` returns early for the
        # same reason.
        final_status = TaskStatus(task.status)

        # Delegated work exists below this task, so the parent cannot be
        # `completed`: the state machine has no such transition, and inventing
        # one would let a goal close with its children still open.
        #
        # **Read from the database, not from `outcome.any_accepted`.** The
        # in-memory outcome only knows about the *proposal* path. A run that
        # delegated with the `delegate_to_agent` tool leaves it empty -- which is
        # documented in the same file, twenty lines below, as the reason
        # `coordination_may_complete` counts delegations in SQL. The status
        # decision kept reading the memory, so the two halves of one fact
        # disagreed:
        #
        #     a goal handed to the chief, delegated to an office with the tool,
        #     and was marked `completed` while the office's own task sat
        #     `assigned` and nobody had run it.
        #
        # Measured on a three-tier run: the root finished after ONE execution and
        # the department never ran. "The pipeline works" was true of the queue and
        # false of the organisation.
        #
        # **Live descendants, not delegations made.** The first version of this
        # guard counted delegations and was wrong in the other direction: an office
        # that has delegated anything can then *never* be `completed`, so a goal
        # whose whole point is to report upward stayed `running` forever and the
        # driver re-ran it until the execution bound -- 60 executions of an office
        # re-delegating the same work. The question is not "did I delegate?" but
        # "is anything below me still open?", and that is one recursive query.
        # A **flag**, not the status. The first version of this overwrote
        # `final_status` with RUNNING and then tested `final_status is RUNNING`
        # later -- and `final_status` is *initialised* to the task's current
        # status, which the executor has already set to RUNNING. So every leaf
        # task that finished took the "already running" branch and never
        # completed: a department's work came back, was good, and stayed open
        # forever. The two states mean different things and need two names.
        has_open_work_below = (
            outcome.any_accepted or await self._tasks.live_descendant_count(task.id) > 0
        )
        if has_open_work_below:
            final_status = TaskStatus.RUNNING
        if result.status is AgentResultStatus.COMPLETED:
            if not coordination_may_complete(
                task=task,
                context=context,
                outcome=outcome,
                delegations_made=await self._count_delegations(task.id),
            ):
                return await self._fail(
                    task,
                    "a coordination task completed without delegating: the agent had "
                    f"{len(context.delegate_targets)} agents it could have handed work to "
                    "and did the work itself",
                    "no_delegation",
                )
            # **A declared output is checked before `completed` is granted.**
            #
            # `expected_output_schema` was stored and passed to the agent and never read back:
            # **113 tasks** on the development tenant carried one and nothing anywhere enforced
            # it. So the first stage of `ONX-BO-HR-SOP-004` ran, declared `approved_headcount`,
            # finished `completed` and wrote `{proposal_count, scripted}` -- the stage reported
            # success and produced something else. A declared output nobody checks is a comment
            # in a column.
            #
            # The check is here rather than in a domain validator because this is the moment the
            # claim is *made*: granting `completed` is what tells the queue, the department view
            # and the operator that the work was done. One call, before the transition.
            #
            # `COMPLETED` only. A task that is paused, blocked or waiting for approval has not
            # claimed to have finished, and failing it for missing output would make any process
            # that waits on a person unrunnable.
            # **A coordinator is not held to the contract it passed down.**
            #
            # Found on the first real free-model run of the three-tier pipeline.
            # The contract `{"required": ["verdicts", "reason"]}` belongs to the
            # department that does the work; it propagates down so the department
            # can be held to it -- and then it was enforced on the *chief*, which
            # delegated and returned. The chief has no verdicts of its own to
            # produce, so it failed with:
            #
            #     this task said it would produce reason, verdicts, and produced
            #     nothing. A task that does not produce what it declared has not
            #     done the work
            #
            # which is true, and is the wrong question for a task whose job was to
            # route the work rather than to do it. The answer a coordinator owes is
            # what it delegated and what came back, and that is composed from the
            # children's accepted outputs by `settle_finished` -- not re-derived by
            # the model that only forwarded it.
            #
            # So: the check is for the worker. A task that delegated is judged on
            # its delegation, which `coordination_may_complete` already does.
            delegated = await self._count_delegations(task.id)
            missing = [] if delegated else missing_keys(result.output, task.expected_output_schema)
            if missing:
                await self._tasks.set_output(task.id, result.output or {})
                return await self._fail(
                    task,
                    describe_mismatch(result.output, task.expected_output_schema),
                    "output_contract_unmet",
                )
            if result.output:
                await self._tasks.set_output(task.id, result.output)
            if has_open_work_below:
                # Work was delegated below this task and is still open, so the
                # task is *not* finished -- the office's own task, or the
                # department's, is still to run and be reviewed. Marking it
                # `completed` here is what let a three-tier goal report success
                # after one execution with the department never having run.
                #
                # The executor already moved the task to `running` when the run
                # began, so there is nothing to transition *to* -- and
                # `running --begin_work-->` is correctly illegal, because a task
                # already running does not need telling that it is running. The
                # transition is only for the case where the run started from
                # `assigned` and the guard below moved the intent without the row.
                if TaskStatus(task.status) is not TaskStatus.RUNNING:
                    task = await self._tasks.transition(task.id, Transition.BEGIN_WORK)
                final_status = TaskStatus(task.status)
            else:
                task = await self._tasks.transition(task.id, Transition.COMPLETE)
                final_status = TaskStatus(task.status)
        elif result.status is AgentResultStatus.NEEDS_APPROVAL:
            task = await self._tasks.transition(task.id, Transition.REQUEST_APPROVAL)
            final_status = TaskStatus(task.status)
            # The task pausing is not the same as a person being asked. Without
            # this row there is nothing in the inbox, nothing on the UI, and
            # nobody who can say yes — the task simply waits forever with a
            # status that says somebody is deciding when nobody has been asked.
            await self._open_approval(
                task=task,
                resolved=resolved,
                result=result,
                execution_id=execution_id,
            )
        elif result.status is AgentResultStatus.NEEDS_INPUT:
            task = await self._tasks.transition(task.id, Transition.REQUEST_INPUT)
            final_status = TaskStatus(task.status)
        elif result.status is AgentResultStatus.BLOCKED:
            return await self._block(task, result.summary or "the agent reported a block")
        elif result.status is AgentResultStatus.CANCELED:
            task = await self._tasks.transition(task.id, Transition.CANCEL)
            final_status = TaskStatus(task.status)
        else:
            failed = await self._fail(
                task,
                result.summary or result.error_code or "execution failed",
                _category_for(result.error_code),
                execution_id=execution_id,
                started=started,
            )
            final_status = failed.status

        # --- the learning loop --------------------------------------------
        # A finished run that a person accepted has something to teach. Not the
        # moment the button was pressed: an approval authorises *continuing*, so
        # learning at the gate would teach from a run that stopped, and its log
        # has no result in it. Not a run nobody approved either -- the approval
        # is the evidence that the work was accepted, and an unapproved run is a
        # different fact.
        #
        # Placed here, at the one point where `final_status` is settled, because
        # the earlier version of this hook read `final_status` before the
        # branches below had set it -- so it compared the status the task *started*
        # in and never fired. A hook placed near the event it reacts to is not the
        # same thing as a hook placed before the event is decided.
        if final_status is TaskStatus.COMPLETED:
            await self._learn_if_approved(task)

        return ExecutionOutcome(
            task_id=task.id,
            status=final_status,
            summary=result.summary,
            execution_id=execution_id,
            agent_id=resolved.agent.id,
            needs_approval=result.status is AgentResultStatus.NEEDS_APPROVAL,
            result=result,
            proposed_actions=result.follow_up_actions,
            cost_usd=cost,
            tokens=usage.total if usage else 0,
            duration_ms=duration_ms,
            retryable=result.status is AgentResultStatus.FAILED,
            procedure_fingerprint=procedure[0] if procedure else None,
            prior_repetitions=procedure[1] if procedure else 0,
        )

    async def _block(self, task: Task, reason: str) -> ExecutionOutcome:
        """Blocked is not failed. A blocked task is waiting for something, and
        the difference decides whether an operator is paged."""
        if task.status not in {TaskStatus.BLOCKED.value}:
            # Already blocked: the desired end state holds, so a refusal is not a
            # failure. Anything else would mean a task in a state that cannot be
            # blocked, which the state machine is right to refuse.
            with contextlib.suppress(PreconditionError):
                await self._tasks.transition(task.id, Transition.BLOCK)
        await self._audit.record(
            actor=actor_for_agent("", self._org),
            action="task.block",
            resource_type="task",
            resource_id=task.id,
            task_id=task.id,
            outcome="blocked",
            policy_reason=reason[:500],
        )
        return ExecutionOutcome(task_id=task.id, status=TaskStatus.BLOCKED, blocked_reason=reason)

    async def _fail_without_a_usable_session(
        self,
        *,
        task: Task,
        message: str,
        execution_id: str | None,
        cause: BaseException,
    ) -> ExecutionOutcome:
        """Record a failure on a session that may already be unusable.

        The failure handler writes through the same session as the work that just
        failed. When the work failed *because* the transaction was rolled back,
        that write raises a second error — `Can't operate on closed transaction` —
        and it replaces the real cause in the traceback. The operator then spends
        the afternoon on a session-lifecycle bug that does not exist.

        So: the cause is logged and returned, and the database write is attempted
        only when the session can still take it. Losing the `failed` status on a
        task whose transaction is gone is the smaller loss, and it is recorded as
        such rather than hidden.
        """
        write_failure = ""
        if self._session_can_write():
            try:
                return await self._fail(task, message, "internal_error", execution_id=execution_id)
            except Exception as write_error:
                # Asked and answered. Every attempt to *predict* whether this
                # session can take a statement was wrong: `in_transaction()` is
                # `True` for a rolled-back transaction, and `is_active` was still
                # `True` on a session whose context manager had already closed.
                # The only reliable test is to try, and to have somewhere to go when
                # the try fails.
                write_failure = type(write_error).__name__
        logger.error(
            "task.failed_transaction_lost",
            task_id=str(task.id),
            category="internal_error",
            reason=message[:200],
            cause=type(cause).__name__,
            write_failure=write_failure,
            note="the failure could not be written: the transaction was already gone",
            # The traceback, because the message above is the *symptom* the
            # transaction produced and the operator is here to fix the cause. This
            # log deliberately threw the cause away and named only its type, so a
            # run that failed with `AttributeError: 'AgentTask' object has no
            # attribute 'title'` gave no way to find which of several hundred call
            # sites read a field that does not exist.
            exc_info=cause,
        )
        return ExecutionOutcome(
            task_id=task.id,
            status=TaskStatus.FAILED,
            summary=message[:500],
            failure_category="internal_error",
            execution_id=execution_id,
        )

    def _session_can_write(self) -> bool:
        """Whether this session can still accept a statement.

        `is_active`, not `in_transaction()`. A transaction that has been rolled back
        is still *the current transaction* — `in_transaction()` answers `True` for
        a session whose transaction is dead, which is precisely the state this
        function exists to detect. `is_active` is the flag that says whether the
        transaction can still carry a statement.

        A hint, not a verdict — and the caller treats it that way, falling back to
        the log-only path if the write raises anyway. Both earlier versions of this
        guard were wrong: `in_transaction()` is `True` for a rolled-back
        transaction, and `is_active` was still `True` on a session whose context
        manager had already closed. A guard that cannot fail is worse than no guard,
        because it is believed.
        """
        try:
            transaction = self._session.get_transaction()
        except Exception:
            return False
        return transaction is not None and bool(transaction.is_active)

    async def _fail(
        self,
        task: Task,
        message: str,
        category: str,
        *,
        execution_id: str | None = None,
        started: float | None = None,
    ) -> ExecutionOutcome:
        await self._tasks.transition(
            task.id, Transition.FAIL, error=message[:2000], failure_category=category
        )
        # `exc_info` so an `internal_error` carries the frame that raised it. The
        # message alone named the symptom -- `AttributeError: 'AgentTask' object has
        # no attribute 'title'` -- and gave no way to find which of several hundred
        # call sites read a field that does not exist, which cost the better part of
        # an hour on a bug that this line now answers outright.
        logger.warning(
            "task.failed",
            task_id=task.id,
            category=category,
            reason=message[:200],
            exc_info=category == "internal_error" or None,
        )
        return ExecutionOutcome(
            task_id=task.id,
            status=TaskStatus.FAILED,
            summary=message,
            execution_id=execution_id,
            failure_category=category,
            duration_ms=int((time.monotonic() - (started or time.monotonic())) * 1000),
        )

    async def _contract_for_binding(self, binding: Any) -> ToolContract | None:
        """Load a tool and turn its row into a contract.

        Returns None when the tool has gone or been deactivated, rather than
        raising. A binding can outlive the tool it points at, and one dangling
        binding should not stop an agent running with its remaining tools — but
        the tool must also not be silently exposed, so it is simply absent.
        """
        from sqlalchemy import select

        from ai_orchestrator.persistence.models import Tool, ToolVersion

        result = await self._session.execute(
            select(Tool, ToolVersion)
            .outerjoin(ToolVersion, ToolVersion.tool_id == Tool.id)
            .where(Tool.id == binding.tool_id, Tool.is_active.is_(True))
        )
        row = result.first()
        if row is None:
            return None
        tool, version = row

        # The query tool's description is where the schema belongs. It is the
        # one place the model is guaranteed to read it -- the runtime renders
        # the description as the function docstring -- and it costs nothing when
        # the tool is not offered, because an agent without this binding never
        # sees it.
        description = tool.description
        if tool.name == "internal_database_query":
            from ai_orchestrator.application.schema_brief import schema_note

            description = f"{description}{schema_note()}"

        return ToolContract(
            tool_id=ToolId(tool.id),
            name=tool.name,
            description=description,
            input_schema=version.input_schema if version else {},
            output_schema=version.output_schema if version else None,
            risk=ToolRisk(tool.risk_level),
            effect_class=EffectClass(tool.effect_class),
            requires_approval=tool.requires_approval,
            timeout_s=tool.timeout_seconds,
            data_classification=DataClassification(tool.data_classification),
            mcp_server=tool.mcp_server_id,
        )

    # ------------------------------------------------------------- helpers --
    def _to_agent_task(self, task: Task, input_override: dict[str, Any] | None = None) -> Any:
        from ai_orchestrator.domain.contracts import AgentTask

        return AgentTask(
            task_id=TaskId(task.id),
            organization_id=OrganizationId(self._org),
            goal=task.goal,
            task_type=TaskType(task.task_type),
            execution_id=ExecutionId.create(),
            deadline=task.deadline_at,
            input=input_override if input_override is not None else task.input,
            constraints=task.constraints,
            expected_output_schema=task.expected_output_schema,
            parent_task_id=TaskId(task.parent_task_id) if task.parent_task_id else None,
        )

    async def _record_call_counts(self) -> None:
        """Write this run's call counts onto its execution. Once, at the end.

        Kept out of the failure path on purpose: a run that errored before the
        counters were touched still gets its numbers recorded, because a ceiling
        that only counts its successes is a ceiling nobody can learn from.
        """
        execution_id = getattr(self, "_active_execution_id", None)
        if not execution_id:
            return
        try:
            await self._executions.record_call_counts(
                str(execution_id),
                tool_calls=int(getattr(self, "_tool_calls", 0)),
                model_calls=int(getattr(self, "_model_calls", 0)),
            )
        except PlatformError as exc:
            # Reported, never raised. A missing statistic is not a failed task,
            # and making it one would put the instrumentation in the path of the
            # thing it measures.
            logger.info(
                "execution.call_counts_not_recorded",
                execution_id=str(execution_id),
                error=str(exc),
            )

    def _tool_call_limit(self) -> int:
        """The per-run tool-call ceiling, from settings.

        Read the same way the token and cost limits are read, so all three
        ceilings are configured in one place and a test that pins one does not
        silently leave the other two at their defaults.
        """
        from ai_orchestrator.config.settings import get_settings

        return get_settings().max_tool_calls

    def _token_limit(self) -> int:
        from ai_orchestrator.config.settings import get_settings

        return self._default_limits or get_settings().default_max_tokens_per_task

    def _cost_limit(self) -> float:
        from ai_orchestrator.config.settings import get_settings

        return float(self._default_limits or get_settings().default_max_cost_usd_per_task)

    def _runtime_limit(self) -> int:
        from ai_orchestrator.config.settings import get_settings

        return get_settings().default_task_timeout_s


# ----------------------------------------------------------------- rules ---


def coordination_may_complete(
    *,
    task: Task,
    context: AgentContext,
    outcome: DelegationOutcome,
    delegations_made: int = 0,
) -> bool:
    """Whether this coordination task is allowed to finish, given who held it.

    A `coordination` task is a request to decompose, not to do. If the agent
    holding it had departments to hand work to and finished without a single
    accepted delegation, it did the work itself, and the goal was answered by one
    agent wearing nine hats while the audit trail says the company coordinated.

    A rule only the model is asked to honour is a suggestion, which is why this
    lives in the platform rather than in the prompt. The seeded Executive
    instructions already say "you do not do the work yourself".

    **`delegations_made` rather than `outcome`, and the distinction is the whole
    subtlety.** There are two delegation paths and they are not the same thing:

    * the **proposal** path — `follow_up_actions` become `DelegationOutcome` after
      the run, and
    * the **tool** path — the model calls `delegate_to_agent` *during* the run, the
      delegation is created immediately, and nothing appears in
      `follow_up_actions`, because the bridge files it as a `tool_call`.

    The tool path is the one that works. Reading only `outcome` therefore saw zero
    delegations in a run that had genuinely delegated, and failed a task that had
    done exactly what it was asked. The count is passed in from the database
    rather than inferred, so this rule cannot be wrong about a delegation that
    already exists.

    Scoped to `coordination` deliberately, in both directions. An `analysis` task
    that needs no decomposition must still be able to finish alone, and an agent
    with no subordinates cannot delegate at all — demanding that it try would make
    the platform the thing that fails.
    """
    if task.task_type != "coordination":
        return True
    if not context.delegate_targets:
        return True
    return outcome.any_accepted or delegations_made > 0


# ------------------------------------------------------------- conversions --
#: `RiskLevel` -> the `ToolRisk` at that severity.
#:
#: The two vocabularies are related but not identical, and comparing across them
#: with one ordering table raises `KeyError` on the first use. A binding records
#: a severity; a tool records a classification. This is the translation.
_RISK_LEVEL_TO_TOOL_RISK: dict[str, str] = {
    "low": "read_only",
    "medium": "low_risk_write",
    "high": "external_side_effect",
    "critical": "destructive",
    "privileged": "privileged",
}


def tool_risk_ceiling_for(risk_level: str) -> str:
    """The tool-risk ceiling implied by a stored risk level.

    An unknown level maps to the *lowest* ceiling rather than the highest. A
    binding whose level the platform does not recognise should narrow an agent's
    reach, never widen it.
    """
    return _RISK_LEVEL_TO_TOOL_RISK.get(risk_level, "read_only")


_TOOL_RISK_ORDER = {
    "read_only": 0,
    "low_risk_write": 1,
    "external_side_effect": 2,
    "privileged": 3,
    "destructive": 4,
}
_RISK_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
    "privileged": 4,
}


def _tool_risk_order(value: ToolRisk | str) -> int:
    return _TOOL_RISK_ORDER[value.value if isinstance(value, ToolRisk) else value]


def _as_risk(value: str) -> RiskLevel:
    return RiskLevel(value)


def _as_skill_id(value: str) -> SkillId:
    return SkillId(value)


def _profile_from_role(role: Role) -> AuthorityProfile:
    """Rebuild the domain authority profile from its stored form.

    A malformed stored profile yields an empty grant set, which denies
    everything. That is the correct failure direction: a corrupted profile must
    reduce an agent's authority to nothing, never to everything.
    """
    from ai_orchestrator.domain.authority import SUBAGENT_FLOOR_PROFILE

    raw = role.authority_profile or {}
    grants: list[AuthorityGrant] = []
    for entry in raw.get("grants", []):
        try:
            grants.append(
                AuthorityGrant(
                    action=AuthorityAction(entry["action"]),
                    scope=entry.get("scope", "task"),
                    resource_type=entry.get("resource_type", "*"),
                    resource_id=entry.get("resource_id"),
                    allowed_effects=frozenset(
                        EffectClass(e) for e in entry.get("allowed_effects", [])
                    ),
                    max_risk=RiskLevel(entry.get("max_risk", "high")),
                    data_classification_ceiling=DataClassification(
                        entry.get("data_classification_ceiling", "internal")
                    ),
                )
            )
        except KeyError, ValueError:
            # Skip an unreadable grant rather than failing the whole profile.
            continue

    if not grants:
        return SUBAGENT_FLOOR_PROFILE

    return AuthorityProfile(
        role_id=role.id,
        grants=frozenset(grants),
        max_autonomy=AutonomyLevel(role.max_autonomy),
        may_delegate_to_peers=role.may_delegate_to_peers,
        may_spawn_subagents=role.may_spawn_subagents,
        max_delegation_depth=role.max_delegation_depth,
    )


__all__ = ["ExecutionOutcome", "ResolvedAgent", "TaskExecutionService"]
