"""A delegated child is not a duplicate of the task it was delegated from.

Found by running the work rather than by reading the code: the child's dedup key
hashed the goal alone, and a child inherits its parent's goal, so the second hop
of every chain matched its own parent and was refused. **A three-tier tree could
not carry work two tiers** -- CEO to office worked, office to department never
did, and the refusal named the parent as the task already in progress, which
reads as a sensible de-duplication rather than as a chain that stops at one hop.

Against the database, because the constraint is part of the behaviour: the
Python check and `uq_tasks_active_dedup_key` have to agree, and only a real
insert proves they do.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_a_child_may_repeat_its_parents_goal(tenant) -> None:  # type: ignore[no-untyped-def]
    """The second hop of a chain must not collide with the first.

    Both tasks carry the same goal, which is what delegation does: the office is
    handed the CEO's objective and hands it on. Before this was fixed, `create`
    raised `ConflictError` and the insert would have violated
    `uq_tasks_active_dedup_key` anyway, so the delegation was reported as the
    platform declining to delegate -- with a message naming the parent as the
    task already in progress.
    """
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    org_id = tenant.organization_id
    tasks = TaskRepository(tenant.session, org_id)

    goal = "Reconcile the tender documents for the Bãi Trầm project"
    parent = await tasks.create(title="Tender reconciliation", goal=goal)
    child = await tasks.create(
        title="Tender reconciliation",
        goal=goal,
        parent_task_id=parent.id,
        owner_agent_id=parent.owner_agent_id,
    )

    assert child.id != parent.id
    assert child.fingerprint == parent.fingerprint, (
        "the goal is the same, so this assertion documents *why* the key has to "
        "be more than the goal"
    )
    assert child.dedup_key != parent.dedup_key


async def test_two_siblings_with_the_same_goal_are_still_caught(tenant) -> None:  # type: ignore[no-untyped-def]
    """The guard the dedup key exists for must survive the fix.

    Scoping the key to the parent is only safe if the sibling case still fails.
    A dedup that catches nothing is not a weaker guard, it is no guard, and it
    would look identical in a run that only ever delegates.
    """
    from ai_orchestrator.domain.errors import ConflictError
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    org_id = tenant.organization_id
    tasks = TaskRepository(tenant.session, org_id)

    goal = "Chase the outstanding invoices for August"
    parent = await tasks.create(title="Invoice chase", goal=goal)
    await tasks.create(title="Invoice chase", goal=goal, parent_task_id=parent.id)

    with pytest.raises(ConflictError, match="equivalent task is already active"):
        await tasks.create(title="Invoice chase", goal=goal, parent_task_id=parent.id)


async def test_the_same_goal_from_two_roots_is_still_caught(tenant) -> None:  # type: ignore[no-untyped-def]
    """Two independent requests of the same work, with no shared parent, collide.

    The scope is `parent_task_id or "root"`, so this is the case where the scope
    does not separate them and the goal has to.
    """
    from ai_orchestrator.domain.errors import ConflictError
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    tasks = TaskRepository(tenant.session, tenant.organization_id)
    goal = "Draft the change order for the Bãi Trầm foundation package"

    await tasks.create(title="Change order", goal=goal)
    with pytest.raises(ConflictError, match="equivalent task is already active"):
        await tasks.create(title="Change order", goal=goal)
