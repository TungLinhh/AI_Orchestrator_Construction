#!/usr/bin/env python3
"""Bounded synthetic CEO→office→department acceptance via normal product runtime.

Requires explicit tenant, existing campaign ledger and unused output directory.
All inference requests are counted; no fake runtime or auto-approval is used.
"""
# ruff: noqa: E402 -- interpreter validation must precede importing project helpers.

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path


def _repo_root() -> Path:
    """Robust repo-root discovery (root review: parents[1] is the private dir).

    Walk up from this file until a directory containing the product source tree
    is found. Never guess from depth: this script lives under
    <repo>/.orchestra/.../chain-acceptance/, so a fixed parents[N] points at
    private evidence, not the repo.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "src" / "ai_orchestrator").is_dir() and (
            candidate / "examples" / "organization_eval" / "cases.json"
        ).is_file():
            return candidate
    raise RuntimeError(f"repo root not found above {here}")


ROOT = _repo_root()
sys.path.insert(0, str(ROOT))


def _require_project_interpreter() -> None:
    """Structural lesson (not a repeated prompt): the project requires Python 3.14
    (pyproject `requires-python`), whose grammar accepts the shared runner's
    `except A, B:` form. Running this entrypoint under system python3 (3.12) once
    produced a FALSE 'shared source corrupted' diagnosis -- a harness/interpreter
    failure that must never be conflated with provider/metadata errors or product
    failures. Enforced here, before any ledger touch or network: the EXACT live
    launcher (uv, project interpreter) is mandatory for every invocation,
    including --help and preflight.
    """
    import re

    want: tuple[int, int] = (3, 14)
    try:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"""requires-python\s*=\s*["']([^"']+)["']""", text)
        if match:
            bound = re.search(r"(\d+)\.(\d+)", match.group(1))
            if bound:
                want = (int(bound.group(1)), int(bound.group(2)))
    except OSError:
        pass
    got = (sys.version_info.major, sys.version_info.minor)
    if got < want:
        raise SystemExit(
            "WRONG INTERPRETER (harness failure, not a product/provider failure): "
            f"project requires Python >={want[0]}.{want[1]} (pyproject), "
            f"this is {got[0]}.{got[1]} ({sys.executable}). "
            "Re-run with the exact live launcher: "
            "AO_MODEL_PROVIDER_DEFAULT=openrouter PYTHONPATH=src:. uv run python "
            "scripts/run_chain_acceptance.py ..."
        )


_require_project_interpreter()

import scripts.run_department_campaign as c2

# Import test for the shared counter contract, with the exact launcher in force.
for _name in (
    "GLOBAL_MAX",
    "PROVIDER_TIMEOUT_CAP_S",
    "BudgetAbort",
    "CaseCapAbort",
    "CostStop",
    "Ledger",
    "SubmissionGate",
    "_install_counting",
):
    if not hasattr(c2, _name):
        raise SystemExit(
            f"shared counter contract drift (harness failure, not product failure): "
            f"scripts/run_department_campaign.{_name} missing; refusing to run "
            f"with a substitute counter"
        )
del _name

import hashlib as _hashlib

COUNTING_SOURCE = (
    "shared:scripts/run_department_campaign.py@"
    + _hashlib.sha256((ROOT / "scripts" / "run_department_campaign.py").read_bytes()).hexdigest()[
        :12
    ]
)

from scripts.organization_eval import corpus_hash, grade, load_corpus, output_schema

DEFAULT_LEDGER = ROOT / ".orchestra/chain-b-2026-10-16/ledger.json"
CHAIN_ID = "chain-acceptance-c3"
RUN_CAP = 12
WALLCLOCK_S = 600.0
MAX_EXECUTIONS = 8
CAP_CHARS_SUMMARY = 1000
CAP_CHARS_ARTIFACT = 4096


ALLOWED_MODELS = frozenset(
    {
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "thinkingmachines/inkling-small:free",
        "dots-studio/dots-3-note-preview:free",
    }
)


class _FreeSubmissionGate(c2.SubmissionGate):
    """Reject unauthorized model names before reserving budget or sending HTTP."""

    def check_and_reserve(self, model: str) -> tuple[int, float]:
        if model not in ALLOWED_MODELS:
            raise c2.BudgetAbort("model outside the authorized free-model campaign")
        return super().check_and_reserve(model)


def _load_ledger(path: Path, run_id: str) -> c2.Ledger:
    """Honor the campaign's persisted cap, never silently expand it to 60."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    limit = raw.get("max")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= c2.GLOBAL_MAX:
        raise ValueError("campaign max must be a positive integer no larger than 60")
    if not isinstance(raw.get("entries"), list):
        raise ValueError("campaign entries must be a list")
    if any(entry.get("owner") == f"chain-{run_id}" for entry in raw["entries"]):
        raise ValueError("run-id already present in ledger; use a new run-id")
    return c2.Ledger(path, owner=f"chain-{run_id}", max_submissions=limit)


