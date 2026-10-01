"""The playbook's 28 SOPs, as work a department agent can be handed.

The dossier (`docs/O-Nexus_Playbook_Trien_Khai_Chi_Tiet.docx`) is a set of
procedure documents. It is not a set of tasks: nothing in it can be executed, and
an organisation of agents that cannot execute its own procedures is a diagram.

So this module is the translation. For every SOP it carries what the playbook
states — the **step chain** ("chuỗi bước chính"), the **control point**
("điểm kiểm soát then chốt"), and where one exists a **forbidden zone** — plus
the two things the dossier cannot supply because it is not about this
implementation: which of this build's seven departments owns it, and what a
finished answer must contain so the office can review it.

**Why the contract matters more here than anywhere else.** These are the jobs a
company is judged on -- a payment that must not be made without a 3-way match, a
supplier that must not be appointed without an assessment, a Gate that must not be
passed on a verbal assurance. A run that produces prose and is marked `completed`
has passed none of them. So every SOP declares `required`, and
`domain.review` holds the run to it.

**The forbidden zones are contracts, not warnings.** The dossier is emphatic
(`MO-HSE-SOP-008`: "Vùng cấm AI: lệnh dừng thi công và kết luận điều tra do con
người quyết"). Encoding that as prose in a prompt is how it is lost the first time
someone is in a hurry. Here it is a required key plus a refusal the run cannot
satisfy: an SOP in a forbidden zone must produce a *recommendation* and the
evidence for it, and a run that claims to have decided is rejected by the office
on sight.

**Titles are the governance spine's, verbatim, including its en dashes.** A
catalogue that shortens or re-punctuates a procedure's name cannot be matched
against the document it came from, and the mismatch is invisible until somebody
goes looking. `test_every_name_matches_the_spine_it_quotes` is the instrument; it
caught 22 abbreviations and one outright typo ("vòi đời" for "vòng đời") the first
time it ran. The `# noqa: RUF001` markers on those lines are for the en dash, which
is the spine's own punctuation and not ours to tidy.

**The department mapping is a real decision and it does not fit cleanly.** The
dossier has 17 owning departments; this build has six, because the owner asked for
three offices of two. Several mappings are a stretch and are marked `stretched`
with the reason, because a reader needs to know which SOPs are being run by a
department that does not naturally own them. Two have no defensible home at all --
`BO-IT-SOP-007` and the PMO block -- and they are marked `gap` rather than
quietly assigned to whoever is left.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A zone where an agent may prepare, advise and assemble, but never decide.
#:
#: Named after the dossier's own phrase. Carried on the SOP so the contract and the
#: restriction travel together -- an SOP that says "a person decides this" and a
#: contract that asks for `decision` would cancel each other out, and the agent
#: would resolve the contradiction in whichever direction it was feeling.
FORBIDDEN_DECIDE = "forbidden_decide"
FORBIDDEN_STOP_WORK = "forbidden_stop_work"


@dataclass(frozen=True, slots=True)
class PlaybookSop:
    """One procedure, ready to be handed to a department."""

    code: str
    name: str
    block: str
    #: The department the dossier names. Kept, because it is what the mapping below
    #: is measured against and hiding it would make the mapping unverifiable.
    dossier_department: str
    #: Which of this build's seven departments runs it. `None` where nothing fits.
    #: `None` is still possible for a new SOP, and the runner refuses it rather than
    #: guessing -- see `run_sop.py`.
    department: str | None
    #: The step chain, verbatim in intent. Order matters: the control point applies
    #: to a step, not to the SOP as a whole.
    steps: tuple[str, ...]
    #: What must not be skipped. The single most useful sentence in the dossier for
    #: an agent, because it is the sentence that says *no*.
    control_point: str
    #: Keys a finished answer must contain. The office reviews against these.
    required: tuple[str, ...]
    forbidden: str | None = None
    #: `True` when the dossier's department is not this build's. See the module
    #: docstring: a reader needs to know which SOPs are being run by a department
    #: that does not naturally own them.
    stretched: bool = False
    note: str = ""

    @property
    def runnable(self) -> bool:
        return self.department is not None

    def as_task_input(self) -> dict[str, object]:
        """The brief, as it goes onto the task.

        The steps and the control point are in the `input` rather than only in the
        goal, so the office's review can read what the department was asked to do
        and judge the answer against it.
        """
        return {
            "sop_code": self.code,
            "sop_name": self.name,
            "block": self.block,
            "steps": list(self.steps),
            "control_point": self.control_point,
            "forbidden_zone": self.forbidden,
            "owning_department": self.department,
            "owning_office": OFFICE_OF.get(self.department or "", ""),
        }


#: Which office each of the seven reports to. Read from the tree rather than restated
#: where possible; this is the one place the pairing is written down, because a
#: task needs it at creation time and a DB read per task would be silly.
OFFICE_OF = {
    "Sales": "front-office",
    "Procurement": "front-office",
    "QA/QC-HSE": "middle-office",
    "Design": "middle-office",
    "Finance": "back-office",
    "HR": "back-office",
    # Added with the seventh department, for the two SOPs that had no owner:
    # `ONX-BO-IT-SOP-007` and `ONX-PMO-KNW-SOP-006`. Both are Back Office work --
    # systems and records -- and neither is a finance, a quality or an HR question.
    "IT": "back-office",
}

#: The seven departments and the agent that does each one's work.
#:
#: **Keyed by the department's name, because that is what a department is called.**
#: It was keyed by slug -- `sales`, `qa` -- which is what `application.scenarios`
#: wanted, and the two maps had drifted into being two maps with the same name.
#: Now there is one, keyed by the name the seed builds, and
#: `test_the_agent_map_agrees_with_the_seed` holds it to the tree.
#:
#: The QA department is run by the *Quality* Agent, so the two are not always the
#: same string. That is not a bug in the map; it is the reason the map exists.
AGENT_BY_DEPARTMENT = {
    "Sales": "Sales Agent",
    "Procurement": "Procurement Agent",
    "QA/QC-HSE": "Quality Agent",
    "Design": "Design Agent",
    "Finance": "Finance Agent",
    "HR": "HR Agent",
    "IT": "IT Agent",
}


def agent_for(department: str) -> str | None:
    """The agent that does this department's work, by name."""
    return AGENT_BY_DEPARTMENT.get(department)


