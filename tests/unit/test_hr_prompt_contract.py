"""HR standing instructions support custom tasks as well as candidate reviews."""

from ai_orchestrator.seed import SYSTEM_PROMPTS


def test_hr_deliverable_follows_task_and_scoring_remains_conditional():
    prompt = SYSTEM_PROMPTS["HR Director"]
    assert "deliverable requested by the current task" in prompt
    assert "Your deliverable is an assessment" not in prompt
    assert "When you assess a candidate, score each stated requirement separately" in prompt
    assert "unverified" in prompt
    assert "not authorised to access" in prompt
