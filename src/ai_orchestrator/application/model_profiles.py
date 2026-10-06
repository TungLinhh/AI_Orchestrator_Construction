"""Load a tenant's `model_profiles` rows into the code's model catalogue.

`await load_tenant_profiles(db, organization_id)` returns `dict[str, ModelProfile]`.

## What was broken

`agents.model_profile` and `agent_definitions.model_profile` exist. Nothing on the
execution path read them. `_default_gateway()` built the gateway from
`default_profiles()` -- a Python literal -- and `ModelRequest.profile` carried a *name*,
so an agent could only name one of the five profiles the code happened to define.

The consequence was concrete and measured: a tenant that wrote a row binding an agent to
`mpr_...` got `ValidationError: unknown model profile: mpr_...`, and a tenant that wrote
`primary` got the same error, because `primary` is in the database and not in the code. The
workaround was to bind by the literal name `primary`, which is a coincidence rather than a
mechanism, and it means **a tenant cannot choose its model from the database.** For a
product whose first requirement is per-tenant configuration, that is the gap.

## The mapping, field by field

A database row is a `ModelProfile` with its candidate list in a JSON column:

| `model_profiles` | `ModelProfile` / `ModelCandidate` |
|---|---|
| `name` | `ModelProfile.name` |
| `description` | `ModelProfile.description` |
| `providers` (JSON array) | `candidates` |
| `max_latency_ms` | `max_latency_ms` |
| `max_retries` | `max_retries` |
| `requires_tool_calling` | `requires_tools` |
| `requires_structured_output` | `requires_structured_output` |
| `max_classification` | `max_classification` |
| `fallback_profile_id` | `fallback_profile` (one hop, id to name) |
| `providers[].model` / `.provider` / `.weight` | `ModelCandidate.model` / `.provider` / `.weight` |

`ModelCandidate` also carries `max_input_tokens`, `supports_tools`, `supports_vision`,
`pricing` and `max_classification`, which the JSON column does not. Those keep their code
defaults -- and the defaults are *right* for the models this repository uses: a `:free`
tier model costs nothing, so `Money("0")` pricing is the truth and not a placeholder.

## Rows are additive, not a replacement

The result is `default_profiles()` with the tenant's rows **overlaid by name**. A tenant
that has configured nothing still gets the code catalogue, so nothing regresses; a tenant
that adds `primary` gets a profile the code never had. Overlaying rather than replacing is
the choice that makes the feature safe to ship: the failure mode of replacing would be a
tenant whose only configured profile is a name the gateway does not also carry, and an
agent bound to a built-in name would stop resolving.

## An unknown key in `providers[]` is reported, not dropped

The JSON column is operator-supplied, so it can carry a key this reader does not know. A
key it does not understand is *reported and skipped*, because silently dropping
`supports_vision: true` gives a tenant that believes it has a vision model an agent that
cannot see -- the failure is invisible and the belief is not. `ProfileLoad` carries the
warnings so the caller can log them once per load rather than once per request.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ai_orchestrator.domain.enums import DataClassification
from ai_orchestrator.models.gateway import ModelCandidate, ModelProfile
from ai_orchestrator.models.profiles import default_profiles
from ai_orchestrator.persistence.session import Database

logger = logging.getLogger(__name__)

#: The candidate keys this reader understands. Anything else in a `providers[]` entry is
#: reported. `max_input_tokens` and `pricing` are deliberately absent: they are not in the
#: column today, and a tenant writing `max_input_tokens: 999999` into a JSON blob and
#: having it ignored is the same invisible-belief failure as `supports_vision`.
_CANDIDATE_KEYS = frozenset(
    {
        "model",
        "provider",
        "weight",
        "max_input_tokens",
        "supports_tools",
        "supports_structured_output",
        "supports_vision",
        "max_classification",
        "base_url",
    }
)

_SQL = """
SELECT id, name, description, providers, max_latency_ms, max_retries,
       requires_tool_calling, requires_structured_output, max_classification,
       fallback_profile_id, is_active
  FROM model_profiles
 WHERE organization_id = CAST(:org AS varchar(64))
 ORDER BY name