#: The six as the seed names them, so a coverage test can check the mapping against
#: the tree rather than against a list in this file.
DEPARTMENTS = ("Sales", "Procurement", "QA/QC-HSE", "Design", "Finance", "HR")


PLAYBOOK: tuple[PlaybookSop, ...] = (
    # ------------------------------------------------------------------ FRONT
    PlaybookSop(
        code="ONX-FO-BD-SOP-001",
        name="Quản lý pipeline & đánh giá cơ hội",
        block="FO",
        dossier_department="BD",
        department="Sales",
        steps=(
            "Ghi nhận cơ hội vào CRM với 12 trường bắt buộc",
            "Sàng lọc nhanh theo 6 tiêu chí loại trừ",
            "Chấm điểm cơ hội theo thang",
            "Ra quyết định Go/No-Go và ghi lý do",
        ),
        control_point=(
            "Không được nộp giá khi chưa có bảng phân tích rủi ro; "
            "điểm dưới ngưỡng phải trình Giám đốc Kinh doanh duyệt"
        ),
        required=("opportunity_record", "exclusion_screen", "score", "go_no_go", "reason"),
    ),
    PlaybookSop(
        code="ONX-FO-BD-SOP-004",
        name="Bàn giao hợp đồng sang thi công",
        block="FO",
        dossier_department="BD",
        department="Sales",
        steps=(
            "Họp bàn giao Front→PM trong 5 ngày sau ký",
            "Chuyển giao hợp đồng, baseline budget, cam kết với CĐT, giả định dự toán",
            "PM ký xác nhận tiếp nhận tại G2",
        ),
        control_point=(
            "Không huy động công trường khi chưa có biên bản G2; "
            "mọi giả định dự toán phải liệt kê thành văn"
        ),
        required=("handover_pack", "assumptions", "client_commitments", "g2_status"),
    ),
    PlaybookSop(
        code="ONX-FO-CR-SOP-005",
        name="Quản lý quan hệ khách hàng & khảo sát hài lòng",
        block="FO",
        dossier_department="CR",
        department="Sales",
        steps=(
            "Kế hoạch chăm sóc theo tier khách hàng",
            "Khảo sát hài lòng tại G4/G5",
            "Xử lý phàn nàn trong 5 ngày",
            "Báo cáo NPS theo quý",
        ),
        control_point="Phàn nàn cấp nghiêm trọng phải lên CEO trong 24 giờ",
        required=("care_plan", "complaint_handling", "nps", "escalations"),
    ),
    PlaybookSop(
        code="ONX-FO-TE-SOP-002",
        name="Lập dự toán & giá dự thầu",
        block="FO",
        dossier_department="TE",
        department="Procurement",
        steps=(
            "Nhận hồ sơ mời thầu",
            "Bóc tách BOQ theo 5 hệ MEPF",
            "Hỏi giá NCC, tối thiểu 3 báo giá mỗi nhóm chính",
            "Tổng hợp giá vốn và phân tích rủi ro giá",
            "Đề xuất markup và trình duyệt theo DOA",
        ),
        control_point=(
            "Không được nộp giá khi chưa có bảng phân tích rủi ro; markup dưới sàn phải CEO duyệt"
        ),
        required=("boq_breakdown", "quotes", "cost_summary", "risk_analysis", "markup"),
        stretched=True,
        note="Estimating sits with buying in this build; the dossier has it in Front Office.",
    ),
    PlaybookSop(
        code="ONX-FO-TE-SOP-003",
        name="Chuẩn bị & nộp hồ sơ thầu; thương thảo hợp đồng",
        block="FO",
        dossier_department="TE",
        department="Procurement",
        steps=(
            "Checklist hồ sơ pháp lý",
            "Rà soát điều khoản hợp đồng",
            "Duyệt nộp tại G1",
            "Nộp hồ sơ và thương thảo, ghi biên bản từng vòng",
        ),
        control_point=(
            "G1: CEO duyệt mọi hồ sơ ≥ ng��ng DOA; điều khoản phạt và bồi thường "
            "phải có ý kiến Pháp chế bằng văn bản"
        ),
        required=("legal_checklist", "submission_status", "negotiation_record"),
        stretched=True,
        note="Tendering sits with buying in this build.",
    ),
    # ----------------------------------------------------------------- MIDDLE
    PlaybookSop(
        code="ONX-MO-DES-SOP-001",
        name="Quản lý thiết kế M&E: đầu vào, phối hợp BIM, phê duyệt IFC",
        block="MO",
        dossier_department="DES",
        department="Design",
        steps=(
            "Đăng ký đầu vào thiết kế",
            "Kế hoạch thiết kế theo phân giai đoạn Concept/Basic/Detail",
            "Phối hợp BIM và clash detection",
            "Thẩm tra nội bộ 2 cấp",
            "Trình CĐT và phát hành IFC có kiểm soát phiên bản",
        ),
        control_point=(
            "Design Freeze tại G3: sau IFC mọi thay đổi đi theo MO-PM-SOP-004; "
            "bản vẽ chỉ hợp lệ khi phát hành từ CDE"
        ),
        required=("design_register", "clash_findings", "internal_reviews", "issue_status"),
    ),
    PlaybookSop(
        code="ONX-MO-PM-SOP-002",
        name="Khởi động dự án: PEP, WBS, baseline tiến độ & ngân sách",
        block="MO",
        dossier_department="PM",
        department="Design",
        steps=(
            "Nhận bàn giao G2",
            "Lập PEP",
            "Lập WBS chuẩn PMO kèm cost code",
            "Chốt baseline tiến độ & ngân sách",
            "Kick-off nội bộ và với CĐT",
        ),
        control_point="Baseline khóa sau phê duyệt; mọi thay đổi baseline phải qua PMO",
        required=("pep", "wbs", "baseline", "kickoff_record"),
        stretched=True,
        note="Project management has no department of its own in this build; Design holds it.",
    ),
    PlaybookSop(
        code="ONX-MO-PM-SOP-003",
        name="Kiểm soát tiến độ – chi phí (EVM: SPI/CPI, forecast EAC)",  # noqa: RUF001
        block="MO",
        dossier_department="PM",
        department="Design",
        steps=(
            "Cập nhật khối lượng tuần",
            "Tính SPI, CPI và EAC",
            "Phân tích mọi sai lệch từ 5%",
            "Lập kế hoạch khắc phục",
            "Báo cáo tuần lên PMO",
        ),
        control_point=(
            "SPI hoặc CPI dưới 0,9 trong hai tuần liên tiếp: tự động triệu tập họp "
            "cấp Phó Tổng Giám đốc"
        ),
        required=("spi", "cpi", "eac", "variance_analysis", "corrective_plan"),
        stretched=True,
        note="Project controls sit with Design in this build.",
    ),
    PlaybookSop(
        code="ONX-MO-PM-SOP-004",
        name="Quản lý thay đổi & phát sinh (Variation/Claim Management)",
        block="MO",
        dossier_department="PM",
        department="Design",
        steps=(
            "Ghi nhận sự kiện trong 5 ngày",
            "Thông báo hợp đồng đúng hạn",
            "Định giá bằng QS",
            "Đàm phán",
            "Cập nhật giá trị hợp đồng",
        ),
        control_point=(
            "Trễ hạn thông báo theo hợp đồng là mất quyền claim; Agent nhắc hạn tự động"
        ),
        required=("event_register", "notice_status", "valuation", "contract_value_update"),
        stretched=True,
        note="Variations sit with Design in this build.",
    ),
    PlaybookSop(
        code="ONX-MO-PM-SOP-009",
        name="Chạy thử & nghiệm thu hệ thống (Testing & Commissioning)",
        block="MO",
        dossier_department="PM",
        department="QA/QC-HSE",
        steps=(
            "Kế hoạch T&C từ Design",
            "Pre-commissioning từng hệ",
            "Chạy thử liên động 5 hệ MEPF",
            "Nghiệm thu với CĐT",
            "Hồ sơ T&C",
        ),
        control_point="Không T&C liên động khi từng hệ chưa đạt; chứng kiến của CĐT ghi biên bản",
        required=("tc_plan", "precommissioning", "system_results", "acceptance_record"),
        stretched=True,
        note="Commissioning is a quality activity here, which is defensible.",
    ),
    PlaybookSop(
        code="ONX-MO-PM-SOP-010",
        name="Bàn giao, hồ sơ hoàn công, bảo hành & đóng dự án",
        block="MO",
        dossier_department="PM",
        department="Finance",
        steps=(
            "Lập punch-list",
            "Hoàn thiện hồ sơ hoàn công",
            "Bàn giao vận hành & đào tạo CĐT",
            "Quyết toán",
            "Giải chấp bảo lãnh",
            "Lessons learned về PMO",
        ),
        control_point=("G5 chỉ đóng khi Finance xác nhận quyết toán và PMO nhận lessons learned"),
        required=("punch_list", "as_built", "final_account", "lessons_learned"),
        stretched=True,
        note="Close-out is run by Finance here because the gate names Finance.",
    ),
    PlaybookSop(
        code="ONX-MO-PRC-SOP-005",
        name="Mua sắm vật tư & lựa chọn thầu phụ (kèm thẩm định NCC)",
        block="MO",
        dossier_department="PRC",
        department="Procurement",
        steps=(
            "Lập kế hoạch mua sắm từ BOQ",
            "Phát hành RFQ cho tối thiểu 3 NCC trong danh mục",
            "So sánh thầu trên bảng chuẩn",
            "Đàm phán",
            "Phát hành PO theo DOA",
            "Theo dõi giao hàng và GRN",
        ),
        control_point="3-way match bắt buộc; long-lead items phải đặt trước mốc G3",
        required=("rfq", "comparison", "award", "po", "delivery_status"),
    ),
    PlaybookSop(
        code="ONX-MO-PRC-SOP-006",
        name="Thẩm định năng lực & rủi ro nhà cung cấp",
        block="MO",
        dossier_department="PRC",
        department="Procurement",
        steps=(
            "Thu thập hồ sơ năng lực NCC",
            "Kiểm tra pháp lý, tài chính và an toàn lao động",
            "Đánh giá rủi ro theo thang",
            "Phân loại NCC: xanh, vàng, đỏ",
            "Ra quyết định chấp nhận có điều kiện hoặc từ chối",
        ),
        control_point=(
            "NCC đỏ không được đưa vào danh sách ngắn; mọi điểm chấm dưới ngưỡng phải nêu rõ lý do"
        ),
        required=("capability_review", "risk_score", "classification", "decision", "reason"),
    ),
    PlaybookSop(
        code="ONX-MO-QA-SOP-007",
        name="Kiểm soát chất lượng: ITP, nghiệm thu vật liệu/công việc, NCR",
        block="MO",
        dossier_department="QA",
        department="QA/QC-HSE",
        steps=(
            "Lập ITP theo từng hệ",
            "Nghiệm thu vật tư đầu vào",
            "Nghiệm thu công việc theo ITP",
            "Lập NCR và xử lý",
            "Lập hồ sơ chất lượng theo tiến độ",
        ),
        control_point=(
            "Không che lấp công việc chưa nghiệm thu; NCR mở quá 14 ngày phải lên Phó Tổng Giám đốc"
        ),
        required=("itp", "material_acceptance", "work_acceptance", "ncr_register"),
    ),
    PlaybookSop(
        code="ONX-MO-HSE-SOP-008",
        name="An toàn – Môi trường: đánh giá rủi ro, permit-to-work, điều tra sự cố",  # noqa: RUF001
        block="MO",
        dossier_department="HSE",
        department="QA/QC-HSE",
        steps=(
            "Đánh giá rủi ro trước khi bắt đầu hạng mục",
            "Cấp permit-to-work cho việc nguy hiểm",
            "Kiểm tra hằng ngày",
            "Điều tra sự cố theo 5-Why",
            "Báo cáo TRIR theo tháng",
        ),
        control_point=("Vùng cấm AI: lệnh dừng thi công và kết luận điều tra do con người quyết"),
        required=("risk_assessment", "permit_status", "daily_checks", "trir"),
        forbidden=FORBIDDEN_STOP_WORK,
    ),
    # ------------------------------------------------------------------ BACK
    PlaybookSop(
        code="ONX-BO-FIN-SOP-001",
        name="Lập & kiểm soát ngân sách; phê duyệt chi theo hạn mức ủy quyền",
        block="BO",
        dossier_department="FIN",
        department="Finance",
        steps=(
            "Lập ngân sách năm từ dưới lên",
            "Hợp nhất và trình duyệt",
            "Phân bổ theo dự án và phòng",
            "Kiểm soát cam kết chi",
            "Rà soát quý và reforecast",
        ),
        control_point=(
            "Không cam kết chi khi chưa có ngân sách còn số dư; "
            "ma trận DOA phải công bố toàn công ty và nhúng vào hệ thống"
        ),
        required=("budget", "allocation", "commitment_control", "forecast"),
    ),
    PlaybookSop(
        code="ONX-BO-FIN-SOP-002",
        name="Thanh toán nhà cung cấp/thầu phụ 3-way match (PO–GRN–Invoice)",  # noqa: RUF001
        block="BO",
        dossier_department="FIN",
        department="Finance",
        steps=(
            "Nhận hồ sơ thanh toán",
            "Đối chiếu PO, GRN và hóa đơn",
            "Kiểm tra khớp ba chiều",
            "Đối chiếu hạn thanh toán theo hợp đồng",
            "Trình duyệt và chi trả",
        ),
        control_point=(
            "Không chi khi PO, GRN và hóa đơn lệch nhau dù chỉ một chiều; "
            "trả cho công trình chưa nghiệm thu là vi phạm"
        ),
        required=("po", "grn", "invoice", "three_way_match", "payment_decision"),
    ),
    PlaybookSop(
        code="ONX-BO-FIN-SOP-003",
        name="Quản lý dòng tiền dự án, nghiệm thu thanh toán với CĐT, thu hồi công nợ",
        block="BO",
        dossier_department="FIN",
        department="Finance",
        steps=(
            "Lập kế hoạch dòng tiền 13 tuần cuốn chiếu",
            "Lập hồ sơ nghiệm thu thanh toán với CĐT đúng mốc hợp đồng",
            "Theo dõi tuổi nợ",
            "Leo thang thu hồi: nhắc, công văn, Pháp chế",
        ),
        control_point=("DSO mục tiêu; hồ sơ thanh toán CĐT phải nộp trong 5 ngày sau nghiệm thu"),
        required=("cash_forecast", "ageing", "collection_actions", "dso"),
    ),
    PlaybookSop(
        code="ONX-BO-HR-SOP-004",
        name="Tuyển dụng – Onboarding – Đánh giá hiệu suất – Offboarding",  # noqa: RUF001
        block="BO",
        dossier_department="HR",
        department="HR",
        steps=(
            "Nhận yêu cầu tuyển từ định biên",
            "Đăng tuyển và sàng lọc hồ sơ",
            "Phỏng vấn hai vòng",
            "Lập offer theo khung lương",
            "Onboarding checklist 30-60-90",
            "Đánh giá hiệu suất bán niên",
            "Offboarding và bàn giao",
        ),
        control_point=(
            "Vùng cấm AI: mọi quyết định tuyển, thôi việc, kỷ luật và lương do "
            "con người quyết; HR Agent tối đa L2 (sàng lọc, soạn thảo, nhắc quy trình)"
        ),
        required=("screening", "interview_notes", "offer_draft", "onboarding_checklist"),
        forbidden=FORBIDDEN_DECIDE,
    ),
    PlaybookSop(
        code="ONX-BO-HR-SOP-005",
        name="Chấm công, tính lương, BHXH & tuân thủ luật lao động",
        block="BO",
        dossier_department="HR",
        department="HR",
        steps=(
            "Chấm công từ hệ thống",
            "Tổng hợp biến động nhân sự",
            "Tính lương",
            "Đối soát độc lập",
            "Trình duyệt hai cấp",
            "Chi trả",
            "Kê khai BHXH và thuế TNCN đúng hạn",
        ),
        control_point=(
            "Đối chiếu độc lập trước khi chi; mọi điều chỉnh tay phải có phê "
            "duyệt và ghi lý do. Vùng cấm AI: quyết định lương và chi trả do "
            "con người quyết"
        ),
        required=("timesheet_summary", "payroll", "reconciliation", "adjustments"),
        forbidden=FORBIDDEN_DECIDE,
    ),
    PlaybookSop(
        code="ONX-BO-LEG-SOP-006",
        name="Rà soát pháp lý hợp đồng; quản lý bảo lãnh, bảo hiểm, tranh chấp",
        block="BO",
        dossier_department="LEG",
        department="Procurement",
        steps=(
            "Rà soát điều khoản trước G1/G2 theo checklist rủi ro",
            "Kiểm tra bảo lãnh và bảo hiểm theo sổ đăng ký",
            "Lập danh sách điều khoản lệch chuẩn",
            "Xử lý tranh chấp theo phân cấp",
        ),
        control_point=(
            "Không ký hợp đồng thiếu ý kiến Pháp chế bằng văn bản với các điều khoản lệch chuẩn"
        ),
        required=("clause_review", "deviations", "bond_register", "opinion"),
        stretched=True,
        note=(
            "There is no legal department in this build. Contract review runs with "
            "Procurement because it already owns supplier and subcontract "
            "agreements; a legal opinion is the one judgement on this list that "
            "this build should not be trusted to produce unsupervised."
        ),
    ),
    PlaybookSop(
        code="ONX-BO-IT-SOP-007",
        name="Quản trị hệ thống CNTT, phân quyền, an ninh dữ liệu & sao lưu",
        block="BO",
        dossier_department="IT",
        department="IT",
        steps=(
            "Quản lý tài khoản theo vòng đời nhân sự",
            "Phân quyền RBAC theo ma trận RACI",
            "Sao lưu và khôi phục có diễn tập",
            "Ứng cứu sự cố",
            "Quản lý thay đổi hệ thống",
        ),
        control_point=(
            "Quyền truy cập phải thu hồi trong 24 giờ khi nghỉ việc; "
            "kiểm thử phục hồi dữ liệu mỗi quý"
        ),
        required=("systems_reviewed", "access_changes_requested", "backup_tested"),
        note=(
            "**Had no home until the seventh department was added.** The owner asked "
            "for three offices of two departments, and none of the six was an IT "
            "department, so the runner refused this rather than assign it — an "
            "account-provisioning run inside QA or Finance is worse than no run, "
            "because it produces a plausible answer to a question nobody asked and an "
            "organisation believes its access control is handled. Refusing is not a "
            "resolution, so IT now exists under Back Office and this SOP has an owner."
        ),
    ),
    # -------------------------------------------------------------------- PMO
    PlaybookSop(
        code="ONX-PMO-GOV-SOP-001",
        name="Vận hành cổng phê duyệt Gate G0–G5",  # noqa: RUF001
        block="PMO",
        dossier_department="GOV",
        department="Design",
        steps=(
            "Sinh checklist hồ sơ Entry Criteria theo loại Gate",
            "Rà soát hồ sơ và phát hành pre-read kèm nhận định độc lập",
            "Ghi biên bản phiên Gate",
            "Ban hành kết luận và tạo action items có hạn",
            "Giám sát action items và leo thang khi quá hạn",
        ),
        control_point=(
            "PASS có điều kiện quá hai lần gia hạn: tự động chuyển HOLD và báo CEO; "
            "quyết định tại phiên Gate do con người quyết"
        ),
        required=("entry_checklist", "pre_read", "minutes", "decision", "action_items"),
        forbidden=FORBIDDEN_DECIDE,
        stretched=True,
        note="Gate administration runs with Design as the programme owner here.",
    ),
    PlaybookSop(
        code="ONX-PMO-STD-SOP-005",
        name="Quản lý vòng đời SOP: ban hành, sửa đổi, huấn luyện, kiểm tra tuân thủ",
        block="PMO",
        dossier_department="STD",
        department="Design",
        steps=(
            "Đăng ký SOP vào danh mục",
            "Đánh giá định kỳ",
            "Phát hành bản sửa đổi có kiểm soát phiên bản",
            "Gia hạn hoặc thu hồi",
        ),
        control_point="Không SOP nào được ban hành khi chưa đạt đủ bộ tiêu chí nghiệm thu",
        required=("register", "review_findings", "version_status"),
        stretched=True,
        note="Document control runs with Design here.",
    ),
    PlaybookSop(
        code="ONX-PMO-RSK-SOP-003",
        name="Quản trị rủi ro danh mục & báo cáo rủi ro trọng yếu lên CEO",
        block="PMO",
        dossier_department="RSK",
        department="QA/QC-HSE",
        steps=(
            "Lập sổ rủi ro danh mục",
            "Đánh giá xác suất và tác động",
            "Chọn phương án xử lý",
            "Báo cáo rủi ro định kỳ",
        ),
        control_point="Rủi ro ở mức đỏ phải lên AI Governance Board",
        required=("risk_register", "assessment", "treatment", "reporting"),
        stretched=True,
        note="Risk runs with QA/QC-HSE, which already audits rather than produces.",
    ),
    PlaybookSop(
        code="ONX-PMO-RES-SOP-004",
        name="Điều phối nguồn lực chéo dự án (Resource Levelling)",
        block="PMO",
        dossier_department="RES",
        department="Design",
        steps=(
            "Lập kế hoạch nhân lực theo WBS",
            "Đối chiếu năng lực và tải",
            "Điều phối xung đột giữa các dự án",
            "Báo cáo tình trạng nguồn lực",
        ),
        control_point="Không giao nhân sự vượt 120% năng lực mà không có phê duyệt",
        required=("resource_plan", "capacity_check", "conflicts", "escalations"),
        stretched=True,
        note="Resourcing runs with Design as programme owner here.",
    ),
    PlaybookSop(
        code="ONX-PMO-REP-SOP-002",
        name="Báo cáo hợp nhất danh mục dự án hằng tuần/tháng",
        block="PMO",
        dossier_department="REP",
        department="Finance",
        steps=(
            "Thu thập số liệu từ các báo cáo tuần",
            "Hợp nhất tiến độ, chi phí và rủi ro",
            "So sánh với baseline",
            "Phát hành báo cáo danh mục",
        ),
        control_point="Báo cáo phải nêu sai lệch ≥5% và nguyên nhân, không chỉ số",
        required=("portfolio_progress", "cost_status", "risk_status", "variance_explanation"),
        stretched=True,
        note="Portfolio reporting runs with Finance, which holds the cost data.",
    ),
    PlaybookSop(
        code="ONX-PMO-KNW-SOP-006",
        name="Quản lý tri thức & bài học kinh nghiệm (Lessons Learned Register)",
        block="PMO",
        dossier_department="KNW",
        department="IT",
        steps=(
            "Thu thập lessons learned từ các dự án",
            "Phân loại theo lĩnh vực",
            "Đăng ký vào cơ sở tri thức",
            "Truyền đạt khi khởi động dự án mới",
        ),
        control_point="Bài học phải gắn với một sự kiện cụ thể, không ghi chung chung",
        required=("lessons_collected", "lessons_registered", "briefings_given"),
        note=(
            "**Had no home in this build.** The platform *has* the mechanism — "
            "`skill_learner` proposes a lesson from a run log and a person publishes "
            "it — but no department was chartered to curate it. IT now is, because "
            "the knowledge register is the same Back Office system work as the "
            "document control and access registers next to it: someone has to own "
            "the register, or the lessons are written and never found."
        ),
    ),
)

BY_CODE = {sop.code: sop for sop in PLAYBOOK}

#: Every department the dossier names, so a test can prove none was dropped.
DOSSIER_DEPARTMENTS = tuple(sorted({s.dossier_department for s in PLAYBOOK}))


def for_department(name: str) -> tuple[PlaybookSop, ...]:
    return tuple(s for s in PLAYBOOK if s.department == name)


def runnable_sops() -> tuple[PlaybookSop, ...]:
    return tuple(s for s in PLAYBOOK if s.runnable)


def unassigned() -> tuple[PlaybookSop, ...]:
    """SOPs with no home in this build. A list, so it cannot be forgotten."""
    return tuple(s for s in PLAYBOOK if not s.runnable)


__all__ = [
    "AGENT_BY_DEPARTMENT",
    "BY_CODE",
    "DEPARTMENTS",
    "DOSSIER_DEPARTMENTS",
    "FORBIDDEN_DECIDE",
    "FORBIDDEN_STOP_WORK",
    "OFFICE_OF",
    "PLAYBOOK",
    "PlaybookSop",
    "agent_for",
    "by_code",
    "for_department",
    "runnable_sops",
    "unassigned",
]


def by_code(code: str) -> PlaybookSop | None:
    return BY_CODE.get(code)
