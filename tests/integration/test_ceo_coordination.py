"""The CEO's two questions: *what needs me* and *what happened to it*.

## Why these two are tested together

`ceo_work_queue` sorts by `waiting_on` and `task_report` counts the decisions. They read the
same rows, and a test of one without the other cannot catch the failure that matters: a
queue that says "nothing needs you" while a report shows a decision waiting. So the pairing
test at the end of this file is the load-bearing one.

## `waiting_on` is a classification, not a flag

Three cases, mutually exclusive, and the sort puts `you` first:

| `waiting_on` | when |
|---|---|
| `you` | an approval is pending, **or** the task is `created`/`assigned` and unfinished |
| `an_agent` | a terminal state that is not `completed` — `failed`, `blocked` |
| `nobody` | `completed` |

`you` is the only reason to open the page, which is why it is computed here and asserted
rather than filtered in the browser.

## The report assembles five tables and says so

`tasks`, `delegations`, `executions`, `approvals`, `events`. A report that silently omits
a source is a report that can be wrong in a way nobody notices, so every assertion here
names the table it is checking, and the cross-checks assert that the counts agree **across**
the report's own sections rather than within one.

## The two things it will not do

**It will not say the work was good.** There is no verdict anywhere in the payload, and
`test_the_report_carries_no_verdict` asserts the absence of one — because a report that
implies a verdict is doing an approval's job with a `SELECT`.

**It will not hide a failure.** A failed execution and a refused delegation both appear,
and `test_a_failed_execution_is_reported_rather_than_hidden` says so. A report of a system
that only shows successes is a report about a different system.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy import text

from ai_orchestrator.application.coordination import ceo_work_queue, task_report
from ai_orchestrator.domain.delegation import DelegationLimits
from ai_orchestrator.domain.errors import NotFoundError
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]


def _limits() -> DelegationLimits:
    """A generous envelope, so the test exercises the *writer* and not the limits.

    All six fields are required and named for what they cap -- depth, fanout, active
    descendants, tokens, cost, runtime -- and a delegation under a zero budget is refused
    before a row exists, which would test the authorisation and not the column.
    """
    return DelegationLimits(
        max_depth=4,
        max_fanout=8,
        max_active_descendants=16,
        max_tokens=200_000,
        max_cost_usd=5.0,
        max_runtime_s=3_600,
    )


async def _user(tenant: Tenant, name: str = "Nguyen Thi Lan") -> str:
    """A person who can sign a decision.

    `approvals.decided_by` is a **composite foreign key to `users`**, not a free-text name
    -- so a decision cannot be attributed to somebody who does not exist. That is the right
    rule, and it is why this fixture creates a user rather than writing a name: the column
    is an identity, not a label. The first version wrote `'dev:no-auth'` into it and was
    refused by `fk_approvals_decided_by_users`.
    """
    user_id = f"usr_{tenant.organization_id[-8:]}"
    await tenant.session.execute(
        text(
            "INSERT INTO users (id, organization_id, email, display_name, password_hash, "
            "  role, is_org_admin, is_privileged, mfa_enabled, token_version, is_active) "
            "VALUES (:i, CAST(:o AS varchar(64)), :e, :n, 'not-a-real-hash', 'org_admin', "
            "  true, true, false, 1, true)"
        ),
        {
            "i": user_id,
            "o": tenant.organization_id,
            "e": f"{user_id}@example.invalid",
            "n": name,
        },
    )
    await tenant.commit()
    return user_id


async def _agent(tenant: Tenant, name: str) -> str:
    """An agent, created through the chain the foreign keys require.

    `agents.definition_id` and `agents.role_id` are `NOT NULL` and both reference a
    *composite* `(organization_id, id)` key -- so an agent cannot be inserted on its own at
    all. The chain is `roles` -> `agent_definitions` -> `agents`, and the fixture walks it
    in that order.

    The alternative -- reusing an agent that happens to exist -- is not available, because
    the `tenant` fixture is a fresh organization and a cross-tenant id is exactly what the
    composite keys exist to refuse.

    Two column types here are the mirror image of each other elsewhere in this repository:
    `agents.capabilities` and `roles.allowed_capabilities` are both a *list of strings*,
    but `agents.capabilities` is `varchar[]` and `roles.allowed_capabilities` is `jsonb`.
    Same word, two types, two tables. And within *one* table the pair splits too:
    `agent_definitions.allowed_child_roles` is jsonb while `agents.capabilities` two lines
    later is `varchar[]`. The only way to get this right is to read
    `information_schema.columns`, which is what was done -- three times, three different
    answers.
    """
    tail = tenant.organization_id[-8:]
    # Idempotent, because a test may legitimately ask for the same agent twice -- one to
    # delegate from and one to look up. `roles.id` is the primary key on its own and the
    # test database is never truncated, so a second insert is a `UniqueViolationError` in a
    # test about something else. F156, the fifth time.
    already = (
        await tenant.session.execute(
            text(
                "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                "  AND name = :n"
            ),
            {"o": tenant.organization_id, "n": name},
        )
    ).first()
    if already is not None:
        return str(already[0])

    role_id = f"rol_{tail}_{name.split()[0].lower()}"
    definition_id = f"agd_{tail}_{name.split()[0].lower()}"
    agent_id = f"agt_{tail}_{name.split()[0].lower()}"
    await tenant.session.execute(
        text(
            "INSERT INTO roles (id, organization_id, name, description, authority_profile, "
            "  allowed_capabilities, max_autonomy, may_delegate_to_peers, may_spawn_subagents, "
            "  max_delegation_depth, is_system_role) "
            "VALUES (:i, CAST(:o AS varchar(64)), :n, 'mock role', '{}'::jsonb, "
            "  '[]'::jsonb, 'l2_parent_review', false, false, 0, false)"
        ),
        {"i": role_id, "o": tenant.organization_id, "n": f"{name} role"},
    )
    await tenant.session.execute(
        text(
            "INSERT INTO agent_definitions (id, organization_id, name, version, role_id, "
            "  system_instructions, behavior_config, model_profile, allowed_child_roles, "
            "  allowed_task_types, memory_policy, escalation_policy, prompt_version, "
            "  is_active) VALUES (:i, CAST(:o AS varchar(64)), :n, 1, :r, 'mock', "
            "  '{}'::jsonb, 'deterministic', '[]'::jsonb, "
            '  \'["analysis","coordination","research"]\'::jsonb, \'{}\'::jsonb, '
            "  '{}'::jsonb, 'v1', true)"
        ),
        {"i": definition_id, "o": tenant.organization_id, "n": name, "r": role_id},
    )
    await tenant.session.execute(
        text(
            "INSERT INTO agents (id, organization_id, definition_id, role_id, name, "
            "  description, lifecycle_status, runtime_status, health, autonomy_level, "
            "  runtime_adapter, model_profile, active_tasks, queue_depth, "
            "  estimated_latency_ms, capabilities, metadata, definition_version, "
            "  autonomy_ceiling, granted_level) "
            "VALUES (:i, CAST(:o AS varchar(64)), :d, :r, :n, 'mock', 'active', 'idle', "
            "  'healthy', 'L1', 'scripted', 'deterministic', 0, 0, 0, "
            "  ARRAY[]::varchar[], '{}'::jsonb, 1, 'L2', 'L1')"
        ),
        {
            "i": agent_id,
            "o": tenant.organization_id,
            "d": definition_id,
            "r": role_id,
            "n": name,
        },
    )
    await tenant.commit()
    return agent_id


async def _task(
    tenant: Tenant,
    title: str,
    *,
    status: str = "created",
    goal: str | None = None,
    owner: str | None = None,
) -> str:
    task_id = f"tsk_{tenant.organization_id[-8:]}_{title.split()[0].lower()}"
    await tenant.session.execute(
        text(
            # `input`, `output`, `constraints`, `fingerprint`, `dedup_key`, `spent_usd` and
            # `spent_tokens` are all `NOT NULL`. The fingerprint and the dedup key are the
            # ones worth noticing: a fixture that invents them would produce a task the
            # repository could not recognise as equivalent to anything, so they are derived
            # from the title -- the same way `TaskRepository.create` derives them.
            "INSERT INTO tasks (id, organization_id, title, goal, task_type, status, "
            "  priority, requester_type, input, output, constraints, fingerprint, "
            "  dedup_key, spent_usd, spent_tokens, attempt_count) "
            "VALUES (:i, CAST(:o AS varchar(64)), :t, :g, 'analysis', :s, 'normal', "
            "  'human', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, :fp, :dk, 0, 0, 0)"
        ),
        {
            "i": task_id,
            "o": tenant.organization_id,
            "t": title,
            "g": goal or f"goal for {title}",
            "s": status,
            "fp": "fp_" + task_id,
            "dk": "dk_" + task_id,
        },
    )
    if owner is not None:
        agent_id = await _agent(tenant, owner)
        await tenant.session.execute(
            text("UPDATE tasks SET owner_agent_id = :a WHERE id = :i"),
            {"a": agent_id, "i": task_id},
        )
    await tenant.commit()
    return task_id


class TestTheWorkQueue:
    async def test_an_empty_queue_is_empty_not_missing(self, tenant: Tenant) -> None:
        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        assert queue["items"] == []
        assert (queue["total"], queue["needs_you"]) == (0, 0)

    @pytest.mark.parametrize(
        ("status", "expected"),
        (
            ("created", "you"),
            ("assigned", "you"),
            # **`failed` moved from `an_agent` to `you`, 2026-10-03.**
            #
            # This line used to pin `an_agent`, with a fair argument: a failed task needs
            # somebody, and it is not waiting for a button press. Both halves were right
            # and the conclusion was wrong, because `an_agent` is the column the register
            # renders as the tile **"With an agent"** and a filter of the same name. That
            # is the wording of a system that is working, attached to work that stopped.
            # Measured on the demo company: the register read `With an agent: 2` while
            # both of those tasks had `status = failed`.
            #
            # A failed task is waiting on a **decision**, and `you` is the column a person
            # acts in. So is where it goes.
            ("failed", "you"),
            ("blocked", "you"),
            ("completed", "nobody"),
            ("cancelled", "nobody"),
            # `running` is the *only* thing in this column, and that is the point: a
            # filter called "With an agent" that can hold a task nobody is working on
            # cannot be used to answer "is anybody working on this".
            ("running", "an_agent"),
        ),
    )
    async def test_waiting_on_is_classified_by_what_is_left_to_do(
        self, tenant: Tenant, status: str, expected: str
    ) -> None:
        """The three-way split, asserted for every case.

        **`an_agent` means one thing: a running task.** Everything else is either waiting
        on a person -- an approval, an unclaimed task, a failure awaiting a decision -- or
        finished. The register's three filters are read by eye against these numbers, so a
        column whose name is a claim about the system's state has to be true.
        """
        await _task(tenant, f"Task {status}", status=status)
        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        assert queue["items"][0]["waiting_on"] == expected

    async def test_nothing_failed_is_ever_reported_as_an_agent_holding_it(
        self, tenant: Tenant
    ) -> None:
        """**The invariant, stated directly**, because the bug was a wording problem.

        The register shows `With an agent: N` and a filter named after it. If a single
        failed task can appear there, the number means "work that stopped" and the label
        means "work in progress", and a reader has no way to tell which one they are
        looking at. This is the assertion that would have caught it on the day the label
        was written.
        """
        for title, status in (("Dead", "failed"), ("Stuck", "blocked"), ("Over", "completed")):
            await _task(tenant, title, status=status)
        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        holders = [t for t in queue["items"] if t["waiting_on"] == "an_agent"]
        assert holders == [], (
            f"{[t['status'] for t in holders]} counted as held by an agent; only a running task is"
        )
        assert queue["in_flight"] == 0

    async def test_a_pending_approval_puts_the_task_in_your_column(self, tenant: Tenant) -> None:
        """Even when the task itself is finished.

        This is the case the whole view exists for: work that is *done* and a decision that
        is not. Sorted by task status it disappears; sorted by `waiting_on` it is first.
        """
        task = await _task(tenant, "Finished but undecided", status="completed")
        await tenant.session.execute(
            text(
                "INSERT INTO approvals (id, organization_id, task_id, action_type, "
                "  action_payload, effect_class, risk_level, reason, payload_hash, "
                "  requested_by, requested_by_type, required_approver_roles, status) "
                "VALUES (:i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), 'commit', "
                "  '{}'::jsonb, 'mutate_internal', 'high', 'needs a signature', :h, "
                "  'mock', 'agent', ARRAY['org_admin'], 'pending')"
            ),
            {
                "i": f"apr_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "t": task,
                "h": "0" * 64,
            },
        )
        await tenant.commit()
        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        item = queue["items"][0]
        assert item["waiting_on"] == "you"
        assert item["approvals_waiting"] == 1
        assert queue["needs_you"] == 1

    async def test_the_three_counts_add_up_to_the_list(self, tenant: Tenant) -> None:
        """The counts and the items must not disagree, or the tiles and the list can.

        The only arithmetic on the page that is worth asserting, and it is worth asserting
        because it is the one a reader checks by eye: three numbers and a list of rows.
        """
        for title, status in (
            ("Alpha", "created"),
            ("Beta", "assigned"),
            ("Gamma", "completed"),
            ("Delta", "failed"),
        ):
            await _task(tenant, title, status=status)
        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        assert queue["needs_you"] + queue["in_flight"] + queue["settled"] == len(queue["items"]), (
            f"the tiles say {queue['needs_you']}/{queue['in_flight']}/{queue['settled']} "
            f"and the list has {len(queue['items'])} rows"
        )
        # created, assigned **and** failed: the first two need an owner, the third needs a
        # decision. `completed` needs nobody.
        assert queue["needs_you"] == 3, "created, assigned and failed all need a person"

    async def test_the_filter_narrows_and_the_count_follows(self, tenant: Tenant) -> None:
        await _task(tenant, "Open one", status="created")
        await _task(tenant, "Done one", status="completed")
        everything = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        settled = await ceo_work_queue(
            tenant.session, organization_id=tenant.organization_id, state="completed"
        )
        assert everything["total"] == 2
        assert settled["total"] == 1 and len(settled["items"]) == 1


class TestTheReport:
    async def test_a_task_with_nothing_happened_yet_still_reports(self, tenant: Tenant) -> None:
        """An empty report is a report, not a 404. The task exists; that is the answer."""
        task = await _task(tenant, "Bare")
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        assert report["task"]["id"] == task
        assert report["delegations"] == [] and report["executions"] == []
        assert report["approvals"] == [] and report["tree_size"] == 1
        assert report["summary"]["outcome"] == "created"

    async def test_a_delegation_appears_with_both_agents_named(self, tenant: Tenant) -> None:
        """A tree edge with `from_agent: null` is a tree the reader cannot read."""
        task = await _task(tenant, "Coord", status="assigned", owner="Executive Agent")
        target = await _agent(tenant, "Finance Agent")
        head = await _agent(tenant, "Executive Agent")
        child = await _task(tenant, "Child", status="assigned")
        await tenant.session.execute(
            text(
                "INSERT INTO delegations (id, organization_id, parent_task_id, child_task_id, "
                "  source_agent_id, target_agent_id, status, objective, depth, "
                "  delegation_path) VALUES (:i, CAST(:o AS varchar(64)), CAST(:p AS varchar(64)), "
                "  CAST(:c AS varchar(64)), :s, :t, 'accepted', 'check the band', 1, "
                "  CAST(:path AS jsonb))"
            ),
            {
                "i": f"dlg_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "p": task,
                "c": child,
                "s": head,
                "t": target,
                "path": json.dumps([{"depth": 1, "task_id": child}]),
            },
        )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        edge = report["delegations"][0]
        assert (edge["from_agent"], edge["to_agent"]) == ("Executive Agent", "Finance Agent")
        assert report["summary"]["delegated_to"] == 1

    async def test_a_refused_delegation_is_reported_with_its_reason(self, tenant: Tenant) -> None:
        """ "The agent could not do this and said why" is the most useful line in the account.

        A report that only shows accepted edges is a report about a system where nothing is
        ever refused, and this repository has 45 failed tasks that would vanish from it.
        """
        task = await _task(tenant, "Refuses", status="failed", owner="Executive Agent")
        source = await _agent(tenant, "Executive Agent")
        await tenant.session.execute(
            text(
                "INSERT INTO delegations (id, organization_id, parent_task_id, "
                "  source_agent_id, target_agent_id, status, objective, denial_reason, depth) "
                "VALUES (:i, CAST(:o AS varchar(64)), CAST(:p AS varchar(64)), :s, :t, "
                "  'refused', 'sign the payment', 'the amount is above the DOA band', 1)"
            ),
            {
                "i": f"dlg_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "p": task,
                "s": source,
                "t": await _agent(tenant, "Finance Agent"),
            },
        )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        assert report["summary"]["refused"] == 1
        assert "DOA band" in report["delegations"][0]["denial_reason"]

    async def test_a_failed_execution_is_reported_rather_than_hidden(self, tenant: Tenant) -> None:
        task = await _task(tenant, "Fails", status="failed", owner="Executive Agent")
        agent = await _agent(tenant, "Executive Agent")
        await tenant.session.execute(
            text(
                "INSERT INTO executions (id, organization_id, task_id, agent_id, attempt, "
                "  status, runtime_adapter, model_profile, policy_version, input_hash, "
                "  output_hash, summary, error_message, input_tokens, output_tokens, "
                "  cost_usd, duration_ms, retry_count) "
                "VALUES (:i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), :a, 1, "
                "  'failed', 'scripted', 'deterministic', 1, :ih, :oh, 'gave up', "
                "  'the agent exceeded its turn budget', 100, 0, 0.0, 160356, 0)"
            ),
            {
                "i": f"exe_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "t": task,
                "a": agent,
                "ih": "a" * 64,
                "oh": "b" * 64,
            },
        )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        assert report["summary"]["failed"] == 1
        assert report["summary"]["executed"] == 0
        assert "turn budget" in report["executions"][0]["error_message"]
        assert "gave up" in report["executions"][0]["summary"]

    async def test_an_approval_carries_its_decision_and_who_made_it(self, tenant: Tenant) -> None:
        task = await _task(tenant, "Decided", status="completed")
        user = await _user(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO approvals (id, organization_id, task_id, action_type, "
                "  action_payload, effect_class, risk_level, reason, payload_hash, "
                "  requested_by, requested_by_type, required_approver_roles, status, "
                "  decision, decision_note, decided_by, decided_at) "
                "VALUES (:i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), 'commit', "
                "  '{}'::jsonb, 'mutate_internal', 'high', 'above the band', :h, "
                "  'mock', 'agent', ARRAY['org_admin'], 'approved', 'approved', "
                "  'within the delegated authority', :by, now())"
            ),
            {
                "i": f"apr_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "t": task,
                "h": "0" * 64,
                "by": user,
            },
        )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        decision = report["approvals"][0]
        assert decision["decision"] == "approved"
        assert decision["decided_by"] == user, (
            "an approval with nobody behind it is a tick in a box, so the actor travels "
            "with the decision -- and the column is a foreign key to `users`, so the name "
            "cannot be invented"
        )
        assert report["summary"]["approvals_decided"] == 1
        assert report["summary"]["approvals_pending"] == 0

    async def test_the_report_carries_no_verdict(self, tenant: Tenant) -> None:
        """No `good`, no `correct`, no `succeeded` judgement on the work itself.

        Quality is the approval's subject. A report that implies a verdict is doing an
        approval's job with a `SELECT`, and the day somebody trusts it, the approval is
        decorative.
        """
        task = await _task(tenant, "Judged", status="completed")
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        blob = str(report).lower()
        for verdict in ("quality_score", "verdict", "was_correct", "looks_good", "grade"):
            assert verdict not in blob, f"the report carries a verdict field: {verdict}"

    async def test_the_tree_includes_the_work_the_task_produced(self, tenant: Tenant) -> None:
        """Only the task you asked about is a shell, not an account.

        Three children under a root, and the report has to show all three -- otherwise a
        person reads "handed to 3 agents" against a tree with one row in it.
        """
        root = await _task(tenant, "Root", status="completed")
        for name in ("First", "Second", "Third"):
            child = await _task(tenant, name, status="completed")
            await tenant.session.execute(
                text("UPDATE tasks SET parent_task_id = :p, root_task_id = :r WHERE id = :i"),
                {"p": root, "r": root, "i": child},
            )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=root
        )
        assert report["tree_size"] == 4, "the root and the three it produced"
        assert {n["id"] for n in report["tree"]} >= {root} | set(
            report["tree"][1].__iter__() and [n["id"] for n in report["tree"]]
        )

    async def test_another_tenants_task_is_a_404_not_a_403(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        theirs = await _task(other_tenant, "Theirs")
        with pytest.raises(NotFoundError):
            await task_report(
                tenant.session, organization_id=tenant.organization_id, task_id=theirs
            )


class TestTheTwoAgree:
    async def test_a_waiting_decision_shows_up_in_both(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """The load-bearing test of this file.

        The queue says "you" and the report says one decision is pending, **for the same
        task**. Testing one without the other cannot catch a queue that reads a different
        table than the report -- which is how a dashboard ends up saying nothing needs
        attention while a decision sits unanswered.
        """
        task = await _task(tenant, "Both", status="completed")
        await tenant.session.execute(
            text(
                "INSERT INTO approvals (id, organization_id, task_id, action_type, "
                "  action_payload, effect_class, risk_level, reason, payload_hash, "
                "  requested_by, requested_by_type, required_approver_roles, status) "
                "VALUES (:i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), 'commit', "
                "  '{}'::jsonb, 'mutate_internal', 'high', 'a signature', :h, "
                "  'mock', 'agent', ARRAY['org_admin'], 'pending')"
            ),
            {
                "i": f"apr_{tenant.organization_id[-8:]}",
                "o": tenant.organization_id,
                "t": task,
                "h": "0" * 64,
            },
        )
        await tenant.commit()

        queue = await ceo_work_queue(tenant.session, organization_id=tenant.organization_id)
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        row = next(i for i in queue["items"] if i["id"] == task)
        assert row["waiting_on"] == "you"
        assert row["approvals_waiting"] == report["summary"]["approvals_pending"] == 1
        assert queue["needs_you"] == 1

        # And the other tenant sees nothing of it, on either side.
        their_queue = await ceo_work_queue(
            other_tenant.session, organization_id=other_tenant.organization_id
        )
        assert their_queue["total"] == 0 and their_queue["needs_you"] == 0


class TestTime:
    async def test_a_deadline_travels_as_a_datetime_not_a_string(self, tenant: Tenant) -> None:
        """A deadline the browser has to parse is a deadline somebody will get wrong."""
        task = await _task(tenant, "Timed")
        await tenant.session.execute(
            text("UPDATE tasks SET deadline_at = :d WHERE id = :i"),
            {"d": dt.datetime(2027, 1, 31, 17, 0, tzinfo=dt.UTC), "i": task},
        )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        assert isinstance(report["task"]["deadline_at"], dt.datetime)
        assert report["task"]["deadline_at"].year == 2027


class TestTheThreeFixesThisSuiteExistsFor:
    """Each of these is a defect that was found by running the product, not by reading it.

    They are grouped rather than scattered because they share a shape: **something that
    looked like the truth and was not.** A delegation with no child, a run that started
    nothing, and a stuck execution counted as a success. In all three the code was
    reasonable and the number or the state on the page was wrong.
    """

    async def test_a_delegation_names_its_child_task(self, tenant: Tenant) -> None:
        """`delegations.child_task_id` was NULL on 7 of 7 real delegations.

        `DelegationExecutor` created the child task and then called `record()` without
        passing the id it was holding. So the column was null on every row the platform
        had ever written, and every reader that walked the tree had to fall back to
        `tasks.parent_task_id`. The page's delegation graph did exactly that and drew a
        row of boxes with no edges.

        The repository's own `record` is the writer under test, so this drives it the way
        the executor does -- with the child, and asserting the edge survives.
        """

        from ai_orchestrator.application.coordination import task_report as _report
        from ai_orchestrator.domain.delegation import DelegationPath
        from ai_orchestrator.domain.ids import AgentId, TaskId
        from ai_orchestrator.persistence.repositories.task import DelegationRepository

        parent = await _task(tenant, "Parent", status="assigned", owner="Executive Agent")
        child = await _task(tenant, "Child", status="assigned")
        source = (
            await tenant.session.execute(
                text(
                    "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                    "  AND name = 'Executive Agent'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar_one()
        target = await _agent(tenant, "Finance Agent")

        repo = DelegationRepository(tenant.session, tenant.organization_id)
        recorded = await repo.record(
            parent_task_id=parent,
            child_task_id=child,
            source_agent_id=str(source),
            target_agent_id=target,
            objective="check the band",
            path=DelegationPath.root(AgentId(str(source)), TaskId(parent)),
            platform_limits=_limits(),
            parent_limits=_limits(),
        )
        # Read the attribute **before** committing. `commit()` expires every object in the
        # session, so the next access to a column is a lazy load, and a lazy load in a
        # synchronous expression raises `MissingGreenlet: greenlet_spawn has not been
        # called`. The commit is not what the assertion is about.
        recorded_child = recorded.child_task_id
        await tenant.commit()
        assert recorded_child == child, (
            "record() accepted a delegation and dropped the child task, which is how every "
            "delegation row in this database ended up with a null child"
        )
        report = await _report(
            tenant.session, organization_id=tenant.organization_id, task_id=parent
        )
        assert report["delegations"][0]["child_task_id"] == child

    async def test_a_stuck_execution_is_not_counted_as_a_success(self, tenant: Tenant) -> None:
        """`len(executions) - len(failed)` counts `running` as completed.

        The seeded history holds four executions still marked `running` on tasks that
        reached `failed`, so the report said "1 ran, 0 failed" for a failure -- wrong in
        the reassuring direction, which is the direction that hurts.

        A stuck execution is also *information*: a run was interrupted and nothing closed
        it. So it gets its own key rather than being folded into either total.
        """
        task = await _task(tenant, "Interrupted", status="failed", owner="Executive Agent")
        agent = (
            await tenant.session.execute(
                text(
                    "SELECT id FROM agents WHERE organization_id = CAST(:o AS varchar(64)) "
                    "  AND name = 'Executive Agent'"
                ),
                {"o": tenant.organization_id},
            )
        ).scalar_one()
        for status, suffix in (("running", "r1"), ("failed", "f1")):
            await tenant.session.execute(
                text(
                    "INSERT INTO executions (id, organization_id, task_id, agent_id, "
                    "  attempt, status, runtime_adapter, model_profile, policy_version, "
                    "  input_hash, output_hash, summary, input_tokens, output_tokens, "
                    "  cost_usd, duration_ms, retry_count) "
                    "VALUES (:i, CAST(:o AS varchar(64)), CAST(:t AS varchar(64)), :a, 1, "
                    "  :st, 'scripted', 'deterministic', 1, :ih, :oh, 's', 0, 0, 0.0, 1, 0)"
                ),
                {
                    "i": f"exe_{tenant.organization_id[-8:]}_{suffix}",
                    "o": tenant.organization_id,
                    "t": task,
                    "a": str(agent),
                    "st": status,
                    "ih": suffix.ljust(64, "0")[:64],
                    "oh": suffix.ljust(64, "0")[:64],
                },
            )
        await tenant.commit()
        report = await task_report(
            tenant.session, organization_id=tenant.organization_id, task_id=task
        )
        summary = report["summary"]
        assert summary["executed"] == 0, (
            f"a `running` execution was counted as completed: {summary}"
        )
        assert summary["failed"] == 1
        assert summary["in_flight"] == 1, (
            "the stuck execution must be visible; folding it into either total is how it "
            f"stayed invisible: {summary}"
        )
        assert summary["attempts"] == 2
