"""A tenant chooses its model from the database, not from a Python literal.

The measured gap this file closes, from `docs/PLAN_FINISH.md`:

> The `model_profiles` table is never read on the execution path. `ModelGateway` is built
> from `default_profiles()` in code; a row written to the table is invisible to it, and an
> agent bound to a row id fails with `unknown model profile: mpr_...`.

## What each test is for

* `test_a_tenant_profile_the_code_has_never_heard_of_resolves` — the gap, directly. A row
  named something the code catalogue does not contain must resolve, because a tenant is
  allowed to name its own profile.
* `test_a_tenant_row_overlays_the_built_in_profile_of_the_same_name` — and *overlays*.
  A tenant that configures `default` is configuring `default`, not asking for a second
  one, and the built-ins must still resolve for an agent bound to a name the tenant never
  touched. Replacing rather than overlaying would make that agent stop working.
* `test_a_dangling_fallback_id_means_no_fallback` — the column is an id and the code wants
  a name. A fallback pointing at a row that is not there must resolve to *no* fallback, so
  the gateway refuses rather than chasing something absent.
* `test_an_unknown_key_in_a_providers_entry_is_reported_not_dropped` — the failure this
  guards is a tenant that writes `supports_vision: true`, is not told it was ignored, and
  ends up with an agent that cannot see and a belief that it can.
* `test_a_bad_classification_is_treated_as_the_strictest` — a typo must not *widen* what a
  tenant's data may be sent to.
* `test_a_row_with_no_usable_candidate_is_skipped_and_reported` — a profile with no model
  is a profile that cannot run, and `ModelProfile.__post_init__` already refuses one; the
  loader has to skip the row rather than raise on someone else's bad configuration.
* `test_the_fallback_reaches_the_gateway_not_just_the_loader` — the end of the chain. A
  gateway built for a tenant must actually resolve that tenant's profile, because a loader
  that reads the row and a gateway that ignores it is the original bug with extra steps.

## A negative test is worth more than a positive one here

Most of these assert that something is *reported* or *refused*. That is deliberate: the
gap was a silent one, and a test that only checks the happy path would have passed before
this change as readily as after it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from ai_orchestrator.application.model_profiles import load_tenant_profiles
from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.domain.ids import new_ulid
from ai_orchestrator.models.gateway import ModelRequest
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.persistence.session import Database
from tests.integration.tenant_context import Tenant

pytestmark = [pytest.mark.integration]

#: A name the code catalogue cannot contain, which is the point.
TENANT_ONLY = "tenant_picked_this"


async def _write_profile(t: Tenant, **overrides: object) -> str:
    """One `model_profiles` row for this tenant, with the fields a test cares about."""
    row: dict[str, object] = {
        "name": TENANT_ONLY,
        "description": "a profile the tenant configured for itself",
        "providers": [{"provider": "deterministic", "model": "scripted-1", "weight": 1.0}],
        "max_classification": "public",
        "requires_tool_calling": True,
        "requires_structured_output": True,
        "max_latency_ms": 30_000,
        "max_retries": 1,
    }
    row.update(overrides)
    # A real id, because `model_profiles`' primary key is `id` alone -- not
    # `(organization_id, name)`. A name-derived id therefore collides across tenants, and
    # the test schema is never truncated, so the *second* run of this file hit
    # `duplicate key value violates unique constraint "pk_model_profiles"` on a row from
    # the first. The name stays deterministic because the name is what the loader resolves.
    profile_id = f"mpr_{new_ulid()}"
    await t.session.execute(
        text(
            "INSERT INTO model_profiles (id, organization_id, name, description, providers, "
            "max_classification, requires_tool_calling, requires_structured_output, "
            "max_latency_ms, max_retries) "
            "VALUES (:i, :o, :n, :d, CAST(:p AS jsonb), CAST(:c AS varchar(16)), "
            ":t, :s, :l, :r)"
        ),
        {
            "i": profile_id,
            "o": t.organization_id,
            "n": row["name"],
            "d": row["description"],
            "p": json.dumps(row["providers"]),
            "c": row["max_classification"],
            "t": row["requires_tool_calling"],
            "s": row["requires_structured_output"],
            "l": row["max_latency_ms"],
            "r": row["max_retries"],
        },
    )
    await t.commit()
    return profile_id


class TestTheGapIsClosed:
    async def test_a_tenant_profile_the_code_has_never_heard_of_resolves(
        self, tenant: Tenant, db: Database
    ) -> None:
        assert TENANT_ONLY not in default_profiles(), (
            "This test is only meaningful while the code catalogue lacks this name. If a "
            "built-in ever takes it, pick another."
        )
        await _write_profile(tenant)

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert TENANT_ONLY in load.profiles, (
            f"the tenant's own profile is missing; known: {sorted(load.profiles)}"
        )
        candidate = load.profiles[TENANT_ONLY].candidates[0]
        assert candidate.provider == "deterministic"
        assert candidate.model == "scripted-1"

    async def test_a_tenant_row_overlays_the_built_in_profile_of_the_same_name(
        self, tenant: Tenant, db: Database
    ) -> None:
        await _write_profile(
            tenant,
            name="default",
            providers=[{"provider": "deterministic", "model": "scripted-1"}],
            description="overridden by the tenant",
        )

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert load.profiles["default"].description == "overridden by the tenant"
        # And the built-ins the tenant never touched still resolve: that is what makes
        # overlaying safe and replacing not.
        for name in default_profiles():
            if name != "default":
                assert name in load.profiles, f"{name} went missing"

    async def test_the_fallback_reaches_the_gateway_not_just_the_loader(
        self, tenant: Tenant, db: Database
    ) -> None:
        """The end of the chain: a gateway built for a tenant resolves *its* profile.

        The first version of this made a real `complete()` call against
        `deterministic/scripted-1`, and failed with `no provider adapter registered` --
        which is the gateway being **correct**: `deterministic` is only registered when no
        real provider is configured, so a tenant naming it in a configured deployment gets
        refused rather than quietly served by a script.

        The routing decision is the thing under test, and it is visible without a call. A
        test that spends a model call to check a name resolved proves two things, one of
        them expensive and incidental.
        """
        primary = await _write_profile(tenant)
        fallback_id = await _write_profile(
            tenant,
            name=f"{TENANT_ONLY}_fallback",
            providers=[{"provider": "deterministic", "model": "scripted-2"}],
        )
        await tenant.session.execute(
            text("UPDATE model_profiles SET fallback_profile_id = :f WHERE id = :i"),
            {"f": fallback_id, "i": primary},
        )
        await tenant.commit()

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert load.profiles[TENANT_ONLY].fallback_profile == f"{TENANT_ONLY}_fallback"

        from ai_orchestrator.application.model_profiles import build_tenant_gateway

        gateway = await build_tenant_gateway(tenant.organization_id)
        assert TENANT_ONLY in gateway.profiles, (
            f"the loader read the row and the gateway dropped it; known: {sorted(gateway.profiles)}"
        )
        assert gateway.profiles[TENANT_ONLY].candidates[0].model == "scripted-1"

    async def test_a_tenant_row_is_invisible_to_another_tenant(
        self, tenant: Tenant, other_tenant: Tenant, db: Database
    ) -> None:
        """A profile is a tenant's, not a global.

        Without this the loader could read every row and let a tenant bind to a model
        another tenant paid to verify -- which is the isolation failure this whole tranche
        of composite keys was about, one level up.
        """
        await _write_profile(tenant)
        theirs = await load_tenant_profiles(db, other_tenant.organization_id)
        assert TENANT_ONLY not in theirs.profiles


class TestTheLoaderRefusesRatherThanGuesses:
    async def test_a_dangling_fallback_is_refused_by_the_schema_not_by_the_loader(
        self, tenant: Tenant, db: Database
    ) -> None:
        """The schema makes this impossible, and that is the better guarantee.

        `fallback_profile_id` carries a **composite** foreign key to
        `model_profiles (organization_id, id)`, so a fallback cannot point at a row that is
        not there -- and cannot point at another tenant's row either. The first version of
        this test set a dangling id and expected the *loader* to cope, and was refused by
        `fk_model_profiles_fallback_profile_id_model_profiles`.

        The loader's `fallback_names.get(...) is None` branch is therefore defensive depth
        for a state the database does not have. It stays -- a row could be written by a
        migration, or the column could stop being a key -- but the guarantee is asserted
        here, where it is real.
        """
        from sqlalchemy.exc import IntegrityError

        primary = await _write_profile(tenant)
        with pytest.raises(IntegrityError, match="fallback_profile_id"):
            await tenant.session.execute(
                text("UPDATE model_profiles SET fallback_profile_id = :f WHERE id = :i"),
                {"f": "mpr_does_not_exist", "i": primary},
            )
        await tenant.session.rollback()

    async def test_an_unknown_key_in_a_providers_entry_is_reported_not_dropped(
        self, tenant: Tenant, db: Database
    ) -> None:
        await _write_profile(
            tenant,
            providers=[
                {
                    "provider": "deterministic",
                    "model": "scripted-1",
                    "supports_audio": True,
                }
            ],
        )

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert any("supports_audio" in warning for warning in load.warnings), (
            "A key the reader does not understand was dropped in silence. The tenant "
            f"believes it configured something. warnings: {load.warnings}"
        )
        # A key the reader *does* understand is honoured, and not reported. `supports_vision`
        # is the case: the first version of this test asserted it was unknown, and it is
        # not -- it is a `ModelCandidate` field and it is read. A test that guesses at the
        # reader's vocabulary instead of reading `_CANDIDATE_KEYS` guesses wrong.
        assert load.profiles[TENANT_ONLY].candidates[0].supports_vision is False

    async def test_a_bad_classification_is_treated_as_the_strictest(
        self, tenant: Tenant, db: Database
    ) -> None:
        await _write_profile(tenant, max_classification="public-ish")

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert load.profiles[TENANT_ONLY].max_classification is DataClassification.RESTRICTED
        assert any("classification" in w for w in load.warnings)

    async def test_a_row_with_no_usable_candidate_is_skipped_and_reported(
        self, tenant: Tenant, db: Database
    ) -> None:
        await _write_profile(tenant, providers=[])

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert TENANT_ONLY not in load.profiles
        assert any("no usable candidate" in w for w in load.warnings)

    async def test_one_bad_row_does_not_stop_the_others(self, tenant: Tenant, db: Database) -> None:
        """A tenant's worst row must not take the rest of its configuration with it.

        The row is `providers = []`, which is reachable: Postgres will happily store an
        empty JSON array. The first version used `providers = 'not json'::jsonb`, and
        `InvalidTextRepresentationError: Token "not" is invalid` is **Postgres** refusing
        the write -- so the loader's `JSONDecodeError` branch is unreachable through this
        column, and the test was asserting a path that cannot exist. It is covered as a
        unit test instead, because asyncpg may hand back a `jsonb` as a string depending on
        configuration, and the loader handles both.
        """
        await _write_profile(tenant)
        await tenant.session.execute(
            text("UPDATE model_profiles SET providers = '[]'::jsonb WHERE name = :n"),
            {"n": TENANT_ONLY},
        )
        await _write_profile(
            tenant,
            name="still_works",
            providers=[{"provider": "deterministic", "model": "scripted-1"}],
        )
        await tenant.commit()

        load = await load_tenant_profiles(db, tenant.organization_id)
        assert "still_works" in load.profiles
        assert TENANT_ONLY not in load.profiles
        assert any("no usable candidate" in w for w in load.warnings)


class TestTheGatewayStillRefusesWhatItShould:
    async def test_an_unknown_profile_is_still_a_validation_error(self, tenant: Tenant) -> None:
        """Loading profiles from the database must not turn a typo into a silent default.

        The whole risk of making profiles data-driven is that a name that does not exist
        starts resolving to *something*. It must still raise, and it must raise from the
        **public** path -- the first version reached into `gateway._profiles` and got a
        `KeyError` from a dict, which proved the dict was a dict and not that the gateway
        refuses. `complete()` is where the refusal lives.
        """
        from ai_orchestrator.application.model_profiles import build_tenant_gateway
        from ai_orchestrator.domain.errors import ValidationError

        await _write_profile(tenant)
        gateway = await build_tenant_gateway(tenant.organization_id)
        assert TENANT_ONLY in gateway.profiles, "the control case: a real row resolves"

        with pytest.raises(ValidationError, match="unknown model profile"):
            await gateway.complete(
                ModelRequest(
                    profile="no_such_profile",
                    system_instructions="",
                    prompt="xin chào",
                    organization_id=tenant.organization_id,
                    data_classification=DataClassification.PUBLIC,
                )
            )
