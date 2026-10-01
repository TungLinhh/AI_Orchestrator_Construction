"""Organization, org-unit and role endpoints.

Reads are available to any member of the tenant. Writes that change the shape of
the company — creating a unit, moving one, changing a role's authority — are
privileged, because those are the operations whose blast radius is the whole
organisation.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ai_orchestrator.api.deps import (
    ApiContext,
    get_context,
    paginate,
)
from ai_orchestrator.api.health import bump
from ai_orchestrator.domain.errors import NotFoundError
from ai_orchestrator.persistence.repositories.organization import (
    OrganizationRepository,
    OrgUnitRepository,
    RoleRepository,
)

router = APIRouter(tags=["organizations"])


class CreateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""


class UpdateOrganizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    spend_cap_usd: float | None = Field(default=None, ge=0)
    default_model_profile: str | None = None
    data_retention_days: int | None = Field(default=None, ge=1, le=3650)


class CreateOrgUnitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9-]*$")
    parent_id: str | None = None
    unit_type: str = "department"
    purpose: str = ""


class CreateRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = ""
    authority_profile: dict[str, Any] = Field(default_factory=dict)
    max_autonomy: str = "l2_parent_review"
    may_delegate_to_peers: bool = False
    may_spawn_subagents: bool = False
    max_delegation_depth: int = Field(default=2, ge=0, le=8)


def _org_dict(org: Any) -> dict[str, Any]:
    return {
        "id": org.id,
        "slug": org.slug,
        "name": org.name,
        "description": org.description,
        "status": org.status,
        "settings": org.settings,
        "spend_cap_usd": float(org.spend_cap_usd) if org.spend_cap_usd is not None else None,
        "default_model_profile": org.default_model_profile,
        "data_retention_days": org.data_retention_days,
        "created_at": org.created_at.isoformat(),
    }


def _unit_dict(unit: Any, *, children: int = 0) -> dict[str, Any]:
    return {
        "id": unit.id,
        "name": unit.name,
        "slug": unit.slug,
        "parent_id": unit.parent_id,
        "unit_type": unit.unit_type,
        "purpose": unit.purpose,
        "head_agent_id": unit.head_agent_id,
        "depth": unit.depth,
        "path": unit.path,
        "child_count": children,
        "created_at": unit.created_at.isoformat(),
    }


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_organization(body: CreateOrganizationRequest, request: Request) -> dict[str, Any]:
    """Create a tenant.

    A service credential, because creating a tenant is not something an
    authenticated user of an existing tenant can do: the caller has no
    organization to authenticate against yet.
    """
    from ai_orchestrator.domain.errors import AuthorizationError
    from ai_orchestrator.security.auth import check_internal_secret

    presented = request.headers.get("authorization", "")
    if not presented.startswith("svc.") or not check_internal_secret(presented[4:]):
        msg = "creating an organization requires a service credential"
        raise AuthorizationError(msg, details={"reason": "service_credential_required"})

    # `organizations` is the one table with no row-level-security policy: it is
    # the tenant root, so a policy would have no column to compare. Creating one
    # therefore runs as the owner, which is safe only because the service
    # credential was checked above.
    from ai_orchestrator.persistence.session import Database

    admin = Database.from_settings(use_admin_role=True)
    try:
        async with admin.session() as session:
            org_repo = OrganizationRepository(session, "")
            org = await org_repo.create(
                name=body.name, slug=body.slug, description=body.description
            )
            await session.commit()
    finally:
        await admin.dispose()
    bump("organizations_created_total")
    return _org_dict(org)


@router.get("/organizations/{organization_id}")
async def get_organization(
    organization_id: str, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    if organization_id != ctx.organization_id:
        msg = "not found"
        raise NotFoundError(msg, resource_type="organization", resource_id=organization_id)
    repo = OrganizationRepository(ctx.session, ctx.organization_id)
    return _org_dict(await repo.get(organization_id))


@router.patch("/organizations/{organization_id}")
async def update_organization(
    organization_id: str,
    body: UpdateOrganizationRequest,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Privileged. Changes here affect budgets and retention for the whole tenant."""
    ctx.require_admin()
    if organization_id != ctx.organization_id:
        msg = "not found"
        raise NotFoundError(msg, resource_type="organization", resource_id=organization_id)
    repo = OrganizationRepository(ctx.session, ctx.organization_id)
    org = await repo.update_settings(organization_id, body.model_dump(exclude_none=True))
    return _org_dict(org)


