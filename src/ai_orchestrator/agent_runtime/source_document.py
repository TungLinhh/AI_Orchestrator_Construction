"""Source reports reuse the bounded artifact repair and fallback runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime
from ai_orchestrator.domain.contracts import AgentContext, AgentResult, AgentTask
from ai_orchestrator.domain.source_report import report_schema, validate_report


class SourceDocumentRuntime(WorkflowEvidenceRuntime):
    name = "source_document"
    output_token_limit = 3500
    repair_evidence_instruction = (
        "Every selected source still requires a non-empty exact citation. "
        "Put missing or unverified information in limitations, never in an empty quote. "
    )
    transient_response_retries = 1
    transient_gateway_retries = 1
    system_instruction_suffix = (
        "\nYou are analyzing selected source documents to produce a report. Your "
        "deliverable is a structured report with findings, each backed by an exact "
        "citation from the sources.\n"
        "\n"
        "Rules:\n"
        "- Only analyze the sources provided. No external actions or delegation.\n"
        "- Cite source_id, exact locator and verbatim quote in each finding, "
        "covering every source.\n"
        "- Copy locator from sources[].segments[].locator exactly; never invent "
        "row/page labels.\n"
        "- Quote a short single-line exact passage, not an entire table.\n"
        "- Write a substantive summary of at least two sentences (60+ characters) "
        "stating the conclusion; for a quote comparison name the winner, the "
        "winning total in VND, comparing eligibility and totals for the other quotes.\n"
        "- State missing/unverified information explicitly.\n"
        "- No invented approvals or completed transactions.\n"
        "- Invoke submit_evidence as a tool call; never write the artifact or the "
        "tool call itself as prose or fenced JSON.\n"
        "- For quote comparison copy the deterministic expected winner/total, "
        "explaining eligibility.\n"
        "\n"
        "If a required source is missing or insufficient, state what is missing "
        "rather than guessing. Do not infer information not present in the sources."
    )

    def __init__(self, gateway: Any, source_context: dict[str, Any]) -> None:
        self.source_context = source_context
        super().__init__(
            gateway,
            lambda output: validate_report(
                output, source_context["sources"], source_context["expected"]
            ),
        )

    async def execute(
        self,
        task: AgentTask,
        context: AgentContext,
        *,
        record_usage: Callable[[Any], Awaitable[None]] | None = None,
        execute_tool: Any = None,
    ) -> AgentResult:
        schema = report_schema(self.source_context["expected"] is not None)
        citation_fields = schema["properties"]["findings"]["items"]["properties"]
        citation_fields["quote"]["minLength"] = 8
        citation_fields["source_id"]["enum"] = [s["id"] for s in self.source_context["sources"]]
        citation_fields["locator"]["enum"] = list(
            dict.fromkeys(
                segment["locator"]
                for source in self.source_context["sources"]
                for segment in source["segments"]
            )
        )
        if self.source_context["expected"] is not None:
            # The deterministic calculation already knows whether these values are null.
            # A concrete wire schema avoids ambiguous union grammars in tool adapters.
            for key, value in self.source_context["expected"].items():
                schema["properties"][key] = {
                    "type": "null"
                    if value is None
                    else "integer"
                    if key == "total_vnd"
                    else "string",
                    "enum": [value],
                }
        task = task.model_copy(
            update={
                "input": {
                    "sources": self.source_context["sources"],
                    "expected": self.source_context["expected"],
                },
                "expected_output_schema": schema,
            }
        )
        context = context.model_copy(update={"task": task})
        try:
            return await super().execute(task, context, record_usage=record_usage)
        finally:
            await self.gateway.aclose()
