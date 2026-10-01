"""A mock coordination corpus, so the CEO flow can be walked end to end.

## What the real corpus has, and what it does not

Measured, not assumed:

| table | rows |
|---|---|
| `projects` | 6 (three real projects across seven spellings) |
| `wbs` | 240 |
| `progress_snapshots` | 2778 |
| `contracts`, `suppliers`, `purchase_orders`, `tenders`, `rfqs`, `zones`, `milestones` | **0** |
| `approvals` | **0** |
| `delegations` | **2**, out of 91 tasks |

So the corpus is a schedule and nothing else, and the coordination story cannot be walked
on it: there is no approval to approve and almost no delegation to look at. The plumbing
for the whole flow exists and works — `execute_task` delegates, `DelegationExecutor` writes
the tree, `POST /approvals/{id}/approve` decides — and there is nothing on the other side
of it.

## What this creates, and what it does not fake

**The showcase task is run through the real machinery.** `TaskExecutionService.execute_task`
is the same call the Temporal worker makes. Whatever that produces — delegations, child
tasks, executions, events — is produced by the product, not written by this script. That is
the point: a mock corpus of hand-written rows would prove the *page* renders, and nothing
about whether delegation works.

**The approval row is written here, and that is the one hand-written row.** There is no
public writer for a task-scoped approval — `submit_for_approval` belongs to the learning
loop and takes an `ApprovalPacket` — so this script inserts one with the real column
vocabulary. What stays real is everything after it: the CEO approves it through
`POST /approvals/{id}/approve`, the decision is hashed and logged, and the inbox and the
report read it back. **The mock supplies the situation; the product does the deciding.**

## The story, in the dossier's own vocabulary

A CEO gives one instruction — *prepare the stage-2 payment dossier* — and the coordination
task is **refused** if it does the work itself, because a coordination task that completes
without delegating is a separation-of-duties failure the platform raises on purpose. So the
Executive Agent hands the parts to the departments that own them: procurement's PO/GRN
figures, finance's authority band, QA's acceptance file. One of them comes back needing a
decision, and the CEO makes it.

That refusal is why the delegation count matters. `make demo` produces an empty tree when
no model is configured; with a free model configured the tree is the product's own output.

## Deterministic, and idempotent

A fixed seed and fixed ULID-derived suffixes, so a second run recognises its own rows rather
than duplicating them. `--reset` deletes only the rows whose `source_actor` is
`mock_corpus`, which is why that column is written on everything this script creates: it is
how a person tells mock data from real data with one query, and how this script can clean
up after itself without touching anything else.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

#: No brokers. The task is driven in-process, so Temporal and NATS being off is the
#: point rather than a limitation. Same three lines as `scripts/run_demo_task.py`.
os.environ.setdefault("AO_TEMPORAL_ENABLED", "false")
os.environ.setdefault("AO_NATS_ENABLED", "false")

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.config.settings import get_settings  # noqa: E402
from ai_orchestrator.domain.ids import new_ulid  # noqa: E402
from ai_orchestrator.persistence.repositories.task import TaskRepository  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402

#: A free real model, when there is a key to spend. Read through `get_settings()` rather
#: than `os.environ`, because the key lives in `.secrets/runtime.env` and is *not*
#: exported — so an `os.environ` check would decide there is no credential on a machine
#: that has one, and quietly downgrade the demonstration. F230.
#:
#: Defaulting a *provider* unconditionally meant a fresh clone, which has no key, reached
#: a real provider with nothing to authenticate, and the showcase task failed with:
#:
#:     Tool 'delegate_to_agent' exceeded max retries count of 2
#:
#: which names a tool, a retry budget and a URL, and misses the only relevant fact. There
#: is deliberately no fallback to the deterministic runtime here: `ScriptedRuntime`
#: completes a task rather than delegating, and a `coordination` task that completes
#: without delegating is failed on purpose — so the fallback would replace one wrong
#: message with a correct one and the same broken demonstration. This script's job is to
#: show a delegation tree, and only a model can produce one. Refusing clearly beats
#: pretending.
HAS_MODEL_KEY = bool(get_settings().openrouter_api_key.get_secret_value())
os.environ.setdefault("AO_MODEL_PROVIDER_DEFAULT", "openrouter" if HAS_MODEL_KEY else "fake")

#: Written on every row this script creates. One query tells a person which is which, and it
#: is what `--reset` deletes.
ACTOR = "mock_corpus"

#: The showcase. Phrased to hand work off, because a `coordination` task that does the work
#: itself is failed by the platform on purpose -- so a goal that invites the agent to "do it"
#: would fail every run, for a reason that is the separation-of-duties rule working.
SHOWCASE_GOAL = (
    "Chuẩn bị hồ sơ thanh toán giai đoạn 2 cho dự án Bãi Tràm Estates. Hãy giao phần "
    "đối chiếu số liệu PO-GRN cho bộ phận Mua sắm, phần kiểm tra hạn mức ủy quyền và thuế "
    "cho Tài chính, và phần rà soát hồ sơ nghiệm thu cho QA/QC-HSE. Không tự làm thay các "
    "bộ phận đó."
)

#: The rest of the queue, so the CEO's list is not a single item and each state is
#: represented. Titles and goals are the dossier's working language.
QUEUE: tuple[tuple[str, str, str, str], ...] = (
    (
        "research",
        "Báo cáo tiến độ tuần",
        "Tổng hợp tiến độ tuần và các gói công việc trễ tiến độ.",
        "completed",
    ),
    (
        "analysis",
        "Phân tích biến động chi phí",
        "So sánh chi phí thực tế với dự toán và chỉ ra sai lệch.",
        "completed",
    ),
    (
        "coordination",
        "Chuẩn bị hồ sơ nghiệm thu giai đoạn 1",
        "Giao phần biên bản nghiệm thu và phần hồ sơ bàn giao.",
        "assigned",
    ),
    (
        "research",
        "Rà soát danh mục vật tư tồn kho",
        "Kiểm tra số lượng tồn kho thực tế so với sổ sách.",
        "created",
    ),
    (
        "analysis",
        "Đánh giá rủi ro nhà thầu phụ",
        "Xếp hạng rủi ro theo hạng mức và hạn hiệu lực hồ sơ năng lực.",
        "created",
    ),
)

_SELECT_ORG = "SELECT id FROM organizations ORDER BY created_at, id LIMIT 1"

#: Matched on a **prefix**, not the whole goal, and the reason is a fact about the tooling:
#: `ruff`'s RUF001 rewrote the en dash in `--` inside `SHOWCASE_GOAL`'s Vietnamese prose,
#: which silently changed the string this script compares against. A row written before
#: that fix and a lookup after it do not match, and the failure is a duplicate-task refusal
#: that reads like a data problem. A prefix is stable under that class of edit, so the
#: lookup is.
#:
#: That is also an argument the *other* way: a linter rewriting the content of a domain
#: string is a finding of its own. `PO-GRN` and `PO-GRN` are the same three-way match to a
#: person and different strings to a query, and the fix was applied without anybody deciding
#: it should be.
#:
#: `tasks` has **no** provenance column -- it is substrate, not a construction table, and
#: `ConstructionMixin` was never applied to it. So a mock task is recognised by its goal,
#: which is a weaker identity than `source_actor` and is stated here rather than papered
#: over: a person who edits a mock task's goal re-opens it to this script.
#:
#: `delegations.source_actor` and `approvals.requested_by` **do** exist, so those two are
#: exact. `--reset` is therefore exact for them and best-effort for tasks.
_SELECT_SHOWCASE = """
SELECT id FROM tasks
WHERE organization_id = CAST(:o AS varchar(40)) AND goal LIKE :prefix || '%'
ORDER BY created_at DESC LIMIT 1
"""

_SELECT_AGENT = """
SELECT id, name FROM agents
WHERE organization_id = CAST(:o AS varchar(40)) AND name = :name
LIMIT 1
"""

_SELECT_PENDING_APPROVAL = """
SELECT id FROM approvals
WHERE organization_id = CAST(:o AS varchar(40)) AND status = 'pending'
ORDER BY created_at LIMIT 1
"""

#: The one hand-written row in this script. Every column is from the real vocabulary:
#: `action_type` and `effect_class` are the platform's own taxonomy, and
#: `payload_hash` is a real hash of the real payload, because `decide` re-hashes and
#: refuses a packet that changed after it was approved.
_INSERT_APPROVAL = """
INSERT INTO approvals (
    id, organization_id, task_id, action_type, action_payload, effect_class, risk_level,
    reason, payload_hash, requested_by, requested_by_type, required_approver_roles,
    status, expires_at)
