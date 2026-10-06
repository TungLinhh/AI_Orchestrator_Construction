"""Reviewable department blueprints; no authority is granted by model output."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=12000)]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")]


class BlueprintStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Key
    title: Annotated[str, Field(min_length=1, max_length=180)]
    instructions: Text
    input_keys: list[Key] = Field(min_length=1, max_length=20)
    output_fields: list[Key] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[Text] = Field(min_length=1, max_length=10)
    source_sop_codes: list[str] = Field(min_length=1, max_length=20)
    source_step_refs: list[str] = Field(default_factory=list, max_length=60)
    human_review: bool


class AgentBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Text
    system_instructions: Text
    responsibilities: list[Text] = Field(min_length=1, max_length=30)
    required_inputs: dict[Key, Text] = Field(min_length=1, max_length=20)
    steps: list[BlueprintStep] = Field(min_length=3, max_length=30)

    @model_validator(mode="after")
    def ordered_complete(self) -> AgentBlueprint:
        keys = [s.key for s in self.steps]
        if len(set(keys)) != len(keys):
            raise ValueError("Workflow step keys must be unique")
        if not self.steps[-1].human_review:
            raise ValueError("The final artifact must have a human review")
        outputs = {key for step in self.steps for key in step.output_fields}
        if outputs & self.required_inputs.keys():
            raise ValueError("A generated output cannot also be a required source input")
        available = set(self.required_inputs)
        for step in self.steps:
            missing = set(step.input_keys) - available
            if missing:
                raise ValueError(
                    "Step "
                    + step.key
                    + " references unavailable inputs "
                    + str(sorted(missing))
                    + "; use source inputs or outputs from earlier steps: "
                    + str(sorted(available))
                )
            available.update(step.output_fields)
            if len(set(step.output_fields)) != len(step.output_fields):
                raise ValueError("Output field keys must be unique")
        return self

    def validate_sources(
        self, available_codes: set[str], available_steps: set[str] | None = None
    ) -> None:
        used = {code for step in self.steps for code in step.source_sop_codes}
        if not available_codes or used != available_codes:
            raise ValueError("The workflow must cover every supplied SOP, without invented codes")
        if available_steps is not None:
            references = {ref for step in self.steps for ref in step.source_step_refs}
            if references != available_steps:
                raise ValueError("Cover every supplied source step, without invented references")
            for step in self.steps:
                if any(
                    ref.rsplit("#", 1)[0] not in step.source_sop_codes
                    for ref in step.source_step_refs
                ):
                    raise ValueError("A source step reference must belong to the step's SOP codes")