def _utcnow() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _cap(text: object, limit: int) -> dict[str, object]:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    if len(s) > limit:
        return {"value": s[:limit], "truncated": True, "full_chars": len(s)}
    return {"value": s, "truncated": False, "full_chars": len(s)}


def _evidence_gaps(
    task_ids: set[str],
    step_task_ids: list[str],
    delegation_edges: list[tuple[str, str]],
) -> list[str]:
    """Pure consistency check: manifest tiers must cover the pipeline steps.

    The product walks the tree through the `delegations` table
    (`application/work_review._children`), so the evidence walk must too: a
    task reachable only via a delegation edge is still part of the run, even
    when the `parent_task_id` closure missed it (chain round-3: closure found
    2/4 tasks, delegations showed chief->office, office->leaf-1,
    office->leaf-2). Returns failure strings, never raises.
    """
    gaps: list[str] = []
    for step_id in step_task_ids:
        if step_id not in task_ids:
            gaps.append(f"harness gap: pipeline step {step_id} has no row in manifest tiers")
    dangling = sorted({c for _, c in delegation_edges} - set(task_ids))
    for child_id in dangling:
        gaps.append(
            f"harness gap: delegation edge points at {child_id}, which has no row in manifest tiers"
        )
    return gaps


def _final_verdict(blocked: bool, chain_ok: bool, failures: list[str], cost_stop: bool) -> str:
    """A final positive-cost response cannot turn an unauthorized run into PASS."""
    if cost_stop:
        return "FAIL"
    if blocked:
        return "BLOCKED"
    return "PASS" if chain_ok and not failures else "FAIL"


def _completion_gaps(outcome: object, tasks: list, office_agent_id: str) -> list[str]:
    """Leaf success alone cannot certify completed coordination and settlement."""
    failures = []
    if outcome is None or str(outcome.root_status) != "completed":
        failures.append("root did not settle completed")
    unfinished = [
        str(t["id"])
        for t in tasks
        if str(t["status"]) not in {"completed", "failed", "canceled", "expired"}
    ]
    if unfinished:
        failures.append(f"chain contains nonterminal tasks: {unfinished}")
    office_rows = [t for t in tasks if str(t["owner_agent_id"]) == office_agent_id]
    # A failed earlier attempt is retained
    # when the product orders a repair; it must not invalidate a completed
    # replacement. The latest office attempt must still settle successfully.
    latest_office = max(
        office_rows, key=lambda row: (str(row.get("created_at", "")), str(row["id"])), default=None
    )
    if latest_office is None or str(latest_office["status"]) != "completed":
        failures.append("office did not settle completed")
    # The department's artifact is reviewed explicitly. Coordinators settle
    # through self_review_of_a_coordinator, not a second artifact review count.
    if outcome is None or outcome.reviews_accepted < 1:
        failures.append("missing accepted review of the department artifact")
    return failures


