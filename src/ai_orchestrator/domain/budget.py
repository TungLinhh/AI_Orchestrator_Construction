"""Budget accounting.

Budgets are a correctness concern, not a reporting nicety: an agent loop that
keeps calling a paid model until it runs out of the org's money is a denial of
service against the operator. So the check happens *before* the call, from
pre-flight estimates, and again after the call from actuals.

The ledger is append-only. A corrected entry is a new row that reverses the
old one, never an update — otherwise the audit trail cannot explain how the
org arrived at its current spend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from ai_orchestrator.domain.errors import BudgetExceeded, ValidationError

# Decimal all the way through. Binary floats accumulate error in money
# comparisons, and "is this over budget by one cent" must be answerable
# exactly. Floats appear only at the API edge, already rounded.
Money = Decimal

#: Six decimal places, not two. A single model call can cost $0.0004, and
#: rounding that to $0.00 means a thousand cheap calls report as free. Six also
#: matches the NUMERIC(18, 6) columns, so no precision is lost on the way into
#: the ledger. Rounding to a currency unit belongs at the presentation boundary,
#: not in the arithmetic.
_MONEY_QUANTUM = Decimal("0.000001")


class BudgetScope(StrEnum):
    ORGANIZATION = "organization"
    DEPARTMENT = "department"
    AGENT = "agent"
    TASK = "task"
    SUBAGENT = "subagent"
    MODEL = "model"
    WORKFLOW = "workflow"


class BudgetPeriod(StrEnum):
    """Budgets reset on a period. `LIFETIME` never resets — used for a subagent
    that must not outlive its parent's budget regardless of wall-clock time."""

    HOURLY = "hourly"
    DAILY = "daily"
    MONTHLY = "monthly"
    LIFETIME = "lifetime"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Providers that expose it (extended thinking, reasoning models) report this
    # separately. It is billed, so it must be counted and budgeted.
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            if getattr(self, name) < 0:
                msg = f"{name} must be >= 0"
                raise ValidationError(msg, details={"field": name})

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.reasoning_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_tokens + other.reasoning_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total,
        }


#: The one immutable zero. A default that calls `Money("0")` is a call in a
#: default, which is the same shape as the shared-mutable-default bug a linter
#: warns about. `Money` is a `Decimal` subclass and therefore immutable, so a
#: single shared instance is correct and cheap.
ZERO = Money("0")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per 1M tokens, as published by the provider."""

    input_per_mtok: Money
    output_per_mtok: Money
    reasoning_per_mtok: Money | None = None
    cache_read_per_mtok: Money = ZERO
    cache_write_per_mtok: Money = ZERO

    def cost_of(self, usage: TokenUsage) -> Money:
        m = Decimal(1_000_000)
        cost = usage.input_tokens * self.input_per_mtok / m
        cost += usage.output_tokens * self.output_per_mtok / m
        if usage.reasoning_tokens:
            rate = (
                self.reasoning_per_mtok
                if self.reasoning_per_mtok is not None
                else self.output_per_mtok
            )
            cost += usage.reasoning_tokens * rate / m
        cost += usage.cache_read_tokens * self.cache_read_per_mtok / m
        cost += usage.cache_write_tokens * self.cache_write_per_mtok / m
        # Round once, at the end, to the precision the ledger stores.
        return cost.quantize(_MONEY_QUANTUM, rounding="ROUND_HALF_UP")


@dataclass(slots=True)
class BudgetState:
    """A running total against one limit."""

    max_tokens: int
    max_cost_usd: Money
    spent_tokens: int = 0
    spent_cost_usd: Money = field(default_factory=lambda: Money("0"))
    # What the operator reserved but has not yet spent. Lets us refuse a call
    # whose *estimate* would exceed the ceiling, rather than discovering the
    # overspend after the provider has already billed us.
    reserved_tokens: int = 0
    reserved_cost_usd: Money = field(default_factory=lambda: Money("0"))

    def __post_init__(self) -> None:
        self.max_cost_usd = Money(self.max_cost_usd)
        if self.max_tokens < 0:
            msg = "max_tokens must be >= 0"
            raise ValidationError(msg, details={"field": "max_tokens"})
        if self.max_cost_usd < 0:
            msg = "max_cost_usd must be >= 0"
            raise ValidationError(msg, details={"field": "max_cost_usd"})

    @property
    def committed_tokens(self) -> int:
        return self.spent_tokens + self.reserved_tokens

    @property
    def committed_cost_usd(self) -> Money:
        return self.spent_cost_usd + self.reserved_cost_usd

    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.committed_tokens)

    def remaining_cost_usd(self) -> Money:
        return max(Money("0"), self.max_cost_usd - self.committed_cost_usd)

    def check(self, *, est_tokens: int = 0, est_cost_usd: Money | None = None) -> None:
        """Pre-flight gate. Raises `BudgetExceeded` if the call cannot proceed."""
        cost = Money(est_cost_usd) if est_cost_usd is not None else Money("0")
        if self.committed_tokens + est_tokens > self.max_tokens:
            raise BudgetExceeded(
                "token budget exhausted",
                details={
                    "limit_tokens": self.max_tokens,
                    "committed_tokens": self.committed_tokens,
                    "requested_tokens": est_tokens,
                },
            )
        if self.committed_cost_usd + cost > self.max_cost_usd:
            raise BudgetExceeded(
                "cost budget exhausted",
                details={
                    "limit_usd": str(self.max_cost_usd),
                    "committed_usd": str(self.committed_cost_usd),
                    "requested_usd": str(cost),
                },
            )

    def reserve(self, *, tokens: int = 0, cost_usd: Money = ZERO) -> None:
        """Hold funds for an in-flight call. Release or commit after."""
        self.reserved_tokens += tokens
        self.reserved_cost_usd += Money(cost_usd)

    def commit(self, usage: TokenUsage, cost: Money) -> None:
        """Convert a reservation into spend. Actual always wins over the estimate."""
        self.spent_tokens += usage.total
        self.spent_cost_usd += Money(cost)
        self.reserved_tokens = max(0, self.reserved_tokens - usage.total)
        self.reserved_cost_usd = max(Money("0"), self.reserved_cost_usd - Money(cost))

    def release(self, *, tokens: int = 0, cost_usd: Money = ZERO) -> None:
        """Give back an unused reservation (a failed or cancelled call)."""
        self.reserved_tokens = max(0, self.reserved_tokens - tokens)
        self.reserved_cost_usd = max(Money("0"), self.reserved_cost_usd - Money(cost_usd))

    def to_dict(self) -> dict[str, object]:
        return {
            "max_tokens": self.max_tokens,
            "max_cost_usd": str(self.max_cost_usd),
            "spent_tokens": self.spent_tokens,
            "spent_cost_usd": str(self.spent_cost_usd),
            "reserved_tokens": self.reserved_tokens,
            "reserved_cost_usd": str(self.reserved_cost_usd),
            "remaining_tokens": self.remaining_tokens(),
            "remaining_cost_usd": str(self.remaining_cost_usd()),
        }


def estimate_cost(usage: TokenUsage, pricing: ModelPricing) -> Money:
    return pricing.cost_of(usage)


def can_afford(state: BudgetState, *, tokens: int, cost: Money) -> bool:
    try:
        state.check(est_tokens=tokens, est_cost_usd=cost)
    except BudgetExceeded:
        return False
    return True


__all__ = [
    "BudgetPeriod",
    "BudgetScope",
    "BudgetState",
    "ModelPricing",
    "Money",
    "TokenUsage",
    "can_afford",
    "estimate_cost",
]
