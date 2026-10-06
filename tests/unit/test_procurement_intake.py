"""Malformed or synthetic source intake cannot create a live campaign."""

import copy

import pytest
from pydantic import ValidationError

from ai_orchestrator.application.workflow_fixtures import procurement_fixture
from ai_orchestrator.domain.procurement_intake import ProcurementIntake


def source_intake():
    """Fictional source declarations for schema tests only, never business evidence."""
    brief = procurement_fixture()
    brief.pop("synthetic")
    brief.pop("delivery")
    brief.update(
        boq_source="mock-documents/boq-v1.pdf", material_review_owner="Mock materials reviewer"
    )
    for supplier in brief["suppliers"]:
        supplier["synthetic"] = False
        supplier["quote_ref"] = "mock-documents/quotation-" + supplier["supplier_id"]
        for key in ("legal_ref", "financial_ref", "hse_ref"):
            supplier[key] = supplier[key].replace("fixture:", "mock-documents/")
        for quote in supplier["quotes"]:
            quote["certificate_ref"] = quote["certificate_ref"].replace(
                "fixture:", "mock-documents/"
            )
    return brief


def test_procurement_source_intake_keeps_qa_refusals_and_human_owner():
    result = ProcurementIntake.model_validate(source_intake())
    assert len(result.boq) == 3 and len(result.suppliers) == 3
    assert result.suppliers[-1].legal_valid is False
    assert result.suppliers[-1].quotes[0].certificate_ref == ""
    assert result.material_review_owner == "Mock materials reviewer"


@pytest.mark.parametrize(
    "problem",
    [
        "duplicate_material",
        "duplicate_supplier",
        "missing_quote",
        "unknown_material",
        "wrong_quantity",
        "fixture",
        "synthetic",
        "delivery",
        "date",
        "fractional_money",
    ],
)
def test_invalid_intake_is_rejected(problem):
    data = copy.deepcopy(source_intake())
    if problem == "duplicate_material":
        data["boq"].append(data["boq"][0])
    elif problem == "duplicate_supplier":
        data["suppliers"].append(data["suppliers"][0])
    elif problem == "missing_quote":
        data["suppliers"][0]["quotes"].pop()
    elif problem == "unknown_material":
        data["suppliers"][0]["quotes"][0]["material_id"] = "UNKNOWN"
    elif problem == "wrong_quantity":
        data["suppliers"][0]["quotes"][0]["quantity"] += 1
    elif problem == "fixture":
        data["suppliers"][0]["quote_ref"] = "fixture:quotation"
    elif problem == "synthetic":
        data["suppliers"][0]["synthetic"] = True
    elif problem == "delivery":
        data["delivery"] = {"claimed": "Already received"}
    elif problem == "fractional_money":
        data["suppliers"][0]["quotes"][0]["unit_price"] = 1.5
    else:
        data["need_date"] = "2027-01-01"
    with pytest.raises(ValidationError):
        ProcurementIntake.model_validate(data)
