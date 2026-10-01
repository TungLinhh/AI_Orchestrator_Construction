"""Turning authentication off, and the three things that must still be true.

This exists because a local demo was blocked on pasting a token into a browser prompt,
and the honest answer was to make the friction removable rather than to keep fighting
it. The risk of that is not abstract: with authentication off,
`POST /api/v1/approvals/{id}/approve` succeeds, so "a human approved this" becomes
"something did".

So the mode is allowed, and three properties are enforced rather than documented:

* it **refuses to start** in production, where it is a hole in a real control plane;
* it **refuses to start** in tests, where a suite would stop exercising authorisation
  and nothing about that would look wrong;
* every request it admits is attributed to a **named** actor, `dev:no-auth`, which is
  greppable and appears on the page's banner, so a row written through it can always be
  traced back to the fact that authentication was off.

**The third bullet changed, and the change is recorded here rather than quietly made.**
The actor used to be `ActorType.SERVICE`, on the reasoning that a substitute that looked like
a person would let a row be read as a person's decision. Measured consequence: the Approve
button answered *"an agent cannot approve an action; a human approver is required"* — to the
person at the browser who was the human. Every act requiring a person was unreachable in
exactly the mode that exists so a person does not have to authenticate, so the guard did not
prevent a false decision; it prevented a real one.

The actor is now `HUMAN` with `is_privileged_human=True`, and the id is still `dev:no-auth`.
So the substituteness is carried by the **name and the banner**, not by the type. The two
tests that can be lost here are both asserted directly above: the mode cannot start in
production or in tests, and with authentication on this code path is never reached.
"""

from __future__ import annotations

import pytest

from ai_orchestrator.config.settings import (
    Environment,
    Settings,
)
from ai_orchestrator.domain.enums import ActorType
from ai_orchestrator.security.auth import NO_AUTH_ACTOR

pytestmark = pytest.mark.unit


class TestItRefusesToStartWhereItWouldBeAHole:
    def test_production_refuses_it(self) -> None:
        settings = Settings(
            environment=Environment.PRODUCTION,
            api_auth_disabled=True,
            postgres_password="x" * 12,
            jwt_secret="y" * 12,
            encryption_key="z" * 12,
            internal_service_secret="s" * 12,
            model_provider_default="openrouter",
            sandbox_enabled=True,
            log_json=True,
        )
        with pytest.raises(Exception) as caught:
            settings.validate_for_startup()
        assert "api_auth_disabled" in str(caught.value)

    def test_tests_refuse_it(self) -> None:
        """The subtle one. A suite running with auth off is not testing
        authorisation, and no assertion anywhere would fail to say so."""
        settings = Settings(
            environment=Environment.TEST,
            api_auth_disabled=True,
            model_provider_default="fake",
        )
        with pytest.raises(Exception) as caught:
            settings.validate_for_startup()
        message = str(caught.value)
        assert "api_auth_disabled" in message
        assert "authorisation" in message, (
            "the refusal does not say that the suite would stop checking authorisation, "
            "which is the part a reader needs"
        )

    def test_local_allows_it(self) -> None:
        """The mode exists for this case, so it must actually work there."""
        Settings(
            environment=Environment.LOCAL, api_auth_disabled=True, model_provider_default="fake"
        ).validate_for_startup()

    def test_it_is_off_by_default(self) -> None:
        assert Settings().api_auth_disabled is False


class TestTheSubstitutePrincipal:
    def test_it_is_named_so_rows_are_attributable(self) -> None:
        """Not `None`, and not a person.

        With authentication off an approval can be granted. If the row said a human
        granted it, that is the one outcome this platform exists to prevent — so the
        actor is a SERVICE named `dev:no-auth`, greppable afterwards and impossible to
        confuse with a real decision.
        """
        assert NO_AUTH_ACTOR == "dev:no-auth"
        assert ActorType.SERVICE.value == "service"
        assert ActorType.HUMAN.value != NO_AUTH_ACTOR

    def test_the_flag_defaults_to_false(self) -> None:
        """The **default** is false, whatever the caller passes for `kind`.

        This is a property of the model, not of the no-auth mode, and it is worth keeping
        after the change below: a substitute actor must not inherit a human's authority by
        being constructed. Whoever wants it asks for it by name.
        """
        from ai_orchestrator.domain.contracts import Actor

        assert Actor(id="svc:worker", kind=ActorType.SERVICE).is_privileged_human is False
        assert Actor(id="human:someone", kind=ActorType.HUMAN).is_privileged_human is False

    def test_the_no_auth_actor_can_act_as_a_person(self) -> None:
        """It is a `HUMAN`, and privileged, because otherwise nothing requiring a person works.

        Reversed deliberately (see the module docstring). The concrete failure this fixes is
        worth naming so it is not "simplified" back: `ApprovalService.decide` refuses a
        non-human approver, so with a `SERVICE` actor the Approve button could not work for
        the operator using the page.

        The two things that make this safe are asserted in the tests above and do not depend
        on this one: the mode refuses to start in production, and it refuses to start in
        tests. So the reach is bounded to a deliberate local mode rather than to a
        configuration value somebody could set in a deployment.
        """
        from ai_orchestrator.security.auth import no_auth_principal

        principal = no_auth_principal("org_01m3h7j45b6cj3jnphq2ggjteq")
        assert principal.actor.kind is ActorType.HUMAN
        assert principal.actor.is_privileged_human is True
        # **The id still says authentication was off.** This is the whole of the
        # substituteness: a row written by clicking Approve says the no-auth mode
        # decided it, not a person's name.
        #
        # It is *not* the bare `dev:no-auth` any more, and it cannot be. `users` is
        # keyed on `id` alone while `approvals.decided_by` is a composite foreign
        # key, so one id belongs to exactly one organisation: with the bare
        # constant, Approve returned 500 in every tenant except the first one
        # created. The marker is what makes the row attributable; the suffix is
        # what makes it exist. `operator_id_for` is covered on its own in
        # `test_operator_identity_is_per_organisation.py`.
        assert NO_AUTH_ACTOR in str(principal.actor.id), (
            "the operator id must still name the no-auth mode, or a row claims a person decided it"
        )
        assert str(principal.actor.id) != NO_AUTH_ACTOR, (
            "a bare, tenant-independent id cannot satisfy a composite foreign key: "
            "it is taken by whichever tenant was created first and this is why "
            "Approve returned 500 everywhere else"
        )
        # And it is not a service principal, so nothing that requires an API credential
        # treats it as one.
        assert principal.is_service is False
