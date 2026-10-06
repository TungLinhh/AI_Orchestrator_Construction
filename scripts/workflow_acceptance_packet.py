"""Build and verify an original, explicitly fictional document packet for acceptance."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from ai_orchestrator.application.workflow_fixtures import hiring_fixture, procurement_fixture

SOURCES = [
    {
        "title": "Acas recruitment checklist and job description templates",
        "url": "https://www.acas.org.uk/job-description-templates",
        "used_for": "Role purpose, reporting line, duties and person specification fields",
    },
    {
        "title": "Acas job offer templates",
        "url": "https://www.acas.org.uk/job-offer-templates",
        "used_for": "Offer conditions, start date and acceptance fields",
    },
    {
        "title": "Acas planning an induction programme",
        "url": "https://www.acas.org.uk/inductions/planning-an-induction-programme",
        "used_for": "Buddy, equipment, HSE induction, role training and later review fields",
    },
    {
        "title": "World Bank Evaluating Bids and Proposals, February 2025",
        "url": "https://thedocs.worldbank.org/en/doc/"
        "9dcb7971706bf29b2732779c39922b77-0290012025/original/"
        "Evaluating-Bids-and-Proposals-with-Rated-Criteria-Feb-4-2025.pdf",
        "used_for": "Predeclared qualification, technical checks, price comparison and audit trail",
    },
]

STRONG_CV = """SYNTHETIC CV — MEP Test A. All people, employers and projects below are fictional.
Position: MEP engineer. Independent training exercise; not a real job application.
2018\N{EN DASH}2026: Senior MEP engineer at Training Engineering Studio (fictional).
Mechanical: Personally designed HVAC and plumbing for four training buildings. Calculated
cooling loads room by room (total 420 kW), sized chilled-water pumps at 35 m3/h and 28 m head,
checked friction loss and equipment schedules, and signed the internal design review record.
Authored TR-M01 load calculation workbook and TR-M02 pump sizing sheet; peer review closed
all 12 comments. Performed duct sizing and PPR PN20 pressure-loss checks.
Electrical: Personally calculated electrical demand, cable ampacity, voltage drop and protective
device coordination for a 400 kVA training distribution board. Selected copper 4x16mm2 feeders
only after checking installation method, derating and voltage drop; produced TR-E01 calculations,
single-line diagrams, lighting and earthing layouts reviewed by the electrical lead.
BIM coordination: Owned the Revit MEP federated model and Navisworks clash register TR-B01.
Chaired six weekly multidisciplinary coordination meetings; assigned owners and deadlines,
resolved 85 of 90 clashes and recorded the five remaining issues with escalation dates.
Commissioning: Led testing, adjusting and balancing of two training HVAC installations;
measured air/water flows, reconciled measured flows against design tolerances, witnessed plumbing
pressure tests and electrical insulation tests, closed punch lists and authored TR-C01 signed
mock commissioning and handover records. These were sandbox tests, not real-site certification.
HSE: Completed mock electrical isolation/LOTO and working-at-height training. Led pre-task
risk assessments, checked permit-to-work and verified absence of voltage before mock inspection.
Stopped an unsafe mock activity, recorded the near miss and verified corrective action in TR-H01.
Documentation: Independently authored coordinated shop drawings, as-built drawings, RFIs,
inspection/test plans and O&M manuals in Vietnamese and English. Maintained TR-D01 revision
register, linked each RFI to its drawing revision and closed 18 handover-document review comments.
Availability: 2026-10-15. Salary expectation: 28000000 VND/month.
All evidence references are authored simulation records; none has been independently verified
as a real professional qualification. Original documents can be requested in a real recruitment.
"""


def encoded(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def build_packet(folder: Path) -> Path:
    """Write deterministic source records; expected outcomes never enter the brief."""
    folder.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def put(name: str, content: Any) -> str:
        data = content.encode() if isinstance(content, str) else encoded(content)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
        return "fixture:packet-v1:" + name

    hiring = copy.deepcopy(hiring_fixture())
    hiring["test_cvs"][0]["text"] = STRONG_CV
    for cv in hiring["test_cvs"]:
        put("hiring/" + cv["filename"], cv["text"])
    put(
        "hiring/headcount.json",
        {
            k: hiring[k]
            for k in (
                "synthetic",
                "position",
                "headcount",
                "salary_min",
                "salary_max",
                "start_date",
                "boss_brief",
            )
        },
    )
    put(
        "hiring/role-and-induction.md",
        """# SIMULATED role and induction source record
