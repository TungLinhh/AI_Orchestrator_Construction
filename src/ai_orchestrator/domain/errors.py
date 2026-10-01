"""Normalised error model.

One error vocabulary for the whole platform. When a service invents its own
error shape, the API layer has to guess, the UI has to guess, and the retry
policy has to guess. So the taxonomy lives here and everything raises from it.

The split that matters operationally:

    ErrorKind.RETRYABLE          -> the caller may transparently retry
    ErrorKind.REQUIRES_APPROVAL  -> a human must decide; retrying is pointless
    ErrorKind.TERMINAL           -> retrying is guaranteed to fail again

`classify()` is the single place that decides which bucket an error falls in,
so retry policy is a data question rather than a per-callsite judgement.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    """How the platform is allowed to react."""

    VALIDATION = "VALIDATION"
    AUTHORIZATION = "AUTHORIZATION"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PRECONDITION = "PRECONDITION"
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_DENIED = "TOOL_DENIED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    WORKFLOW_TIMEOUT = "WORKFLOW_TIMEOUT"
    EXTERNAL_SERVICE = "EXTERNAL_SERVICE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CANCELED = "CANCELED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    DEADLOCK = "DEADLOCK"
    CYCLE_DETECTED = "CYCLE_DETECTED"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    INTERNAL = "INTERNAL"


# The single retry-decision table. Anything not listed is treated as terminal,
# which is the safe default: a blind retry of an authorization denial is a
# security incident, not a retry.
_RETRYABLE: frozenset[ErrorKind] = frozenset(
    {
        ErrorKind.MODEL_RATE_LIMITED,
        ErrorKind.MODEL_UNAVAILABLE,
        ErrorKind.EXTERNAL_SERVICE,
        ErrorKind.DEPENDENCY_FAILURE,
        ErrorKind.AGENT_UNAVAILABLE,
        ErrorKind.TOOL_UNAVAILABLE,
    }
)

# Transient by nature but bounded: retry with backoff a small number of times,
# then surface. Prevents one dead dependency from consuming a whole retry budget.
_BOUNCED_RETRYABLE: frozenset[ErrorKind] = frozenset({ErrorKind.WORKFLOW_TIMEOUT})


class ErrorCategory(StrEnum):
    """Used by telemetry and the evaluation harness (docs/EVALUATION.md)."""

    VALIDATION = "validation_error"
    AUTHORIZATION = "authorization_error"
    POLICY = "policy_error"
    PLANNING = "planning_error"
    DELEGATION = "delegation_error"
    TOOL_SELECTION = "tool_selection_error"
    TOOL_EXECUTION = "tool_execution_error"
    MODEL = "model_error"
    CONTEXT = "context_error"
    MEMORY = "memory_error"
    WORKFLOW = "workflow_error"
    COMMUNICATION = "communication_error"
    DATA = "data_error"
    EXTERNAL = "external_dependency_error"
    BUDGET = "budget_error"
    CONCURRENCY = "concurrency_error"
    PRECONDITION = "precondition_error"
    INTERNAL = "internal_error"


# Enum members bound to module-level names before being used as exception class
# attributes. Assigning `ErrorCategory.X` directly inside an exception class body
# triggers a CPython 3.14 attribute-resolution fault that is independent of the
# enum contents (reproduced on 3.14.7 with a minimal two-enum case). Binding once
# at module scope is both the fix and the cheaper bytecode.
_CAT: dict[str, ErrorCategory] = {}
for _n in (
    "VALIDATION",
    "AUTHORIZATION",
    "POLICY",
    "PLANNING",
    "DELEGATION",
    "TOOL_SELECTION",
    "TOOL_EXECUTION",
    "MODEL",
    "CONTEXT",
    "MEMORY",
    "WORKFLOW",
    "COMMUNICATION",
    "DATA",
    "EXTERNAL",
    "BUDGET",
    "CONCURRENCY",
    "PRECONDITION",
    "INTERNAL",
):
    _CAT[_n] = ErrorCategory[_n]
_KIND: dict[str, ErrorKind] = {}
for _n in (
    "VALIDATION",
    "AUTHORIZATION",
    "POLICY_DENIED",
    "APPROVAL_REQUIRED",
    "NOT_FOUND",
    "CONFLICT",
    "PRECONDITION",
    "AGENT_UNAVAILABLE",
    "TOOL_UNAVAILABLE",
    "TOOL_DENIED",
    "MODEL_UNAVAILABLE",
    "MODEL_RATE_LIMITED",
    "WORKFLOW_TIMEOUT",
    "EXTERNAL_SERVICE",
    "DEPENDENCY_FAILURE",
    "CANCELED",
    "BUDGET_EXCEEDED",
    "DEADLOCK",
    "CYCLE_DETECTED",
    "SANDBOX_VIOLATION",
    "INTERNAL",
):
    _KIND[_n] = ErrorKind[_n]
del _n


class PlatformError(Exception):
    """Base class for every error the platform raises deliberately."""

    kind: ErrorKind = _KIND["INTERNAL"]
    category: ErrorCategory = _CAT["INTERNAL"]
    default_message: str = "an internal error occurred"
    # Default HTTP status for the API layer. Overridden per instance when a
    # subdomain needs different semantics (e.g. 409 for a task conflict).
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.message = message or self.default_message
        # `details` is for the operator. It must never contain a secret or a raw
        # provider payload; callers are responsible for redacting before raising.
        self.details: dict[str, Any] = details or {}
        self.resource_type = resource_type
        self.resource_id = resource_id
        self.cause = cause
        super().__init__(self.message)

    @property
    def is_retryable(self) -> bool:
        return self.kind in _RETRYABLE

    @property
    def is_bounded_retryable(self) -> bool:
        return self.kind in _BOUNCED_RETRYABLE

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "category": self.category.value,
            "message": self.message,
            "details": self.details,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "retryable": self.is_retryable,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(kind={self.kind.value}, message={self.message!r})"


# --------------------------------------------------------------- validation --
class ValidationError(PlatformError):
    kind = _KIND["VALIDATION"]
    category = _CAT["VALIDATION"]
    default_message = "request payload failed validation"
    http_status = 422


class PreconditionError(PlatformError):
    """The request was well-formed but the system's state forbids it."""

    kind = _KIND["PRECONDITION"]
    category = _CAT["PRECONDITION"]
    default_message = "precondition not met"
    http_status = 409


