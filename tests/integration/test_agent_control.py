"""The kill switch, the decision log, and the two ways to get one wrong.

`agents.kill_switch` existed for several tranches with three constraints around it and
**no code path that could set it**. A kill switch nobody can pull is a comment, and it
is the comment on the one control Tập 1 §5.3 names. These tests are what makes it a
control.

Three things are asserted that a naive implementation gets wrong:

* **The reason is required, and the database already said so.**
  `ck_agents_kill_is_recorded` refuses `kill_switch` without a non-empty `kill_reason`
  and a `killed_at`. So a blank reason must be a **422 naming the field** at the edge of
  the API, not an `IntegrityError` naming a constraint at the bottom of a stack trace.

* **A kill writes to the decision log**, with `actor_type = 'system'` and `agent_id`
  naming the *subject*. A row saying "agent X decided to kill agent X" would be a lie in
  the one table whose purpose is to be believed.

* **Reviving is not the inverse.** It grants L1 rather than restoring the previous grant,
  because an agent stopped for a reason and running again has not had that grant
  re-justified. And the reason survives in the log even though the column is cleared.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from ai_orchestrator.domain.ids import new_ulid
from tests.integration.tenant_context import Tenant

pytestmark = pytest.mark.integration


async def _agent(tenant: Tenant, name: str = "probe") -> str:
    """A minimal agent: a role, an org unit, a definition, then the row.

    Built the same way in every test module, which is the smell this file could have
    removed -- but the alternative is a fixture in `conftest.py` that every test depends
    on, and a fixture that is subtly wrong is worse than a repeated six lines.
    """
    tail = tenant.organization_id[-10:]
    for statement, params in (
        (
            "INSERT INTO roles (id, organization_id, name) VALUES (:i, :o, :n)"
            " ON CONFLICT DO NOTHING",
            {"i": f"rol_{tail}", "o": tenant.organization_id, "n": f"role {tail}"},
        ),
        (
            "INSERT INTO organizational_units (id, organization_id, name, slug)"
            " VALUES (:i, :o, :n, :s) ON CONFLICT DO NOTHING",
            {
                "i": f"oru_{tail}",
                "o": tenant.organization_id,
                "n": f"unit {tail}",
                "s": f"u-{tail}",
            },
        ),
        (
            "INSERT INTO agent_definitions (id, organization_id, name, role_id)"
            " VALUES (:i, :o, :n, :r) ON CONFLICT DO NOTHING",
            {
                "i": f"def_{tail}",
                "o": tenant.organization_id,
                "n": f"def {tail}",
                "r": f"rol_{tail}",
            },
        ),
        (
            "INSERT INTO agents (id, organization_id, name, definition_id, role_id,"
            " org_unit_id) VALUES (:i, :o, :n, :d, :r, :u) ON CONFLICT DO NOTHING",
            {
                "i": f"agt_{tail}",
                "o": tenant.organization_id,
                "n": name,
                "d": f"def_{tail}",
                "r": f"rol_{tail}",
                "u": f"oru_{tail}",
            },
        ),
    ):
        await tenant.session.execute(text(statement), params)
    await tenant.commit()
    return f"agt_{tail}"


def _headers(organization_id: str) -> dict[str, str]:
    from tests.integration.api_client import auth_headers

    return auth_headers(organization_id)


class TestTheControlView:
    async def test_it_reports_the_posture(self, client: Any, tenant: Tenant) -> None:
        agent = await _agent(tenant)
        got = (
            await client.get(
                f"/api/v1/agents/{agent}/control", headers=_headers(tenant.organization_id)
            )
        ).json()
        assert got["agent_id"] == agent
        assert got["kill_switch"] is False
        assert got["granted_level"] == "L1"
        assert got["autonomy_ceiling"] == "L1"

    async def test_an_unknown_agent_is_404(self, client: Any, tenant: Tenant) -> None:
        """404 and not 403: "forbidden" would confirm the row exists."""
        got = await client.get(
            "/api/v1/agents/agt_nope/control", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 404


class TestKilling:
    async def test_it_records_the_reason_the_moment_and_the_actor(
        self, client: Any, tenant: Tenant
    ) -> None:
        agent = await _agent(tenant)
        got = await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "emitting costs without an approved procedure"},
        )
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["kill_switch"] is True
        assert body["kill_reason"] == "emitting costs without an approved procedure"
        assert body["killed_at"] is not None
        assert body["killed_by"], "a kill with nobody behind it is an accident"
        assert body["logged"] is True

    async def test_a_blank_reason_is_refused_by_name(self, client: Any, tenant: Tenant) -> None:
        """422 at the edge, not an `IntegrityError` at the bottom.

        `ck_agents_kill_is_recorded` is the backstop; the endpoint is the door, and a
        door names the field.
        """
        agent = await _agent(tenant)
        for reason in ("", "  ", "x"):
            got = await client.post(
                f"/api/v1/agents/{agent}/kill",
                headers=_headers(tenant.organization_id),
                json={"reason": reason},
            )
            assert got.status_code == 422, f"{reason!r} was accepted"
        assert "reason" in got.text

    async def test_a_missing_body_is_refused(self, client: Any, tenant: Tenant) -> None:
        agent = await _agent(tenant)
        got = await client.post(
            f"/api/v1/agents/{agent}/kill", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 422

    async def test_killing_twice_keeps_the_second_reason(self, client: Any, tenant: Tenant) -> None:
        """Not a 409.

        An operator who pulls a switch twice wants the second reason to be the one on the
        record. A refusal here is a dead end at exactly the moment somebody needs to act,
        and the row it would protect is the row they are trying to correct.
        """
        agent = await _agent(tenant)
        for reason in ("first, wrong", "second, right"):
            got = await client.post(
                f"/api/v1/agents/{agent}/kill",
                headers=_headers(tenant.organization_id),
                json={"reason": reason},
            )
            assert got.status_code == 200
            assert got.json()["kill_reason"] == reason

    async def test_it_writes_a_system_decision_about_the_agent(
        self, client: Any, tenant: Tenant
    ) -> None:
        """`actor_type = 'system'`, and `agent_id` names the **subject**.

        A row saying "agent X decided to kill agent X" would be a lie in the one table
        whose whole purpose is to be believed. `agent_id` is nullable and `actor_type` is
        required for exactly this shape, and it has been relied on since `0016`.
        """
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "the pilot is producing costs with no measured benefit"},
        )
        row = (
            await tenant.session.execute(
                text(
                    "SELECT actor_type, decision, rationale, agent_id FROM ai_decision_log"
                    " WHERE organization_id = CAST(:o AS varchar(40)) ORDER BY occurred_at DESC"
                    " LIMIT 1"
                ),
                {"o": tenant.organization_id},
            )
        ).one()
        assert row.actor_type == "system"
        assert row.decision == "killed"
        assert row.agent_id == agent
        assert "costs" in row.rationale
        await tenant.session.rollback()

    async def test_the_killed_agent_reads_back_as_killed(self, client: Any, tenant: Tenant) -> None:
        """The write and the read are the same fact, so they are tested together."""
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "stopped for a test"},
        )
        got = (
            await client.get(
                f"/api/v1/agents/{agent}/control", headers=_headers(tenant.organization_id)
            )
        ).json()
        assert got["kill_switch"] is True
        assert got["kill_reason"] == "stopped for a test"
        assert got["killed_at"] is not None


class TestReviving:
    async def test_it_clears_the_switch(self, client: Any, tenant: Tenant) -> None:
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "a temporary halt"},
        )
        got = await client.post(
            f"/api/v1/agents/{agent}/revive", headers=_headers(tenant.organization_id)
        )
        assert got.status_code == 200
        assert got.json()["kill_switch"] is False

    async def test_it_grants_l1_rather_than_restoring(self, client: Any, tenant: Tenant) -> None:
        """And this is the point of the asymmetry.

        The agent is given L2, killed, and revived. It comes back at **L1**. An agent
        that was stopped for a reason and is running again has not had its L2
        re-justified, and the only level that needs no justification is L1.
        """
        agent = await _agent(tenant)
        await tenant.session.execute(
            text(
                "UPDATE agents SET autonomy_ceiling = 'L2', granted_level = 'L2'"
                " WHERE organization_id = CAST(:o AS varchar(40))"
                " AND id = CAST(:a AS varchar(40))"
            ),
            {"o": tenant.organization_id, "a": agent},
        )
        await tenant.commit()
        got = await client.post(
            f"/api/v1/agents/{agent}/revive", headers=_headers(tenant.organization_id)
        )
        assert got.json()["granted_level"] == "L1"
        assert got.json()["restored_to"] == "L1"

    async def test_the_reason_survives_in_the_log_and_not_in_the_row(
        self, client: Any, tenant: Tenant
    ) -> None:
        """Cleared from the column, kept in the log.

        `kill_reason` has to be empty for the agent to be running at all
        (`ck_agents_kill_is_recorded`). Clearing it is what makes the state legal;
        *keeping the text there* would mean a live agent carrying a stale accusation in
        its registry row. The record belongs in the audit trail.
        """
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "the reason that must not be lost"},
        )
        await client.post(
            f"/api/v1/agents/{agent}/revive", headers=_headers(tenant.organization_id)
        )
        control = (
            await client.get(
                f"/api/v1/agents/{agent}/control", headers=_headers(tenant.organization_id)
            )
        ).json()
        assert control["kill_reason"] == ""
        assert control["killed_at"] is None

        logged = (
            await client.get(
                "/api/v1/ai-decisions",
                params={"agent_id": agent},
                headers=_headers(tenant.organization_id),
            )
        ).json()
        kills = [d for d in logged["items"] if d["decision"] == "killed"]
        assert kills, "the kill must remain on the record after the revive"
        assert "must not be lost" in kills[0]["rationale"]

    async def test_reviving_an_agent_that_was_never_killed_is_fine(
        self, client: Any, tenant: Tenant
    ) -> None:
        """Idempotent. An operator clicking twice must not get an error."""
        agent = await _agent(tenant)
        for _ in range(2):
            got = await client.post(
                f"/api/v1/agents/{agent}/revive",
                headers=_headers(tenant.organization_id),
            )
            assert got.status_code == 200


class TestTheDecisionLog:
    async def test_it_is_readable_and_starts_empty(self, client: Any, tenant: Tenant) -> None:
        """The whole point of the reader: a table nothing could read was not an audit
        trail, it was a write-only log."""
        got = (
            await client.get("/api/v1/ai-decisions", headers=_headers(tenant.organization_id))
        ).json()
        assert got["items"] == []
        assert got["total"] == 0

    async def test_every_optional_filter_may_be_omitted(self, client: Any, tenant: Tenant) -> None:
        """All four shapes, because omitting a parameter is the *simplest* request.

        A bare `:since IS NULL` is a separate bind from the `:since` inside its `CAST`,
        so asyncpg cannot type it and the statement fails with
        `AmbiguousParameterError: could not determine data type of parameter $2`. That is
        a 500 on the easiest call there is, and it is the second time in this repository
        -- so it gets a test that walks every combination rather than a comment.
        """
        base = {"headers": _headers(tenant.organization_id)}
        for params in (
            {},
            {"limit": 3},
            {"agent_id": "agt_nope"},
            {"decision": "agent.kill"},
            {"since": "2020-01-01T00:00:00+00:00"},
            {
                "agent_id": "a",
                "decision": "b",
                "since": "2020-01-01T00:00:00+00:00",
                "limit": 1,
                "offset": 0,
            },
        ):
            got = await client.get("/api/v1/ai-decisions", params=params, **base)
            assert got.status_code == 200, f"{params} -> {got.status_code} {got.text[:150]}"

    async def test_it_filters_by_agent(self, client: Any, tenant: Tenant) -> None:
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "filtered into view"},
        )
        got = (
            await client.get(
                "/api/v1/ai-decisions",
                params={"agent_id": agent},
                headers=_headers(tenant.organization_id),
            )
        ).json()
        assert got["total"] == 1
        assert got["items"][0]["agent_id"] == agent
        assert got["items"][0]["agent_name"], "the join supplies the readable name"

    async def test_it_carries_the_name_of_an_agent_it_joins(
        self, client: Any, tenant: Tenant
    ) -> None:
        agent = await _agent(tenant, name="Cost Analyst")
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "named agent"},
        )
        got = (
            await client.get("/api/v1/ai-decisions", headers=_headers(tenant.organization_id))
        ).json()
        assert got["items"][0]["agent_name"] == "Cost Analyst"

    async def test_a_decision_cannot_name_an_agent_that_does_not_exist(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The composite FK refuses it, and that is the guarantee.

        I wrote this test expecting the log to *outlive* a deleted subject -- a
        `LEFT JOIN` and a null name. The database answers differently and better:
        `fk_ai_decision_log_agent` has no `ON DELETE` clause, so an agent with a
        decision on the record **cannot be deleted at all**. The row keeps its subject
        because the subject cannot go anywhere.

        That is a stronger property than the one I wanted. An audit trail that goes
        quiet because somebody cleaned up a table has a hole exactly where you would go
        looking; this one makes the hole impossible to create.
        """
        # The violation is raised by `execute`, not by `commit`: SQLAlchemy flushes
        # pending work before the commit round-trip, so the constraint fires first.
        # Wrapping only the commit misses it entirely and the test fails with the
        # IntegrityError rather than the assertion.
        with pytest.raises(Exception) as excinfo:
            await tenant.session.execute(
                text(
                    "INSERT INTO ai_decision_log (id, organization_id, agent_id,"
                    " actor_type, decision, autonomy_level, rationale, policy_rule,"
                    " inputs_hash, outcome, occurred_at)"
                    " VALUES ('adl_orphan', :o, 'agt_never_existed', 'system',"
                    " 'killed', 'L1', 'a subject that is not there', 'kill_switch',"
                    " repeat('0', 64), 'killed', now())"
                ),
                {"o": tenant.organization_id},
            )
        assert "fk_ai_decision_log_agent" in str(excinfo.value), (
            f"the FK should refuse it: {excinfo.value}"
        )
        await tenant.session.rollback()

    async def test_an_agent_with_decisions_on_record_cannot_be_deleted(
        self, client: Any, tenant: Tenant
    ) -> None:
        """The other half, and the one that matters operationally.

        So a caller that tries to delete an agent and gets a constraint violation needs
        to know why: the agent has history, and history is not deletable. The refusal
        names the constraint, which is the only clue a caller gets.
        """
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "so this agent has history"},
        )
        with pytest.raises(Exception) as excinfo:
            await tenant.session.execute(
                text(
                    "DELETE FROM agents WHERE organization_id = CAST(:o AS varchar(40))"
                    " AND id = CAST(:a AS varchar(40))"
                ),
                {"o": tenant.organization_id, "a": agent},
            )
            await tenant.session.commit()
        assert "fk_ai_decision_log_agent" in str(excinfo.value)
        await tenant.session.rollback()

    async def test_a_system_decision_about_nothing_is_allowed(
        self, client: Any, tenant: Tenant
    ) -> None:
        """`agent_id` nullable, `actor_type` required, and the pair is the constraint.

        `ck_ai_decision_log_is_attributable` refuses a row that has neither a subject
        nor an actor. So a system decision with no subject -- a policy that fired
        without an agent -- is allowed, and one with neither is not.
        """
        await tenant.session.execute(
            text(
                "INSERT INTO ai_decision_log (id, organization_id, agent_id,"
                " actor_type, decision, autonomy_level, rationale, policy_rule,"
                " inputs_hash, outcome, occurred_at)"
                " VALUES (CAST(:log_id AS varchar(40)), :o, NULL, 'system',"
                " 'acted', 'L1',"
                " 'a policy fired with no agent behind it', 'budget',"
                " repeat('0', 64), 'blocked', now())"
            ),
            {
                "o": tenant.organization_id,
                # Unique per test: `ai_decision_log`'s key is `id` alone, so a
                # hardcoded one collides with an earlier test that shared a database.
                # A real ULID, not a hand-written one: `ai_decision_log`'s key is
                # `id` alone and rows outlive the tenant, so a fixed id collides with
                # an earlier test in the same session. The first version of this test
                # used `adl_nosubject` and failed on `pk_ai_decision_log`.
                "log_id": new_ulid(),
            },
        )
        await tenant.commit()
        got = (
            await client.get("/api/v1/ai-decisions", headers=_headers(tenant.organization_id))
        ).json()
        row = next(d for d in got["items"] if d["actor_type"] == "system" and d["agent_id"] is None)
        assert row["agent_id"] is None
        assert row["agent_name"] is None
        assert row["actor_type"] == "system"

    async def test_it_is_newest_first(self, client: Any, tenant: Tenant) -> None:
        agent = await _agent(tenant)
        for reason in ("older", "newer"):
            await client.post(
                f"/api/v1/agents/{agent}/kill",
                headers=_headers(tenant.organization_id),
                json={"reason": reason},
            )
        got = (
            await client.get("/api/v1/ai-decisions", headers=_headers(tenant.organization_id))
        ).json()
        stamps = [d["occurred_at"] for d in got["items"]]
        assert stamps == sorted(stamps, reverse=True), (
            "an audit log read oldest-first is a log you have to page through"
        )

    async def test_another_tenants_decisions_are_not_listed(
        self, client: Any, tenant: Tenant
    ) -> None:
        agent = await _agent(tenant)
        await client.post(
            f"/api/v1/agents/{agent}/kill",
            headers=_headers(tenant.organization_id),
            json={"reason": "mine"},
        )
        got = await client.get(
            "/api/v1/ai-decisions",
            headers=_headers("org_01m3d5hwxet3x61vjc1ffjyrzh"),
        )
        assert got.status_code == 200
        assert got.json()["items"] == []
