"""Migration `0016`'s central invariants, as refusals.

The schema work in this tranche is only worth anything if the constraints it claims to
enforce actually refuse. A `CHECK` that has never rejected a row is indistinguishable
from a comment, and the two central claims of `0016` are both of that kind:

* **At most one `active` version per procedure.** Two agents running two versions of
  the same SOP is the failure this exists to make impossible. It is a *partial* unique
  index, which is a thing application code can route around by accident and a thing
  the database cannot.
* **An agent-proposed version must name the approval that authorised it.** This is the
  link that closes the learning loop. A learned change that cannot cite its approval is
  a change nobody agreed to, and without the constraint it is a two-line omission.

Both are tested by *trying the forbidden thing* and asserting the database says no.
That is the only kind of assertion for a constraint: a test that inserts a legal row
and reads it back proves the table is writable, not that the rule is enforced.
"""

from __future__ import annotations

from itertools import count

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_orchestrator.domain.ids import new_ulid
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]


#: A monotonic counter for names that must be unique within one transaction. A ULID's
#: first eight characters are its timestamp, so `new_ulid()[:8]` is a constant for the
#: length of a millisecond and cannot make a name unique.
_SERIAL = count()


def _serial() -> int:
    return next(_SERIAL)


async def _procedure(tenant: Tenant, code: str = "SOP-TEST") -> str:
    procedure_id = f"prc_{new_ulid()}"
    await tenant.session.execute(
        text(
            "INSERT INTO procedures (id, organization_id, code, name) "
            "VALUES (:i, :o, :c, 'Test procedure')"
        ),
        {"i": procedure_id, "o": tenant.organization_id, "c": code},
    )
    return procedure_id


async def _version(
    tenant: Tenant,
    procedure_id: str,
    version_no: int,
    *,
    status: str = "shadow",
    source: str = "human",
    proposal_id: str | None = None,
    approval_id: str | None = None,
) -> str:
    version_id = f"prv_{new_ulid()}"
    await tenant.session.execute(
        text(
            "INSERT INTO procedure_versions (id, organization_id, procedure_id, "
            "version_no, status, source, source_actor, proposal_id, approval_id) "
            "VALUES (:i, :o, :p, :n, CAST(:s AS varchar(16)), "
            "CAST(:src AS varchar(128)), 'test', :prop, :appr)"
        ),
        {
            "i": version_id,
            "o": tenant.organization_id,
            "p": procedure_id,
            "n": version_no,
            "s": status,
            "src": source,
            "prop": proposal_id,
            "appr": approval_id,
        },
    )
    return version_id


async def refused(tenant: Tenant, coro, *, constraint: str) -> None:
    """Assert that a write is refused **and that the transaction survives it**.

    The savepoint is not optional. A constraint violation aborts the whole
    transaction, and Postgres then refuses every subsequent command with
    `current transaction is aborted` — so a test that provokes a violation and then
    reads anything back sees the *consequence* of the violation rather than the state
    it meant to check. This is the same discipline `progress_operations.write_reading`
    uses to write a whole report without leaving a half-written one, and for the same
    reason: without a savepoint, the test after the failure is testing the wrong thing.

    `constraint` is asserted in the message so that a failure says *which* rule fired
    rather than only that something did.

    The savepoint is issued by hand rather than through `session.begin_nested()`.
    SQLAlchemy's context manager recovers a failed savepoint with
    `ROLLBACK TO` followed by `RELEASE`, and with asyncpg the `RELEASE` is itself a
    statement in the aborted transaction, so it fails with
    `InFailedSQLTransactionError` and the test sees an error about the savepoint
    instead of the constraint. Rolling back to the savepoint is enough on its own and
    leaves the transaction usable, which is the state the rest of the test needs.
    """
    await tenant.session.execute(text("SAVEPOINT constraint_probe"))
    try:
        await coro
    except IntegrityError as caught:
        assert constraint in str(caught), f"expected {constraint} to fire, got: {caught}"
        await tenant.session.execute(text("ROLLBACK TO SAVEPOINT constraint_probe"))
        return
    raise AssertionError(f"expected {constraint} to refuse the write, and it was accepted")


async def _status(tenant: Tenant, version_id: str) -> str:
    return (
        await tenant.session.execute(
            text("SELECT status FROM procedure_versions WHERE id = :i"),
            {"i": version_id},
        )
    ).scalar_one()


