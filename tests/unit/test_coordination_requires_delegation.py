"""A coordination task must actually be coordinated.

The live model was asked to decompose a purchase requisition across a company
with nine agents, and answered it itself. The seeded instructions already said
"you do not do the work yourself" — the model read them and did not care. So the
rule cannot live in the prompt; it lives here, where it holds regardless of which
model is configured tomorrow.

The negative direction matters as much as the positive one: a rule that punished
an agent for finishing alone would push it into pointless delegation, which is a
worse failure than doing the work.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai_orchestrator.application.delegation_executor import DelegationOutcome
from ai_orchestrator.application.task_execution import coordination_may_complete
from ai_orchestrator.domain.contracts import AgentContext
from ai_orchestrator.domain.ids import TaskId


@dataclass
class _Task:
    """The one field the rule reads. A real `Task` needs a session and a tenant."""

    task_type: str
    id: TaskId


_TWO_DEPARTMENTS = (("Finance Agent", "approves spend"), ("IT Agent", "buys kit"))


def _may_complete(
    task_type: str,
    outcome: DelegationOutcome,
    targets: tuple[tuple[str, str], ...] = _TWO_DEPARTMENTS,
    delegations_made: int = 0,
) -> bool:
    context = AgentContext.model_construct(delegate_targets=targets)
    return coordination_may_complete(
        task=_Task(task_type=task_type, id=TaskId.create()),  # type: ignore[arg-type]
        context=context,
        outcome=outcome,
        delegations_made=delegations_made,
    )


class TestCoordinationRequiresDelegation:
    def test_coordination_with_no_delegation_is_refused(self) -> None:
        assert not _may_complete("coordination", DelegationOutcome())

    def test_coordination_with_an_accepted_delegation_is_allowed(self) -> None:
        outcome = DelegationOutcome(accepted=["fin"], child_task_ids=[TaskId.create()])
        assert _may_complete("coordination", outcome)

    def test_a_refused_delegation_does_not_count(self) -> None:
        """Every proposal coming back refused is not coordination, it is a no-op.

        The platform refused it for a real reason — a cycle, a budget, an agent
        that does not exist. Counting that as decomposition would let a goal close
        with the work undone and a clean audit trail.
        """
        refused = DelegationOutcome(refused=[("Finance Agent", "delegation cycle")])
        assert not _may_complete("coordination", refused)

    def test_a_delegation_already_in_the_database_counts(self) -> None:
        """The tool path creates delegations mid-run and never touches the outcome.

        This is the case that failed a task that had genuinely delegated: the run
        produced an empty `DelegationOutcome` and a real `delegations` row, and the
        rule read only the outcome. Asking the database is the difference between
        a gate and a guess.
        """
        assert _may_complete("coordination", DelegationOutcome(), delegations_made=1)

    def test_the_database_count_alone_does_not_excuse_a_refused_proposal(self) -> None:
        """One real delegation and one refusal is still a coordination.

        The count answers "did any work get delegated", not "was everything
        delegated". A goal whose other three subtasks were dropped is not a
        coordinated goal, and a count cannot tell those apart — so this test
        documents the limit rather than pretending the gate is tighter than it is.
        """
        refused = DelegationOutcome(refused=[("IT Agent", "depth limit")])
        assert _may_complete("coordination", refused, delegations_made=1)


class TestOtherTaskTypesAreUnaffected:
    def test_analysis_may_finish_alone(self) -> None:
        """Forcing delegation here would buy nothing and cost a pointless round trip."""
        assert _may_complete("analysis", DelegationOutcome())

    def test_coordination_with_nobody_to_delegate_to_is_allowed(self) -> None:
        """A leaf agent cannot delegate. Demanding that it try is demanding the impossible."""
        assert _may_complete("coordination", DelegationOutcome(), targets=())