"""


@dataclass(slots=True)
class ProfileLoad:
    """The result of a load, with what it could not read."""

    profiles: dict[str, ModelProfile] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    rows: int = 0

    def __bool__(self) -> bool:
        return bool(self.profiles)


def _classification(
    value: str | None, *, where: str, warn: Callable[[str], None]
) -> DataClassification:
    """Parse a classification, falling back to the strictest on nonsense.

    The fallback is `RESTRICTED`, not `PUBLIC`. A typo in an operator's row must not
    *widen* what a tenant's data may be sent to, and the strictest value means a
    misconfigured row refuses work rather than leaking it.
    """
    if value is None:
        return DataClassification.RESTRICTED
    try:
        return DataClassification(value)
    except ValueError:
        warn(f"{where}: unknown data classification {value!r}; treated as RESTRICTED")
        return DataClassification.RESTRICTED


def _candidate(
    entry: dict[str, Any], *, where: str, warn: Callable[[str], None]
) -> ModelCandidate | None:
    model = entry.get("model")
    provider = entry.get("provider")
    if not isinstance(model, str) or not isinstance(provider, str) or not model:
        warn(f"{where}: a providers[] entry has no usable model/provider; skipped")
        return None
    for key in entry:
        if key not in _CANDIDATE_KEYS:
            warn(f"{where}: providers[] entry for {model!r} has unknown key {key!r}; ignored")
    return ModelCandidate(
        provider=provider,
        model=model,
        weight=float(entry.get("weight", 1.0)),
        max_input_tokens=int(entry.get("max_input_tokens", 200_000)),
        supports_tools=bool(entry.get("supports_tools", True)),
        supports_structured_output=bool(entry.get("supports_structured_output", True)),
        supports_vision=bool(entry.get("supports_vision", False)),
        max_classification=_classification(
            entry.get("max_classification"), where=f"{where} {model!r}", warn=warn
        ),
        base_url=entry.get("base_url") if isinstance(entry.get("base_url"), str) else None,
    )


def _profile(
    row: Any, *, fallback_names: dict[str, str], warn: Callable[[str], None]
) -> ModelProfile | None:
    name = str(row.name)
    where = f"model_profiles.{name}"
    raw = row.providers
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            warn(f"{where}: providers is not valid JSON ({exc}); row skipped")
            return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        warn(f"{where}: providers is not a list; row skipped")
        return None

    candidates = tuple(
        candidate
        for entry in raw
        if isinstance(entry, dict)
        for candidate in (_candidate(entry, where=where, warn=warn),)
        if candidate is not None
    )
    if not candidates:
        # `ModelProfile.__post_init__` refuses an empty candidate list, which is the right
        # rule: a profile with no model is a profile that cannot run.
        warn(f"{where}: no usable candidate; row skipped")
        return None

    fallback_id = row.fallback_profile_id
    return ModelProfile(
        name=name,
        candidates=candidates,
        description=str(row.description or ""),
        fallback_profile=(
            fallback_names.get(str(fallback_id)) if fallback_id is not None else None
        ),
        max_latency_ms=int(row.max_latency_ms or 60_000),
        max_retries=int(row.max_retries if row.max_retries is not None else 2),
        requires_tools=bool(row.requires_tool_calling),
        requires_structured_output=bool(row.requires_structured_output),
        max_classification=_classification(row.max_classification, where=where, warn=warn),
    )


async def load_tenant_profiles(db: Database, organization_id: str) -> ProfileLoad:
    """The tenant's profiles, overlaid on the code catalogue.

    Overlaid, not replacing: see the module docstring. A tenant row of the same name wins,
    because a tenant configuring `default` is configuring `default` and not asking for a
    second one.
    """
    async with db.tenant_session(organization_id) as session:
        return await load_profiles_from_session(session, organization_id)


async def load_profiles_from_session(session: Any, organization_id: str) -> ProfileLoad:
    """Resolve profiles in the caller's transaction using the execution catalogue."""
    from sqlalchemy import text

    load = ProfileLoad()
    profiles: dict[str, ModelProfile] = default_profiles()

    def warn(message: str) -> None:
        load.warnings.append(message)

    rows = (await session.execute(text(_SQL), {"org": organization_id})).all()
    load.rows = len(rows)

    # The fallback column is an id and `ModelProfile` wants a name, so the names are read
    # first. A dangling id yields `None`, which means "no fallback" -- the gateway then
    # refuses rather than chasing an id that is not there.
    names = {str(row.id): str(row.name) for row in rows}

    for row in rows:
        if not row.is_active:
            continue
        profile = _profile(row, fallback_names=names, warn=warn)
        if profile is not None:
            profiles[profile.name] = profile

    load.profiles = profiles
    return load


#: One `Database` **per running event loop**.
#:
#: `Database.from_settings()` builds a connection pool, so building one per agent run would
#: open a pool per model call. The agent is about to spend a request on a provider; spending
#: a round trip to read its own profile is proportionate, and this keeps it to one pool
#: rather than one per turn.
#:
#: Keyed by the loop because **a connection pool belongs to the loop that made it.**
#: `tests/integration/test_tenant_model_profiles.py` hit
#: `RuntimeError: got Future ... attached to a different loop` on its second test with a
#: module-level singleton, because each `pytest-asyncio` test runs on a fresh loop and the
#: cached engine still pointed at the first one. In a server the loop is stable and a bare
#: singleton is fine; in a test process it is not, and a cache that only works in production
#: is a cache that hides a bug until someone runs it under a loop-per-test runner.
_DATABASES: dict[object, Database] = {}


def _database() -> Database:
    import asyncio

    loop = asyncio.get_running_loop()
    database = _DATABASES.get(loop)
    if database is None:
        database = Database.from_settings()
        _DATABASES[loop] = database
    return database


async def build_tenant_gateway(organization_id: str, *, session: Any = None) -> Any:
    """A `ModelGateway` whose profiles are this tenant's, overlaid on the code catalogue.

    Returns the gateway, so the caller can register providers on it and keep the
    gateway's own contract — privacy, capability, budget, fallback — untouched.
    """
    from ai_orchestrator.config.settings import get_settings
    from ai_orchestrator.models.gateway import ModelGateway
    from ai_orchestrator.models.providers import build_providers_from_settings

    gateway = ModelGateway()
    settings = get_settings()
    if settings.model_provider_default != "fake":
        for provider in build_providers_from_settings(settings).values():
            gateway.register_provider(provider)
    if settings.model_provider_default == "fake":
        # Explicit fake mode must remain local even when runtime.env contains a key.
        # Real deployments fail visibly when no real adapter is usable.
        from ai_orchestrator.models.providers import DeterministicProvider

        gateway.register_provider(DeterministicProvider())

    if not organization_id:
        return gateway

    load = (
        await load_profiles_from_session(session, organization_id)
        if session is not None
        else await load_tenant_profiles(_database(), organization_id)
    )
    for profile in load.profiles.values():
        gateway.register_profile(profile)
    if load.warnings:
        for warning in set(load.warnings):
            logger.warning("model profile load for %s: %s", organization_id, warning)
    return gateway