class TestOneActiveVersionPerProcedure:
    async def test_a_second_active_version_is_refused(self, tenant: Tenant) -> None:
        """The invariant, stated as a refusal rather than as a design note.

        A partial unique index is the only enforcement that survives a second writer,
        a concurrent promotion and a migration run by hand.
        """
        procedure_id = await _procedure(tenant)
        await _version(tenant, procedure_id, 1, status="active")

        # The refusal must name the constraint, so a caller can tell a duplicate
        # active version from any other uniqueness failure.
        await refused(
            tenant,
            _version(tenant, procedure_id, 2, status="active"),
            constraint="uq_procedure_versions_one_active",
        )

    async def test_shadow_versions_do_not_compete(self, tenant: Tenant) -> None:
        """The other half: the constraint must not block the queue.

        A `shadow` version that could only exist one at a time would make the whole
        learning loop serial — you could not evaluate two candidate changes to the same
        SOP at once, which is the situation the queue exists for.
        """
        procedure_id = await _procedure(tenant)
        await _version(tenant, procedure_id, 1, status="shadow")
        await _version(tenant, procedure_id, 2, status="shadow")
        await _version(tenant, procedure_id, 3, status="shadow")
        await _version(tenant, procedure_id, 4, status="rejected")
        assert await _status(tenant, await _version(tenant, procedure_id, 5)) == "shadow"

    async def test_a_second_procedure_may_have_its_own_active_version(self, tenant: Tenant) -> None:
        """The index is scoped by procedure, not global.

        Scoping it wrongly would allow exactly one live SOP in the entire tenant.
        """
        first = await _procedure(tenant, "SOP-ONE")
        second = await _procedure(tenant, "SOP-TWO")
        await _version(tenant, first, 1, status="active")
        await _version(tenant, second, 1, status="active")

    async def test_superseding_then_activating_is_the_way_through(self, tenant: Tenant) -> None:
        """Replacing a version is two writes, and the order matters.

        Promoting the new one first is refused, because at that instant two versions
        would be live. The existing one has to be marked `superseded` first — which is
        the ordering a promotion service must get right, and the reason it is worth
        proving here rather than in a comment on the writer.
        """
        procedure_id = await _procedure(tenant)
        first = await _version(tenant, procedure_id, 1, status="active")

        await refused(
            tenant,
            _version(tenant, procedure_id, 2, status="active"),
            constraint="uq_procedure_versions_one_active",
        )
        await tenant.session.execute(
            text("UPDATE procedure_versions SET status = 'superseded' WHERE id = :i"),
            {"i": first},
        )
        second = await _version(tenant, procedure_id, 2, status="active")
        assert await _status(tenant, second) == "active"
        assert await _status(tenant, first) == "superseded"


class TestAnAgentProposalMustNameItsApproval:
    async def test_an_agent_proposal_without_an_approval_is_refused(self, tenant: Tenant) -> None:
        """The link that closes the learning loop.

        Without it, `source = 'agent_proposal'` is a label anybody can write, and the
        loop reopens: a change lands in `shadow` with nothing behind it.
        """
        procedure_id = await _procedure(tenant)
        await refused(
            tenant,
            _version(
                tenant,
                procedure_id,
                1,
                source="agent_proposal",
                proposal_id=f"prp_{new_ulid()}",
                approval_id=None,
            ),
            constraint="ck_procedure_versions_agent_needs_approval",
        )

    async def test_an_agent_proposal_with_an_approval_is_written(self, tenant: Tenant) -> None:
        """And the positive case, so the constraint is a rule and not a prohibition.

        A test that only proves the refusal cannot tell a working constraint from a
        column that can never be set.
        """
        procedure_id = await _procedure(tenant)
        approval_id = await _approval(tenant)
        version_id = await _version(
            tenant,
            procedure_id,
            1,
            source="agent_proposal",
            proposal_id=f"prp_{new_ulid()}",
            approval_id=approval_id,
        )
        assert await _status(tenant, version_id) == "shadow"

    async def test_a_human_written_version_needs_no_approval(self, tenant: Tenant) -> None:
        """The constraint is scoped to agent proposals, not to all versions.

        The seeded SOPs are `human` source and none of them has an approval, so a
        constraint that applied to everything would refuse the entire existing corpus
        of procedures.
        """
        procedure_id = await _procedure(tenant)
        version_id = await _version(tenant, procedure_id, 1, source="human")
        assert await _status(tenant, version_id) == "shadow"

    async def test_a_proposal_id_without_the_agent_provenance_is_refused(
        self, tenant: Tenant
    ) -> None:
        """The other direction of the same rule.

        `(source = 'agent_proposal') = (proposal_id IS NOT NULL)` is an equivalence, so
        a `human` row carrying a `proposal_id` is also refused — otherwise a row can
        claim to be human while pointing at an agent's proposal.
        """
        procedure_id = await _procedure(tenant)
        await refused(
            tenant,
            _version(tenant, procedure_id, 1, source="human", proposal_id=f"prp_{new_ulid()}"),
            constraint="ck_procedure_versions_agent_needs_proposal",
        )