async def _drain(
    db: object, args: argparse.Namespace, root_id: str, deadline_wall: float, wallclock: float
) -> tuple[object | None, str | None]:
    """Run the pipeline drain; budget/wallclock stops return as strings, never raise."""
    from ai_orchestrator.application.pipeline import run_pipeline
    from ai_orchestrator.domain.enums import RunMode

    try:
        outcome = await asyncio.wait_for(
            run_pipeline(
                db,  # type: ignore[arg-type]
                args.org,
                root_id,
                max_executions=min(args.max_executions, MAX_EXECUTIONS),
                concurrency=1,
                run_mode=RunMode.LIVE,
            ),
            timeout=max(deadline_wall - time.monotonic(), 0.001),
        )
        return outcome, None
    except (c2.BudgetAbort, c2.CaseCapAbort, c2.CostStop) as exc:
        return None, f"submission budget reached: {exc}"
    except TimeoutError:
        return None, f"whole-run wallclock ({wallclock}s) exceeded; drain cancelled"


async def _run(args: argparse.Namespace) -> int:
    from sqlalchemy import select, text

    from ai_orchestrator.config.settings import get_settings
    from ai_orchestrator.persistence.session import Database

    cap = min(args.submission_cap, RUN_CAP)
    wallclock = min(args.wallclock_s, WALLCLOCK_S)
    started_wall = time.monotonic()
    started_at = _utcnow()
    deadline_wall = started_wall + wallclock
    outdir = args.output_dir
    if outdir.exists() and any(outdir.iterdir()):
        raise ValueError("output directory must be empty; preserve prior receipts")
    outdir.mkdir(parents=True, exist_ok=True)

    ledger = _load_ledger(args.ledger_path, args.run_id)
    campaign_max = ledger.snapshot()["max"]
    if ledger.used + cap > campaign_max:
        reason = (
            f"refusing launch: ledger used={ledger.used} + run cap {cap} "
            f"exceeds shared global {campaign_max}; wait for budget, never reset/extend"
        )
        (outdir / "chain-blocked.json").write_text(
            json.dumps({"blocked": True, "reason": reason, "run_id": args.run_id}, indent=2) + "\n",
            encoding="utf-8",
        )
        print(reason, file=sys.stderr)
        return 3

    settings = get_settings()
    if settings.model_provider_default != "openrouter":
        reason = (
            "configuration error BEFORE network: model_provider_default="
            f"{settings.model_provider_default!r}; launch with "
            "AO_MODEL_PROVIDER_DEFAULT=openrouter (round-1 lesson); burning nothing"
        )
        print(reason, file=sys.stderr)
        return 2
    if settings.approval_auto_approve:
        print("configuration error: approval auto-approve must be disabled", file=sys.stderr)
        return 2
    if float(settings.model_request_timeout_s) > c2.PROVIDER_TIMEOUT_CAP_S:
        reason = (
            f"provider timeout {settings.model_request_timeout_s}s exceeds "
            f"{c2.PROVIDER_TIMEOUT_CAP_S}s cap; refusing without tracked edits"
        )
        print(reason, file=sys.stderr)
        return 2

    cases = load_corpus(args.dataset)
    case = next((c for c in cases if c.id == args.case), None)
    if case is None:
        print(f"unknown case {args.case!r}", file=sys.stderr)
        return 2
    digest = corpus_hash(cases)

    # Material for the tiers. Gold (expected_values, lesson) NEVER enters inputs.
    brief = case.goal
    root_input = {
        "synthetic": True,
        "evaluation_case": case.id,
        "chain_acceptance": args.run_id,
        "sources": [s.model_dump() for s in case.sources],
        "required_sources": list(case.required_sources),
        "brief": brief,
    }
    assert "expected_values" not in json.dumps(root_input)
    assert case.expected_values, "case carries gold for post-hoc grading only"
    schema = output_schema(case)
    schema_sha = hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()

    # Read-only gateway precheck: normal tenant profiles, no narrowing, no reorder.
    from ai_orchestrator.application.model_profiles import build_tenant_gateway

    db = Database.from_settings()
    try:
        gw = await build_tenant_gateway(args.org)
        try:
            providers = gw.available_providers()
            primary = gw.profiles.get("primary")
            primary_order = [c.model for c in primary.candidates] if primary is not None else []
        finally:
            await gw.aclose()
    finally:
        pass
    if "openrouter" not in providers:
        print("configuration error BEFORE network: openrouter adapter absent", file=sys.stderr)
        await db.dispose()
        return 2

    contract = {
        "chain_id": CHAIN_ID,
        "run_id": args.run_id,
        "org": args.org,
        "dataset": str(args.dataset),
        "case": case.id,
        "corpus_sha256": digest,
        "output_schema_sha256": schema_sha,
        "tenant_primary_order_readonly": primary_order,
        "started_at_utc": started_at,
        "wallclock_s": wallclock,
        "run_submission_cap": cap,
        "global_max": campaign_max,
        "ledger_resume_used": ledger.used,
        "ledger_path": str(args.ledger_path),
        "max_executions": min(args.max_executions, MAX_EXECUTIONS),
        "concurrency": 1,
        "run_mode": "live",
        "counting_source": COUNTING_SOURCE,
        "runtime": "normal build_runtime/PydanticAIRuntime via run_pipeline (ordinary UI path)",
        "gold_separation": "expected_values + lesson in corpus only; absent from all task inputs",
        "verdict_rules": {
            "pass": "leaf owned by dept head agent, completed, grade() clean, "
            "ancestor chain chief->office->dept exact, office task owned by "
            "parent-office head agent, no waiting_for_human, no granted approvals",
            "blocked": "outcome.waiting_for_human non-empty or approval gate pause; "
            "never reported as completion",
            "fail": "anything else, with concrete reason",
        },
    }
    (outdir / "chain-contract.json").write_text(
        json.dumps(contract, indent=2) + "\n", encoding="utf-8"
    )

    gate = _FreeSubmissionGate(ledger, per_case_max=cap, campaign_deadline_wall=deadline_wall)
    gate.reset_case(deadline_wall)
    observer: list[dict[str, object]] = []
    import httpx

    original_post, counted_post = c2._install_counting(ledger, observer, gate)
    httpx.AsyncClient.post = counted_post  # type: ignore[method-assign]
    from ai_orchestrator.persistence.repositories.task import TaskRepository

    receipt: dict[str, object] = {
        "chain_id": CHAIN_ID,
        "run_id": args.run_id,
        "synthetic": True,
        "started_at_utc": started_at,
        "case": case.id,
        "corpus_sha256": digest,
        "counting_source": COUNTING_SOURCE,
        "partial": True,
    }
    root_id = ""
    expected: dict[str, object] = {}
    try:
        async with db.committing_tenant_session(args.org) as session:
            from ai_orchestrator.persistence.models import Agent

            ceo = (
                await session.execute(
                    select(Agent).where(
                        Agent.organization_id == args.org,
                        Agent.name == "Executive Agent",
                    )
                )
            ).scalar_one()
            # Expected route, resolved from the live org chart (read-only) for
            # post-hoc grading -- NEVER written into the task for the model.
            units = (
                (
                    await session.execute(
                        text(
                            "SELECT slug, parent_id, head_agent_id, unit_type "
                            "FROM organizational_units WHERE organization_id=:o"
                        ),
                        {"o": args.org},
                    )
                )
                .mappings()
                .all()
            )
            by_id = {u["slug"]: u for u in units}
            # Resolve the owning office via the department unit's parent_id chain
            # (read-only, for post-hoc grading -- NEVER written into task inputs,
            # so the model must discover the route itself).
            dept_unit = by_id[case.department]
            parent_slug = (
                await session.execute(
                    text("SELECT slug FROM organizational_units WHERE id=:i"),
                    {"i": dept_unit["parent_id"]},
                )
            ).scalar_one()
            office_unit = next(u for u in units if u["slug"] == parent_slug)
            expected = {
                "chief_agent_id": str(ceo.id),
                "office_agent_id": str(office_unit["head_agent_id"]),
                "office_slug": str(office_unit["slug"]),
                "dept_agent_id": str(dept_unit["head_agent_id"]),
                "dept_slug": case.department,
            }
            repo = TaskRepository(session, args.org)
            ceo_goal = (
                case.goal + "\n\n"
                "You are the chief executive. A business request needs handling under "
                "company policy; some commitments may need an approver and no external "
                "action (no email) may be taken. You cannot resolve this alone: work "
                "the organisation chart -- delegate to the owning office, which "
                "delegates to the owning department. Report only through the normal "
                "chain. The brief below is the material; the declared output schema "
                "is the contract. Route by the requested deliverable; a later approval "
                "requirement does not transfer ownership of the original work. "
                "Preserve human gates."
                f"\n[chain run {args.run_id} case {case.id}]"
            )
            root = await repo.create(
                title=f"[CHAIN {args.run_id}] {case.id}",
                goal=ceo_goal,
                # "coordination", not "analysis": product coordination guards
                # (coordination_may_complete, _to_agent_task root handling)
                # key off this value. An "analysis" chief may answer alone and
                # complete, which forces the worker schema into the manager and
                # reports one agent wearing nine hats as coordination.
                task_type="coordination",
                requester_type="system",
                input=root_input,
                expected_output_schema=schema,
                constraints={
                    "synthetic": True,
                    "chain_acceptance": args.run_id,
                    "evaluation_corpus": digest,
                    "no_external_actions": True,
                },
                budget_limit_usd=0.01,
                budget_limit_tokens=24000,
                allow_parallel=False,
            )
            await repo.assign(root.id, ceo.id)
            root_id = str(root.id)
            await session.commit()
        receipt["root_task_id"] = root_id
        receipt["expected_route"] = expected

        try:
            outcome, stop_reason = await _drain(db, args, root_id, deadline_wall, wallclock)

            # Post-hoc evidence read (no network): tree, executions, approvals, delegations.
            # Tree walk by parent_task_id closure, UNION the delegations-table
            # closure (product truth: work_review._children reads delegations,
            # not parent_task_id). Chain round-3 correction, kept on record: the
            # old walk alone found 2/4 tasks and the harness reported FAIL with
            # "chain never reached leaf", while the pipeline steps and the
            # delegations in the SAME receipt showed chief->office->leaf twice.
            # That verdict was a harness evidence-walk error, not a product
            # error; the old receipt files keep their FAIL + failures list as
            # history, and new receipts carry this note instead of rewriting it.
            async with db.tenant_session(args.org) as session:
                _cols = (
                    "id,parent_task_id,root_task_id,owner_agent_id,title,status,"
                    "failure_category,last_error,output,attempt_count,completed_at,"
                    "expected_output_schema,input,created_at"
                )
                _by_id: dict[str, object] = {}
                _frontier = [root_id]
                for _ in range(8):
                    if not _frontier:
                        break
                    _rows = (
                        (
                            await session.execute(
                                text(
                                    "SELECT " + _cols + " FROM tasks "
                                    "WHERE organization_id=:o AND (id = ANY(:ids) "
                                    "OR parent_task_id = ANY(:ids)) ORDER BY created_at"
                                ),
                                {"o": args.org, "ids": _frontier},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    _frontier = []
                    for _row in _rows:
                        _key = str(_row["id"])
                        if _key not in _by_id:
                            _by_id[_key] = _row
                            _frontier.append(_key)
                tasks = list(_by_id.values())
                # Delegations closure from the root: every child the product
                # delegated, however deep, even when parent_task_id linkage is
                # absent. Any id it finds that the closure above missed is
                # fetched and merged, so manifest tiers cover the real tree.
                _deleg_seen: set[str] = {root_id}
                _deleg_frontier = [root_id]
                _deleg_edges: list[tuple[str, str]] = []
                for _ in range(8):
                    if not _deleg_frontier:
                        break
                    _edge_rows = (
                        (
                            await session.execute(
                                text(
                                    "SELECT parent_task_id, child_task_id FROM delegations "
                                    "WHERE organization_id=:o AND parent_task_id = ANY(:ids)"
                                ),
                                {"o": args.org, "ids": _deleg_frontier},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    _deleg_frontier = []
                    for _edge in _edge_rows:
                        _pair = (str(_edge["parent_task_id"]), str(_edge["child_task_id"]))
                        _deleg_edges.append(_pair)
                        if _pair[1] not in _deleg_seen:
                            _deleg_seen.add(_pair[1])
                            _deleg_frontier.append(_pair[1])
                _missed = [i for i in _deleg_seen if i not in _by_id and i != root_id]
                if _missed:
                    _extra = (
                        (
                            await session.execute(
                                text(
                                    "SELECT " + _cols + " FROM tasks "
                                    "WHERE organization_id=:o AND id = ANY(:ids) "
                                    "ORDER BY created_at"
                                ),
                                {"o": args.org, "ids": _missed},
                            )
                        )
                        .mappings()
                        .all()
                    )
                    for _row in _extra:
                        _by_id.setdefault(str(_row["id"]), _row)
                tasks = list(_by_id.values())
                task_ids = [str(t["id"]) for t in tasks]
                execs = (
                    (
                        await session.execute(
                            text(
                                "SELECT task_id,agent_id,status,runtime_adapter,model_profile,"
                                "model_used,input_tokens,output_tokens,reasoning_tokens,cost_usd,"
                                "duration_ms,model_call_count,error_kind,error_category,"
                                "error_message,summary,finished_at FROM executions "
                                "WHERE organization_id=:o AND task_id = ANY(:ids) "
                                "ORDER BY created_at"
                            ),
                            {"o": args.org, "ids": task_ids},
                        )
                    )
                    .mappings()
                    .all()
                    if task_ids
                    else []
                )
                delegs = (
                    (
                        await session.execute(
                            text(
                                "SELECT parent_task_id,child_task_id,"
                                "source_agent_id,target_agent_id,"
                                "objective,created_at FROM delegations "
                                "WHERE organization_id=:o AND (parent_task_id = ANY(:ids) "
                                "OR child_task_id = ANY(:ids)) ORDER BY created_at"
                            ),
                            {"o": args.org, "ids": task_ids},
                        )
                    )
                    .mappings()
                    .all()
                    if task_ids
                    else []
                )
                apprs = (
                    (
                        await session.execute(
                            text(
                                "SELECT * FROM approvals WHERE organization_id=:o "
                                "AND task_id = ANY(:ids)"
                            ),
                            {"o": args.org, "ids": task_ids},
                        )
                    )
                    .mappings()
                    .all()
                    if task_ids
                    else []
                )

            by_id_t = {str(t["id"]): t for t in tasks}
            leaf = None
            for t in tasks:
                if str(t["owner_agent_id"]) == expected["dept_agent_id"]:
                    leaf = t
            failures: list[str] = []
            blocked = False
            if outcome is not None and list(outcome.waiting_for_human):
                blocked = True
                failures.append(
                    "human gate pause (waiting_for_human="
                    f"{list(outcome.waiting_for_human)}); NOT completion"
                )
            granted = [
                a
                for a in apprs
                if str(dict(a).get("status", "")).lower() in ("granted", "approved")
            ]
            if granted:
                failures.append(f"{len(granted)} approval(s) granted during run; refusing to bless")
            # Manifest tiers must cover every pipeline step: the steps are what
            # the product actually executed, so a step with no tier row means the
            # evidence walk is blind, not that the product skipped work.
            _step_ids = [str(s.task_id) for s in outcome.steps] if outcome is not None else []
            failures.extend(_evidence_gaps(set(task_ids), _step_ids, _deleg_edges))
            failures.extend(_completion_gaps(outcome, tasks, str(expected["office_agent_id"])))
            if not execs or any(str(e.get("runtime_adapter")) != "pydantic_ai" for e in execs):
                failures.append("all chain executions must use the ordinary PydanticAI runtime")
            chain_ok = False
            if leaf is None:
                failures.append(
                    "no task owned by the department head agent; chain never reached leaf"
                )
            else:
                # Ancestor walk prefers the delegations table (product truth),
                # falling back to parent_task_id only where no delegation names
                # the parent. A rerun ordered by review_office_work always has a
                # delegation row; the column may not.
                _deleg_parent = {c: p for p, c in _deleg_edges}
                anc = []
                cur: object = leaf
                seen = set()
                while cur is not None and str(cur["id"]) not in seen:
                    seen.add(str(cur["id"]))
                    anc.append(str(cur["owner_agent_id"]))
                    pid = _deleg_parent.get(str(cur["id"])) or cur["parent_task_id"]
                    cur = by_id_t.get(str(pid)) if pid else None
                anc.reverse()  # root-first owner chain
                want = [
                    expected["chief_agent_id"],
                    expected["office_agent_id"],
                    expected["dept_agent_id"],
                ]
                if anc == want and len(tasks) >= 3:
                    chain_ok = True
                else:
                    failures.append(f"ancestor owner chain {anc} != expected chief->office->dept")
                if str(leaf["status"]) != "completed":
                    failures.append("leaf execution did not complete: " + str(leaf["status"]))
                failures.extend(grade(case, leaf["output"]))
                # Ordinary-path proof: the leaf must have run on PydanticAIRuntime
                # (build_runtime for a real provider), never a special harness.
                leaf_adapters = sorted(
                    {
                        str(e.get("runtime_adapter"))
                        for e in execs
                        if str(e.get("task_id")) == str(leaf["id"])
                    }
                )
                receipt["leaf_runtime_adapters"] = leaf_adapters
                if "pydantic_ai" not in leaf_adapters:
                    failures.append(
                        "ordinary-path proof missing: leaf executions ran on "
                        f"{leaf_adapters}, not PydanticAIRuntime"
                    )
            if gate.cost_stop:
                failures.append("provider reported nonzero cost; campaign stopped")
            verdict = _final_verdict(blocked, chain_ok, failures, gate.cost_stop)
            if stop_reason:
                failures.append(stop_reason)
                verdict = "FAIL" if verdict == "PASS" else verdict

            def _task_row(t: object) -> dict[str, object]:
                d = dict(t)
                d["output"] = _cap(d.get("output"), CAP_CHARS_ARTIFACT)
                d["input"] = {"keys": sorted((d.get("input") or {}).keys())}
                d["last_error"] = _cap(d.get("last_error"), CAP_CHARS_SUMMARY)
                for k in ("completed_at",):
                    d[k] = str(d.get(k)) if d.get(k) is not None else None
                return d

            manifest = {
                "run_id": args.run_id,
                "marker": {"chain_acceptance": args.run_id},
                "root_task_id": root_id,
                "expected_route": expected,
                "task_ids": task_ids,
                "tiers": [
                    {
                        "task_id": str(t["id"]),
                        "parent_task_id": str(t["parent_task_id"]) if t["parent_task_id"] else None,
                        "owner_agent_id": str(t["owner_agent_id"]),
                        "status": str(t["status"]),
                    }
                    for t in tasks
                ],
            }
            (outdir / "chain-manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            receipt.update(
                {
                    "partial": False,
                    "ledger": ledger.snapshot(),
                    "observer": observer,
                    "observer_n": len(observer),
                    "stop_reason": stop_reason,
                    "pipeline": {
                        "root_status": outcome.root_status if outcome else None,
                        "stopped_because": outcome.stopped_because if outcome else None,
                        "waiting_for_human": list(outcome.waiting_for_human) if outcome else [],
                        "steps": [
                            dataclasses.asdict(s) if dataclasses.is_dataclass(s) else dict(s)
                            for s in (outcome.steps if outcome else [])
                        ],
                        "reviews_accepted": outcome.reviews_accepted if outcome else 0,
                        "reviews_rerun": outcome.reviews_rerun if outcome else 0,
                        "reviews_escalated": outcome.reviews_escalated if outcome else 0,
                        "settled": list(outcome.settled) if outcome else [],
                        "settled_failed": list(outcome.settled_failed) if outcome else [],
                        "queue_counts": dict(outcome.queue_counts) if outcome else {},
                    },
                    "tasks": [_task_row(t) for t in tasks],
                    "leaf_task_id": str(leaf["id"]) if leaf is not None else None,
                    "leaf_output_full": leaf["output"] if leaf is not None else None,
                    "executions": [
                        {
                            k: (str(v) if k.endswith("_at") and v is not None else v)
                            for k, v in dict(e).items()
                            if k not in ("summary",)
                        }
                        | {"summary": _cap(dict(e).get("summary"), CAP_CHARS_SUMMARY)}
                        for e in execs
                    ],
                    "delegations": [
                        dict(d) | {"created_at": str(d.get("created_at"))} for d in delegs
                    ],
                    "approvals": [dict(a) for a in apprs],
                    "verdict": verdict,
                    "failures": failures,
                    "harness_note": (
                        "chain round-3 correction, kept as history not rewrite: "
                        "the round-3 receipt verdict FAIL with "
                        "'chain never reached leaf' was a harness evidence-walk "
                        "error (parent_task_id closure found 2/4 tasks). The "
                        "pipeline steps and delegations in that same file show "
                        "the chain reached the department tier twice. This run "
                        "walks delegations (product truth) and asserts manifest "
                        "tiers cover every pipeline step; the round-3 files "
                        "keep their FAIL + failures list unchanged."
                    ),
                    "exit_code": 0 if verdict == "PASS" else 2,
                    "cost_note": (
                        "cost unknown-never-zero: failed calls without provider usage "
                        "stay 'unknown'"
                    ),
                }
            )
            (outdir / "chain-round-1.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=1, default=str) + "\n",
                encoding="utf-8",
            )
            print(f"verdict {verdict}; ledger {ledger.snapshot()['used']}/{campaign_max}")
            return 0 if verdict == "PASS" else 2
        except BaseException as exc:
            # Save the receipt and actual task IDs even on exception: evidence
            # first, propagate interrupts after.
            receipt.update(
                {
                    "partial": True,
                    "ledger": ledger.snapshot(),
                    "observer": observer,
                    "observer_n": len(observer),
                    "root_task_id": root_id,
                    "expected_route": expected,
                    "exception": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "verdict": "FAIL",
                    "failures": [
                        f"runner exception before receipt: {type(exc).__name__}: {str(exc)[:300]}"
                    ],
                    "exit_code": 2,
                }
            )
            (outdir / "chain-round-1.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=1, default=str) + "\n",
                encoding="utf-8",
            )
            print(f"verdict FAIL (exception); ledger {ledger.snapshot()['used']}/{campaign_max}")
            if isinstance(exc, (KeyboardInterrupt, asyncio.CancelledError)):
                raise
            return 2
    finally:
        httpx.AsyncClient.post = original_post  # type: ignore[method-assign]
        await db.dispose()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--org", required=True, help="Authorized synthetic validation tenant")
    p.add_argument("--case", default="sales-normal-v1")
    p.add_argument("--dataset", type=Path, default=ROOT / "examples/organization_eval/cases.json")
    p.add_argument("--run-id", default="chain-1")
    p.add_argument("--ledger-path", type=Path, default=DEFAULT_LEDGER)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT
        / ".orchestra/chain-acceptance"
        / dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ"),
    )
    p.add_argument("--submission-cap", type=int, default=RUN_CAP)
    p.add_argument("--wallclock-s", type=float, default=WALLCLOCK_S)
    p.add_argument("--max-executions", type=int, default=MAX_EXECUTIONS)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.submission_cap > RUN_CAP:
        print(f"submission cap {args.submission_cap} exceeds {RUN_CAP}; clamping", file=sys.stderr)
    if args.wallclock_s > WALLCLOCK_S:
        print(f"wallclock {args.wallclock_s}s exceeds {WALLCLOCK_S}s; clamping", file=sys.stderr)
    if args.submission_cap < 1 or args.wallclock_s <= 0 or args.max_executions < 1:
        parser.error("submission cap, wallclock and max executions must be positive")
    # Single owner for the whole run: another process must not race reservations.
    lock_path = args.ledger_path.with_suffix(args.ledger_path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("campaign ledger is already owned by another acceptance runner")
        return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
