import pytest

from ai_orchestrator.domain.workflow_feedback import validate_feedback


def proposal():
    return {
        "understanding": "Cần xác minh tài liệu commissioning",
        "questions": ["Nguồn tài liệu kỹ thuật còn thiếu ở đâu?"],
        "plan": [
            {
                "stage_key": "brief",
                "action": "Kiểm tra căn cứ kỹ thuật",
                "owner": "HR",
                "acceptance": "Có nguồn và người phụ trách xác nhận",
            }
        ],
        "skill_lesson": "Hỏi về chứng cứ còn thiếu trước khi đề xuất lựa chọn.",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("questions", ["<arg_key>questions</arg_key>"]),
        ("questions", ["SOP nào là哪些 tài liệu?"]),
        ("plan", []),
    ],
)
def test_protocol_noise_and_empty_plans_are_refused(field, value):
    output = {**proposal(), field: value}
    with pytest.raises(ValueError):
        validate_feedback(output, {"HR"})


def test_invented_department_is_refused():
    output = proposal()
    output["plan"][0]["owner"] = "Unregistered department"
    with pytest.raises(ValueError):
        validate_feedback(output, {"HR"})


def test_actual_question_and_bounded_plan_are_accepted():
    validate_feedback(proposal(), {"HR"})


def test_damaged_text_and_wrong_workflow_order_are_refused():
    output = proposal()
    output["understanding"] = "Phỏng vấ\ufffd HR"
    with pytest.raises(ValueError, match="Unicode"):
        validate_feedback(output, {"HR"})
    output = proposal()
    output["plan"] = [
        {**output["plan"][0], "stage_key": stage} for stage in ("jd", "headcount_review")
    ]
    with pytest.raises(ValueError, match="workflow order"):
        validate_feedback(output, {"HR"}, ["brief", "headcount_review", "jd"])


def test_a_model_cannot_assign_a_human_review_to_an_agent():
    output = proposal()
    output["plan"][0].update(stage_key="jd_review", owner="HR Agent")
    with pytest.raises(ValueError, match="human role"):
        validate_feedback(output, {"HR Agent", "HR"}, ["jd_review"], {"jd_review"})
    output["plan"][0]["owner"] = "HR"
    validate_feedback(output, {"HR Agent", "HR"}, ["jd_review"], {"jd_review"})


def test_repeated_question_with_different_case_or_punctuation_is_refused():
    output = proposal()
    output["questions"] = ["Nguồn tài liệu ở đâu?", "  NGUỒN tài liệu ở đâu ! "]
    with pytest.raises(ValueError, match="repeat the same question"):
        validate_feedback(output, {"HR"})
