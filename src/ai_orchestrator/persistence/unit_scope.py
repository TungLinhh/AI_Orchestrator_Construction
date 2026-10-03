"""Turning the unit tree into the list an agent's reads are restricted to.

`domain/access.py` decides the policy; this turns it into something a database policy
can compare against — a flat list of unit ids — and reads the tree once to do it.

**The list is of units whose *content* the agent may read**, which is exactly
`scope_of(...) == Visibility.CONTENT`: its own unit, its descendants, and its
ancestors. Peers are absent, and that absence is the separation.

Two details that are easy to get wrong and are commented where they matter:

* **The list is empty for an agent with no unit.** An empty list means "no rows", which
  is the safe direction. Treating empty as "unset, therefore unrestricted" would make a
  mis-bound agent the most privileged agent on the tenant.
* **A unit with no `path` still contributes its descendants.** Older rows predate the
  materialised path, and a policy that quietly drops a branch is worse than one that
  over-collects: it would hide work from the people who own it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from ai_orchestrator.domain.access import UnitRef, Visibility, scope_of

#: Every unit in the tenant, with the three columns the policy needs. One read, and the
#: alternative — a recursive CTE per lookup — is a query per task on a hot path.
_TREE = """
SELECT u.id, u.slug, u.depth, u.path
  FROM organizational_units u
 WHERE u.organization_id = CAST(:org AS varchar(40))
"""


async def tenant_units(session: Any, organization_id: str) -> list[UnitRef]:
    """Every unit in the tenant, as the policy's inputs."""
    rows = (
        (
            await session.execute(
                text(_TREE),
                {"org": organization_id},
            )
        )
        .mappings()
        .all()
    )
    units: list[UnitRef] = []
    for row in rows:
        depth = int(row["depth"] or 0)
        path = str(row["path"] or "/")
        if not path.startswith("/"):
            # A malformed path would make every prefix test wrong in the same direction,
            # which is how a boundary that grants becomes a boundary that leaks. The
            # unit is still listed; it just cannot be above or below anything.
            path = "/"
        units.append(
            UnitRef(
                id=str(row["id"]),
                slug=str(row["slug"]),
                depth=depth,
                path=path,
            )
        )
    return units


async def permitted_unit_ids_for(
    session: Any, organization_id: str, unit_id: str | None
) -> list[str]:
    """The unit ids whose content `unit_id` may read, plus its own.

    Returns `[]` for an unknown or missing unit. **That is the point:** an agent the
    platform cannot place in the tree reads nothing, rather than everything.
    """
    if not unit_id:
        return []
    units = await tenant_units(session, organization_id)
    reader = next((u for u in units if u.id == str(unit_id)), None)
    if reader is None:
        return []
    scope = scope_of(reader, units)
    return [u.id for u in units if scope.get(u.slug) == Visibility.CONTENT]


def scope_of_unit(units: list[UnitRef], unit_id: str) -> dict[str, Visibility]:
    """`scope_of` for a unit that is known to exist in `units`."""
    reader = next(u for u in units if u.id == unit_id)
    return scope_of(reader, units)


__all__ = ["permitted_unit_ids_for", "scope_of_unit", "tenant_units"]
