"""Blueprints cannot invent source procedures or drop the final human gate."""

import copy

import pytest

from ai_orchestrator.application.agent_blueprints import (
    blueprint_tool_schema,
    configure_free_profile,
)
from ai_orchestrator.domain.agent_blueprint import AgentBlueprint
from ai_orchestrator.domain.errors import PreconditionError
from ai_orchestrator.models.gateway import ModelCandidate, ModelGateway, ModelProfile


def example_plan():
    return {
        "description": "HR department operating plan",
        "system_instructions": "Draft only; humans decide personnel actions.",
        "responsibilities": ["Prepare recruitment artifacts from source evidence"],
        "required_inputs": {"brief": "Boss brief and source documents"},
        "steps": [
            {
                "key": "step_" + str(i),
                "title": "HR step " + str(i),
                "instructions": "Review source documents and prepare evidence",
                "input_keys": ["brief"],
                "output_fields": ["report"],
                "acceptance_criteria": ["Every finding cites source evidence"],
                "source_sop_codes": ["ONX-BO-HR-SOP-004"],
                "human_review": i == 3,
            }
            for i in range(1, 4)
        ],
    }


def test_blueprint_requires_complete_sources_and_final_review():
    plan = AgentBlueprint.model_validate(example_plan())
    plan.validate_sources({"ONX-BO-HR-SOP-004"})
    with pytest.raises(ValueError, match="every supplied SOP"):
        plan.validate_sources({"ONX-BO-HR-SOP-004", "ONX-BO-HR-SOP-005"})
    changed = copy.deepcopy(example_plan())
    changed["steps"][-1]["human_review"] = False
    with pytest.raises(ValueError, match="final artifact"):
        AgentBlueprint.model_validate(changed)
    changed = copy.deepcopy(example_plan())
    changed["steps"][1]["key"] = changed["steps"][0]["key"]
    with pytest.raises(ValueError, match="unique"):
        AgentBlueprint.model_validate(changed)


def test_free_profile_excludes_paid_and_scripted_fallbacks():
    free = ModelCandidate(provider="openrouter", model="test/model:free")
    paid = ModelCandidate(provider="openrouter", model="test/model")
    fake = ModelCandidate(provider="deterministic", model="scripted-1")
    gateway = ModelGateway(
        profiles={
            "primary": ModelProfile(
                name="primary", candidates=(paid, free, fake), fallback_profile="paid"
            )
        }
    )
    configure_free_profile(gateway, "primary")
    p = gateway.profiles["agent-blueprint-free"]
    assert p.candidates == (free,)
    assert p.fallback_profile is None
    gateway = ModelGateway(
        profiles={"primary": ModelProfile(name="primary", candidates=(paid, fake))}
    )
    with pytest.raises(PreconditionError, match="explicitly free"):
        configure_free_profile(gateway, "primary")


def test_tool_schema_has_explicit_properties_without_local_references():
    import json

    schema = blueprint_tool_schema()
    assert '"$ref"' not in json.dumps(schema)
    assert schema["properties"]["required_inputs"]["items"]["required"] == ["key", "description"]
    assert schema["properties"]["steps"]["items"]["required"]
    assert "title" in schema["properties"]["steps"]["items"]["properties"]


def test_plan_cannot_require_its_own_outputs_or_drop_source_steps():
    changed = copy.deepcopy(example_plan())
    changed["required_inputs"]["report"] = "Circular report dependency"
    with pytest.raises(ValueError, match="generated output"):
        AgentBlueprint.model_validate(changed)
    changed = example_plan()
    changed["steps"][0]["source_step_refs"] = ["ONX-BO-HR-SOP-004#1"]
    plan = AgentBlueprint.model_validate(changed)
    plan.validate_sources({"ONX-BO-HR-SOP-004"}, {"ONX-BO-HR-SOP-004#1"})
    with pytest.raises(ValueError, match="every supplied source step"):
        plan.validate_sources({"ONX-BO-HR-SOP-004"}, {"ONX-BO-HR-SOP-004#1", "ONX-BO-HR-SOP-004#2"})


def test_steps_can_use_previous_artifacts_but_not_future_outputs():
    plan = example_plan()
    plan["steps"][1]["input_keys"] = ["report"]
    assert AgentBlueprint.model_validate(plan).steps[1].input_keys == ["report"]
    plan["steps"][0]["input_keys"] = ["report"]
    with pytest.raises(ValueError, match="unavailable inputs"):
        AgentBlueprint.model_validate(plan)