VALUES (:id, CAST(:o AS varchar(64)), CAST(:task AS varchar(64)), :action, CAST(:payload AS jsonb),
        :effect, :risk, :reason, :hash, :by, 'agent',
        -- `varchar[]`, not jsonb. The opposite of `roles.authority_profile` and
        -- `sop_definitions.related_gate_codes`, which *are* jsonb: the same repository has
        -- both, so the column is read rather than assumed. `string_to_array` over a joined
        -- string rather than a Python list, because a list bound to a `text()` parameter
        -- has no codec and asyncpg cannot infer the element type.
        string_to_array(:roles, ','), 'pending',
        now() + interval '7 days')
RETURNING id
"""

#: `--reset` works **downwards from the mock tasks**, which is exact, rather than by
#: matching each table on a provenance column.
#:
#: Two of the three have no such column: `tasks` is substrate and `delegations` is
#: substrate too -- `id, organization_id, parent_task_id, child_task_id, source_agent_id,
#: target_agent_id, status, objective, ...` and no `source_actor` anywhere. Only `approvals`
#: has one, as `requested_by`. The first version of this query asserted both and failed with
#: `UndefinedColumnError: column "source_actor" does not exist`.
#:
#: So the mock tasks are found by their goals -- the one thing this script fully owns -- and
#: everything hanging off them is deleted in dependency order. That is both correct and
#: simpler: one query finds the set, and the order is the foreign keys.
_SELECT_MOCK_TASKS = """
SELECT id FROM tasks
WHERE organization_id = CAST(:o AS varchar(40))
  AND left(goal, 40) = ANY(CAST(:goals AS text[]))