Original authored form inspired by Acas; O-Nexus governs the actual workflow.
Role: MEP engineer; reports to engineering lead; one headcount; approved budget in headcount.json.
Duties: HVAC/plumbing, electrical coordination, BIM, testing, HSE and handover documents.
Authority: propose engineering solutions; purchases and hiring remain subject to Boss approval.
Day one: HR checks mock personnel documents; IT prepares a sandbox account and equipment;
HSE officer covers isolation, emergency response and permit-to-work; mentor introduces the team.
Days 30/60/90: review design calculations, coordination ownership and commissioning deliverables.
These future reviews are scheduled only. No real account, equipment delivery or induction occurred.
""",
    )
    for key in ("interview_technical", "interview_hr", "offer_acceptance"):
        record = hiring[key]
        record["source"] = "fixture:packet-v1:hiring/" + key + ".json"
        if key == "interview_technical":
            record["transcripts"][0]["notes"] = (
                "SIMULATED technical panel: candidate explained 420 kW cooling load and "
                "35 m3/h pump sizing, voltage-drop/derating checks, clash escalation, TAB "
                "measurement and LOTO. Panel reviewed fictional TR-M01/E01/B01/C01/H01/D01. "
                "Result pass for the exercise; not an actual interview or verified credential."
            )
        put("hiring/" + key + ".json", record)
    put(
        "hiring/review-policy.json",
        {
            "synthetic": True,
            "human_decision": False,
            "decision": "simulated_approved",
            "scope": "all controller simulation gates only",
            "note": "No actual HR/Boss signature or Approval row is created.",
        },
    )
    procurement = copy.deepcopy(procurement_fixture())
    procurement["boq_source"] = put(
        "procurement/boq.json",
        {
            "synthetic": True,
            "lines": procurement["boq"],
            "need_date": procurement["need_date"],
            "g3_date": procurement["g3_date"],
        },
    )
    for supplier in procurement["suppliers"]:
        sid = supplier["supplier_id"]
        for field, contents in (
            ("legal_ref", {"legal_valid": supplier["legal_valid"]}),
            ("financial_ref", {"financial_health": supplier["financial_health"]}),
            ("hse_ref", {"hse": supplier["hse"]}),
            ("quote_source", {"quotes": supplier["quotes"]}),
        ):
            name = f"procurement/{sid}/{field}.json"
            supplier[field] = "fixture:packet-v1:" + name
            # Quotes are written after certificate references have been finalized.
            if field != "quote_source":
                put(name, {"synthetic": True, "supplier_id": sid, **contents})
        for quote in supplier["quotes"]:
            if quote["certificate_ref"]:
                quote["certificate_ref"] = put(
                    f"procurement/{sid}/{quote['material_id']}-certificate.json",
                    {
                        "synthetic": True,
                        "supplier_id": sid,
                        "material_id": quote["material_id"],
                        "spec_compliant": True,
                        "note": "Mock quality reviewer exercised compliance; no real certificate.",
                    },
                )
        put(
            f"procurement/{sid}/quote_source.json",
            {
                "synthetic": True,
                "supplier_id": sid,
                "quotes": supplier["quotes"],
            },
        )
    delivery = procurement["delivery"]
    delivery["source"] = "fixture:packet-v1:procurement/delivery-and-invoice.json"
    put("procurement/delivery-and-invoice.json", delivery)
    put(
        "procurement/rfq-and-review.md",
        """# SIMULATED RFQ and quality-review form
