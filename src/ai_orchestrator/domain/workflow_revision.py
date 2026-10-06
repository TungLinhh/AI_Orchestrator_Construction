"""Explicit hiring brief changes. Evidence and permissions never come from edits."""

from datetime import date
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HiringBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position: Annotated[str, Field(min_length=3, max_length=200)]
    boss_brief: Annotated[str, Field(min_length=20, max_length=10000)]
    salary_min: Annotated[int, Field(gt=0, strict=True)]
    salary_max: Annotated[int, Field(gt=0, strict=True)]
    start_date: date

    @model_validator(mode="after")
    def salary_range(self) -> HiringBrief:
        if self.salary_min > self.salary_max:
            raise ValueError("Minimum salary must not exceed maximum salary")
        return self


def brief_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"field": key, "before": before.get(key), "after": value}
        for key, value in after.items()
        if before.get(key) != value
    ]
