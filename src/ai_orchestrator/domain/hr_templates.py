"""Independent HR cycles, scoped to exact steps of the O-Nexus source SOPs."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HRTemplate:
    key: str
    title: str
    trigger: str
    sources: tuple[tuple[str, tuple[int, ...]], ...]
    inputs: tuple[str, ...]


HR_TEMPLATES = (
    HRTemplate(
        "recruitment",
        "Tuyển dụng và onboarding",
        "Nhu cầu tuyển đã được Boss duyệt",
        (("ONX-BO-HR-SOP-004", (1, 2, 3, 4, 5)),),
        ("headcount_brief",),
    ),
    HRTemplate(
        "performance",
        "Đánh giá hiệu suất",
        "Đến kỳ đánh giá bán niên",
        (("ONX-BO-HR-SOP-004", (6,)),),
        ("performance_evidence",),
    ),
    HRTemplate(
        "payroll",
        "Đối soát và đề xuất bảng lương",
        "Chốt kỳ chấm công",
        (("ONX-BO-HR-SOP-005", (1, 2, 3, 4, 5, 6, 7)),),
        ("timesheets", "approved_salary_policy"),
    ),
    HRTemplate(
        "offboarding",
        "Offboarding và bàn giao",
        "Quyết định nhân sự của người có thẩm quyền",
        (("ONX-BO-HR-SOP-004", (7,)),),
        ("human_personnel_decision", "handover_sources"),
    ),
)


def hr_template(key: str) -> HRTemplate:
    return next(template for template in HR_TEMPLATES if template.key == key)