Original form inspired by World Bank evaluation guidance; no World Bank financed procurement.
Scope: the three BOQ lines. Compare identical quantities/specifications and VND unit prices.
Predeclared checks: legal eligibility, financial/HSE evidence, required technical certificates,
delivery before need date, warranty, and quality review before commercial award.
NCC-TEST-A and B: fictional acceptable qualification/certificates. NCC-TEST-C: legal evidence
invalid and certificates missing despite lower prices. Escalate; do not award to a red supplier.
All quality/Boss decisions are simulated controller gates; no employee signed this form.
PO, GRN and invoice remain sandbox artifacts. No supplier receives an RFQ, PO or payment.
""",
    )
    put(
        "expectations.json",
        {
            "synthetic": True,
            "hiring": (
                "Strong CV >= unchanged rubric threshold > weak CV > injection CV; exact quotes"
            ),
            "procurement": (
                "All BOQ lines covered; no red supplier; PO/GRN/invoice quantities/prices match"
            ),
            "both": "Every ordered stage, real model provenance, events/audit, no forged approvals",
            "not_passed_to_models": True,
        },
    )
    put("briefs/mep_hiring.json", hiring)
    put("briefs/procurement.json", procurement)
    manifest = {
        "format_version": 1,
        "packet_id": "onexus-synthetic-acceptance-v1",
        "synthetic": True,
        "retrieved_on": "2026-10-06",
        "sources": SOURCES,
        "source_authority": "O-Nexus seeded SOPs; external sources inform form structure only",
        "human_approved": False,
        "files": files,
    }
    path = folder / "manifest.json"
    path.write_bytes(encoded(manifest))
    return path


def load_brief(manifest_path: Path, kind: str) -> dict[str, Any]:
    """Reject modified, escaping or non-synthetic packets before creating any task."""
    if kind not in {"mep_hiring", "procurement"}:
        raise ValueError("Unknown workflow kind")
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("format_version") != 1
        or manifest.get("synthetic") is not True
        or manifest.get("human_approved") is not False
    ):
        raise ValueError("Acceptance packets must be explicitly synthetic and not human-approved")
    folder = manifest_path.parent.resolve()
    for name, digest in manifest["files"].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file():
            raise ValueError("Packet file missing or outside packet: " + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Packet hash mismatch: " + name)
    key = f"briefs/{kind}.json"
    if key not in manifest["files"]:
        raise ValueError("Packet brief not hashed")
    brief = json.loads((folder / key).read_text())
    if brief.get("synthetic") is not True:
        raise ValueError("Packet brief must be synthetic")
    if kind == "mep_hiring":
        for cv in brief["test_cvs"]:
            cv_key = "hiring/" + cv["filename"]
            if cv_key not in manifest["files"] or (folder / cv_key).read_text() != cv["text"]:
                raise ValueError("CV attachment differs from source document")

    def verify_refs(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                verify_refs(child)
        elif isinstance(value, list):
            for child in value:
                verify_refs(child)
        elif (
            isinstance(value, str)
            and value.startswith("fixture:packet-v1:")
            and value.removeprefix("fixture:packet-v1:") not in manifest["files"]
        ):
            raise ValueError("Packet reference has no hashed document: " + value)

    verify_refs(brief)
    source_keys = (
        ("interview_technical", "interview_hr", "offer_acceptance")
        if kind == "mep_hiring"
        else ("delivery",)
    )
    for source_key in source_keys:
        source_record = brief[source_key]
        name = source_record["source"].removeprefix("fixture:packet-v1:")
        if json.loads((folder / name).read_text()) != source_record:
            raise ValueError("Embedded source differs from document: " + source_key)
    if kind == "procurement":

        def source_doc(reference: str) -> dict[str, Any]:
            name = reference.removeprefix("fixture:packet-v1:")
            return json.loads((folder / name).read_text())

        boq = source_doc(brief["boq_source"])
        if (
            boq["lines"] != brief["boq"]
            or boq["need_date"] != brief["need_date"]
            or boq["g3_date"] != brief["g3_date"]
        ):
            raise ValueError("Embedded BOQ differs from document")
        for supplier in brief["suppliers"]:
            for reference, fields in (
                ("legal_ref", ("legal_valid",)),
                ("financial_ref", ("financial_health",)),
                ("hse_ref", ("hse",)),
                ("quote_source", ("quotes",)),
            ):
                document = source_doc(supplier[reference])
                if (
                    document.get("synthetic") is not True
                    or document["supplier_id"] != supplier["supplier_id"]
                    or any(document[field] != supplier[field] for field in fields)
                ):
                    raise ValueError("Embedded supplier differs from document: " + reference)
    brief["acceptance_packet"] = {
        "id": manifest["packet_id"],
        "synthetic": True,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    return brief


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("examples/workflow_acceptance/v1"))
    args = parser.parse_args()
    packet = build_packet(args.out)
    for workflow_kind in ("mep_hiring", "procurement"):
        load_brief(packet, workflow_kind)
    print("Verified synthetic document packet:", packet)
