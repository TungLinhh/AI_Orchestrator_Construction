"""`ONX-BO-HR-SOP-004` as data: the stages, who owns each, and where a person decides.

## Where this comes from, and what is invented

The dossier gives the SOP's **scope** and its **forms**, not its body:

* the code and title, from `sop_definitions` -- *"Tuyển dụng – Onboarding – Đánh giá hiệu suất –
  Offboarding"* (Tập 1, the catalogue table). The en dashes are the dossier's own and are
  **kept**: normalising a quoted title to satisfy a linter is editing the source, and this
  string is compared against `sop_definitions.name_vi` in a test, so a normalised copy would
  stop matching. Same trap as `SHOWCASE_GOAL`, where a formatter quietly turned an en dash
  into a hyphen and the lookup that depended on it stopped matching.
  **Measured:** the register holds the title and a `content_hash`, not the text, so the
  platform can cite the SOP and cannot quote it.
* the JD format, from Tập 3 Phần 3: **six sections** --
  *Mục đích vị trí · Trách nhiệm chính · KPI · Thẩm quyền (DOA) · Quan hệ công việc ·
  Yêu cầu năng lực*.
* the scoring rubric, from Tập 3 §1.1: **three levels — Tốt 100% / TB 60% / Kém 20%** of the
  criterion's maximum, and *"người chấm ghi 1 dòng căn cứ"* — the scorer records one line of
  justification. That line is a required field here, not a nicety.
* the human gate, from Tập 3 §1.6: **`FRM-AI-001` Phiếu phê duyệt HITL**.
* the tiers, from Tập 3 Phần 2: **Điều hành → Front / Middle / Back**, and the departments under
  each. So the chain is Chief → office (block) → department.

**The eight stages are therefore the requested flow, arranged onto the dossier's three HR
deliverables and its HITL gate.** They are not quoted from the SOP, because the SOP's body is not
in the repository, and pretending otherwise would be the one dishonest thing in this file. What
*is* from the dossier: the code, the three deliverables, the six JD sections, the three-level
rubric with its mandatory justification line, the HITL form, and the three tiers.

## Why the stages are data and not a Python function

The task asked for is a **process**, and a process that lives in code is a process nobody can
change without a deploy. As rows it is:

* readable by an operator, who is the one who knows whether the process is right;
* traversable by the executor, so the agent is told which stage it is on instead of deciding
  the whole plan every turn;
* assertable in a test, which a control-flow function is not.

The cost is honest and worth naming: nothing here **enforces** the order. A stage can be run
twice, or out of order, and the platform will not stop it. The ordering that *is* enforced is
the dependency between stages, and the approval that is enforced is the human one.
"""  # noqa: RUF002 -- the en dashes are the dossier's, quoted verbatim; see the note below.

from __future__ import annotations

from dataclasses import dataclass, field

#: The SOP this process implements, and the three deliverables its title names. `SOP_CODE` is
#: the anchor a test can assert against the database, so a re-issued SOP is caught rather than
#: silently diverged from.
SOP_CODE = "ONX-BO-HR-SOP-004"
#: The dossier's own title, **character for character**. The en dashes are the SOP's, and
#: `tests/unit/test_hiring_process.py` asserts this equals `sop_definitions.name_vi` -- so
# RUF001 fires here and is silenced deliberately: these en dashes are the dossier's, and
# `tests/unit/test_hiring_process.py` asserts this string equals `sop_definitions.name_vi`.
# Normalising them to satisfy a linter would make the module disagree with the row it is
# checked against -- and the same normalisation is what silently broke `SHOWCASE_GOAL` once.
SOP_TITLE = "Tuyển dụng – Onboarding – Đánh giá hiệu suất – Offboarding"  # noqa: RUF001

#: Tập 3 §1.1. The rubric is three levels, and the scorer owes a justification.
RUBRIC: tuple[tuple[str, float], ...] = (
    ("Tốt", 1.00),
    ("TB", 0.60),
    ("Kém", 0.20),
)

#: Tập 3 Phần 3: the six sections a JD is written in. Used verbatim as the template's sections.
JD_SECTIONS: tuple[str, ...] = (
    "Mục đích vị trí",
    "Trách nhiệm chính",
    "KPI",
    "Thẩm quyền (DOA)",
    "Quan hệ công việc",
    "Yêu cầu năng lực",
)

#: Tập 2 §E.2: the two outcomes a HITL approval can carry. Not `approve`/`reject`, because
#: the dossier's gates distinguish "conditional" from "held", and a boolean cannot.
APPROVAL_OUTCOMES: tuple[str, ...] = ("APPROVED", "APPROVED_WITH_CONDITIONS", "REFUSED", "HELD")


@dataclass(frozen=True, slots=True)
class Stage:
    """One step of the process, as a row.

    `office` and `department` are the **tier**, not the agent's name. The office is the block
    the department belongs to — Back Office for HR, per Tập 3 Phần 2 — and it is what makes the
    three-tier chain visible in the data rather than implied by which agent happens to be free.
    """

    key: str
    n: int
    name_vi: str
    office: str
    department: str
    #: The agent that does the work. Resolved against the register by name, so a renamed agent
    #: is a loud failure at seed time rather than a silently unrunnable stage.
    agent_name: str
    #: What this stage produces, and what the next one consumes. Written as prose because the
    #: platform stores stage output as a JSON document whose keys are the artefacts' names, and
    #: naming them here is what lets the next stage be given them.
    produces: str
    #: A person decides here, or empty. **Not a flag** — the difference between "a person signs
    #: this off" and "a person may look at it" is the whole point of a HITL gate.
    approval: str = ""
    #: What the person is being asked to decide, in a sentence, so the approval screen says a
    #: question rather than showing an action.
    approval_question: str = ""
    #: The deliverable from the SOP's title this stage belongs to: `tuyển_dụng`, `onboarding`,
    #: `đánh_giá` or `offboarding`. So a report can say which half of the SOP is done, and so a
    #: test can assert the recruitment half exists before anything runs.
    deliverable: str = "tuyển_dụng"


