"""Run the whole agent fleet, once, on real work from the real corpus.

## What "set up all the agents so they run" means here, measured

Sixteen agents, and before this script every one of them had:

| field | value | what it says |
|---|---|---|
| `granted_level` | `L1` | nobody has been given autonomy |
| `health` | `unknown` | nothing has ever reported in |
| `runtime_status` | `idle` | and nothing is running |
| executions | 0 from this script | no agent had done work drawn from the corpus |

So the fleet existed and was idle. This gives **each of the dossier's eight agents one real
task**, drawn from what its own SOPs say it owns, and runs it through the real
`TaskExecutionService` -- the same call the Temporal activity makes.

**Eight, not sixteen.** The register is the dossier's eight (Tập 1 table 10). The other
eight rows are the block placeholders from the ingest, and giving an agent with no
procedure a task would be inventing a role rather than exercising one.

## Parallel, because serial is eighty minutes

Each run is measured at **644 seconds and 67,136 tokens** on the free model. Eight of those
in series is 86 minutes of waiting; in parallel it is one run's wall time plus contention.
`--concurrency` caps it, and the default is 4 -- eight concurrent calls to one free
endpoint is how you get rate-limited and diagnose it as "the agent is broken".

## The grant is not raised here, and that is the point

The dossier's rule is that a grant rises on **measured shadow agreement**, not on
convenience (Tập 1 §5.3). This script produces shadow runs; the promotion gate in
`application/procedure_operations.py` is what turns them into a grant. Raising a level here
would be the platform marking its own homework, and the whole point of the ceiling is that
it is not the agent's to move.

## What it prints, and what a failure means

Per agent: status, tokens, wall time, whether it delegated, and **the failure category and
message when it did not succeed**. A failure here is a finding, not a script error -- 24 of
the 47 tasks in the seeded history failed exactly the separation-of-duties rule, and that is
the rule working. The script distinguishes the two shapes: a *refusal* (the platform said
no, and said why) is reported as a refusal; a *crash* is reported as a crash.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("AO_MODEL_PROVIDER_DEFAULT", "openrouter")
os.environ.setdefault("AO_TEMPORAL_ENABLED", "false")
os.environ.setdefault("AO_NATS_ENABLED", "false")

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.persistence.repositories.task import TaskRepository  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402

#: One task per dossier agent, in the register's own order (Tập 1 table 10). Each goal is
#: the work that agent's SOPs say it owns, phrased so a single agent can finish it --
#: **no delegation**, because a delegated task that the *delegate* also delegates is a
#: recursion the depth limit exists to stop, and because this script is measuring eight
#: agents rather than one tree.
#:
#: The project name is the real one from the corpus, and the numbers are real: 240 WBS
#: nodes and 2778 progress readings across six rows. A task that says "analyse the
#: schedule" over a fictional project would exercise the same code and prove nothing about
#: the data.
WORK: tuple[tuple[str, str, str, str], ...] = (
    (
        "HR Agent",
        "analysis",
        "Bãi Tràm Estates",
        "Tổng hợp hồ sơ nhân sự của dự án Bãi Tràm Estates: ai làm gì, hợp đồng thử việc "
        "còn bao lâu, và ai quá hạn. Không tự quyết định về nhân sự.",
    ),
    (
        "Procurement Agent",
        "analysis",
        "Bãi Tràm Estates",
        "Đối chiếu cấu trúc chi phí mua sắm của dự án Bãi Tràm Estates: nhóm vật tư nào "
        "chiếm tỷ trọng lớn nhất theo 240 nút WBS đã nạp.",
    ),
    (
        "Knowledge Agent",
        "research",
        "BÃI TRÀM ESTATES",
        "Tóm tắt 28 quy trình SOP của hồ sơ O-Nexus theo khối: Back Office, Front Office, "
        "Middle Office và PMO, và cho biết khối nào có nhiều quy trình nhất.",
    ),
    (
        "Finance Agent",
        "analysis",
        "Bãi Tràm Estates",
        "Kiểm tra hạn mức ủy quyền theo sáu nhóm hành động của hồ sơ: nhóm nào bị chặn ở "
        "L1, và điều đó có nghĩa là gì cho người ký.",
    ),
    (
        "Project Mgmt Agent",
        "analysis",
        "Bãi Tràm Estates",
        "Tìm các nút WBS của dự án Bãi Tràm Estates chưa có số liệu thực tế nào được ghi, "
        "và nói rõ vì sao 'có kế hoạch mà chưa có thực tế' không phải là 'đúng tiến độ'.",
    ),
    (
        "Design/M&E Agent",
        "research",
        "Bãi Tràm Estates",
        "Rà soát hệ thống kỹ thuật của dự án Bãi Tràm Estates trong cây WBS: nhóm nào "
        "thuộc M&E, và các hạng mục nào còn thiếu số liệu.",
    ),
    (
        "Sales/BD Agent",
        "research",
        "MELIA CAM RANH BAY VILLA & RESORT",
        "Tóm tắt hồ sơ đấu thầu của dự án Melia Cam Ranh Bay Villa & Resort: các gói thầu "
        "và rủi ro nào cần lưu ý trước khi báo giá.",
    ),
    (
        "QA/QC-HSE Agent",
        "analysis",
        "BÃI TRÀM ESTATES",
        "Rà soát yêu cầu về an toàn và chất lượng trong cây WBS của dự án Bãi Tràm Estates, "
        "và nêu rõ những gì **không** thể kết luận từ dữ liệu hiện có. Không đưa ra kết luận "
        "về an toàn.",
    ),
)

ACTOR = "fleet_run"


@dataclass(slots=True)
class Result:
    agent: str
    task_id: str | None = None
    status: str = "not started"
    tokens: int = 0
    seconds: float = 0.0
    delegated: int = 0
    refusal: str = ""
    crash: str = ""
    summary: str = ""

    @property
    def verdict(self) -> str:
        if self.crash:
            return "CRASH"
        if self.refusal:
            return "REFUSED"
        if self.status == "completed":
            return "done"
        return self.status


@dataclass(slots=True)
class Tally:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)


async def _org() -> str:
    """The first organization. **Awaited, not wrapped in `asyncio.run`.**

    F157, the third occurrence in this repository: `asyncio.run()` inside a running loop
    raises `RuntimeError: asyncio.run() cannot be called from a running event loop`, and it
    did so on the first line of the first run of this script. Three costumes for one rule
    now -- `test_delegation_control.py`'s synchronous helper, `verify_page.mjs`'s missing
    `history`, and this.
    """
    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.engine.connect() as conn:
            found = (
                await conn.execute(text("SELECT id FROM organizations ORDER BY created_at LIMIT 1"))
            ).scalar()
    finally:
        await db.dispose()
    if found is None:
        raise SystemExit("no organization; run `make ingest` first")
    return str(found)


async def _reusable(session, org: str, goal: str) -> str | None:
    """An open equivalent task, or `None`.

    `TaskRepository.create` refuses an equivalent *active* task by fingerprint, which is
    right -- two agents doing the same work at once is waste. But a task created over HTTP
    sits at `created` for ever when no worker is running, and it then blocks the very run it
    was created for. So a task left **open** by a previous run is adopted, and its row is
    reset so the executor does the work rather than reading its own old answer.

    The `status IN (...)` filter is in the **SELECT**, not only in the `UPDATE`. The first
    version filtered on read and reset on write, so a task that had *completed* was found,
    not reset, and then `assign` raised
    `illegal task transition: completed --assign--> ? (legal from completed: none)`.

    A completed task is not adopted. It is a finished piece of work, and re-running a
    fresh measurement on top of it would either collide with it or quietly answer from it --
    which is the thing `local_runner` now refuses to do explicitly.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, status FROM tasks WHERE organization_id = CAST(:o AS varchar(64)) "
                "  AND goal = :g AND status IN ('created','assigned','failed','running') "
                " ORDER BY created_at DESC LIMIT 1"
            ),
            {"o": org, "g": goal},
        )
    ).first()
    if row is None:
        return None
    # `running` is in the set, and that is the defect this run found: a crashed execution
    # leaves the task in `running`, and the state machine refuses `running --assign-->`
    # ("legal from running: block, cancel, complete, expire, fail, ..."), so a task left
    # running by a previous crash cannot be re-run at all. Before the executor took a lease
    # there was no way for anything to move it either -- see `task_reaper`'s condition.
    await session.execute(
        text(
            "UPDATE tasks SET status = 'assigned', completed_at = NULL, started_at = NULL, "
            "  last_error = NULL, failure_category = NULL, lease_expires_at = NULL "
            " WHERE id = :i AND status IN ('created','assigned','failed','running')"
        ),
        {"i": str(row[0])},
    )
    return str(row[0])