async def _approval(tenant: Tenant) -> str:
    """A real `approvals` row, because the FK is composite and means it.

    The five NOT NULL columns with no default — `id`, `organization_id`,
    `action_type`, `payload_hash`, `requested_by` — were read from
    `information_schema` rather than from the ORM, because this fixture was guessed
    column by column and got it wrong three times: a `classification` column that does
    not exist, then a missing `payload_hash`, then a missing `requested_by`. Three
    attempts at the same table is enough to stop guessing and read the schema.
    """
    from sqlalchemy import text as _t

    approval_id = f"apr_{new_ulid()}"
    result = await tenant.session.execute(
        _t(
            "INSERT INTO approvals (id, organization_id, action_type, action_payload, "
            "payload_hash, requested_by, status) "
            "VALUES (:i, :o, 'procedure.change', CAST(:p AS jsonb), :h, :by, 'pending') "
            "RETURNING id"
        ),
        {
            "i": approval_id,
            "o": tenant.organization_id,
            "p": '{"change": "test"}',
            # `payload_hash` is NOT NULL and the column exists precisely so an approval
            # can be invalidated when the thing it approved changes underneath it.
            # A row with no hash is an approval nothing can check.
            "h": "0" * 64,
            "by": "system:test",
        },
    )
    return result.scalar_one()


