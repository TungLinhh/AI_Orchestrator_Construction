"""Feedback is a bounded proposal, never an approval or an execution receipt."""

import re
from collections.abc import Sequence
from typing import Any


def validate_feedback(
    output: dict[str, Any],
    allowed_owners: set[str],
    stage_keys: Sequence[str] | None = None,
    human_stage_keys: set[str] | None = None,
) -> None:
    if set(output) != {"understanding", "questions", "plan", "skill_lesson"}:
        raise ValueError("Return exactly understanding, questions, plan and skill_lesson")
    questions, plan = output["questions"], output["plan"]
    if not isinstance(questions, list) or len(questions) > 5:
        raise ValueError("Ask at most five unanswered critical questions")
    if not isinstance(plan, list) or not 1 <= len(plan) <= 12:
        raise ValueError("Include one to twelve concrete planned actions")
    text = [output["understanding"], output["skill_lesson"], *questions]
    previous = -1
    for step in plan:
        if not isinstance(step, dict) or set(step) != {
            "stage_key",
            "action",
            "owner",
            "acceptance",
        }:
            raise ValueError("Every action needs stage_key, owner and measurable acceptance")
        if stage_keys is not None:
            if step["stage_key"] not in stage_keys:
                raise ValueError("Use only stage_key values from the actual workflow catalog")
            current = stage_keys.index(step["stage_key"])
            if current <= previous:
                raise ValueError("Keep proposed actions in workflow order; do not duplicate stages")
            previous = current
        if step["owner"] not in allowed_owners:
            raise ValueError("Use an owner from the provided workflow owner list")
        if step["stage_key"] in (human_stage_keys or set()) and step["owner"] not in {
            "HR",
            "Design",
            "Procurement",
            "QA/QC",
            "Boss",
            "CEO",
        }:
            raise ValueError("A review gate needs a human role owner, never an agent")
        text.extend(step.values())
    for value in text:
        if not isinstance(value, str) or len(value.strip()) < 2:
            raise ValueError("Feedback fields must contain meaningful text")
        if re.search(r"</?(?:arg_key|arg_value|tool_call|parameter)\b", value, re.I):
            raise ValueError("Replace protocol placeholders with actual questions or actions")
        if re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("Use Vietnamese consistently for this feedback discussion")
        if "\ufffd" in value:
            raise ValueError("Replace damaged Unicode text with readable Vietnamese")
