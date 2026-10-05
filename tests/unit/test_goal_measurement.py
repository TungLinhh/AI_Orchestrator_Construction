"""The live report must distinguish a required pause from an autonomous completion."""

from dataclasses import replace

import pytest
from scripts.measure_goal_queue import expected_review_pause

from ai_orchestrator.application.pipeline import PipelineOutcome
from ai_orchestrator.application.scenarios import SCENARIOS


def _pause():
    scenario = next(s for s in SCENARIOS if s.key == "hiring-pipeline")
    outcome = PipelineOutcome(
        root_task_id="root",
        root_status="running",
        waiting_for_human=["hr"],
        human_exceptions=[
            {"task_id": "hr", "class": "agent_request", "reason": "Review JD salary"}
        ],
    )
    owned = [
        {
            "task_id": "hr",
            "status": "waiting_for_approval",
            "returned_keys": ["jd"],
            "draft_review_ready": True,
        }
    ]
    statuses = {"root": "running", "office": "running", "hr": "waiting_for_approval"}
    return scenario, outcome, owned, statuses


def test_a_required_review_with_its_draft_is_an_expected_pause():
    assert expected_review_pause(*_pause())


@pytest.mark.parametrize(
    "fault", ["no_draft", "later_stage", "escalation", "unrun_work", "routine", "unrelated_gate"]
)
def test_failures_and_work_after_the_gate_cannot_count_as_an_expected_pause(fault):
    scenario, outcome, owned, statuses = _pause()
    if fault == "no_draft":
        owned[0]["draft_review_ready"] = False
    elif fault == "later_stage":
        owned[0]["returned_keys"].append("shortlist")
        owned[0]["work_past_review"] = True
    elif fault == "escalation":
        outcome.human_exceptions[0]["class"] = "failed_escalation"
    elif fault == "unrun_work":
        statuses["another"] = "assigned"
    elif fault == "routine":
        scenario = replace(scenario, human_review_field="")
    elif fault == "unrelated_gate":
        outcome.human_exceptions[0]["task_id"] = "other"
    assert not expected_review_pause(scenario, outcome, owned, statuses)


def test_null_later_fields_and_status_metadata_do_not_claim_later_work():
    scenario, outcome, owned, statuses = _pause()
    owned[0]["returned_keys"] = ["jd", "rubric", "shortlist", "status"]
    owned[0]["work_past_review"] = False
    assert expected_review_pause(scenario, outcome, owned, statuses)


def test_a_sibling_finishing_past_the_gate_invalidates_the_expected_pause():
    scenario, outcome, owned, statuses = _pause()
    owned.append(
        {
            "task_id": "bypass",
            "status": "completed",
            "returned_keys": ["jd", "rubric", "shortlist"],
            "work_past_review": True,
        }
    )
    statuses["bypass"] = "completed"
    assert not expected_review_pause(scenario, outcome, owned, statuses)
