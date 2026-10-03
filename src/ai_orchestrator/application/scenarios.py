"""Real pieces of work, written out in full so an agent can actually do them.

The "Give work" form took a free-text goal and a choice of agent. That is a
developer interface: it asks the person using it to know both what work exists
and who does it, which is exactly the two things the organisation exists to hold.

So the work is written here -- a purchase requisition with a supplier and an
amount, a CV with a history, a customer with a complaint -- and handed over
whole. Three reasons that matters:

* **An agent can finish one.** "Reconcile the tender documents" names a task with
  no document, no supplier and no amount, so the only completion available is
  prose about the absence of documents. A requisition for 47,000,000 VND of
  steel against a named supplier can be checked against a limit, and the answer
  is a decision rather than a sentence.
* **The work has a home.** Each scenario names the department that owns it, which
  is what makes the three-tier delegation worth running: the CEO cannot do this,
  the office cannot do this, and the department can.
* **The artefacts come from the data, not from the imagination.** A quote that
  invents a supplier is not wrong in an interesting way -- it is wrong in the way
  that matters, which is a reviewer approving a purchase against a supplier who
  does not exist.

Every amount is fictional, every person is fictional, and every document id is in
the reserved-looking range so nobody mistakes the corpus for a record of a real
company.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_orchestrator.seed import DEPARTMENTS as _SEED_DEPARTMENTS


@dataclass(frozen=True, slots=True)
class Scenario:
    """One piece of work, complete enough that finishing it is possible.

    `expected_output` is not decoration: it is the contract the run is held to,
    and it is the reason a stage can fail for producing the wrong thing. A task
    with no declared output can be "completed" by writing anything at all, which
    is how a platform learns to say yes to work it did not do.
    """

    key: str
    title: str
    #: Vietnamese, because the people reading the board are the people this is
    #: for, and a demo written only in the language of the codebase is a demo
    #: its own authors are the only audience for.
    goal: str
    #: Which department owns it. Drives the delegation the run should perform,
    #: and is checked afterwards -- a run that finished without reaching this
    #: department has not demonstrated anything.
    department: str
    #: The office that department reports to. Stated rather than looked up, and
    #: the reason is worth being plain about: the scripted runner below routes
    #: from this field, so it is told the answer rather than finding it. Finding
    #: it is the live model's job, and the two are not claimed to be the same
    #: thing -- this harness proves the chain reaches the owning department and
    #: that the department produces its declared output. It does not prove a
    #: model picks the right office, and nothing here should be read as saying it
    #: does. Left out, the fallback picked whichever name sorted first, and all
    #: seven scenarios went to Back Office and then Finance regardless of what
    #: they were about.
    office: str
    task_type: str = "analysis"
    expected_output: dict[str, object] = field(default_factory=dict)
    #: Facts a run needs that are not otherwise in the database.
    context: dict[str, object] = field(default_factory=dict)
    #: Shown on the picker. Says what a person would expect to come back.
    deliverable: str = ""
    #: What the **chief** is asked, in one line, with no data in it.
    #:
    #: Separate from `goal` on purpose, and the separation is load-bearing. `goal`
    #: is the material a department works from -- the policy, the three amounts. Hand
    #: that to the chief and the chief answers it: two consecutive real-model runs
    #: failed with `no_delegation` because the cheapest answer to "here is the
    #: policy and here are the three claims, decide" is to decide. The chief has
    #: to be given something it *cannot* answer alone, or the first tier of the
    #: organisation is decorative.
    objective: str = ""


#: The catalogue. Ordered so the cheapest, most self-contained work comes first:
#: it is what a person should run first, because it is the one most likely to
#: work first time.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        key="expense-policy",
        objective=(
            "Quyết định duyệt hay từ chối 3 khoản chi này theo chính sách của "
            "công ty, và nêu rõ khoản nào cần cấp trên."
        ),
        office="back-office",
        title="Chấm nhận 3 khoản chi vượt hạn mức",
        goal=(
            "Kiểm tra 3 khoản chi dưới đây so với chính sách chi phí của công ty, "
            "và đề xuất duyệt hoặc từ chối từng khoản kèm lý do.\n\n"
            "Chính sách: mỗi khoản dưới 5.000.000 VND không cần duyệt; "
            "từ 5.000.000 đến 20.000.000 VND cần trưởng bộ phận duyệt; "
            "trên 20.000.000 VND phải có Giám đốc điều hành duyệt.\n\n"
            "1. Công ty Minh Châu, hóa đơn thuê xe 6 tháng: 3.600.000 VND, "
            "có đơn vị cung cấp, đủ chứng từ.\n"
            "2. Văn phòng pháp luật, phí tư vấn hợp đồng nhà thầu Bãi Trầm: "
            "32.000.000 VND, có hợp đồng.\n"
            "3. Cửa hàng Kiên Phát, tiền thuê kho hàng quý II: 18.500.000 VND, "
            "thiếu hợp đồng thuê kho."
        ),
        department="finance",
        task_type="decision",
        expected_output={
            "verdicts": "mỗi khoản: duyệt / duyệt có điều kiện / từ chối",
            "reason": "lý do theo chính sách",
        },
        deliverable="Quyết định 3 khoản, mỗi khoản một dòng kèm lý do",
        context={"policy": "5tr duyệt trưởng; >20tr duyệt GĐH", "currency": "VND"},
    ),
    Scenario(
        key="supplier-tender",
        objective=("Chọn nhà thầu cho gói thiết bị điều hòa dự án Bãi Trầm, nêu lý do và rủi ro."),
        office="front-office",
        title="So sánh 3 báo giá thiết bị HVAC",
        goal=(
            "So sánh 3 báo giá thiết bị điều hòa cho dự án Bãi Trầm và "
            "đề xuất nhà thầu trúng thầu.\n\n"
            "Báo giá A — Công ty Cơ Điện Hải Phòng: 1.240.000.000 VND, "
            "bảo hành 3 năm, giao 45 ngày.\n"
            "Báo giá B — Công ty Điện Lạnh Việt: 1.180.000.000 VND, bảo hành 2 năm, giao 60 ngày.\n"
            "Báo giá C — Công ty Toàn Cầu: 1.090.000.000 VND, bảo hành 2 năm, giao 90 ngày.\n\n"
            "Tiêu chí theo thứ tự ưu tiên: giá, bảo hành, thời gian giao. "
            "Nêu rõ điểm mạnh và điểm yếu của lựa chọn đề xuất."
        ),
        department="procurement",
        task_type="decision",
        expected_output={
            "winner": "mã báo giá được chọn",
            "reason": "lý do theo tiêu chí",
            "risk": "rủi ro thấy được",
        },
        deliverable="Báo giá được chọn kèm lý do và rủi ro",
        context={"project": "Bãi Trầm", "currency": "VND"},
    ),
    Scenario(
        key="cv-screen",
        objective="Đánh giá hồ sơ ứng viên theo thang điểm của dự án và kết luận có điều kiện.",
        office="middle-office",
        title="Rà soát CV kỹ sư chất lượng",
        goal=(
            "Đọc CV dưới đây và chấm điểm theo thang 3 mức của dự án: "
            "Tốt 100%, Trung bình 60%, Kém 20%.\n\n"
            "Hồ sơ Nguyễn Minh Tuấn — 7 năm kiểm soát chất lượng tại xây dựng, "
            "3 năm quản lý nhà thầu, chứng chỉ QA/QC nội bộ còn hiệu lực, "
            "từng trực site dự án cầu Vĩnh Tuyên (2.500 căn).\n\n"
            "Yêu cầu vị trí: 5 năm QA/QC dự án dân dụng, chứng chỉ, biết lập Hồ sơ nghiệm thu.\n\n"
            "Cho điểm từng tiêu chí và kết luận có điều kiện, không nói chung chung."
        ),
        department="qa",
        task_type="review",
        expected_output={
            "score": "điểm 100/60/20 theo từng tiêu chí",
            "verdict": "kết luận có điều kiện cụ thể",
        },
        deliverable="Điểm theo tiêu chí + kết luận có điều kiện",
    ),
    Scenario(
        key="customer-complaint",
        objective=(
            "Xử lý khiếu nại khách hàng về thấm nước: đánh giá trách nhiệm, đề xuất "
            "bồi thường, soạn thư trả lời."
        ),
        office="front-office",
        title="Xử lý khiếu nại khách hàng lớn",
        goal=(
            "Khách hàng Công ty Đông Á phản ánh 40 căn hộ bị thấm nước sau mưa, "
            "đã gửi 3 lần qua bản tin. Họ yêu cầu giải quyết trong 15 ngày.\n\n"
            "Hợp đồng quy định bảo hành 24 tháng; căn hộ bàn giao tháng 3 năm nay, "
            "nên còn trong thời hạn. Khách đã đóng 70% giá trị căn hộ.\n\n"
            "Soạn: (1) đánh giá trách nhiệm của chủ đầu tư, (2) đề xuất phương án bồi thường, "
            "(3) nội dung trả lời khách hàng. Nêu rõ điều khoản hợp đồng áp dụng."
        ),
        department="sales",
        task_type="analysis",
        expected_output={
            "liability": "đánh giá trách nhiệm",
            "compensation": "phương án bồi thường cụ thể",
            "reply": "nội dung trả lời",
        },
        deliverable="Đánh giá trách nhiệm + phương án bồi thường + thư trả lời",
    ),
    Scenario(
        key="contract-review",
        objective="Rà soát hợp đồng thầu phụ và đề xuất sửa các điều khoản rủi ro cho chủ đầu tư.",
        office="middle-office",
        title="Rà soát hợp đồng thầu phụ",
        goal=(
            "Rà soát hợp đồng thầu phụ sau và nêu các điều khoản rủi ro cho chủ đầu tư.\n\n"
            "Điểm cần chú ý: phạt chậm bàn giao 0,03%/ngày nhưng không có trần; "
            "bảo hành 6 tháng trong khi công trình chính là 24 tháng; "
            "không có điều khoản buộc bồi thường khi chủ thầu sai khối lượng nghiệm thu; "
            "thanh toán 60/40 nhưng 40% cuối chỉ trả sau khi công trình đã nghiệm thu.\n\n"
            "Cho từng điểm: vì sao là rủi ro, và nên đề xuất sửa thế nào."
        ),
        department="design",
        task_type="review",
        expected_output={
            "risks": "từng điều khoản rủi ro",
            "proposed_changes": "đề xuất sửa cụ thể",
        },
        deliverable="Danh sách rủi ro + đề xuất sửa điều khoản",
    ),
    Scenario(
        key="progress-report",
        objective="Lập báo cáo tiến độ dự án Bãi Trầm cho ban lãnh đạo, nêu rõ vấn đề cần quyết.",
        office="middle-office",
        title="Báo cáo tiến độ dự án Bãi Trầm",
        goal=(
            "Tổng hợp tiến độ dự án Bãi Trầm trong 3 tháng gần nhất.\n\n"
            "Khoản 1 (hạ tầng): 82% khối lượng, đúng hạn.\n"
            "Khoản 2 (thân vỏ): 64%, chậm 12 ngày do chờ cấp thép.\n"
            "Khoản 3 (nội thất 240 căn): 41%, trễ 25 ngày, rủi ro cao.\n\n"
            "Tổng tiến độ dự án và các khoản vay đã ký: 1.820 tỷ đồng.\n\n"
            "Báo cáo cho ban lãnh đạo: tiến độ, 2 vấn đề cần quyết định, và đề xuất cụ thể. "
            "Không dùng phần trăm đẹp — nêu số thật kèm ngày."
        ),
        department="design",
        task_type="report",
        expected_output={
            "progress": "tiến độ theo từng khoản",
            "decisions_needed": "vấn đề cần lãnh đạo quyết",
            "recommendation": "đề xuất cụ thể",
        },
        deliverable="Báo cáo 3 phần: tiến độ, quyết định cần, đề xuất",
    ),
    Scenario(
        key="offer-approval",
        objective=(
            "Đề xuất mức lương và ngày bắt đầu cho ứng viên, và cho biết có cần "
            "trình cấp trên duyệt không."
        ),
        office="back-office",
        title="Duyệt lương và ngày bắt đầu cho ứng viên",
        goal=(
            "Đề xuất mức lương và ngày bắt đầu cho ứng viên dưới đây, theo lương thị trường "
            "và ngân sách đã duyệt cho vị trí.\n\n"
            "Hồ sơ Trần Thị Mai — 6 năm thu mua, 2 năm trưởng nhóm, "
            "ứng viên đề nghị 28 triệu/tháng.\n"
            "Thị trường cho vị trí này: 22-27 triệu. Ngân sách đã duyệt tối đa 26 triệu.\n"
            "Quy chế: trả cao hơn thị trường tối đa 5% và cần Giám đốc điều hành duyệt.\n\n"
            "Cho ra: mức đề xuất, ngày bắt đầu, và nêu rõ việc này có cần trình Giám đốc "
            "điều hành duyệt hay không."
        ),
        department="hr",
        task_type="decision",
        expected_output={
            "salary": "mức lương đề xuất bằng số",
            "start_date": "ngày bắt đầu",
            "needs_exec_approval": "có cần trình GĐH duyệt không",
        },
        deliverable="Mức lương + ngày bắt đầu + kết luận có cần GĐH duyệt",
        context={"budget_cap_vnd": 26_000_000, "market": "22-27tr"},
    ),
    Scenario(
        key="hiring-pipeline",
        objective=(
            "Soạn JD cho vị trí Kỹ sư Chất lượng, xây dựng rubric chấm điểm và rà soát "
            "hồ sơ ứng viên, theo thứ tự đó"
        ),
        office="back-office",
        title="Tuyển Kỹ sư Chất lượng: JD → rubric → CV",
        goal=(
            "Tuyển một Kỹ sư Chất lượng cho nhà máy Bãi Trầm. Làm theo đúng ba bước sau, "
            "mỗi bước dựa vào bước trước, và dừng lại để người duyệt ở đúng chỗ.\n\n"
            "BƯỚC 1 — JD (cần người duyệt). Viết mô tả công việc: nhiệm vụ, yêu cầu bắt buộc, "
            "yêu cầu lý lẽm, mức lương dự kiến và nơi làm việc. JD là văn bản công khai, "
            "nên **phần lương và điều khoản phải được một người duyệt trước khi đăng** — "
            "hãy yêu cầu phê duyệt cho phần đó và ghi rõ bạn đang chờ.\n\n"
            "BƯỚC 2 — Rubric (cần người duyệt). Từ JD đó, xây dựng thang chấm điểm có trọng "
            "số: mỗi tiêu chí, mức độ tốt/khá/trung bình/kém, và điểm số cụ thể cho từng "
            "mức. Rubric quyết định ai được tham gia phỏng vấn, nên **phải được người duyệt "
            "trước khi dùng để chấm**.\n\n"
            "BƯỚC 3 — Rà soát CV. Dùng rubric đã duyệt để chấm hồ sơ dưới đây, chấm theo "
            "đúng tiêu chí và trọng số đã định nghĩa, rồi kết luận ai đi phỏng vấn.\n\n"
            "Hồ sơ ứng viên:\n"
            "— Nguyễn Thị Lan: 7 năm QA trong ngành điện tử, chứng chỉ ISO 9001 Lead "
            "Auditor, từng dẫn dắt 3 audit nhà cung cấp, thành thạo SAP QM.\n"
            "— Trần Minh Hùng: 4 năm kiểm soát chất lượng sản xuất cơ khí, đọc bản vẽ kỹ "
            "thuật, biết kiểm tra theo ISO 9001 nhưng chưa từng dẫn dắt audit.\n"
            "— Lê Thị Mai: 9 năm ở phòng QC ngành dệt may, mạnh kiểm tra mắt thẻ độ, yếu về "
            "kiểm tra hệ thống và phân tích dữ liệu lỗi."
        ),
        department="hr",
        task_type="coordination",
        expected_output={
            "jd": "mô tả công việc đã soạn",
            "rubric": "thang chấm điểm và trọng số",
            "shortlist": "kết luận mỗi ứng viên và ai đi phỏng vấn",
            "approvals_needed": "những phần đang chờ người duyệt",
        },
        deliverable=(
            "JD, rubric có trọng số, và kết luận shortlist — với hai phần cần người duyệt "
            "được nêu rõ"
        ),
        context={"company": "Bãi Trầm", "currency": "VND", "role": "Kỹ sư Chất lượng"},
    ),
    Scenario(
        key="bom-sourcing",
        objective=(
            "Lập danh mục vật tư cho dự án Bãi Trầm và phân tích chất lượng nhà cung cấp "
            "theo từng nhóm linh kiện"
        ),
        office="front-office",
        title="BOM dự án Bãi Trầm + đánh giá nhà cung cấp",
        goal=(
            "Dự án Bãi Trầm cần lắp đặt một dây chuyền lạnh công nghiệp. Hãy làm hai việc.\n\n"
            "VIỆC 1 — BOM. Lập danh mục vật tư (BOM) gồm các nhóm sau, với số lượng, đơn vị "
            "và chủng loại đề xuất: máy nén xoay 50HP, tủ điện tối thiểu 400kW, dàn trao "
            "đổi nhiệt bằng nhựp gọn, ống thép đồng kích thước DN150, van điện từ, và bơm "
            "tuần hoàn.\n\n"
            "VIỆC 2 — Đánh giá chất lượng. Với từng nhóm, so sánh chất lượng của các nhà cung "
            "cấp dưới đây theo đúng tiêu chí kỹ thuật **và** rủi ro vận hành, rồi chọn "
            "đề xuất. Nêu rõ điểm mạnh, điểm yếu và rủi ro của lựa chọn đề xuất.\n\n"
            "Nhà cung cấp:\n"
            "A — Cơ Điện Hải Phòng: máy nén Đặc Quốc 3 năm, tủ điện Schneider, có chứng "
            "chỉ ISO 9001 và bảo hành tại chỗ trong 24 giờ.\n"
            "B — Điện Lạnh Việt: máy nén Copeland, tủ điện tiết kiệm năng lượng hơn, "
            "chứng chỉ ISO 9001 nhưng bảo hành qua đại lý và không có kho phụ tùng tại chỗ.\n"
            "C — Toàn Cầu: giá thấp nhất, không có chứng chỉ ISO 9001, bảo hành 12 tháng, "
            "nhiều dự án tương tự đã hoàn thành.\n\n"
            "Lưu ý quy trình: **phần chọn nhà cung cấp cuối cùng cần người ký duyệt** vì nó "
            "gắn với chi phí cam kết. Hãy làm đến mức đề xuất và nêu rõ phần nào đang chờ "
            "phê duyệt."
        ),
        department="procurement",
        task_type="coordination",
        expected_output={
            "bom": "danh mục vật tư theo từng nhóm",
            "quality_comparison": "đánh giá chất lượng từng nhóm giữa các nhà cung cấp",
            "recommended": "lựa chọn đề xuất kèm lý do",
            "approvals_needed": "những phần đang chờ người ký",
        },
        deliverable=(
            "BOM đầy đủ, đánh giá chất lượng theo từng nhóm linh kiện, và lựa chọn đề xuất chờ ký"
        ),
        context={"project": "Bãi Trầm", "currency": "VND"},
    ),
)


def department_name(slug: str) -> str:
    """The department's name from its slug, read from the seed.

    So a scenario says `"qa"` and the roster says `"QA/QC-HSE"`, and the two are
    the same unit because one of them says so. Two hand-written slug-to-name maps
    is how "QA" and "Quality" end up as two different departments.
    """
    for spec in _SEED_DEPARTMENTS[1:]:
        if spec.slug == slug:
            return spec.name
    msg = f"no seeded department has the slug {slug!r}"
    raise KeyError(msg)


def agent_for(slug: str) -> str:
    """The agent that does a department's work, by the department's slug."""
    from ai_orchestrator.application.playbook import agent_for as _agent_for

    name = _agent_for(department_name(slug))
    if name is None:  # pragma: no cover - the seed and the playbook are asserted equal
        msg = f"the department {slug!r} has no agent in the playbook's map"
        raise KeyError(msg)
    return name


def by_key(key: str) -> Scenario | None:
    return next((s for s in SCENARIOS if s.key == key), None)


def catalogue() -> list[dict[str, object]]:
    """The picker payload: everything the page needs, nothing it does not."""
    return [
        {
            "key": s.key,
            "title": s.title,
            "goal": s.goal,
            "department": s.department,
            # The route, stated. Without these two a scenario picked in the
            # browser carries no routing fact at all, and the work stops at the
            # office that received it.
            "office": s.office,
            "agent_name": agent_for(s.department),
            "task_type": s.task_type,
            "expected_output": s.expected_output,
            "deliverable": s.deliverable,
        }
        for s in SCENARIOS
    ]
