"""Delegation safety: the rules that stop runaway agent trees.

These are the tests that matter most in the platform. An agent that can
delegate without bound is a fork bomb with a language model attached, and the
failure is a bill rather than an error message.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.domain.delegation import (
    DelegationLimits,
    DelegationPath,
    assess_duplicate_work,
    authorize_delegation,
    require_delegation_allowed,
    task_fingerprint,
)
from ai_orchestrator.domain.errors import CycleDetected, PreconditionError
from ai_orchestrator.domain.ids import AgentId, TaskId


def _limits(**overrides: int | float) -> DelegationLimits:
    base: dict[str, int | float] = {
        "max_depth": 4,
        "max_fanout": 8,
        "max_active_descendants": 16,
        "max_tokens": 200_000,
        "max_cost_usd": 5.0,
        "max_runtime_s": 900,
    }
    base.update(overrides)
    return DelegationLimits(**base)  # type: ignore[arg-type]


def _path(*agent_ids: AgentId) -> DelegationPath:
    path = DelegationPath.root(agent_ids[0], TaskId.create())
    for agent in agent_ids[1:]:
        path = path.extend(agent, TaskId.create())
    return path


class TestCycleDetection:
    def test_direct_self_delegation_is_blocked(self) -> None:
        agent = AgentId.create()
        path = DelegationPath.root(agent, TaskId.create())
        verdict = authorize_delegation(
            source=agent,
            target=agent,
            path=path,
            requested=None,
            parent_limits=_limits(),
            platform_limits=_limits(),
        )
        assert not verdict.allowed
        assert "itself" in verdict.reason

    def test_two_hop_cycle_a_b_a_is_blocked(self) -> None:
        a, b = AgentId.create(), AgentId.create()
        path = _path(a, b)
        verdict = authorize_delegation(
            source=b,
            target=a,
            path=path,
            requested=None,
            parent_limits=_limits(),
            platform_limits=_limits(),
        )
        assert not verdict.allowed
        assert "cycle" in verdict.reason.lower()
        assert str(a) in verdict.reason, "the denial must name the agent that closed the loop"

    def test_three_hop_cycle_a_b_c_a_is_blocked(self) -> None:
        """Acceptance scenario 5.

        Depth alone does not catch this: the path is only 3 deep, well under
        the limit of 4. Only the ancestor-path check stops it.
        """
        a, b, c = AgentId.create(), AgentId.create(), AgentId.create()
        path = _path(a, b, c)
        verdict = authorize_delegation(
            source=c,
            target=a,
            path=path,
            requested=None,
            parent_limits=_limits(),
            platform_limits=_limits(),
        )
        assert not verdict.allowed
        assert "cycle" in verdict.reason.lower()

    def test_cycle_detection_survives_a_four_hop_revisit(self) -> None:
        a, b, c, d = (AgentId.create() for _ in range(4))
        # A -> B -> C -> D -> B : revisits B from depth 2
        path = _path(a, b, c, d)
        verdict = authorize_delegation(
            source=d,
            target=b,
            path=path,
            requested=None,
            parent_limits=_limits(),
            platform_limits=_limits(),
        )
        assert not verdict.allowed
        assert "cycle" in verdict.reason.lower()

    def test_require_helper_raises_cycle_error(self) -> None:
        a, b = AgentId.create(), AgentId.create()
        with pytest.raises(CycleDetected):
            require_delegation_allowed(
                source=b,
                target=a,
                path=_path(a, b),
                requested=None,
                parent_limits=_limits(),
                platform_limits=_limits(),
            )


class TestDepthLimit:
    def test_delegation_beyond_max_depth_is_blocked(self) -> None:
        a, b, c = (AgentId.create() for _ in range(3))
        path = _path(a, b, c)  # depth 3
        verdict = authorize_delegation(
            source=c,
            target=AgentId.create(),
            path=path,
            requested=None,
            parent_limits=_limits(max_depth=3),
            platform_limits=_limits(),
        )
        assert not verdict.allowed
        assert "depth" in verdict.reason

    def test_legitimate_deep_chain_is_allowed(self) -> None:
        a, b, c = (AgentId.create() for _ in range(3))
        verdict = authorize_delegation(
            source=c,
            target=AgentId.create(),
            path=_path(a, b, c),
            requested=None,
            parent_limits=_limits(max_depth=4),
            platform_limits=_limits(),
        )
        assert verdict.allowed


class TestFanoutAndDescendantCaps:
    def test_fanout_cap_blocks_the_sibling_burst(self) -> None:
        a = AgentId.create()
        verdict = authorize_delegation(
            source=a,
            target=AgentId.create(),
            path=_path(a),
            requested=None,
            parent_limits=_limits(max_fanout=3),
            platform_limits=_limits(),
            current_fanout=3,
        )
        assert not verdict.allowed
        assert "fan-out" in verdict.reason

    def test_active_descendant_cap_blocks_the_broad_tree(self) -> None:
        a = AgentId.create()
        verdict = authorize_delegation(
            source=a,
            target=AgentId.create(),
            path=_path(a),
            requested=None,
            parent_limits=_limits(max_active_descendants=16),
            platform_limits=_limits(),
            current_active_descendants=16,
        )
        assert not verdict.allowed
        assert "active descendants" in verdict.reason


class TestBudgetNarrowing:
    def test_child_cannot_widen_its_own_envelope(self) -> None:
        """A subagent must never hand itself more budget than its parent holds."""
        parent = _limits(max_tokens=1_000, max_cost_usd=1.0, max_depth=2)
        requested = _limits(max_tokens=10_000_000, max_cost_usd=9_999.0, max_depth=9)
        effective = requested.clamp_to(parent)
        assert effective.max_tokens == 1_000
        assert effective.max_cost_usd == 1.0
        assert effective.max_depth == 2

    def test_authorize_returns_the_clamped_envelope(self) -> None:
        a, b = AgentId.create(), AgentId.create()
        verdict = authorize_delegation(
            source=a,
            target=b,
            path=_path(a),
            requested=_limits(max_tokens=10_000_000),
            parent_limits=_limits(max_tokens=5_000),
            platform_limits=_limits(max_tokens=100_000),
        )
        assert verdict.allowed
        assert verdict.effective_limits.max_tokens == 5_000

    def test_platform_ceiling_overrides_a_generous_parent(self) -> None:
        """Even if a role profile is misconfigured, the platform ceiling holds."""
        a, b = AgentId.create(), AgentId.create()
        verdict = authorize_delegation(
            source=a,
            target=b,
            path=_path(a),
            requested=None,
            parent_limits=_limits(max_tokens=10_000_000),
            platform_limits=_limits(max_tokens=1_000),
        )
        assert verdict.effective_limits.max_tokens == 1_000


class TestDuplicateWorkDetection:
    def test_equivalent_goals_produce_the_same_fingerprint(self) -> None:
        """Two agents describing the same job differently must collide."""
        args = {"organization_id": "org_1", "task_type": "research"}
        assert task_fingerprint(goal="Prepare a market analysis.", **args) == task_fingerprint(
            goal="prepare  the MARKET analysis", **args
        )

    def test_different_goals_do_not_collide(self) -> None:
        args = {"organization_id": "org_1", "task_type": "research"}
        assert task_fingerprint(goal="Prepare a market analysis.", **args) != task_fingerprint(
            goal="Audit the payroll ledger", **args
        )

    def test_different_organizations_never_collide(self) -> None:
        """Cross-tenant isolation must hold for dedup too, or one org's work
        silently suppresses another's."""
        assert task_fingerprint(
            organization_id="org_a", task_type="research", goal="analyse revenue"
        ) != task_fingerprint(organization_id="org_b", task_type="research", goal="analyse revenue")

    def test_duplicate_is_detected_against_active_work(self) -> None:
        fp = task_fingerprint(organization_id="org_1", task_type="research", goal="analyse revenue")
        existing = TaskId.create()
        result = assess_duplicate_work(fingerprint=fp, active_tasks=[(existing, fp)])
        assert result.is_duplicate
        assert result.existing_task_id == existing
        assert str(existing) in result.reason

    def test_parallel_work_must_be_requested_explicitly(self) -> None:
        """Two agents doing the same thing is sometimes correct — but only when
        someone said so."""
        fp = task_fingerprint(organization_id="org_1", task_type="research", goal="analyse revenue")
        result = assess_duplicate_work(
            fingerprint=fp, active_tasks=[(TaskId.create(), fp)], allow_parallel=True
        )
        assert not result.is_duplicate
        assert "explicitly allowed" in result.reason


def test_delegation_path_preserves_the_full_route() -> None:
    """The path is written to the audit row, so it must show the real route."""
    a, b, c = (AgentId.create() for _ in range(3))
    path = _path(a, b, c)
    assert path.depth == 3
    assert path.agent_ids == (a, b, c)
    assert [step["depth"] for step in path.to_dict()] == [1, 2, 3]


def test_rejected_delegation_raises_a_precondition_error() -> None:
    a = AgentId.create()
    with pytest.raises(PreconditionError):
        require_delegation_allowed(
            source=a,
            target=a,
            path=_path(a),
            requested=None,
            parent_limits=_limits(),
            platform_limits=_limits(),
        )