class TestTheAutonomyPairCannotBeInverted:
    async def test_granting_more_than_the_ceiling_is_refused(self, tenant: Tenant) -> None:
        """The single most consequential constraint in this migration.

        An agent permitted L1 and granted L3 is an agent acting above what the dossier
        allows, and it is the failure the ceiling exists to make impossible.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "UPDATE agents SET granted_level = 'L3', autonomy_ceiling = 'L1' WHERE id = :i"
                ),
                {"i": agent_id},
            ),
            constraint="ck_agents_granted_within_ceiling",
        )

    async def test_granting_up_to_the_ceiling_is_allowed(self, tenant: Tenant) -> None:
        procedure_free = await _agent(tenant)
        await tenant.session.execute(
            text("UPDATE agents SET granted_level = 'L1' WHERE id = :i"),
            {"i": procedure_free},
        )
        level = (
            await tenant.session.execute(
                text("SELECT granted_level FROM agents WHERE id = :i"),
                {"i": procedure_free},
            )
        ).scalar_one()
        assert level == "L1"


class TestAKillSwitchIsARecordedState:
    async def test_a_kill_with_no_reason_is_refused(self, tenant: Tenant) -> None:
        """A switch thrown silently is indistinguishable from a bug.

        Somebody has to be able to answer "why is this agent off" months later, and
        that answer has to exist at the moment the switch is thrown.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text("UPDATE agents SET kill_switch = true WHERE id = :i"),
                {"i": agent_id},
            ),
            constraint="ck_agents_kill_is_recorded",
        )

    async def test_a_kill_with_a_reason_and_a_moment_is_written(self, tenant: Tenant) -> None:
        agent_id = await _agent(tenant)
        await tenant.session.execute(
            text(
                "UPDATE agents SET kill_switch = true, kill_reason = 'producing "
                "refusals with no rationale', killed_at = now() WHERE id = :i"
            ),
            {"i": agent_id},
        )
        row = (
            await tenant.session.execute(
                text("SELECT kill_reason, killed_at IS NOT NULL FROM agents WHERE id = :i"),
                {"i": agent_id},
            )
        ).one()
        assert row[0].startswith("producing refusals")
        assert row[1] is True

    async def test_a_killed_at_without_the_switch_is_refused(self, tenant: Tenant) -> None:
        """The other direction.

        `kill_switch = false` with a stale `killed_at` reads as "was killed, since
        re-enabled" and cannot be told from "never killed" — two states with different
        reasons to exist and different things to audit.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text("UPDATE agents SET killed_at = now() WHERE id = :i"),
                {"i": agent_id},
            ),
            constraint="ck_agents_killed_at_needs_kill",
        )


class TestADecisionLogRowThatMeansNothingIsRefused:
    async def test_a_refusal_with_no_rationale_is_refused(self, tenant: Tenant) -> None:
        """The row the dossier's segregation-of-duties principle is about.

        A refusal with no reason is indistinguishable from a failure to decide, and
        those two need different investigations.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO ai_decision_log (id, organization_id, agent_id, "
                    "decision, autonomy_level) VALUES (:i, :o, :a, 'refused', 'L1')"
                ),
                {"i": f"adl_{new_ulid()}", "o": tenant.organization_id, "a": agent_id},
            ),
            # Renamed by `0019` from `refusal_is_explained`, because it no longer
            # covers only refusals: `killed` and `revived` joined it, and a constraint
            # whose name says "refusal" while refusing a kill is a name that lies. The
            # three cases are all tested below, which the old name made impossible to
            # express.
            constraint="ck_ai_decision_log_explained_is_explained",
        )

    async def test_a_kill_with_no_rationale_is_refused(self, tenant: Tenant) -> None:
        """An unexplained kill and a kill whose reason was lost are the same row.

        Only one of them is a governance failure rather than a lost record, and the
        database cannot tell them apart — so it refuses both. This is the argument that
        made `0019` extend the constraint rather than add a second one beside it.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO ai_decision_log (id, organization_id, agent_id, "
                    "decision, autonomy_level) VALUES (:i, :o, :a, 'killed', 'L1')"
                ),
                {"i": f"adl_{new_ulid()}", "o": tenant.organization_id, "a": agent_id},
            ),
            constraint="ck_ai_decision_log_explained_is_explained",
        )

    async def test_a_revive_with_no_rationale_is_refused(self, tenant: Tenant) -> None:
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO ai_decision_log (id, organization_id, agent_id, "
                    "decision, autonomy_level) VALUES (:i, :o, :a, 'revived', 'L1')"
                ),
                {"i": f"adl_{new_ulid()}", "o": tenant.organization_id, "a": agent_id},
            ),
            constraint="ck_ai_decision_log_explained_is_explained",
        )

    async def test_a_kill_with_a_rationale_is_written(self, tenant: Tenant) -> None:
        agent_id = await _agent(tenant)
        await tenant.session.execute(
            text(
                "INSERT INTO ai_decision_log (id, organization_id, agent_id, decision, "
                "autonomy_level, rationale) VALUES (:i, :o, :a, 'killed', 'L1', :why)"
            ),
            {
                "i": f"adl_{new_ulid()}",
                "o": tenant.organization_id,
                "a": agent_id,
                "why": "emitting costs with no approved procedure",
            },
        )
        await tenant.commit()

    async def test_a_decision_outside_the_vocabulary_is_refused(self, tenant: Tenant) -> None:
        """The vocabulary is closed, which is the point of a closed vocabulary.

        Phase 4c's first attempt wrote `decision = 'agent.kill'` and this refused it.
        The refusal was correct: `decision` is the *agent's own* decision vocabulary, and
        a kill is a decision about an agent taken by the platform. A constraint that
        refuses an unknown decision means one can never be written and never noticed.
        """
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO ai_decision_log (id, organization_id, agent_id, "
                    "decision, autonomy_level, rationale)"
                    " VALUES (:i, :o, :a, 'agent.kill', 'L1', 'a plausible guess')"
                ),
                {"i": f"adl_{new_ulid()}", "o": tenant.organization_id, "a": agent_id},
            ),
            constraint="ck_ai_decision_log_decision_known",
        )

    async def test_a_refusal_with_a_rationale_is_written(self, tenant: Tenant) -> None:
        agent_id = await _agent(tenant)
        log_id = f"adl_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO ai_decision_log (id, organization_id, agent_id, decision, "
                "autonomy_level, rationale) "
                "VALUES (:i, :o, :a, 'refused', 'L1', 'commercial decision, escalated')"
            ),
            {"i": log_id, "o": tenant.organization_id, "a": agent_id},
        )
        got = (
            await tenant.session.execute(
                text("SELECT decision FROM ai_decision_log WHERE id = :i"),
                {"i": log_id},
            )
        ).scalar_one()
        assert got == "refused"

    async def test_a_shadow_run_attached_to_nothing_is_refused(self, tenant: Tenant) -> None:
        """A run with neither an agent nor a version is not evidence of anything."""
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO agent_shadow_runs (id, organization_id, agreed, "
                    "would_have_decided, actually_decided) "
                    "VALUES (:i, :o, true, 'a', 'a')"
                ),
                {"i": f"asr_{new_ulid()}", "o": tenant.organization_id},
            ),
            constraint="ck_shadow_runs_attached_to_something",
        )

    async def test_a_disagreement_with_no_explanation_is_refused(self, tenant: Tenant) -> None:
        """Promotion is a rate, and a rate is useless without reasons."""
        agent_id = await _agent(tenant)
        await refused(
            tenant,
            tenant.session.execute(
                text(
                    "INSERT INTO agent_shadow_runs (id, organization_id, agent_id, "
                    "agreed, would_have_decided, actually_decided) "
                    "VALUES (:i, :o, :a, false, 'approve', 'escalate')"
                ),
                {"i": f"asr_{new_ulid()}", "o": tenant.organization_id, "a": agent_id},
            ),
            constraint="ck_shadow_runs_disagreement_is_explained",
        )


class TestARefusedWriteLeavesNothingBehind:
    async def test_a_refused_version_is_not_partially_written(self, tenant: Tenant) -> None:
        """A constraint that fires after the row exists is a constraint with a mess.

        Postgres rolls the statement back, so the count has to be checked rather than
        assumed — a service that reads back "did it write?" gets the right answer for
        the wrong reason, and the wrong reason is what breaks when the constraint
        moves.
        """
        procedure_id = await _procedure(tenant)
        await _version(tenant, procedure_id, 1, source="human")
        await refused(
            tenant,
            _version(tenant, procedure_id, 1, source="human"),
            constraint="uq_procedure_versions_org_proc_no",
        )
        count = (
            await tenant.session.execute(
                text("SELECT count(*) FROM procedure_versions WHERE procedure_id = :p"),
                {"p": procedure_id},
            )
        ).scalar_one()
        assert count == 1


async def _agent(tenant: Tenant) -> str:
    """A minimal `agents` row, which needs a definition, a role and an org unit."""
    from sqlalchemy import text as _t

    result = await tenant.session.execute(
        _t(
            "INSERT INTO agents (id, organization_id, name, definition_id, role_id, "
            "org_unit_id) VALUES (:i, :o, 'probe', :d, :r, :u) RETURNING id"
        ),
        {
            "i": f"agt_{new_ulid()}",
            "o": tenant.organization_id,
            "d": await _seed_parent(tenant, "agent_definitions", "definition"),
            "r": await _seed_parent(tenant, "roles", "role"),
            "u": await _seed_parent(tenant, "organizational_units", "unit"),
        },
    )
    return result.scalar_one()


async def _seed_parent(tenant: Tenant, table: str, prefix: str) -> str:
    """The minimum row each of `agents`' three required parents needs.

    The first version of this guessed the columns from the ORM models and was wrong
    twice: `agent_definitions.role_id` is NOT NULL (so a definition needs a role, and
    the role needs a definition — resolved here by making the role first), and
    `organizational_units.slug` is NOT NULL as well as `name`.

    Third version of this fixture, and all three failures were the same mistake: a row
    that is legal on its own and illegal beside another one. `roles.name` is UNIQUE per
    organization, so a test that builds two agents in one tenant needs two
    differently-named roles.

    The suffix was `new_ulid()[:8]`, which is **the same for every call inside one
    millisecond** — a ULID is a timestamp followed by randomness, and the first eight
    characters are entirely timestamp. Two rows created microseconds apart collided
    exactly as two `hash()`-derived ids did in F107. The counter below is what makes
    the name unique; the ULID alone does not, at that slice.

    Columns read from `information_schema` rather than from the models, because the
    models declare what a row *may* contain and this needs what one *must*.
    """
    from sqlalchemy import text as _t

    row_id = f"{prefix[:3]}_{new_ulid()}"
    statements = {
        "roles": ("INSERT INTO roles (id, organization_id, name) VALUES (:i, :o, :n) RETURNING id"),
        "agent_definitions": (
            "INSERT INTO agent_definitions (id, organization_id, name, role_id) "
            "VALUES (:i, :o, :n, :role) RETURNING id"
        ),
        "organizational_units": (
            "INSERT INTO organizational_units (id, organization_id, name, slug) "
            "VALUES (:i, :o, :n, :slug) RETURNING id"
        ),
    }
    params: dict[str, object] = {
        "i": row_id,
        "o": tenant.organization_id,
        "n": f"probe {prefix} {_serial()}",
        "role": None,
        "slug": f"probe-{prefix[:8]}-{_serial()}",
    }
    if table == "agent_definitions":
        params["role"] = await _seed_parent(tenant, "roles", "role")
    result = await tenant.session.execute(_t(statements[table]), params)
    return result.scalar_one()
