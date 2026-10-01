"""Seed the governance spine from the O-Nexus SOP dossier.

The policy tables are worthless empty. A `gate_definitions` with no rows means no
Gate is enforced, and it looks configured. This writes what Tập 1 and Tập 2
actually specify, so the platform starts with the client's own rules in it rather
than with a schema waiting for somebody to type them in.

Idempotent. Safe to run against a database that already has a partial seed: rows
are matched on `(organization_id, code)` and left alone if present, so a Gate
whose criteria somebody has customised is never overwritten.

    python scripts/seed_process_spine.py --org <org_id>

What comes from where:

* the six Gates, their councils and their convening rhythm — Tập 1 §2.2 and
  Tập 2 §E.2
* the Entry/Exit criteria — Tập 3 §1.3, verbatim
* the 28 SOPs with their accountable role and their Gate — Tập 1 §3.2-3.5
* the four absolutely forbidden zones — Tập 1 §5.3
* a starter DOA matrix, since a DOA with no bands enforces nothing

The SOPs are counted in the output and asserted to be 28, because Tập 1 says
"28 SOP" twice and a list that silently loses three is a compliance finding
rather than a bug.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.persistence.session import Database

# --------------------------------------------------------------------------
# The six Gates. Tập 1 §2.2 names them; Tập 2 §E.2 gives the council and the
# rhythm. `member_role_keys` are role keys, because the person holding a role
# changes and the Gate does not.
# --------------------------------------------------------------------------
GATES: list[dict[str, object]] = [
    {
        "code": "G0",
        "sequence": 0,
        "name_vi": "Phê duyệt theo đuổi cơ hội (Go/No-Go)",
        "name_en": "Go / No-Go",
        "description": (
            "Front → Front. Ban Go/No-Go đánh giá cơ hội trước khi dành nguồn lực ước tính."
        ),
        "chair_role_key": "bd_director",
        "member_role_keys": ["pmo_director", "cfo"],
        "entry_summary": (
            "Phiếu đánh giá cơ hội (FRM-FO-001) hoàn chỉnh; qua Quick Screen; "
            "hồ sơ mời thầu/RFP đính kèm"
        ),
        "exit_summary": (
            "Điểm ≥65, không nhóm <40%; CFO xác nhận hạn mức bảo lãnh; nguồn lực dự toán khả dụng"
        ),
        "convened_within": "Họp tuần cố định",
        "lead_time_days": 5,
    },
    {
        "code": "G1",
        "sequence": 1,
        "name_vi": "Phê duyệt nộp thầu (Bid Submission)",
        "name_en": "Bid submission",
        "description": "Front → CEO/PMO. Duyệt trước khi nộp hồ sơ dự thầu.",
        "chair_role_key": "ceo",
        "member_role_keys": ["cfo", "legal_lead", "pmo_director"],
        "entry_summary": (
            "Dự toán chi tiết 5 hệ + phân tích rủi ro giá; ma trận rủi ro hợp "
            "đồng có ý kiến Pháp chế; markup đề xuất; điều kiện thanh toán"
        ),
        "exit_summary": (
            "Biên LN ≥ sàn (dưới sàn: CEO duyệt riêng); điều khoản lệch chuẩn có "
            "phương án xử lý; đủ năng lực bảo lãnh dự thầu"
        ),
        "convened_within": "Theo hạn nộp thầu",
        "lead_time_days": 5,
    },
    {
        "code": "G2",
        "sequence": 2,
        "name_vi": "Bàn giao hợp đồng sang thi công (Contract Handover)",
        "name_en": "Contract handover",
        "description": "Front → Middle (PM). Bàn giao hợp đồng trúng thầu sang khối thi công.",
        "chair_role_key": "deputy_ceo_operations",
        "member_role_keys": ["bd_director", "project_director", "cfo", "pmo_director"],
        "entry_summary": (
            "Hợp đồng ký + phụ lục; baseline budget từ dự toán trúng thầu; danh mục "
            "giả định dự toán; cam kết đặc biệt với CĐT; hồ sơ nhân sự PM đề xuất"
        ),
        "exit_summary": (
            "PM ký xác nhận tiếp nhận từng hạng mục; sai khác dự toán–hợp "
            "đồng được định lượng; kế hoạch huy động 30 ngày đầu"
        ),
        "convened_within": "Trong 5 ngày sau ký HĐ",
        "lead_time_days": 5,
    },
    {
        "code": "G3",
        "sequence": 3,
        "name_vi": "Phê duyệt thiết kế & mua sắm chính (Design Freeze / Major PO)",
        "name_en": "Design freeze / major purchase",
        "description": "Middle (Design) → Middle (PM/Proc). Đóng băng thiết kế và cho phép PO lớn.",
        "chair_role_key": "deputy_ceo_operations",
        "member_role_keys": ["design_director", "procurement_lead", "pm", "pmo_director"],
        "entry_summary": (
            "Bản vẽ IFC phát hành từ CDE; BOQ khớp IFC; kế hoạch mua sắm & "
            "long-lead items; shortlist NCC đã thẩm định"
        ),
        "exit_summary": (
            "Design freeze xác nhận; PO chính trong ngân sách; không NCC ngoài danh mục phê duyệt"
        ),
        "convened_within": "Theo mốc dự án",
        "lead_time_days": 7,
    },
    {
        "code": "G4",
        "sequence": 4,
        "name_vi": "Nghiệm thu chuyển giai đoạn / T&C",
        "name_en": "Progressive acceptance / testing & commissioning",
        "description": "Middle (PM) → Middle (QA/QC) → CĐT. Nghiệm thu từng giai đoạn.",
        "chair_role_key": "project_director",
        "member_role_keys": ["qa_lead", "client_representative", "pmo_director"],
        "entry_summary": (
            "Hồ sơ nghiệm thu theo ITP đầy đủ; NCR đóng hoặc có phương án; hồ sơ "
            "T&C từng hệ; punch-list phân loại"
        ),
        "exit_summary": (
            "CĐT ký nghiệm thu giai đoạn/T&C; NCR nghiêm trọng = 0; điều kiện thanh "
            "toán mốc được kích hoạt"
        ),
        "convened_within": "Theo mốc dự án",
        "lead_time_days": 7,
    },
    {
        "code": "G5",
        "sequence": 5,
        "name_vi": "Bàn giao – Quyết toán – Đóng dự án",
        "name_en": "Handover, final account, project close",
        "description": "Middle → Back (Finance) → PMO. Chốt dự án và giải chấp bảo lãnh.",
        "chair_role_key": "pmo_director",
        "member_role_keys": ["cfo", "project_director", "bd_director"],
        "entry_summary": (
            "Hồ sơ hoàn công; quyết toán đối chiếu Finance; giải chấp bảo lãnh; báo "
            "cáo lessons learned theo mẫu PMO"
        ),
        "exit_summary": (
            "Finance xác nhận quyết toán & thu hồi công nợ ≥ ngưỡng; lessons learned "
            "nhập kho tri thức; đánh giá NCC cập nhật"
        ),
        "convened_within": "Sau quyết toán",
        "lead_time_days": 10,
    },
]

# Tập 3 §1.3, split into the two lists the schema stores. Kept as data rather
# than prose so a Gate's checklist is rows an operator can answer.
GATE_CRITERIA: dict[str, list[tuple[str, str, str, str, bool]]] = {
    "G0": [
        ("E1", "entry", "Phiếu đánh giá cơ hội FRM-FO-001 hoàn chỉnh", "", True),
        ("E2", "entry", "Đã qua Quick Screen", "", True),
        ("E3", "entry", "Hồ sơ mời thầu / RFP đính kèm", "", True),
        ("X1", "exit", "Điểm đánh giá ≥ 65", "", True),
        ("X2", "exit", "Không nhóm tiêu chí nào < 40%", "", True),
        ("X3", "exit", "CFO xác nhận hạn mức bảo lãnh", "", True),
        ("X4", "exit", "Nguồn lực dự toán khả dụng", "", True),
    ],
    "G1": [
        ("E1", "entry", "Dự toán chi tiết 5 hệ", "", True),
        ("E2", "entry", "Phân tích rủi ro giá", "", True),
        ("E3", "entry", "Ma trận rủi ro hợp đồng có ý kiến Pháp chế", "", True),
        ("E4", "entry", "Markup đề xuất", "", True),
        ("E5", "entry", "Điều kiện thanh toán đề xuất", "", False),
        ("X1", "exit", "Biên lợi nhuận ≥ sàn", "", True),
        ("X2", "exit", "Phương án xử lý cho điều khoản lệch chuẩn", "", True),
        ("X3", "exit", "Đủ năng lực bảo lãnh dự thầu", "", True),
    ],
    "G2": [
        ("E1", "entry", "Hợp đồng ký + phụ lục", "", True),
        ("E2", "entry", "Baseline budget từ dự toán trúng thầu", "", True),
        ("E3", "entry", "Danh mục giả định dự toán", "", False),
        ("E4", "entry", "Cam kết đặc biệt với CĐT", "", False),
        ("E5", "entry", "Hồ sơ nhân sự PM đề xuất", "", True),
        ("X1", "exit", "PM ký xác nhận tiếp nhận từng hạng mục", "", True),
        ("X2", "exit", "Sai khác dự toán–hợp đồng đã được định lượng", "", True),
        ("X3", "exit", "Kế hoạch huy động 30 ngày đầu", "", True),
    ],
    "G3": [
        ("E1", "entry", "Bản vẽ IFC phát hành từ CDE", "", True),
        ("E2", "entry", "BOQ khớp IFC", "", True),
        ("E3", "entry", "Kế hoạch mua sắm & long-lead items", "", True),
        ("E4", "entry", "Shortlist NCC đã thẩm định", "", True),
        ("X1", "exit", "Design freeze xác nhận", "", True),
        ("X2", "exit", "PO chính trong ngân sách", "", True),
        ("X3", "exit", "Không NCC ngoài danh mục phê duyệt", "", True),
    ],
    "G4": [
        ("E1", "entry", "Hồ sơ nghiệm thu theo ITP đầy đủ", "", True),
        ("E2", "entry", "NCR đóng hoặc có phương án xử lý", "", True),
        ("E3", "entry", "Hồ sơ T&C từng hệ", "", True),
        ("E4", "entry", "Punch-list phân loại", "", False),
        ("X1", "exit", "CĐT ký nghiệm thu giai đoạn / T&C", "", True),
        ("X2", "exit", "NCR nghiêm trọng = 0", "", True),
        ("X3", "exit", "Điều kiện thanh toán mốc được kích hoạt", "", False),
    ],
    "G5": [
        ("E1", "entry", "Hồ sơ hoàn công", "", True),
        ("E2", "entry", "Quyết toán đối chiếu Finance", "", True),
        ("E3", "entry", "Giải chấp bảo lãnh", "", True),
        ("E4", "entry", "Báo cáo lessons learned theo mẫu PMO", "", False),
        ("X1", "exit", "Finance xác nhận quyết toán", "", True),
        ("X2", "exit", "Thu hồi công nợ ≥ ngưỡng", "", True),
        ("X3", "exit", "Lessons learned nhập kho tri thức", "", True),
        ("X4", "exit", "Đánh giá NCC cập nhật", "", False),
    ],
}

# Tập 1 §3.2-3.5. 28 SOPs, and the count is asserted below because the dossier
# says "28 SOP" twice and a list that quietly loses three is a compliance
# finding, not a typo.
SOPS: list[tuple[str, str, str, str, list[str]]] = [
    # Front Office
    ("ONX-FO-BD-SOP-001", "Quản lý pipeline & đánh giá cơ hội", "FO", "BD", ["G0"]),
    ("ONX-FO-TE-SOP-002", "Lập dự toán & giá dự thầu", "FO", "TE", ["G1"]),
    ("ONX-FO-TE-SOP-003", "Chuẩn bị & nộp hồ sơ thầu; thương thảo hợp đồng", "FO", "TE", ["G1"]),
    ("ONX-FO-BD-SOP-004", "Bàn giao hợp đồng sang thi công", "FO", "BD", ["G2"]),
    ("ONX-FO-CR-SOP-005", "Quản lý quan hệ khách hàng & khảo sát hài lòng", "FO", "CR", []),
    # Middle Office
    (
        "ONX-MO-DES-SOP-001",
        "Quản lý thiết kế M&E: đầu vào, phối hợp BIM, phê duyệt IFC",
        "MO",
        "DES",
        ["G3"],
    ),
    (
        "ONX-MO-PM-SOP-002",
        "Khởi động dự án: PEP, WBS, baseline tiến độ & ngân sách",
        "MO",
        "PM",
        ["G2"],
    ),
    (
        "ONX-MO-PM-SOP-003",
        "Kiểm soát tiến độ – chi phí (EVM: SPI/CPI, forecast EAC)",
        "MO",
        "PM",
        ["G3", "G4", "G5"],
    ),
    (
        "ONX-MO-PM-SOP-004",
        "Quản lý thay đổi & phát sinh (Variation/Claim Management)",
        "MO",
        "PM",
        [],
    ),
    (
        "ONX-MO-PRC-SOP-005",
        "Mua sắm vật tư & lựa chọn thầu phụ (kèm thẩm định NCC)",
        "MO",
        "PRC",
        ["G3"],
    ),
    ("ONX-MO-PRC-SOP-006", "Thẩm định năng lực & rủi ro nhà cung cấp", "MO", "PRC", ["G3"]),
    (
        "ONX-MO-QA-SOP-007",
        "Kiểm soát chất lượng: ITP, nghiệm thu vật liệu/công việc, NCR",
        "MO",
        "QA",
        ["G4"],
    ),
    (
        "ONX-MO-HSE-SOP-008",
        "An toàn – Môi trường: đánh giá rủi ro, permit-to-work, điều tra sự cố",
        "MO",
        "HSE",
        [],
    ),
    (
        "ONX-MO-PM-SOP-009",
        "Chạy thử & nghiệm thu hệ thống (Testing & Commissioning)",
        "MO",
        "PM",
        ["G4"],
    ),
    ("ONX-MO-PM-SOP-010", "Bàn giao, hồ sơ hoàn công, bảo hành & đóng dự án", "MO", "PM", ["G5"]),
    # Back Office
    (
        "ONX-BO-FIN-SOP-001",
        "Lập & kiểm soát ngân sách; phê duyệt chi theo hạn mức ủy quyền",
        "BO",
        "FIN",
        ["G2", "G3", "G4", "G5"],
    ),
    (
        "ONX-BO-FIN-SOP-002",
        "Thanh toán nhà cung cấp/thầu phụ 3-way match (PO–GRN–Invoice)",
        "BO",
        "FIN",
        [],
    ),
    (
        "ONX-BO-FIN-SOP-003",
        "Quản lý dòng tiền dự án, nghiệm thu thanh toán với CĐT, thu hồi công nợ",
        "BO",
        "FIN",
        ["G4", "G5"],
    ),
    (
        "ONX-BO-HR-SOP-004",
        "Tuyển dụng – Onboarding – Đánh giá hiệu suất – Offboarding",
        "BO",
        "HR",
        [],
    ),
    ("ONX-BO-HR-SOP-005", "Chấm công, tính lương, BHXH & tuân thủ luật lao động", "BO", "HR", []),
    (
        "ONX-BO-LEG-SOP-006",
        "Rà soát pháp lý hợp đồng; quản lý bảo lãnh, bảo hiểm, tranh chấp",
        "BO",
        "LEG",
        ["G1", "G2"],
    ),
    (
        "ONX-BO-IT-SOP-007",
        "Quản trị hệ thống CNTT, phân quyền, an ninh dữ liệu & sao lưu",
        "BO",
        "IT",
        [],
    ),
    # PMO
    (
        "ONX-PMO-GOV-SOP-001",
        "Vận hành cổng phê duyệt Gate G0–G5",
        "PMO",
        "GOV",
        ["G0", "G1", "G2", "G3", "G4", "G5"],
    ),
    ("ONX-PMO-REP-SOP-002", "Báo cáo hợp nhất danh mục dự án hằng tuần/tháng", "PMO", "REP", []),
    (
        "ONX-PMO-RSK-SOP-003",
        "Quản trị rủi ro danh mục & báo cáo rủi ro trọng yếu lên CEO",
        "PMO",
        "RSK",
        [],
    ),
    (
        "ONX-PMO-RES-SOP-004",
        "Điều phối nguồn lực chéo dự án (Resource Levelling)",
        "PMO",
        "RES",
        [],
    ),
    (
        "ONX-PMO-STD-SOP-005",
        "Quản lý vòng đời SOP: ban hành, sửa đổi, huấn luyện, kiểm tra tuân thủ",
        "PMO",
        "STD",
        [],
    ),
    (
        "ONX-PMO-KNW-SOP-006",
        "Quản lý tri thức & bài học kinh nghiệm (Lessons Learned Register)",
        "PMO",
        "KNW",
        ["G5"],
    ),
]

#: The accountable role for each department, from the dossier's "Owner (A)"
#: column. Kept beside the SOPs rather than in them because it is a property of
#: the department, and a list is easier to keep right than 28 repeated strings.
DEPARTMENT_OWNER: dict[str, str] = {
    "BD": "bd_director",
    "TE": "estimating_lead",
    "CR": "client_relations_lead",
    "DES": "design_director",
    "PM": "project_director",
    "PRC": "procurement_lead",
    "QA": "qa_lead",
    "HSE": "hse_lead",
    "FIN": "chief_accountant",
    "HR": "hr_director",
    "LEG": "legal_lead",
    "IT": "it_lead",
    "GOV": "pmo_director",
    "REP": "pmo_director",
    "RSK": "pmo_director",
    "RES": "pmo_director",
    "STD": "pmo_director",
    "KNW": "pmo_director",
}

#: Tập 1 §5.3, "Vùng cấm tuyệt đối" — the four absolutely forbidden zones.
#:
#: `is_hard_block` means the action does not happen at all: it is not a low
#: autonomy level, it is no action. That is why three of the four report at L1 —
#: L1 is the level at which the *block* is reported, not at which the work is
#: done. `safety_conclusion` and `supplier_risk_flagged` are hard blocks for
#: exactly that reason; `hr_personnel_decision` and `financial_commitment` are
#: ceilings rather than blocks, because the dossier allows the work with a human
#: decision attached.
FORBIDDEN_ZONES: list[tuple[str, str, str, str, bool]] = [
    (
        "hr_personnel_decision",
        "Quyết định nhân sự",
        "L2",
        False,
        "Tuyển/sa thải/kỷ luật/điều chỉnh lương — AI tối đa L2. Tập 1 §5.3.",
    ),
    (
        "financial_commitment",
        "Cam kết tài chính",
        "L3",
        False,
        "Cam kết vượt hạn mức DOA, ký hợp đồng, bảo lãnh — tối đa L3 và luôn qua "
        "đúng cấp phê duyệt của con người. Tập 1 §5.3.",
    ),
    (
        "safety_conclusion",
        "Kết luận an toàn lao động",
        "L1",
        True,
        "Kết luận an toàn lao động, dừng thi công, kết luận điều tra sự cố — "
        "con người quyết định; AI chỉ L1/L2. Tập 1 §5.3. Hard block: a safety "
        "conclusion is not made at a low autonomy level, it is not made.",
    ),
    (
        "supplier_risk_flagged",
        "Giao dịch với NCC có cờ rủi ro",
        "L1",
        True,
        "Giao dịch với NCC có cờ rủi ro cấm vận/pháp lý — tự động đóng băng, "
        "chuyển Pháp chế xử lý. Tập 1 §5.3.",
    ),
    # Not forbidden, but capped, and recorded so the ceiling is a fact rather
    # than an omission.
    (
        "contract_signature",
        "Ký hợp đồng",
        "L3",
        False,
        "Ký hợp đồng — tối đa L3, không bao giờ tự ký. Tập 1 §5.3.",
    ),
    (
        "routine_classification",
        "Phân loại & đối chiếu thường lệ",
        "L4",
        False,
        "Đối chiếu dữ liệu, phân loại, đặt lịch — tác vụ rủi ro thấp, có thể "
        "hoàn tác. Tập 1 §5.3 L4.",
    ),
]

#: A starter DOA. Tập 3 §4.1 has each RACI `A` generate a system approval role;
#: the bands are a company's own policy, so these are a defensible default rather
#: than a transcription. `max_agent_autonomy` is the dossier's rule: a commitment
#: is at most L3 and always goes through a human.
DOA_BANDS: list[tuple[str, str, str, float, float | None, str, str, str]] = [
    (
        "DOA-01",
        "Dưới 100 triệu",
        "payment",
        0,
        100_000_000,
        "procurement_lead",
        "finance_manager",
        "L3",
    ),
    (
        "DOA-02",
        "100 triệu – 1 tỷ",
        "payment",
        100_000_000,
        1_000_000_000,
        "finance_manager",
        "chief_accountant",
        "L3",
    ),
    (
        "DOA-03",
        "1 tỷ – 10 tỷ",
        "payment",
        1_000_000_000,
        10_000_000_000,
        "chief_accountant",
        "cfo",
        "L3",
    ),
    (
        "DOA-04",
        "Trên 10 tỷ",
        "payment",
        10_000_000_000,
        None,
        "cfo",
        "ceo",
        "L3",
    ),
    (
        "DOA-PO-01",
        "PO dưới 500 triệu",
        "purchase_order",
        0,
        500_000_000,
        "procurement_lead",
        "deputy_ceo_operations",
        "L3",
    ),
    (
        "DOA-PO-02",
        "PO trên 500 triệu",
        "purchase_order",
        500_000_000,
        None,
        "deputy_ceo_operations",
        "ceo",
        "L3",
    ),
    (
        "DOA-CT-01",
        "Hợp đồng dưới 50 tỷ",
        "contract",
        0,
        50_000_000_000,
        "ceo",
        "",
        "L3",
    ),
    (
        "DOA-CT-02",
        "Hợp đồng trên 50 tỷ",
        "contract",
        50_000_000_000,
        None,
        "board",
        "",
        "L3",
    ),
]

#: The Gate session itself, as an SOP. Tập 2 §E.1 — the five steps, their RACI,
#: their SLA and their AI level. This is the "xương sống" made executable, and it
#: is seeded rather than left for somebody to invent, because the sequence is a
#: governance rule and not a local preference.
GATE_SESSION_STEPS: list[tuple[int, str, str, str, str, str, str]] = [
    (
        1,
        "Đăng ký phiên Gate",
        "PM/TP nghiệp vụ đăng ký trên hệ thống ≥5 ngày trước; hệ thống sinh checklist "
        "hồ sơ Entry Criteria theo loại Gate",
        "project_director",
        "pmo_director",
        "5 ngày trước",
        "L4",
    ),
    (
        2,
        "Thẩm định trước (Pre-read)",
        "PMO rà hồ sơ theo checklist; thiếu → trả lại; đủ → phát hành pre-read cho "
        "hội đồng 3 ngày trước họp kèm bản nhận định độc lập của PMO (1–2 trang)",
        "pmo_specialist",
        "pmo_director",
        "3 ngày trước",
        "L2",
    ),
    (
        3,
        "Họp Gate",
        "Trình bày 15–20 phút; hội đồng chất vấn; biểu quyết theo cơ cấu từng Gate. "
        "Kết luận: PASS / PASS có điều kiện / HOLD / FAIL",
        "project_director",
        "pmo_director",
        "60–90 phút",
        "L1",
    ),
    (
        4,
        "Ban hành kết luận",
        "Biên bản ký điện tử trong 24h; điều kiện kèm theo được tạo thành action "
        "items có hạn và người chịu trách nhiệm trên hệ thống",
        "pmo_specialist",
        "pmo_director",
        "24h",
        "L3",
    ),
    (
        5,
        "Theo dõi sau Gate",
        "Agent giám sát action items; quá hạn → leo thang; PASS có điều kiện quá 2 "
        "lần gia hạn → tự động chuyển HOLD và báo CEO",
        "project_director",
        "pmo_director",
        "Liên tục",
        "L4",
    ),
]

#: RACI for the Gate session steps. Tập 2 §E.1. `agent` rows are the
#: project-controls agent drafting the pre-read and the PM agent writing the
#: minutes — both `R`, never `A`, which is the constraint the schema enforces.
GATE_SESSION_RACI: dict[int, list[tuple[str, str, str]]] = {
    1: [("project_director", "person", "R"), ("pmo_director", "person", "A")],
    2: [
        ("pmo_specialist", "person", "R"),
        ("pmo_director", "person", "A"),
        ("project_controls_agent", "agent", "R"),
    ],
    3: [
        ("project_director", "person", "R"),
        ("pmo_director", "person", "A"),
        ("client_representative", "person", "I"),
    ],
    4: [
        ("pmo_specialist", "person", "R"),
        ("pmo_director", "person", "A"),
        ("pm_agent", "agent", "R"),
    ],
    5: [
        ("project_director", "person", "R"),
        ("pmo_director", "person", "A"),
        ("pm_agent", "agent", "R"),
    ],
}


def _uid(prefix: str) -> str:
    from ai_orchestrator.domain.ids import new_ulid

    return f"{prefix}_{new_ulid()}"


async def seed(org_id: str) -> dict[str, int]:
    """Write the dossier into the spine. Idempotent on `(org, code)`."""
    counts = {
        "gates": 0,
        "criteria": 0,
        "sops": 0,
        "forbidden_zones": 0,
        "doa": 0,
        "gate_session_steps": 0,
        "raci": 0,
    }
    database = Database.from_settings(use_admin_role=True)
    try:
        async with database.session() as session:
            gate_ids: dict[str, str] = {}

            for gate in GATES:
                code = str(gate["code"])
                existing = (
                    await session.execute(
                        text(
                            "SELECT id FROM gate_definitions WHERE organization_id = :o "
                            "AND code = :c"
                        ),
                        {"o": org_id, "c": code},
                    )
                ).scalar()
                if existing:
                    gate_ids[code] = str(existing)
                    continue
                gid = _uid("gdf")
                await session.execute(
                    text(
                        "INSERT INTO gate_definitions (id, organization_id, code, sequence, "
                        "name_vi, name_en, description, chair_role_key, member_role_keys, "
                        "entry_summary, exit_summary, convened_within, lead_time_days, "
                        "max_extensions, source, source_actor) "
                        "VALUES (:i, :o, :code, :seq, :nv, :ne, :desc, :chair, "
                        "CAST(:members AS jsonb), :entry, :exit, :within, :lead, 2, "
                        "'human', 'seed:process_spine')"
                    ),
                    {
                        "i": gid,
                        "o": org_id,
                        "code": code,
                        "seq": gate["sequence"],
                        "nv": gate["name_vi"],
                        "ne": gate["name_en"],
                        "desc": gate["description"],
                        "chair": gate["chair_role_key"],
                        "members": __import__("json").dumps(gate["member_role_keys"]),
                        "entry": gate["entry_summary"],
                        "exit": gate["exit_summary"],
                        "within": gate["convened_within"],
                        "lead": gate["lead_time_days"],
                    },
                )
                gate_ids[code] = gid
                counts["gates"] += 1

                # No `enumerate`: the dossier's own criterion codes (E1, X1) are
                # the ordering key, and an unused counter here would be a hint
                # that something was meant to use it.
                for ccode, ctype, title, ref, mandatory in GATE_CRITERIA.get(code, []):
                    await session.execute(
                        text(
                            "INSERT INTO gate_criteria (id, organization_id, "
                            "gate_definition_id, code, criterion_type, title, "
                            "required_document_ref, is_mandatory, waiver_role_key, "
                            "source, source_actor) "
                            "VALUES (:i, :o, :g, :c, :t, :title, :ref, :m, :chair, "
                            "'human', 'seed:process_spine')"
                        ),
                        {
                            "i": _uid("gcr"),
                            "o": org_id,
                            "g": gid,
                            "c": ccode,
                            "t": ctype,
                            "title": title,
                            "ref": ref,
                            "m": mandatory,
                            "chair": gate["chair_role_key"],
                        },
                    )
                    counts["criteria"] += 1

            for code, name, block, dept, gates in SOPS:
                exists = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM sop_definitions WHERE organization_id = :o AND code = :c"
                        ),
                        {"o": org_id, "c": code},
                    )
                ).scalar()
                if exists:
                    continue
                did = _uid("spd")
                await session.execute(
                    text(
                        "INSERT INTO sop_definitions (id, organization_id, code, name_vi, "
                        "doc_type, block, department, owner_role_key, related_gate_codes, "
                        "source, source_actor) "
                        "VALUES (:i, :o, :c, :n, 'SOP', :b, :d, :owner, "
                        "CAST(:gates AS jsonb), 'human', 'seed:process_spine')"
                    ),
                    {
                        "i": did,
                        "o": org_id,
                        "c": code,
                        "n": name,
                        "b": block,
                        "d": dept,
                        "owner": DEPARTMENT_OWNER[dept],
                        "gates": __import__("json").dumps(gates),
                    },
                )
                counts["sops"] += 1

            # The Gate session as an SOP version with its five steps. Its steps
            # and their RACI are the governance rule, so they are seeded from
            # Tập 2 rather than left for an operator to type.
            gate_sop = (
                await session.execute(
                    text(
                        "SELECT id FROM sop_definitions WHERE organization_id = :o "
                        "AND code = 'ONX-PMO-GOV-SOP-001'"
                    ),
                    {"o": org_id},
                )
            ).scalar()
            if gate_sop:
                version_id = (
                    await session.execute(
                        text(
                            "SELECT id FROM sop_versions WHERE organization_id = :o "
                            "AND sop_definition_id = :d AND major = 1 AND minor = 0"
                        ),
                        {"o": org_id, "d": gate_sop},
                    )
                ).scalar()
                if not version_id:
                    version_id = _uid("svr")
                    await session.execute(
                        text(
                            "INSERT INTO sop_versions (id, organization_id, "
                            "sop_definition_id, major, minor, status, approved_by, "
                            "source, source_actor) "
                            "VALUES (:i, :o, :d, 1, 0, 'issued', 'pmo_director', "
                            "'human', 'seed:process_spine')"
                        ),
                        {"i": version_id, "o": org_id, "d": gate_sop},
                    )
                existing_steps = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM sop_steps "
                            "WHERE organization_id = :o AND sop_version_id = :v"
                        ),
                        {"o": org_id, "v": version_id},
                    )
                ).scalar()
                if not existing_steps:
                    for seq, title, action, _r_key, _a_key, sla, level in GATE_SESSION_STEPS:
                        step_id = _uid("ssd")
                        await session.execute(
                            text(
                                "INSERT INTO sop_steps (id, organization_id, sop_version_id, "
                                "sequence, title, action, sla_text, autonomy_level, "
                                "action_class, source, source_actor) "
                                "VALUES (:i, :o, :v, :seq, :title, :action, :sla, :level, "
                                "'gate_operation', 'human', 'seed:process_spine')"
                            ),
                            {
                                "i": step_id,
                                "o": org_id,
                                "v": version_id,
                                "seq": seq,
                                "title": title,
                                "action": action,
                                "sla": sla,
                                "level": level,
                            },
                        )
                        counts["gate_session_steps"] += 1
                        for role_key, role_kind, letter in GATE_SESSION_RACI.get(seq, []):
                            await session.execute(
                                text(
                                    "INSERT INTO sop_raci (id, organization_id, sop_step_id, "
                                    "role_key, role_kind, letter, source, source_actor) "
                                    "VALUES (:i, :o, :s, :rk, :kind, :l, 'human', "
                                    "'seed:process_spine')"
                                ),
                                {
                                    "i": _uid("sra"),
                                    "o": org_id,
                                    "s": step_id,
                                    "rk": role_key,
                                    "kind": role_kind,
                                    "l": letter,
                                },
                            )
                            counts["raci"] += 1

            for action_class, name, level, hard, rationale in FORBIDDEN_ZONES:
                exists = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM autonomy_policies WHERE organization_id = :o "
                            "AND action_class = :a"
                        ),
                        {"o": org_id, "a": action_class},
                    )
                ).scalar()
                if exists:
                    continue
                await session.execute(
                    text(
                        "INSERT INTO autonomy_policies (id, organization_id, action_class, "
                        "name_vi, max_level, is_hard_block, rationale, approved_by, "
                        "source, source_actor) "
                        "VALUES (:i, :o, :a, :n, :l, :h, :r, 'AI Governance Board', "
                        "'human', 'seed:process_spine')"
                    ),
                    {
                        "i": _uid("aut"),
                        "o": org_id,
                        "a": action_class,
                        "n": name,
                        "l": level,
                        "h": hard,
                        "r": rationale,
                    },
                )
                counts["forbidden_zones"] += 1

            for code, name, subject, lo, hi, approver, fallback, level in DOA_BANDS:
                exists = (
                    await session.execute(
                        text("SELECT 1 FROM doa_matrix WHERE organization_id = :o AND code = :c"),
                        {"o": org_id, "c": code},
                    )
                ).scalar()
                if exists:
                    continue
                await session.execute(
                    text(
                        "INSERT INTO doa_matrix (id, organization_id, code, name_vi, "
                        "subject_kind, min_amount, max_amount, approver_role_key, "
                        "fallback_role_key, max_agent_autonomy, source, source_actor) "
                        "VALUES (:i, :o, :c, :n, :s, :lo, :hi, :ap, :fb, :l, 'human', "
                        "'seed:process_spine')"
                    ),
                    {
                        "i": _uid("doa"),
                        "o": org_id,
                        "c": code,
                        "n": name,
                        "s": subject,
                        "lo": lo,
                        "hi": hi,
                        "ap": approver,
                        "fb": fallback,
                        "l": level,
                    },
                )
                counts["doa"] += 1

            await session.commit()
    finally:
        await database.dispose()
    return counts


async def main(org: str | None) -> int:
    settings = get_settings()
    org_id = org or settings.seed_organization_slug
    # Accept either an id or a slug, because an operator will have one of them
    # and guessing wrong produces a confusing "no organisation" message.
    database = Database.from_settings(use_admin_role=True)
    try:
        async with database.session() as session:
            resolved = (
                await session.execute(
                    text("SELECT id FROM organizations WHERE id = :o OR slug = :o"),
                    {"o": org_id},
                )
            ).scalar()
    finally:
        await database.dispose()
    if resolved is None:
        # Name the command, not just the condition. The previous message here was
        # `no organization matches 'autonomous-demo-company'`, which is accurate
        # and leaves the reader to work out that the organization is created by a
        # *different* script — and that the resulting database looks healthy while
        # having no Gates, no SOPs and no DOA matrix in it. An error that does not
        # say what to run next is half an error.
        print(
            f"no organization matches {org_id!r}\n"
            f"  the organization is created by a different step. Run:\n"
            f"      make seed\n"
            f"  then re-run this script, or use `make setup`, which does both."
        )
        return 1

    counts = await seed(str(resolved))
    total_sops = len(SOPS)
    if total_sops != 28:
        print(f"SEED LIST IS WRONG: {total_sops} SOPs, the dossier says 28", file=sys.stderr)
        return 2

    # `counts` is rows *written*, so a second run legitimately reports all zeros —
    # and that output is indistinguishable from a seeder that silently did nothing,
    # which is the failure that actually happens when a database has been rebuilt
    # and the spine was forgotten. The previous line printed `seeded {...zeros...}`
    # on a perfectly healthy database with six Gates in it.
    #
    # So the totals are read back. A run that wrote nothing and a run that found
    # nothing are now different statements, which is the only thing that makes this
    # output worth printing at all. `docs/PRODUCT_GAP.md` §5a records the incident.
    present = await _spine_totals(str(resolved))
    expected = {"gates": len(GATES), "sops": total_sops}
    wrong = {
        k: {"expected": v, "found": present.get(k)}
        for k, v in expected.items()
        if present.get(k) != v
    }
    if wrong:
        # The first version of this message printed only the *expected* value and
        # said it "did not land", which is the wrong sentence for a surplus: with
        # 7 Gates against an expected 6, nothing failed to land, and the advice to
        # re-run could not possibly help. Both directions are stated, because they
        # need different responses and an operator should not have to work out
        # which one they are looking at.
        short = [k for k, v in wrong.items() if (v["found"] or 0) < v["expected"]]
        surplus = [k for k, v in wrong.items() if (v["found"] or 0) > v["expected"]]
        advice = []
        if short:
            advice.append(
                f"missing {short}: re-run, and if it persists the rows are being "
                f"rejected by a constraint and the error above is the real one"
            )
        if surplus:
            advice.append(
                f"surplus {surplus}: a re-run will not remove them, so this is data "
                f"added after the seed. Check whether the extra rows are yours before "
                f"deleting anything"
            )
        print(
            f"SEED MISMATCH for {resolved}: {present}\n"
            f"  expected {expected}\n"
            f"  found {wrong}\n" + "".join(f"  - {a}\n" for a in advice),
            file=sys.stderr,
        )
        return 3

    written = sum(counts.values())
    print(f"spine for {resolved}: {present}")
    print(
        f"  {written} written this run"
        if written
        else "  already seeded, nothing written (this is the expected result of a re-run)"
    )
    if written:
        print(f"  written: {counts}")
    return 0


async def _spine_totals(org_id: str) -> dict[str, int]:
    """What the spine actually contains, read back rather than assumed.

    Counting rows in the database is the only version of this that can catch a
    constraint rejecting a row: `seed()` would have raised on that, but a partial
    seed across a commit boundary would not.
    """
    tables = {
        "gates": "gate_definitions",
        "criteria": "gate_criteria",
        "sops": "sop_definitions",
        "forbidden_zones": "autonomy_policies",
        "doa": "doa_matrix",
        # `sop_steps`, not `gate_session_steps`: the seed key is named for what it
        # holds and the table is named for what it is. Getting this wrong raises
        # `UndefinedTable`, which at least fails loudly — unlike the `int()`
        # mistake that came first here, which would have failed on a healthy
        # database and looked like a connection problem.
        "sop_steps": "sop_steps",
        "raci": "sop_raci",
    }
    database = Database.from_settings(use_admin_role=True)
    try:
        async with database.session() as session:
            totals: dict[str, int] = {}
            for label, table in tables.items():
                totals[label] = int(
                    (
                        await session.execute(
                            text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"),
                            {"o": org_id},
                        )
                    ).scalar()
                    or 0
                )
    finally:
        await database.dispose()
    return totals


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", help="organization id or slug")
    raise SystemExit(asyncio.run(main(parser.parse_args().org)))
