"""Hand a goal to the executive and let the organisation finish it.

The one command that demonstrates the thing the platform is for. It creates the
root task, hands it to the chief, and then gets out of the way: the pipeline picks
the deepest ready task, the office reviews what each department produced, a
rejected answer is sent back with the findings as its brief, and the executive is
settled only when everything below it is finished and accepted.

```bash
uv run python scripts/run_pipeline.py --org org_... --goal "Chấm nhận 3 khoản chi"
uv run python scripts/run_pipeline.py --org org_... --key cv-screen   # from the catalogue
```

Exit code is the number of stages that did not finish, so it can gate something
rather than be read.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from ai_orchestrator.application.pipeline import run_pipeline
from ai_orchestrator.application.scenarios import SCENARIOS, agent_for
from ai_orchestrator.config.settings import get_settings
from ai_orchestrator.domain.enums import RunMode
from ai_orchestrator.persistence.models import Agent
from ai_orchestrator.persistence.repositories.task import TaskRepository
from ai_orchestrator.persistence.session import Database


async def _create_root(
    db: Database,
    org_id: str,
    goal: str,
    contract: dict,
    routing: dict,
    brief: str = "",
) -> str:
    async with db.committing_tenant_session(org_id) as session:
        chief = (
            await session.execute(
                select(Agent).where(
                    Agent.organization_id == org_id, Agent.name == "Executive Agent"
                )
            )
        ).scalar_one()
        root = await TaskRepository(session, org_id).create(
            title=goal[:60],
            goal=goal,
            task_type="coordination",
            requester_type="human",
            owner_agent_id=chief.id,
            expected_output_schema=(
                {
                    "required": sorted(contract),
                    "field_meaning": contract,
                }
                if contract
                else None
            ),
            # The office is told which office and department own this, and each
            # tier passes it down. It is *not* a substitute for the model being
            # able to route: the roster still has to be read and the right
            # colleague still has to be chosen. It removes the need to guess the
            # organisation's own reporting lines from a prompt.
            input={**routing, "brief": brief},
        )
        await session.commit()
        return str(root.id)


async def main(
    org_id: str,
    goal: str | None,
    key: str | None,
    max_exec: int,
    resume: str | None = None,
) -> int:
    scenario = next((s for s in SCENARIOS if s.key == key), None) if key else None
    if key and scenario is None:
        print(f"no scenario named {key!r}; try: {', '.join(s.key for s in SCENARIOS)}")
        return 2
    if scenario is not None:
        goal = scenario.goal

    settings = get_settings()
    print(f"\nprovider: {settings.model_provider_default}")
    db = Database.from_settings()
    if resume:
        # **Continue where an interrupted run stopped, not from scratch.**
        #
        # A run killed by the wall clock leaves a `running` root with `assigned`
        # children that never executed. Starting over duplicates the whole tree;
        # resuming drains it. The driver loop picks the deepest runnable task, so a
        # second run on the same root is a continuation by construction.
        print(f"resuming: {resume}\n")
        outcome = await run_pipeline(
            db,
            org_id,
            resume,
            max_executions=max_exec,
            run_mode=RunMode.LIVE,
        )
        try:
            for step in outcome.steps:
                note = f"  [{step.review}]" if step.review else ""
                line = (
                    f"  {'  ' * step.depth}{step.owner:<20} "
                    f"{step.status:<11} {step.title[:44]}{note}"
                )
                print(line)
            print()
            print(outcome.summary())
            return 0 if outcome.finished else 1
        finally:
            await db.dispose()
    try:
        # `expected_output` in the catalogue is a *description* of each field
        # ("verdicts": "mỗi khoản: duyệt / duyệt có điều kiện / từ chối"), which is
        # what a reader wants. The contract the platform enforces is a list of the
        # keys that must be there. Sending the description map meant the review had
        # no promised keys to check and accepted an empty answer -- so both halves
        # are sent, and the description rides along for the agent to read.
        contract = dict(scenario.expected_output) if scenario else {}
        routing = (
            {
                "owning_department": agent_for(scenario.department),
                "owning_office": scenario.office,
            }
            if scenario
            else {}
        )
        # **The chief gets the objective, not the worked question.**
        #
        # The root used to be handed the whole scenario -- the policy, the three
        # amounts, the names of the suppliers. Two runs of a real model then failed
        # identically with `no_delegation`: given a fully specified question, the
        # cheapest correct-looking answer is to answer it. It was not the model
        # misbehaving, it was the platform handing the chief the department's work.
        #
        # So the goal states what to achieve and the `brief` carries the material.
        # The chief cannot answer without delegating, because the data is not in its
        # question, and the department can answer, because the brief travelled down.
        brief = goal or ""
        # **A second run of the same scenario is a new piece of work, not a duplicate.**
        #
        # Measured: `run_pipeline.py --key supplier-tender` a second time died with
        #
        #     ConflictError: an equivalent task is already active: tsk_01m3xe8qt...
        #
        # and a nineteen-line traceback. The deduplication is working exactly as
        # designed -- the same intent must not be live twice -- but the scenario goal is
        # a constant, so running a scenario twice is *always* a duplicate and the
        # script can therefore never be run twice.
        #
        # The fix is what `demo_real_run.py` already does: a run marker in the brief,
        # so the second run is genuinely different work rather than the same work asked
        # for twice. It rides in the brief and not in the objective, because the
        # objective is what the chief is judged on and a hex string in it is noise the
        # model would have to reason about.
        run_marker = f"[lần chạy {uuid.uuid4().hex[:8]}]"
        objective = (
            scenario.objective
            if scenario is not None
            else f"Hoàn thành yêu cầu sau bằng cách giao cho đúng phòng ban:\n\n{brief}"
        )
        # **On the goal, not on the brief.** The root task's fingerprint is a hash of
        # its goal, so a marker on the brief leaves the conflict exactly where it was --
        # measured, on the first attempt at this fix. A fix aimed at the wrong string
        # is a fix that measures nothing.
        brief = f"{brief}\n\n{run_marker}"
        objective = f"{objective}\n\n{run_marker}"
        root_id = await _create_root(db, org_id, objective, contract, routing, brief)
        print(f"root: {root_id}\n")
        outcome = await run_pipeline(
            db,
            org_id,
            root_id,
            max_executions=max_exec,
            run_mode=RunMode.LIVE,
        )
    finally:
        await db.dispose()

    for step in outcome.steps:
        note = f"  [{step.review}]" if step.review else ""
        print(f"  {'  ' * step.depth}{step.owner:<20} {step.status:<11} {step.title[:44]}{note}")
    print()
    print(outcome.summary())
    return 0 if outcome.finished else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--goal")
    ap.add_argument("--key", help="a scenario from the catalogue")
    ap.add_argument("--max-executions", type=int, default=60)
    ap.add_argument(
        "--resume",
        help="an existing root task id to continue draining instead of starting over",
    )
    args = ap.parse_args()
    raise SystemExit(
        asyncio.run(main(args.org, args.goal, args.key, args.max_executions, args.resume))
    )