class NotFoundError(PlatformError):
    kind = _KIND["NOT_FOUND"]
    category = _CAT["DATA"]
    default_message = "resource not found"
    http_status = 404


class ConflictError(PlatformError):
    kind = _KIND["CONFLICT"]
    category = _CAT["DATA"]
    default_message = "resource conflict"
    http_status = 409


class TaskConflictError(ConflictError):
    category = _CAT["CONCURRENCY"]
    default_message = "task is not in a state that allows this operation"


class TaskCanceledError(PlatformError):
    kind = _KIND["CANCELED"]
    category = _CAT["WORKFLOW"]
    default_message = "operation canceled"
    http_status = 409


# ------------------------------------------------------------------ policy --
class AuthorizationError(PlatformError):
    kind = _KIND["AUTHORIZATION"]
    category = _CAT["AUTHORIZATION"]
    default_message = "actor is not authorized to perform this action"
    http_status = 403


class PolicyDenied(PlatformError):
    kind = _KIND["POLICY_DENIED"]
    category = _CAT["POLICY"]
    default_message = "policy denied this action"
    http_status = 403


class ApprovalRequired(PlatformError):
    """Not a failure: the correct outcome is a pause and a human decision."""

    kind = _KIND["APPROVAL_REQUIRED"]
    category = _CAT["POLICY"]
    default_message = "this action requires human approval"
    http_status = 202


class BudgetExceeded(PlatformError):
    kind = _KIND["BUDGET_EXCEEDED"]
    category = _CAT["BUDGET"]
    default_message = "budget limit exceeded"
    http_status = 402


class CycleDetected(PlatformError):
    """Delegation would revisit an agent already on the current path."""

    kind = _KIND["CYCLE_DETECTED"]
    category = _CAT["DELEGATION"]
    default_message = "delegation cycle detected"
    http_status = 409


