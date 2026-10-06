"""Keep generated prose readable while preserving source quotations and names."""

import re
from typing import Any


def validate_generated_text(value: Any, allowed_names: set[str] | None = None) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "evidence_quote":
                validate_generated_text(child, allowed_names)
    elif isinstance(value, list):
        for child in value:
            validate_generated_text(child, allowed_names)
    elif isinstance(value, str):
        if re.search(r"</?(?:arg_key|arg_value|tool_call|parameter)\b", value, re.I):
            raise ValueError("Replace protocol placeholders with actual artifact text")
        prose = value
        for name in sorted(allowed_names or (), key=len, reverse=True):
            if name:
                prose = prose.replace(name, "")
        if re.search(r"[\u4e00-\u9fff]", prose):
            raise ValueError(
                "Use Vietnamese or English consistently for generated prose; preserve source"
                " quotations and names but replace unexplained Chinese fragments"
            )
