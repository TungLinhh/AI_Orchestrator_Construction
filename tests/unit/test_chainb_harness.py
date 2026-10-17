"""Chain-B harness evidence rule: manifest tiers must cover the pipeline steps.

The product walks the tree through the `delegations` table
(`application/work_review._children`); the chain-acceptance harness used to
walk only the `parent_task_id` closure and reported FAIL with "chain never
reached leaf" on round-3 while the steps and delegations in the same receipt
showed the chain had reached the department tier twice. The harness fix adds
`_evidence_gaps` to `run_chain.py`; these are pure unit tests of that rule on
synthetic graphs, no database, no model.
"""

from __future__ import annotations


def _load_helper():
    from scripts.run_chain_acceptance import _evidence_gaps

    return _evidence_gaps


class TestEvidenceGaps:
    def test_a_fully_covered_tree_reports_no_gaps(self) -> None:
        gaps = _load_helper()(
            {"root", "office", "leaf1", "leaf2"},
            ["root", "office", "leaf1", "leaf2"],
            [("root", "office"), ("office", "leaf1"), ("office", "leaf2")],
        )
        assert gaps == []

    def test_a_step_missing_from_manifest_is_reported(self) -> None:
        gaps = _load_helper()(
            {"root", "office"},
            ["root", "office", "leaf1"],
            [("root", "office"), ("office", "leaf1")],
        )
        assert len(gaps) == 2
        assert any("pipeline step" in g and "leaf1" in g for g in gaps)
        assert any("delegation edge" in g and "leaf1" in g for g in gaps)

    def test_a_dangling_delegation_edge_is_reported(self) -> None:
        gaps = _load_helper()(
            {"root", "office"},
            ["root", "office"],
            [("root", "office"), ("office", "ghost")],
        )
        assert len(gaps) == 1
        assert "ghost" in gaps[0]
        assert "delegation edge" in gaps[0]

    def test_empty_inputs_report_no_gaps(self) -> None:
        assert _load_helper()(set(), [], []) == []


def test_leaf_success_does_not_certify_unsettled_root():
    from types import SimpleNamespace

    from scripts.run_chain_acceptance import _completion_gaps

    tasks = [
        {"id": "root", "status": "running", "owner_agent_id": "chief"},
        {"id": "office", "status": "running", "owner_agent_id": "office-agent"},
        {"id": "leaf", "status": "completed", "owner_agent_id": "dept"},
    ]
    outcome = SimpleNamespace(root_status="running", reviews_accepted=0)
    gaps = _completion_gaps(outcome, tasks, "office-agent")
    assert any("root" in gap for gap in gaps)
    assert any("office did not settle" in gap for gap in gaps)
    assert any("accepted review" in gap for gap in gaps)
    assert any("nonterminal" in gap for gap in gaps)
    for task in tasks:
        task["status"] = "completed"
    outcome.root_status = "completed"
    outcome.reviews_accepted = 1
    assert _completion_gaps(outcome, tasks, "office-agent") == []


def test_repaired_office_keeps_failed_attempt_history_without_failing_settlement():
    from types import SimpleNamespace

    from scripts.run_chain_acceptance import _completion_gaps

    tasks = [
        {"id": "root", "status": "completed", "owner_agent_id": "chief"},
        {
            "id": "office-first",
            "status": "failed",
            "owner_agent_id": "office-agent",
            "created_at": "2026-10-16T01:00:00",
        },
        {
            "id": "office-repair",
            "status": "completed",
            "owner_agent_id": "office-agent",
            "created_at": "2026-10-16T02:00:00",
        },
        {"id": "leaf", "status": "completed", "owner_agent_id": "dept"},
    ]
    outcome = SimpleNamespace(root_status="completed", reviews_accepted=1)
    assert _completion_gaps(outcome, tasks, "office-agent") == []
    assert _completion_gaps(outcome, list(reversed(tasks)), "office-agent") == []
    tasks[1]["status"] = "canceled"
    assert _completion_gaps(outcome, tasks, "office-agent") == []
    tasks[2]["status"] = "failed"
    assert "office did not settle completed" in _completion_gaps(outcome, tasks, "office-agent")


def test_final_positive_cost_response_cannot_pass_or_hide_as_blocked():
    from scripts.run_chain_acceptance import _final_verdict

    assert _final_verdict(False, True, [], False) == "PASS"
    assert _final_verdict(False, True, [], True) == "FAIL"
    assert _final_verdict(True, True, [], True) == "FAIL"
    assert _final_verdict(True, True, [], False) == "BLOCKED"