class DeadlockDetected(PlatformError):
    kind = _KIND["DEADLOCK"]
    category = _CAT["CONCURRENCY"]
    default_message = "no progress possible: circular wait or blocked dependency"
    http_status = 409


# ------------------------------------------------------------- dependencies --
class AgentUnavailable(PlatformError):
    kind = _KIND["AGENT_UNAVAILABLE"]
    category = _CAT["COMMUNICATION"]
    default_message = "agent is not available to accept work"
    http_status = 503


class ToolUnavailable(PlatformError):
    kind = _KIND["TOOL_UNAVAILABLE"]
    category = _CAT["TOOL_EXECUTION"]
    default_message = "tool is not available"
    http_status = 503


class ToolDenied(PlatformError):
    kind = _KIND["TOOL_DENIED"]
    category = _CAT["TOOL_EXECUTION"]
    default_message = "tool invocation denied by policy"
    http_status = 403


class ModelUnavailable(PlatformError):
    kind = _KIND["MODEL_UNAVAILABLE"]
    category = _CAT["MODEL"]
    default_message = "model provider unavailable"
    http_status = 503


class ModelRateLimited(PlatformError):
    kind = _KIND["MODEL_RATE_LIMITED"]
    category = _CAT["MODEL"]
    default_message = "model provider rate limited this request"
    http_status = 429


class WorkflowTimeout(PlatformError):
    kind = _KIND["WORKFLOW_TIMEOUT"]
    category = _CAT["WORKFLOW"]
    default_message = "durable operation exceeded its deadline"
    http_status = 504


class ExternalServiceError(PlatformError):
    kind = _KIND["EXTERNAL_SERVICE"]
    category = _CAT["EXTERNAL"]
    default_message = "an external service failed"
    http_status = 502


class DependencyFailure(PlatformError):
    kind = _KIND["DEPENDENCY_FAILURE"]
    category = _CAT["EXTERNAL"]
    default_message = "a required dependency failed"
    http_status = 503


class SandboxViolation(PlatformError):
    kind = _KIND["SANDBOX_VIOLATION"]
    category = _CAT["TOOL_EXECUTION"]
    default_message = "request attempted to escape the sandbox boundary"
    http_status = 403


class InternalError(PlatformError):
    kind = _KIND["INTERNAL"]
    category = _CAT["INTERNAL"]
    default_message = "an internal error occurred"
    http_status = 500


def classify(exc: BaseException) -> ErrorKind:
    """Map any exception to an ErrorKind. Never raises."""
    if isinstance(exc, PlatformError):
        return exc.kind
    name = type(exc).__name__
    if "Timeout" in name or isinstance(exc, TimeoutError):
        return ErrorKind.WORKFLOW_TIMEOUT
    if isinstance(exc, ConnectionError | OSError):
        return ErrorKind.DEPENDENCY_FAILURE
    return ErrorKind.INTERNAL


def should_retry(exc: BaseException, attempt: int, max_attempts: int = 3) -> bool:
    """The platform's one retry decision function.

    Consult this instead of `except: retry` at a callsite — a policy denied or
    a schema error must never be retried, and that is easy to get wrong.
    """
    if attempt >= max_attempts:
        return False
    return classify(exc) in _RETRYABLE


__all__ = [
    "AgentUnavailable",
    "ApprovalRequired",
    "AuthorizationError",
    "BudgetExceeded",
    "ConflictError",
    "CycleDetected",
    "DeadlockDetected",
    "DependencyFailure",
    "ErrorCategory",
    "ErrorKind",
    "ExternalServiceError",
    "InternalError",
    "ModelRateLimited",
    "ModelUnavailable",
    "NotFoundError",
    "PlatformError",
    "PolicyDenied",
    "PreconditionError",
    "SandboxViolation",
    "TaskCanceledError",
    "TaskConflictError",
    "ToolDenied",
    "ToolUnavailable",
    "ValidationError",
    "WorkflowTimeout",
    "classify",
    "should_retry",
]