@router.post("/org-units", status_code=status.HTTP_201_CREATED)
async def create_org_unit(
    body: CreateOrgUnitRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Creating a unit changes the shape of the company."""
    ctx.require_admin()
    repo = OrgUnitRepository(ctx.session, ctx.organization_id)
    unit = await repo.create(
        name=body.name,
        slug=body.slug,
        parent_id=body.parent_id,
        unit_type=body.unit_type,
        purpose=body.purpose,
    )
    bump("org_units_created_total")
    return _unit_dict(unit)


@router.get("/org-units/{unit_id}")
async def get_org_unit(unit_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = OrgUnitRepository(ctx.session, ctx.organization_id)
    unit = await repo.get(unit_id)
    children = await repo.list_children(unit_id)
    return _unit_dict(unit, children=len(children))


@router.get("/org-units/{unit_id}/tree")
async def get_org_unit_tree(unit_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """The subtree below a unit, as a nested structure.

    Built from the materialised path rather than a recursive query, so a deep
    tree is one index scan. Returns a tree because that is what a caller renders;
    a flat list would push the same nesting logic into every client.
    """
    repo = OrgUnitRepository(ctx.session, ctx.organization_id)
    root = await repo.get(unit_id)
    flat = list(await repo.subtree(unit_id))
    nodes = {u.id: {**_unit_dict(u), "children": [], "head_agent": None} for u in flat}
    roots = []
    for unit in [*flat, root]:
        node = nodes.get(unit.id) or {
            **_unit_dict(unit),
            "children": [],
            "head_agent": None,
        }
        nodes[unit.id] = node
        parent = nodes.get(unit.parent_id) if unit.parent_id else None
        if parent is not None:
            parent["children"].append(node)
        else:
            roots.append(node)
    return {"root": roots[0] if roots else _unit_dict(root)}


@router.get("/org-units")
async def list_org_units(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = OrgUnitRepository(ctx.session, ctx.organization_id)
    units = await repo.tree()
    return paginate([_unit_dict(u) for u in units], 500, 0)


@router.post("/org-units/{unit_id}/move")
async def move_org_unit(
    unit_id: str,
    body: dict[str, Any],
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Re-parent a unit, rewriting the materialised path of its subtree.

    Rejected when the new parent is inside the moved subtree: that would detach
    the branch from the tree and leave agents pointing at a unit with no path to
    the root.
    """
    ctx.require_admin()
    repo = OrgUnitRepository(ctx.session, ctx.organization_id)
    unit = await repo.reparent(unit_id, body.get("parent_id"))
    return _unit_dict(unit)


@router.post("/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    body: CreateRoleRequest, ctx: ApiContext = Depends(get_context)
) -> dict[str, Any]:
    """Creating a role is creating a grant of authority. Privileged."""
    ctx.require_admin()
    repo = RoleRepository(ctx.session, ctx.organization_id)
    role = await repo.create(
        name=body.name,
        description=body.description,
        authority_profile=body.authority_profile,
        max_autonomy=body.max_autonomy,
        may_delegate_to_peers=body.may_delegate_to_peers,
        may_spawn_subagents=body.may_spawn_subagents,
        max_delegation_depth=body.max_delegation_depth,
    )
    bump("roles_created_total")
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "max_autonomy": role.max_autonomy,
        "may_delegate_to_peers": role.may_delegate_to_peers,
        "may_spawn_subagents": role.may_spawn_subagents,
        "max_delegation_depth": role.max_delegation_depth,
        "authority_profile": role.authority_profile,
    }


@router.get("/roles")
async def list_roles(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    repo = RoleRepository(ctx.session, ctx.organization_id)
    roles = await repo.list()
    return paginate(
        [
            {
                "id": r.id,
                "name": r.name,
                "description": r.description,
                "max_autonomy": r.max_autonomy,
                "may_delegate_to_peers": r.may_delegate_to_peers,
                "may_spawn_subagents": r.may_spawn_subagents,
                "max_delegation_depth": r.max_delegation_depth,
            }
            for r in roles
        ],
        200,
        0,
    )


__all__ = ["router"]
