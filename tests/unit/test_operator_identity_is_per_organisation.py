"""The Approve button has to work in every tenant, not only the first.

`approvals.decided_by` is a **composite** foreign key --
`(organization_id, decided_by) REFERENCES users(organization_id, id)` -- while
`users` is keyed on `id` alone. A single id therefore belongs to exactly one
organisation, and a fixed operator id works in whichever tenant was created
first and in no other.

That is not a hypothetical. Measured, in order:

* with a fixed id, Approve returned 200 in the original tenant and **500 in
  every other one**, with the page showing a working button and no way to decide
  anything;
* `ON CONFLICT (id, organization_id)` did not match the primary key, so the
  insert raised, and a raised conflict **aborts the transaction** -- the
  approval decision died with "current transaction is aborted", naming neither
  the duplicate nor the approval;
* `WHERE NOT EXISTS` was worse still: `users` has RLS, so from a second tenant
  the check cannot see the row the first tenant owns, concludes none exists, and
  inserts into a primary key it could not see.

Three implementations, all of which read correctly, and a bug that survived
two of them. These tests assert the *property* -- the operator identity is
distinct per organisation, and it is the one the decision is recorded against
-- so the next person to reach for a constant finds a failing test instead of
a 500 nobody reproduces.

The suite already runs a fresh organisation per test, which is exactly the
condition that exposed this, so nothing extra is needed to keep it exposed.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.security.auth import NO_AUTH_ACTOR, no_auth_principal, operator_id_for


class TestTheOperatorIdentityIsPerOrganisation:
    def test_two_organisations_get_two_operators(self) -> None:
        """The property that the composite foreign key demands.

        One id, one organisation. Not "usually" -- the database enforces it, and
        the failure it produces is a 500 with no explanation.
        """
        a = operator_id_for("org_01m3h7j45b6cj3jnphq2ggjteq")
        b = operator_id_for("org_01m3qspcfz4dns6eqqxten3rc0")
        assert a != b, (
            "one operator id for two organisations means the second tenant cannot "
            "record a decision: the id is taken and the row cannot be created"
        )

    def test_the_identity_still_says_authentication_was_off(self) -> None:
        """Distinct per tenant, and still attributable.

        Solving the foreign key by inventing a plausible-looking person id would
        make an approval row claim a person decided it who does not exist. The
        marker has to survive the fix, or the audit trail is worse than the bug.
        """
        for org in ("org_01m3h7j45b6cj3jnphq2ggjteq", "org_01m3qspcfz4dns6eqqxten3rc0"):
            assert NO_AUTH_ACTOR in operator_id_for(org), (
                "the operator id must still name the no-auth mode; a row that says "
                "somebody decided without saying authentication was off is the one "
                "outcome this platform exists to prevent"
            )

    def test_the_identity_fits_the_column(self) -> None:
        """`users.id` is `varchar(40)`, and an over-long id is silently truncated.

        A truncated id then matches nothing, and the failure is a foreign-key
        error on a value nobody recognises as wrong.
        """
        for org in ("org_01m3h7j45b6cj3jnphq2ggjteq", "org_01m3qspcfz4dns6eqqxten3rc0"):
            assert len(operator_id_for(org)) <= 40

    def test_the_same_organisation_always_gets_the_same_operator(self) -> None:
        """Stable, so two requests in one session do not disagree.

        An id that changed per call would make every provisioning an insert and
        every decision land on a principal that did not exist when it was
        recorded.
        """
        org = "org_01m3h7j45b6cj3jnphq2ggjteq"
        assert operator_id_for(org) == operator_id_for(org)


class TestThePrincipalTheDecisionsRecord:
    def test_the_principal_is_the_operator_of_its_own_organisation(self) -> None:
        """What a click actually records is the tenant-scoped id.

        Asserted through the public path rather than by reading the constant, so
        a change to how the principal is built cannot leave this passing.
        """
        org = "org_01m3qspcfz4dns6eqqxten3rc0"
        principal = no_auth_principal(org)
        assert str(principal.actor.id) == operator_id_for(org)
        assert str(principal.actor.organization_id) == org

    def test_a_principal_is_still_a_privileged_human(self) -> None:
        """The gate in `decide` requires both, and both are load-bearing.

        `kind` being `HUMAN` is what lets a person act at all in this mode; it
        was `SERVICE` once and every approval was refused for the person standing
        at the browser. `is_privileged_human` is the second gate, one level down.
        """
        from ai_orchestrator.domain.enums import ActorType

        principal = no_auth_principal("org_01m3qspcfz4dns6eqqxten3rc0")
        assert principal.actor.kind is ActorType.HUMAN
        assert principal.actor.is_privileged_human

    def test_tenancy_is_still_required(self) -> None:
        """Turning off authentication does not turn off row-level security.

        Without the tenant every query runs unbound, RLS returns nothing, and an
        empty platform is indistinguishable from a broken one.
        """
        from ai_orchestrator.domain.errors import AuthorizationError

        with pytest.raises(AuthorizationError):
            no_auth_principal("")
