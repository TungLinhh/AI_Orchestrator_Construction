import pytest

from ai_orchestrator.domain.workflow_text import validate_generated_text


def test_generated_language_fragments_and_protocol_noise_are_refused():
    for text in ("Điểm 31低于 ngưỡng", "Có thể加班", "<arg_key>reason</arg_key>"):
        with pytest.raises(ValueError):
            validate_generated_text({"rationale": text})


def test_original_quotes_and_supplied_names_are_preserved():
    validate_generated_text(
        {"reason": "Ứng viên 王明 cần phỏng vấn", "evidence_quote": "工程师原文"}, {"王明"}
    )