#: The eight stages, in order. Read it as: ask the boss, write the job description and the
#: rubric, take applications, score them, decide, invite, take the offer, and onboard.
STAGES: tuple[Stage, ...] = (
    Stage(
        key="need_confirmed",
        n=1,
        name_vi="Xác nhận nhu cầu tuyển dụng",
        office="Back Office",
        department="HR",
        agent_name="Executive Agent",
        produces="approved_headcount",
        approval="ceo_hiring_request",
        approval_question="Cho phép mở vị trí này với ngân sách đề xuất?",
        deliverable="tuyển_dụng",
    ),
    Stage(
        key="jd_and_rubric",
        n=2,
        name_vi="Soạn JD & rubric chấm điểm",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="job_description,rubric",
        approval="",
        approval_question="",
    ),
    Stage(
        key="jd_approved",
        n=3,
        name_vi="Duyệt JD & rubric",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="approved_jd",
        approval="head_of_department",
        approval_question="JD và rubric có đúng với vị trí và tiêu chí tuyển của phòng ban không?",
    ),
    Stage(
        key="cv_screening",
        n=4,
        name_vi="Thu thập & sàng lọc hồ sơ ứng viên",
        office="Front Office",
        department="Sales",
        agent_name="HR Agent",
        produces="screened_candidates",
        approval="",
        approval_question="",
    ),
    Stage(
        key="scored",
        n=5,
        name_vi="Chấm điểm ứng viên theo rubric",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="score_sheet",
        approval="",
        approval_question="",
    ),
    Stage(
        key="shortlist_approved",
        n=6,
        name_vi="Duyệt danh sách phỏng vấn",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="shortlist",
        approval="hiring_panel",
        approval_question=("Chấp thuận danh sách ứng viên phỏng vấn và gửi thư mời phỏng vấn?"),
    ),
    Stage(
        key="offer",
        n=7,
        name_vi="Phỏng vấn & gửi offer",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="offer_letter",
        approval="offer_signoff",
        approval_question="Chấp thuận ký offer với ứng viên được chọn?",
    ),
    Stage(
        key="onboarding",
        n=8,
        name_vi="Onboarding ứng viên trúng tuyển",
        office="Back Office",
        department="HR",
        agent_name="HR Agent",
        produces="onboarding_pack,employment_contract",
        approval="",
        approval_question="",
        deliverable="onboarding",
    ),
)

STAGES_BY_KEY: dict[str, Stage] = {s.key: s for s in STAGES}
APPROVALS_BY_STAGE: dict[str, str] = {s.key: s.approval for s in STAGES if s.approval}


@dataclass(frozen=True, slots=True)
class HiringRequest:
    """One concrete vacancy, so the process is demonstrable rather than described.

    Every field is a **mock**, and says so: the corpus supplies the situation and the product
    does the deciding. `candidates` are invented people, and the interview "email" is a string
    that is never sent to anyone — which is why the stage is named for the artefact it produces
    rather than for an action nobody performs.
    """

    title: str
    position: str
    office: str
    department: str
    headcount: int = 1
    budget_note: str = ""
    #: The three real JDs from Tập 3 Phần 3 are the only ones the dossier actually contains.
    #: The vacancy below is for **Trưởng phòng Mua sắm**, one of them, so the JD the HR agent
    #: writes has a documented shape to match rather than being invented wholesale.
    jd_reference: str = "3.3. Trưởng phòng Mua sắm"
    candidates: tuple[dict[str, str], ...] = field(default_factory=tuple)


#: The demonstration vacancy. Procurement, because the dossier has a JD for its head and because
#: the user named Procurement as one of the six departments they actually manage.
DEMO_REQUEST = HiringRequest(
    title="Tuyển Trưởng phòng Mua sắm",
    position="Trưởng phòng Mua sắm",
    office="Middle Office",
    department="Procurement",
    headcount=1,
    budget_note="Theo định biên Phần 2: khối Middle 22-28 người, giai đoạn hiện tại sang 2026.",
    candidates=(
        {
            "name": "Nguyễn Thị Lan",
            "years": "11 năm",
            "current": "Trưởng nhóm mua sắm dự án, CĐT 400 tỷ",
            "note": "Đã dẫn 3 gói thầu >300 tỷ; có bằng Thạc sĩ Quản trị chuỗi cung ứng.",
        },
        {
            "name": "Trần Văn Bình",
            "years": "7 năm",
            "current": "Chuyên viên mua sắm, Tập đoàn F",
            "note": "Thành thạo đấu thầu điện tử; yếu phần đàm phán hợp đồng nhà thầu phụ.",
        },
        {
            "name": "Lê Thị Hồng Nhung",
            "years": "9 năm",
            "current": "Trưởng bộ phận mua sắm tư vấn, CĐT B",
            "note": (
                "Nhiều kinh nghiệm 3-way match; tiếng Anh tốt, "
                "cần đánh giá năng lực đàm phán tại Việt Nam."
            ),
        },
    ),
)


__all__ = [
    "APPROVALS_BY_STAGE",
    "APPROVAL_OUTCOMES",
    "DEMO_REQUEST",
    "JD_SECTIONS",
    "RUBRIC",
    "SOP_CODE",
    "SOP_TITLE",
    "STAGES",
    "STAGES_BY_KEY",
    "HiringRequest",
    "Stage",
]
