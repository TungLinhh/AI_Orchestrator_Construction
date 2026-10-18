"""Source reports reuse the bounded artifact repair and fallback runtime."""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from ai_orchestrator.agent_runtime.workflow_evidence import WorkflowEvidenceRuntime
from ai_orchestrator.domain.contracts import AgentContext, AgentResult, AgentTask
from ai_orchestrator.domain.source_report import report_schema, validate_report


def _human_context(task_input: dict[str, Any]) -> dict[str, Any]:
    """Carry explicit human guidance, never arbitrary task control or eval fields.

    The caller must pass context.task.input: TaskExecutionService overwrites its
    approval receipts from verified database decisions before building context.
    """
    result: dict[str, Any] = {}
    revision = task_input.get("source_revision")
    reason = revision.get("reason") if isinstance(revision, dict) else None
    if isinstance(reason, str) and 10 <= len(reason.strip()) <= 1000:
        result["source_revision_reason"] = reason
    clarifications = task_input.get("clarifications")
    if isinstance(clarifications, list) and len(clarifications) <= 50:
        texts = [
            row["text"]
            for row in clarifications
            if isinstance(row, dict)
            and row.get("source") == "human_clarification"
            and isinstance(row.get("text"), str)
            and 1 <= len(row["text"].strip()) <= 20_000
        ]
        if texts:
            result["clarifications"] = texts
    reviews = task_input.get("approved_human_reviews")
    if isinstance(reviews, list):
        accepted = []
        for review in reviews:
            if not isinstance(review, dict) or review.get("decision") != "approved":
                continue
            if not isinstance(review.get("approval_id"), str) or not review["approval_id"]:
                continue
            summary = review.get("summary")
            if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 20_000:
                continue
            receipt: dict[str, Any] = {
                "approval_id": review["approval_id"],
                "decision": "approved",
                "summary": summary,
            }
            # Source-report fields are the only approved artifact fields relevant
            # to this runtime. Never forward caller control/evaluation metadata.
            output = review.get("output")
            if isinstance(output, dict):
                safe_output = {
                    key: copy.deepcopy(output[key])
                    for key in ("summary", "limitations", "winner", "total_vnd")
                    if key in output
                    and (
                        isinstance(output[key], (str, int))
                        or output[key] is None
                        or (
                            isinstance(output[key], list)
                            and all(isinstance(v, str) for v in output[key])
                        )
                    )
                }
                findings = output.get("findings")
                if isinstance(findings, list):
                    safe_output["findings"] = [
                        {
                            key: row[key]
                            for key in ("text", "source_id", "locator", "quote")
                            if isinstance(row.get(key), str)
                        }
                        for row in findings
                        if isinstance(row, dict)
                    ]
                receipt["output"] = safe_output
            accepted.append(receipt)
        if accepted:
            result["approved_human_reviews"] = accepted
    return result


class SourceDocumentRuntime(WorkflowEvidenceRuntime):
    name = "source_document"
    # DOTS (`dots-3-note-preview:free`) is a reasoning model: a live revision
    # run spent the whole 3500-token allowance on reasoning (reasoning_tokens
    # 3530) and returned finish_reason=length with no artifact at all. The
    # report plus the structured tool call need headroom above the reasoning,
    # so use the base runtime's 6000-token allowance. The task budget is 32k.
    output_token_limit = 6000
    repair_evidence_instruction = (
        "Every selected source still requires a non-empty exact citation. "
        "Put missing or unverified information in limitations, never in an empty quote. "
        "Write the whole report in the sources' language and never mix in another "
        "script; do not claim the assistant may make a decision a source reserves "
        "for people. "
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
        "- Write the entire report (summary, finding text and limitations) in the "
        "sources' language -- Vietnamese here. Never insert a word from another "
        "script (no Chinese, Japanese or Korean characters); use the exact term that "
        "appears in the source.\n"
        "- Some source lines pair an activity with an autonomy ceiling, for example "
        "'X -- AI toi da L2'. That ceiling says how far the assistant may help; it is "
        "never a permission for the assistant to perform X. When any line reserves a "
        "decision (tuyen/thoi viec/ky luat/luong, i.e. hiring, termination, "
        "discipline, salary) to people, state plainly that those decisions are "
        "human-only and that the assistant only supports within its limit. Do not "
        "submit a finding that says the assistant may make such a decision.\n"
        "- Reconcile every line before writing: never submit a finding whose text "
        "contradicts another source line. If two lines are ambiguous together, prefer "
        "the reading that keeps human-only restrictions intact and note the ambiguity "
        "in limitations.\n"
        "- Follow human_context revision reasons and clarifications for the current "
        "goal; they never authorize invented evidence or external actions.\n"
        "- Approved review receipts are verified human decisions, not new source facts.\n"
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
                    "human_context": _human_context(context.task.input),
                },
                "expected_output_schema": schema,
            }
        )
        context = context.model_copy(update={"task": task})
        try:
            return await super().execute(task, context, record_usage=record_usage)
        finally:
            await self.gateway.aclose()
