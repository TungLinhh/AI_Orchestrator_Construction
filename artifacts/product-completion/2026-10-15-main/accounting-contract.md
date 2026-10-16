# R0-HLD-02, Codex accounting and invocation corrections

Reproduction: harness-accounting-reproduction.txt, three FAIL before fixes.
TaskExecutionService._record_model_usage records one ModelUsage per provider call (application/task_execution.py:2088-2140). The same execution's aggregate is recorded later (lines 2160-2195). api/tasks.py exposes both. They describe the same calls, so adding them doubles tokens/cost.
The prior unit expectation 600 input/240 output/$0.006 added execution 300/120/$0.003 to ledger 300/120/$0.003. Correct authoritative ledger totals are 300/120/$0.003. Both raw aggregates are still asserted separately. This strengthens reconciliation rather than changing a model's expected answer.
Completed task evidence now requires both execution and ledger. Non-free IDs are refused even with a custom allowlist. Server owner's profile/fallback chain is verified before any model task is created.
Dependency preflight imports the actual domain validator and render libraries before opening an HTTP client. A missing interpreter dependency yields a durable setup-error receipt, exit 2 and zero calls.
Tests: 58 PASS, including same-call double-count prevention, missing-ledger rejection, paid-allowlist rejection, missing-import-before-network and hidden paid fallback/cycle.
Original full run 240s remains FAIL. Corrected invocation is a new full run with a separately recorded 360s diagnostic window; no SLA comparison, no erasure of first timeout, no assertion/schema weakening and no corpus changes.
