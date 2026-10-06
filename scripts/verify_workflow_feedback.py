"""Explicit real-provider feedback probe. Creates only a paused synthetic campaign.

No SMTP, supplier order, human approval, published skill or production account.
The provider ledger distinguishes live model calls from fixture content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from ai_orchestrator.application.business_workflow import create_workflow
from ai_orchestrator.application.workflow_commands import command_for
from ai_orchestrator.application.workflow_feedback import WorkflowFeedbackService, request_feedback
from ai_orchestrator.application.workflow_fixtures import hiring_fixture
from ai_orchestrator.domain.contracts import Actor
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.persistence.models import ModelUsage, Task
from ai_orchestrator.persistence.session import Database


async def run(org: str, output: Path, resume: str | None = None):
    db = Database.from_settings()
    actor = Actor(id="feedback-probe-system", kind=ActorType.SYSTEM)
    try:
        service = WorkflowFeedbackService(db, org)
        if resume:
            root = resume
            async with db.tenant_session(org) as session:
                task = await session.get(Task, root)
                command = await command_for(session, org, root)
                assert task and task.input.get("mode") == "simulation"
                assert task.input.get("brief", {}).get("synthetic") is True
                assert command and command.feedback.get("requested_by") == str(actor.id)
                first = dict(command.feedback)
            await service.answer(
                root,
                actor,
                revision=first["revision"],
                answers=first.get("answers", "")
                + "\nLàm rõ tình huống thử: đợt probe này chưa nhận CV hay gửi mail. Hộp thư"
                " do connector quản lý; bước test_mail của campaign sẽ dùng CV tổng hợp"
                " đã đăng ký. Người phụ trách an toàn chưa được phân công nên cần chờ"
                " người vận hành cung cấp nguồn. Mọi gate cần người thật theo vai trò;"
                " agent chỉ soạn hoặc thu nhận chứng cứ. Để chạy thật sẽ cần campaign"
                " live mới được Boss xác nhận; probe này giữ dừng, không đổi mode.",
            )
            await service.assess(root)
        else:
            async with db.tenant_session(org) as session:
                root = await create_workflow(
                    session, org, "mep_hiring", "simulation", hiring_fixture()
                )
                await request_feedback(
                    session,
                    org,
                    root,
                    (
                        "Boss yêu cầu tuyển kỹ sư MEP có kinh nghiệm commissioning. Hãy lập kế "
                        "hoạch cụ thể và hỏi lại nếu brief chưa đủ thông tin. Không giả lập phỏ"
                        "ng vấn, quyết định con người hay onboarding đã xong."
                    ),
                    actor,
                )
            await service.assess(root)
        if not resume:
            async with db.tenant_session(org) as session:
                command = await command_for(session, org, root)
                assert command is not None
                first = dict(command.feedback)
        if not resume and first.get("assessment", {}).get("questions"):
            await service.answer(
                root,
                actor,
                revision=first["revision"],
                answers=(
                    "Trả lời các câu hỏi: kinh nghiệm MEP và commissioning tối thiểu ba năm,"
                    " kiểm chứng qua CV và phỏng vấn kỹ thuật. Làm việc tại công trường dự án"
                    " hư cấu demo ở Hà Nội. HSE yêu cầu huấn luyện an toàn công trường, cô lập"
                    " điện và làm việc trên cao; chứng cứ do người phụ trách an toàn xác minh."
                    " BIM dùng Revit MEP và Navisworks. Ngày bắt đầu cố định theo brief,"
                    " 2026-10-15; không cần hỏi lại về độ linh hoạt. Lương theo brief đã ghi."
                    " Chỉ dùng nguồn SOP đã đă"
                    "ng ký. HR duyệt JD/rubric, Design thực hiện phỏng vấn kỹ thuật; Boss d"
                    "uyệt lựa chọn và offer. Onboarding cần bằng chứng HR, an toàn và bàn g"
                    "iao mentor. Những dữ liệu chưa có phải hỏi hoặc chờ; không tuyên bố ho"
                    "àn thành."
                ),
            )
            await service.assess(root)
        async with db.tenant_session(org) as session:
            command = await command_for(session, org, root)
            assert command is not None
            feedback = command.feedback
            ids = [
                feedback["assessment_task_id"],
                *[h["assessment_task_id"] for h in feedback.get("history", [])],
            ]
            usage = (
                await session.scalars(
                    select(ModelUsage).where(
                        ModelUsage.organization_id == org, ModelUsage.task_id.in_(ids)
                    )
                )
            ).all()
            real_provider = bool(usage) and all(
                u.provider not in {"fake", "scripted", "deterministic", "unit-fake"} for u in usage
            )
            assessments = (
                await session.scalars(
                    select(Task).where(Task.organization_id == org, Task.id.in_(ids))
                )
            ).all()
            passed = (
                feedback["state"] == "awaiting_confirmation"
                and real_provider
                and all(t.status == "completed" for t in assessments)
            )
            report = {
                "passed": passed,
                "root": root,
                "scope": "paused_synthetic_feedback_probe",
                "human_approval": False,
                "first_assessment": first,
                "feedback": feedback,
                "model_calls": len(usage),
                "models": sorted({u.model_used for u in usage}),
                "tokens": sum(
                    u.input_tokens + u.output_tokens + (u.reasoning_tokens or 0) for u in usage
                ),
                "provider_output_tokens": sum(u.output_tokens for u in usage),
                "provider_reasoning_tokens": sum(u.reasoning_tokens or 0 for u in usage),
                "model_latency_ms": sum(u.latency_ms for u in usage),
                "cost_usd": str(sum(u.cost_usd for u in usage)),
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(output.write_text, json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps(
                {k: v for k, v in report.items() if k not in {"feedback", "first_assessment"}},
                ensure_ascii=False,
            )
        )
        if not passed:
            raise SystemExit("Feedback probe failed; the report retains its actual error and usage")
    finally:
        await db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--resume", help="Reassess only an existing synthetic SYSTEM feedback probe"
    )
    args = parser.parse_args()
    asyncio.run(run(args.org, args.output, args.resume))