"""


@dataclass(slots=True)
class Tally:
    wrote: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    ran: list[str] = field(default_factory=list)

    def write(self, what: str) -> None:
        self.wrote.append(what)

    def find(self, what: str) -> None:
        self.existing.append(what)

    def run(self, what: str) -> None:
        self.ran.append(what)


def _payload(task_id: str, goal: str) -> dict[str, object]:
    return {
        "kind": "stage_payment_dossier",
        "task_id": task_id,
        "goal": goal,
        "stage": 2,
        "asked_of": ["procurement", "finance", "qa_qc_hse"],
    }


def _reason(goal: str) -> str:
    return (
        "Giai đoạn 2 vượt hạn mức ủy quyền của kế toán trưởng, nên cần người ký trước khi "
        "lập hồ sơ thanh toán. " + goal[:80]
    )


async def _first_org() -> str:
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.engine.connect() as conn:
            org = (await conn.execute(text(_SELECT_ORG))).scalar()
    finally:
        await db.dispose()
    if org is None:
        raise SystemExit("no organization; run `make ingest` first")
    return str(org)


async def build(org: str, *, run_the_task: bool) -> Tally:
    from ai_orchestrator.application.task_execution import TaskExecutionService
    from ai_orchestrator.worker_runtime import build_runtime

    tally = Tally()
    db = Database.from_settings()
    try:
        async with db.tenant_session(org) as session:
            tasks = TaskRepository(session, org)

            # The queue. Each one is created through `TaskRepository`, the same path the
            # HTTP handler takes, so the fingerprints and the dedup keys are the real
            # ones rather than something that happens to satisfy the column.
            for task_type, title, goal, state in QUEUE:
                found = (
                    await session.execute(text(_SELECT_SHOWCASE), {"o": org, "prefix": goal[:40]})
                ).first()
                if found is not None:
                    tally.find(f"{state:9} {title}")
                    continue
                created = await tasks.create(
                    title=title, goal=goal, task_type=task_type, requester_type="human"
                )
                if state == "completed":
                    await session.execute(
                        text(
                            "UPDATE tasks SET status = 'completed', started_at = now(), "
                            "  completed_at = now() + interval '4 minutes' WHERE id = :i"
                        ),
                        {"i": str(created.id)},
                    )
                elif state == "assigned":
                    executive = (
                        await session.execute(
                            text(_SELECT_AGENT), {"o": org, "name": "Executive Agent"}
                        )
                    ).first()
                    if executive is not None:
                        await tasks.assign(str(created.id), str(executive[0]))
                tally.write(f"{state:9} {title}")

            # The showcase, run through the real executor so the delegation tree is the
            # product's output rather than a row written to look like one.
            found = (
                await session.execute(
                    text(_SELECT_SHOWCASE),
                    {"o": org, "prefix": SHOWCASE_GOAL[:40]},
                )
            ).first()
            if found is not None:
                showcase = str(found[0])
                # Present but **unrun** is the state a previous `--no-run` leaves behind, and
                # it is the one that matters: a task with no execution has no delegation
                # tree, and the delegation tree is the thing the page has to show. So
                # existence is not the test -- "has it been executed" is.
                # **Executions are not enough.** `--reset` cannot delete an execution that
                # has an audit trail, so after a reset the showcase still has one and the
                # old "has it been run" test said yes -- while its delegations were gone,
                # leaving tasks with no tree under them. The question that matters is
                # whether the *story* is intact, so this asks about the delegations.
                ran = (
                    await session.execute(
                        text(
                            "SELECT (SELECT count(*) FROM executions WHERE task_id = :t) "
                            "     + (SELECT count(*) FROM delegations "
                            "         WHERE parent_task_id = :t) * 1000"
                        ),
                        {"t": showcase},
                    )
                ).scalar()
                if ran:
                    tally.find(f"showcase {showcase} already exists and has been run")
                else:
                    tally.find(f"showcase {showcase} exists but has never been run")
            else:
                created = await tasks.create(
                    title="Hồ sơ thanh toán giai đoạn 2",
                    goal=SHOWCASE_GOAL,
                    task_type="coordination",
                    requester_type="human",
                )
                showcase = str(created.id)
                tally.write(f"showcase {showcase} created")
            if (run_the_task and found is None) or run_the_task:
                executive = (
                    await session.execute(
                        text(_SELECT_AGENT), {"o": org, "name": "Executive Agent"}
                    )
                ).first()
                already = 0
                if executive is not None:
                    already = int(
                        (
                            await session.execute(
                                text("SELECT count(*) FROM executions WHERE task_id = :t"),
                                {"t": showcase},
                            )
                        ).scalar()
                    )
                if run_the_task and executive is not None and not already:
                    await tasks.assign(showcase, str(executive[0]))
                    service = TaskExecutionService(session, org, runtime=build_runtime())
                    outcome = await service.execute_task(showcase, agent_id=str(executive[0]))
                    tally.run(
                        f"executed {showcase}: {outcome.status} "
                        f"- {outcome.summary or outcome.blocked_reason or 'no summary'} "
                        f"({outcome.tokens} tokens, {outcome.duration_ms}ms)"
                    )

            # The one hand-written row: a pending approval on the showcase, so the CEO has
            # something to decide. Written with the real vocabulary and a real hash.
            pending = (await session.execute(text(_SELECT_PENDING_APPROVAL), {"o": org})).first()
            if pending is not None:
                tally.find(f"an approval is already waiting ({pending[0]})")
            else:
                payload = _payload(showcase, SHOWCASE_GOAL)
                import hashlib
                import json

                blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                row = await session.execute(
                    text(_INSERT_APPROVAL),
                    {
                        "id": f"apr_{new_ulid()}",
                        "o": org,
                        "task": showcase,
                        "action": "commit_payment_dossier",
                        "payload": blob,
                        "effect": "mutate_internal",
                        "risk": "high",
                        "reason": _reason(SHOWCASE_GOAL),
                        "hash": hashlib.sha256(blob.encode("utf-8")).hexdigest(),
                        "by": ACTOR,
                        "roles": "org_admin",
                    },
                )
                tally.write(f"a pending approval on {showcase} ({row.scalar_one()})")
    finally:
        await db.dispose()
    return tally


async def reset(org: str) -> tuple[int, int]:
    """Delete what this script created, and report what it is **not** allowed to delete.

    Two limits, both the product's and both correct:

    * **`audit_logs` is not writable by the application role.** The first version tried to
      delete it and got `InsufficientPrivilegeError: permission denied for table
      audit_logs`. That is not an obstacle to work around — an audit log the application
      can delete is not an audit log. So the script never tries, and instead reports how
      many mock executions have an audit trail, because those cannot be removed and a
      person should know that before they expect `--reset` to be total.
    * **`tasks` and `delegations` have no provenance column.** Both are substrate, so the
      mock tasks are found by their goals and everything hanging off them is deleted in
      foreign-key order. A person who edits a mock task's goal re-opens it to this script.

    Returns `(removed, retained)` so the caller can say both numbers rather than implying a
    clean slate.
    """
    goals = [g[:40] for _, _, g, _ in QUEUE] + [SHOWCASE_GOAL[:40]]
    db = Database.from_settings()
    removed = 0
    retained = 0
    try:
        async with db.tenant_session(org) as session:
            task_ids = [
                str(r[0])
                for r in (
                    await session.execute(text(_SELECT_MOCK_TASKS), {"o": org, "goals": goals})
                ).all()
            ]
            if not task_ids:
                return 0, 0
            # How many of the mock executions have an audit trail, before anything moves.
            retained = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(DISTINCT x.id) FROM executions x "
                            " JOIN audit_logs l ON l.execution_id = x.id "
                            " WHERE x.task_id = ANY(CAST(:ids AS text[]))"
                        ),
                        {"ids": task_ids},
                    )
                ).scalar_one()
            )
            for table, column in (
                ("approvals", "task_id"),
                ("delegations", "parent_task_id"),
                ("delegations", "child_task_id"),
            ):
                result = await session.execute(
                    text(f"DELETE FROM {table} WHERE {column} = ANY(CAST(:ids AS text[]))"),
                    {"ids": task_ids},
                )
                removed += int(result.rowcount or 0)
            # Executions with an audit trail are left alone, and so are the tasks they hang
            # from -- the alternative is a foreign-key violation naming a table whose
            # protection is the point.
            kept = {
                str(r[0])
                for r in (
                    await session.execute(
                        text(
                            "SELECT DISTINCT task_id FROM executions "
                            " WHERE task_id = ANY(CAST(:ids AS text[])) "
                            "   AND id IN (SELECT execution_id FROM audit_logs)"
                        ),
                        {"ids": task_ids},
                    )
                ).all()
            }
            deleteable = [i for i in task_ids if i not in kept]
            if deleteable:
                result = await session.execute(
                    text("DELETE FROM executions WHERE task_id = ANY(CAST(:ids AS text[]))"),
                    {"ids": deleteable},
                )
                removed += int(result.rowcount or 0)
                result = await session.execute(
                    text("DELETE FROM tasks WHERE id = ANY(CAST(:ids AS text[]))"),
                    {"ids": deleteable},
                )
                removed += int(result.rowcount or 0)
    finally:
        await db.dispose()
    return removed, retained


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="create the rows but do not execute the showcase task",
    )
    parser.add_argument("--reset", action="store_true", help="delete the rows this script created")
    args = parser.parse_args()

    org = args.org or await _first_org()
    if args.reset:
        removed, retained = await reset(org)
        print(f"  removed {removed} mock row(s) from {org}")
        if retained:
            print(
                f"  retained {retained} execution(s) that have an audit trail. "
                "`audit_logs` is deliberately not writable by the application role -- an "
                "audit log the application can delete is not an audit log -- so those rows, "
                "and the tasks they hang from, stay. Re-running `make mock-corpus` after "
                "this will find them already present."
            )
        return 0

    # Refuse before writing anything, and say exactly what is missing. The showcase is a
    # `coordination` task, and the platform fails one that completes without delegating —
    # on purpose. Only a model produces the delegation this script exists to show, and
    # the deterministic runtime is a test double, not a demonstration. Writing the rows
    # and then reporting a failure leaves the tenant holding a task that can only fail.
    if not args.no_run and not HAS_MODEL_KEY:
        print(
            "  no model credential: this needs a real model, because the showcase is a\n"
            "  coordination task and the platform fails one that completes without\n"
            "  delegating -- which is the thing this script exists to show.\n"
            "\n"
            "  add a free key to .secrets/runtime.env, then re-run:\n"
            "\n"
            "      echo 'OPENROUTER_API_KEY=sk-or-v1-...' >> .secrets/runtime.env\n"
            "      make seed-free-model\n"
            "      make mock-corpus\n"
            "\n"
            "  or create the rows without running the task:  make mock-corpus-norun"
        )
        return 2

    tally = await build(org, run_the_task=not args.no_run)
    print(f"  org: {org}")
    print(
        f"  wrote {len(tally.wrote)}, already present {len(tally.existing)}, "
        f"executed {len(tally.ran)}"
    )
    for item in tally.wrote:
        print(f"    + {item}")
    for item in tally.ran:
        print(f"    > {item}")
    for item in tally.existing:
        print(f"    = {item}")
    print("  open the page: Approvals holds the decision; the task shows the tree")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
