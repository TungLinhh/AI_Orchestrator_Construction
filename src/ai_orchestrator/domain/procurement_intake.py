"""Source-bearing procurement inputs; live intake cannot import synthetic receipts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=1000)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Amount = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def refuse_fixture_refs(cls, value: object) -> object:
        if isinstance(value, str) and value.lower().startswith(("fixture:", "synthetic:", "test:")):
            raise ValueError("Live intake requires actual sources, not fixture references")
        return value


class Material(SourceModel):
    material_id: Identifier
    name: Text
    quantity: Amount
    unit: Text
    technical_spec: Annotated[str, Field(min_length=5, max_length=5000)]
    long_lead: bool = False


class Quote(SourceModel):
    material_id: Identifier
    unit_price: Annotated[int, Field(gt=0, le=9000000000000, strict=True)]
    quantity: Amount
    currency: Literal["VND"] = "VND"
    delivery_days: Annotated[int, Field(ge=0, le=3650)]
    warranty_months: Annotated[int, Field(ge=0, le=1200)]
    certificate_ref: str = Field(default="", max_length=1000)
    spec_compliant: bool


class Supplier(SourceModel):
    supplier_id: Identifier
    synthetic: Literal[False] = False
    legal_valid: bool
    legal_ref: Text
    financial_ref: Text
    hse_ref: Text
    quote_ref: Text
    financial_health: Text
    hse: Text
    quotes: list[Quote] = Field(min_length=1, max_length=100)


class ProcurementIntake(SourceModel):
    boss_brief: Annotated[str, Field(min_length=20, max_length=10000)]
    boq_source: Text
    material_review_owner: Text
    need_date: date
    g3_date: date
    boq: list[Material] = Field(min_length=1, max_length=100)
    suppliers: list[Supplier] = Field(min_length=3, max_length=20)

    @model_validator(mode="after")
    def complete_sources(self) -> ProcurementIntake:
        materials = {row.material_id: row.quantity for row in self.boq}
        if len(materials) != len(self.boq):
            raise ValueError("BOQ material IDs must be unique")
        if len({s.supplier_id for s in self.suppliers}) != len(self.suppliers):
            raise ValueError("Supplier IDs must be unique")
        for supplier in self.suppliers:
            quoted = {q.material_id: q.quantity for q in supplier.quotes}
            if len(quoted) != len(supplier.quotes) or quoted != materials:
                raise ValueError(
                    "Each supplier must quote every BOQ line exactly once at its quantity"
                )
        if self.need_date > self.g3_date:
            raise ValueError("Materials must be needed no later than G3")
        return self
