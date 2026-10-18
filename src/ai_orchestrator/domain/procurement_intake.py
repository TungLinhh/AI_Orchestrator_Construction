"""Source-bearing procurement inputs; live intake cannot import synthetic receipts."""

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=1000)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]
Amount = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


FIXTURE_PREFIXES = ("fixture:", "synthetic:", "test:")


def fixture_references(value: object) -> list[str]:
    """Walk every string in a dumped model and return fixture-labelled values."""
    found: list[str] = []
    if isinstance(value, str):
        if value.lower().startswith(FIXTURE_PREFIXES):
            found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(fixture_references(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(fixture_references(item))
    return found


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
    synthetic: bool = False
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
    # A live intake is real by default. Only an explicitly synthetic acceptance
    # exercise may carry fixture-labelled records, and the lab running it stays
    # a live, human-reviewed workflow -- never a real purchase.
    synthetic: bool = Field(default=False, strict=True)
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
        if not self.synthetic:
            if any(supplier.synthetic for supplier in self.suppliers):
                raise ValueError(
                    "Live intake requires actual sources, not synthetic supplier records"
                )
            references = fixture_references(self.model_dump())
            if references:
                raise ValueError(
                    "Live intake requires actual sources, not fixture references: " + references[0]
                )
        return self