async def run_one(agent: str, task_type: str, project: str, goal: str, org: str) -> Result:
    from ai_orchestrator.application.task_attempt import TaskAttemptRunner
    from ai_orchestrator.worker_runtime import build_runtime

    result = Result(agent=agent)
    db = Database.from_settings()
    try:
        async with db.committing_tenant_session(org) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                        "  AND name = :n"
                    ),
                    {"o": org, "n": agent},
                )
            ).first()
            if row is None:
                result.crash = f"no agent named {agent!r}"
                return result
            agent_id = str(row[0])

            full = f"{goal} Dự án: {project}."
            repo = TaskRepository(session, org)
            task_id = await _reusable(session, org, full)
            if task_id is None:
                created = await repo.create(
                    title=f"{agent}: {goal[:40]}",
                    goal=full,
                    task_type=task_type,
                    requester_type="human",
                )
                task_id = str(created.id)
            result.task_id = task_id
            await session.execute(
                text("UPDATE tasks SET owner_agent_id = :a WHERE id = :i"),
                {"a": agent_id, "i": task_id},
            )
            await repo.assign(task_id, agent_id)

            await session.commit()
            await db.bind_tenant(session, org)
            started = time.time()
            try:
                outcome = await TaskAttemptRunner(db, org, runtime=build_runtime()).execute_task(
                    task_id, agent_id=agent_id
                )
            except Exception as exc:
                result.crash = f"{type(exc).__name__}: {exc}"[:220]
                result.seconds = time.time() - started
                return result
            result.seconds = time.time() - started
            result.status = str(outcome.status)
            result.tokens = int(outcome.tokens or 0)
            result.summary = (outcome.summary or "")[:300]
            if outcome.blocked_reason:
                result.refusal = outcome.blocked_reason[:300]
            elif outcome.failure_category:
                result.refusal = f"{outcome.failure_category}: {outcome.summary or ''}"[:300]
            count = (
                await session.execute(
                    text("SELECT count(*) FROM delegations WHERE parent_task_id = :t"),
                    {"t": task_id},
                )
            ).scalar()
            result.delegated = int(count or 0)
    finally:
        await db.dispose()
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization; discovered when omitted")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", help="run one agent by name")
    args = parser.parse_args()

    org = args.org or await _org()
    work = [w for w in WORK if not args.only or w[0] == args.only]
    if not work:
        print(f"  no agent named {args.only!r}", file=sys.stderr)
        return 1

    print(f"  org: {org}")
    print(f"  running {len(work)} agent(s), concurrency {args.concurrency}")
    print("  a run is ~11 minutes on the free model; this is the wait, not a hang\n")

    gate = asyncio.Semaphore(args.concurrency)
    tally = Tally()

    async def guarded(item: tuple[str, str, str, str]) -> None:
        async with gate:
            r = await run_one(*item, org)
            tally.add(r)
            mark = {"done": "ok  ", "REFUSED": "REF ", "CRASH": "CRASH"}.get(r.verdict, "..  ")
            print(
                f"  {mark} {r.agent:20} {r.status:10} {r.tokens:>7,} tok "
                f"{r.seconds / 60:5.1f}min  delegated={r.delegated}"
            )
            if r.crash:
                print(f"        crash:   {r.crash}")
            if r.refusal:
                print(f"        refused: {r.refusal}")
            elif r.summary:
                print(f"        said:    {r.summary[:150]}")

    await asyncio.gather(*(guarded(w) for w in work))

    done = [r for r in tally.results if r.verdict == "done"]
    refused = [r for r in tally.results if r.verdict == "REFUSED"]
    crashed = [r for r in tally.results if r.verdict == "CRASH"]
    print(f"\n  {len(done)} completed, {len(refused)} refused, {len(crashed)} crashed")
    print(
        f"  {sum(r.tokens for r in tally.results):,} tokens, "
        f"{sum(r.seconds for r in tally.results) / 60:.0f} minutes of work"
    )
    if crashed:
        print("  A crash is a defect. The message above is the report.")
    if refused:
        print("  A refusal is the platform working: it declined and said why.")
    return 1 if crashed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
