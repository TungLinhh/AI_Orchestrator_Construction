"""Explicit artifact shapes; free text cannot stand in for scored evidence."""

from typing import Any

S = {"type": "string", "minLength": 1}
N = {"type": "number"}
B = {"type": "boolean"}


def array(item: dict[str, Any], minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": minimum}


def obj(**fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields),
        "additionalProperties": False,
    }


def artifact_schema(key: str) -> dict[str, Any]:
    from ai_orchestrator.domain.hiring_process import JD_SECTIONS

    text_list = array(S)
    line = obj(material_id=S, supplier_id=S, quantity=N, unit_price=N)
    shapes = {
        "jd": obj(
            sections=obj(**dict.fromkeys(JD_SECTIONS, S)),
            requirements=array(S, 1),
            kpis=array(S, 1),
        ),
        "rubric": obj(
            criteria=array(
                obj(
                    key=S,
                    max_points=N,
                    description=S,
                    good=S,
                    average=S,
                    poor=S,
                    evidence_required=S,
                ),
                6,
            ),
            threshold=N,
            missing_evidence_rule=S,
        ),
        "scoring": obj(
            candidates=array(
                obj(
                    candidate_id=S,
                    criteria=array(
                        obj(
                            key=S,
                            level={
                                "type": "string",
                                "enum": ["good", "average", "poor", "missing"],
                            },
                            evidence_quote={
                                "type": "string",
                                "description": (
                                    "Verbatim original-language words from this candidate CV. "
                                    "Do not translate. Empty string if missing."
                                ),
                            },
                            reason=S,
                        ),
                        6,
                    ),
                    strengths=array(S, 1),
                    gaps=array(S, 1),
                    interview_questions=array(S, 1),
                ),
                1,
            )
        ),
        "selection": obj(
            recommended_candidate_id=S, rationale=S, conditions=text_list, alternatives=text_list
        ),
        "offer": obj(
            candidate_id=S,
            offer_draft=S,
            contract_draft=S,
            salary=N,
            start_date=S,
            conditions=text_list,
        ),
        "onboarding_plan": obj(
            day_one=array(obj(owner=S, deliverable=S, acceptance=S), 1),
            day_30=array(obj(owner=S, deliverable=S, acceptance=S), 1),
            day_60=array(obj(owner=S, deliverable=S, acceptance=S), 1),
            day_90=array(obj(owner=S, deliverable=S, acceptance=S), 1),
            mentor=S,
            training=array(S, 1),
            access_request=obj(workspace=S, permissions=array(S, 1), production_access=B),
        ),
        "plan": obj(
            material_plan=array(obj(material_id=S, quantity=N, technical_spec=S, long_lead=B), 1),
            schedule=array(S, 1),
            missing_information=text_list,
        ),
        "rfq": obj(
            rfq=S,
            supplier_ids=array(S, 3),
            quote_register=array(obj(supplier_id=S, material_id=S, quantity=N, unit_price=N), 3),
        ),
        "qualification": obj(
            suppliers=array(
                obj(
                    supplier_id=S,
                    classification={"type": "string", "enum": ["green", "yellow", "red"]},
                    reason=S,
                    legal_review=S,
                    financial_review=S,
                    hse_review=S,
                ),
                3,
            )
        ),
        "quality": obj(
            assessments=array(
                obj(
                    supplier_id=S,
                    material_id=S,
                    accepted=B,
                    reason=S,
                    certificate_ref={"type": "string"},
                ),
                3,
            ),
            missing_evidence=text_list,
        ),
        "comparison": obj(
            comparison=array(
                obj(
                    material_id=S,
                    supplier_id=S,
                    total_price=N,
                    delivery_days=N,
                    warranty_months=N,
                    eligible=B,
                    reason=S,
                ),
                3,
            ),
            recommended_awards=array(line, 1),
            reasons=array(S, 1),
        ),
        "negotiation": obj(
            negotiation_record=array(S, 1), commercial_conditions=array(S, 1), risks=text_list
        ),
    }
    return shapes.get(key, obj())


def validate_shape(value: Any, schema: dict[str, Any], path: str = "artifact") -> None:
    kind = schema.get("type")
    known = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": type(value) is bool,
    }
    if kind and not known[kind]:
        raise ValueError(path + " must be " + kind)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(path + " has an unknown value")
    if kind == "object":
        properties = schema.get("properties", {})
        if any(k not in value for k in schema.get("required", [])):
            raise ValueError(
                path
                + " is missing required fields: "
                + ", ".join(sorted(set(schema["required"]) - set(value)))
            )
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(path + " has undeclared fields")
        for k, v in value.items():
            if k in properties:
                validate_shape(v, properties[k], path + "." + k)
    elif kind == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(path + " has too few records")
        for index, item in enumerate(value):
            validate_shape(item, schema["items"], path + "[" + str(index) + "]")
    elif kind == "string" and len(value.strip()) < schema.get("minLength", 0):
        raise ValueError(path + " needs substantive text")
