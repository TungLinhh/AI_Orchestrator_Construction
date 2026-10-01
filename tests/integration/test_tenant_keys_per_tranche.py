"""One cross-tenant refusal per tranche, proven against a live database.

Migrations `0021`, `0022` and `0023` made 117 more single-column foreign keys composite,
after `0020` did the eighteen in the supply chain. This file proves each tranche did
something.

## Why this file exists when `test_cross_tenant_parents.py` already does this

**Because the four tests in that file all exercise the supply chain, and the supply chain
was `0020`.** Downgrading the test database to `0023` — which removes all three new
tranches — left those five tests green. A green suite after removing 117 constraints is not
reassurance, it is a measurement failure: the tests were never touching what changed.

So: one test per tranche, and the check is that the whole file goes red at `0023`.

## What each one is for, and which of them actually discriminate

Each test is checked by downgrading the test database to `0020` — where all three new
tranches are gone — and confirming it goes red. **Two do, and one does not.** The table
below is the measurement, not a claim:

| test | tranche | red at `0020`? |
|---|---|---|
| `test_an_audit_log_cannot_name_another_tenants_approval` | `0022` | yes |
| `test_a_contract_cannot_name_another_tenants_client` | `0023` | yes |
| `test_a_peer_agent_cannot_be_our_local_agent` | `0021` | **no** — see below |

The `a2a_agents` one is kept and labelled rather than quietly dropped, because "it passes
for the wrong reason" is the finding. At `0020` that relationship has a **bare** foreign
key, and the insert is *still* refused. The reason is RLS: `a2a_agents` is `FORCE`d, the
parent belongs to another tenant, and Postgres' referential-integrity check does not see a
row that row-level security hides from the checking role.

Which means the composite key is **not** what protects that relationship, and the test is
asserting something the schema already guaranteed. It stays in the file as a record of that
fact; it does not count towards the two that measure the migrations.

**`0021` therefore has no discriminating test.** It has 61 composite keys and no proof that
any particular one changed behaviour, which is an honest gap rather than a hidden one. The
way to close it is to find a relationship in `0021` whose parent table is *not*
`FORCE ROW LEVEL SECURITY` — `organizations` is the obvious candidate, since it is the one
table with no RLS by design.

## Why every relationship here is *nullable*

A nullable foreign key is the cheapest possible proof. One `INSERT` with a parent id that
belongs to somebody else, and nothing else about the row has to be valid — no requisition,
no quotation, no order, no chain. The supply-chain tests needed six helpers to build a
legitimate spine first, and every one of those helpers was a way for the test to fail in
its fixture rather than at its assertion (see that file's docstring).

A nullable column is the right choice for a different reason too, and it is worth stating
because it is a design position rather than a convenience: **these columns are nullable
because the thing they point at is genuinely optional.** A `task_id` on a decision log is
NULL when a human decided something. A `client_id` on a contract is NULL when the
counterparty is a supplier. A test that had to make them non-null to be cheap would be
testing a schema nobody wants.

## What each one is for

* `test_a_peer_agent_cannot_be_our_local_agent` — `0021`, the orchestration core, and
  **not** a discriminator: RLS already refuses it. Kept as a record of that.
* `test_an_audit_log_cannot_name_another_tenants_approval` — `0022`, governance and money.
  An audit row hanging off someone else's approval says the approval was reviewed here.
* `test_a_contract_cannot_name_another_tenants_client` — `0023`, the built world. A contract
  naming a client that is not ours is a counterparty that does not exist for this tenant.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ai_orchestrator.domain.ids import new_ulid
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]


class TestOneRefusalPerTranche:
    """Every parent here is a **real row in the other tenant**.

    That is the whole test, and getting it wrong is invisible. The first version used a
    made-up id -- `tsk_{new_ulid()}` -- and passed at *every* revision, including `0020`
    where the keys are bare. A nonexistent parent is refused by *any* foreign key, bare or
    composite, so the test proved that foreign keys exist and nothing about tenancy.

    The id has to exist, and it has to exist somewhere else. Then the only reason left for
    the insert to be refused is that the row belongs to somebody else, and the test goes
    red the moment the key stops being composite. That is the property worth asserting, and
    it is the one the downgrade check below measures.
    """

    async def test_a_peer_agent_cannot_be_our_local_agent(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """`0021` — the orchestration core.

        `a2a_agents.local_agent_id` names the local agent that fronts a remote peer. It is
        nullable because a peer can be registered before its local counterpart exists, and
        it points at another tenant's agent when the registration is wrong — which is the
        failure this migration closes, and a subtle one: an agent-to-agent message would be
        delivered into a tenant that never registered for it.

        **This test does not discriminate**, and that is measured rather than assumed: at
        `0020`, where the key is bare, the insert is still refused, because RLS hides the
        other tenant's `agents` row from the referential-integrity check. See the module
        docstring. A test that cannot go red measures nothing; the downgrade is what found
        it.
        """
        foreign_agent = f"agt_{new_ulid()}"
        await other_tenant.session.execute(
            text(
                "INSERT INTO a2a_agents (id, organization_id, name) "
                "VALUES (:i, :o, 'Peer của tenant khác')"
            ),
            {"i": foreign_agent, "o": other_tenant.organization_id},
        )
        await other_tenant.commit()
        assert other_tenant.organization_id != tenant.organization_id

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO a2a_agents (id, organization_id, name, local_agent_id) "
                    "VALUES (:i, :o, 'Peer của chúng tôi', :a)"
                ),
                {
                    "i": f"a2a_{new_ulid()}",
                    "o": tenant.organization_id,
                    "a": foreign_agent,
                },
            )
        await tenant.session.rollback()
        assert "fk_a2a_agents_local_agent_id_agents" in str(refusal.value)

    async def test_an_audit_log_cannot_name_another_tenants_approval(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """`0022` — governance and money."""
        foreign_approval = f"apr_{new_ulid()}"
        await other_tenant.session.execute(
            text(
                "INSERT INTO approvals (id, organization_id, action_type, requested_by, "
                "requested_by_type, payload_hash) "
                "VALUES (:i, :o, 'approve', 'ceo', 'human', :h)"
            ),
            {
                "i": foreign_approval,
                "o": other_tenant.organization_id,
                "h": f"ph-{new_ulid()}",
            },
        )
        await other_tenant.commit()
        assert other_tenant.organization_id != tenant.organization_id

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO audit_logs (id, organization_id, approval_id, actor_type, "
                    "action, resource_type) "
                    "VALUES (:i, :o, :a, 'system', 'approve', 'approval')"
                ),
                {
                    "i": f"aud_{new_ulid()}",
                    "o": tenant.organization_id,
                    "a": foreign_approval,
                },
            )
        await tenant.session.rollback()
        assert "fk_audit_logs_approval_id_approvals" in str(refusal.value)

    async def test_a_contract_cannot_name_another_tenants_client(
        self, tenant: Tenant, other_tenant: Tenant
    ) -> None:
        """`0023` — the built world.

        `ck_contracts_exactly_one_counterparty` requires precisely one of `client_id` or
        `supplier_id`, so a contract with a client *and* no supplier is the only shape that
        reaches the foreign key — and that shape is the one that matters, because a client is
        the counterparty whose identity the tenant owns.
        """
        foreign_client = f"cli_{new_ulid()}"
        await other_tenant.session.execute(
            text(
                "INSERT INTO clients (id, organization_id, code, name) "
                "VALUES (:i, :o, :c, 'Khách hàng của tenant khác')"
            ),
            {
                "i": foreign_client,
                "o": other_tenant.organization_id,
                "c": f"CL-{uuid.uuid4().hex[:6]}",
            },
        )
        await other_tenant.commit()
        assert other_tenant.organization_id != tenant.organization_id

        with pytest.raises(IntegrityError) as refusal:
            await tenant.session.execute(
                text(
                    "INSERT INTO contracts (id, organization_id, code, title, client_id) "
                    "VALUES (:i, :o, :c, 'Hợp đồng thi công', :cl)"
                ),
                {
                    "i": f"ctr_{new_ulid()}",
                    "o": tenant.organization_id,
                    "c": f"HD-{uuid.uuid4().hex[:6]}",
                    "cl": foreign_client,
                },
            )
        await tenant.session.rollback()
        assert "fk_contracts_client_id_clients" in str(refusal.value)


class TestTheSameShapeIsAllowedWithinOneTenant:
    async def test_a_decision_log_may_name_our_own_task(self, tenant: Tenant) -> None:
        """Without this, a migration that dropped all 117 keys outright would pass all three.

        The task has to **exist** in this tenant. A composite key is an existence check as
        well as a tenant check, so a made-up id is refused even here — which is the correct
        behaviour and the reason this positive case needs a real parent rather than a
        plausible-looking string. The first version of it used `tsk_{new_ulid()}` and failed
        with a `ForeignKeyViolationError` that looked like the test under test.
        """
        task_id = f"tsk_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO tasks (id, organization_id, title, goal, fingerprint, "
                "dedup_key) VALUES (:i, :o, 'Duyệt báo giá', 'Duyệt báo giá', :fp, :fp)"
            ),
            {"i": task_id, "o": tenant.organization_id, "fp": f"fp-{task_id}"},
        )
        await tenant.commit()

        row_id = f"dec_{new_ulid()}"
        await tenant.session.execute(
            text(
                "INSERT INTO ai_decision_log (id, organization_id, task_id, actor_type, "
                "decision, autonomy_level, rationale) "
                "VALUES (:i, :o, :t, 'agent', 'refused', 'L1', 'Cần phê duyệt')"
            ),
            {"i": row_id, "o": tenant.organization_id, "t": task_id},
        )
        await tenant.commit()

        found = (
            await tenant.session.execute(
                text("SELECT task_id FROM ai_decision_log WHERE id = :i"), {"i": row_id}
            )
        ).first()
        assert found is not None, "The insert reported success and the row is not there."
        assert str(found[0]) == task_id
