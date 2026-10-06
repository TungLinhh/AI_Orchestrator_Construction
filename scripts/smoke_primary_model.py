"""Check the selected primary model directly, including one tool call, without fallbacks."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.models.gateway import ModelRequest
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.models.providers import build_providers_from_settings


async def main(output: Path) -> int:
    settings = get_settings()
    candidate = default_profiles()["primary"].candidates[0]
    providers = build_providers_from_settings(settings)
    provider = providers.get(candidate.provider)
    report = {"observed_at": datetime.now(UTC).isoformat(), "candidate": candidate.key()}
    try:
        if provider is None:
            raise ValueError(f"no configured adapter for {candidate.provider}")
        response = await provider.complete(
            candidate,
            ModelRequest(
                profile="primary",
                prompt='Call report_check with {"ok": true}. Do not use any other tool.',
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "report_check",
                            "description": "Record the successful model connectivity check.",
                            "parameters": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                max_output_tokens=4096,
                data_classification=DataClassification.PUBLIC,
            ),
        )
        called = any(
            call.get("function", {}).get("name") == "report_check"
            and json.loads(call["function"].get("arguments", "{}")) == {"ok": True}
            for call in response.tool_calls
        )
        report.update(
            {
                "passed": called,
                "provider": response.provider,
                "model_used": response.model_used,
                "tool_calls": response.tool_calls,
                "tokens": response.total_tokens,
                "latency_ms": response.latency_ms,
                "finish_reason": response.finish_reason,
            }
        )
    except Exception as exc:
        report.update({"passed": False, "error_type": type(exc).__name__, "error": str(exc)})
    finally:
        for adapter in providers.values():
            await adapter.aclose()
    output.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(
        output.write_text, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args().output)))
