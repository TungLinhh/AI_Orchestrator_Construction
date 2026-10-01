"""Document control over HTTP: read the register, and handle it.

## Why a separate router

`construction.py` is **read-only by decision**, and its docstring says so at length: every
operation there is a `GET`, and the writers were reached through the ingest pipeline and
through agents. This module is the deliberate exception, and putting it in a separate file
is what makes the exception visible — a reader of `construction.py` can trust its claim
because the thing that breaks it is not in it.

## The operations

| method | path | what it is for |
|---|---|---|
| `GET` | `/documents` | the register, filtered by block or department |
| `GET` | `/documents/{id}` | lineage, distribution matrix, compliance |
| `GET` | `/documents/summary` | counts for the whole organisation |
| `POST` | `/documents/{id}/versions` | a revision goes into force |
| `POST` | `/documents/{id}/distributions` | tell an audience a revision exists |
| `POST` | `/documents/{id}/acknowledge` | record that somebody has read it |

## Three things this module refuses to do

**It does not invent a code.** `code` is optional on a body, and omitting it leaves the
document unnumbered. Minting `ONX-...` for an uploaded attachment would put a false claim in
the identifier column, and the register's first job is to be trustworthy about identity.

**It does not take an organization from the body.** The tenant comes from `ApiContext`, and
`require_body_tenant` refuses a body that names another one. This is the whole of
multi-tenancy on the write path: a body cannot redirect a distribution at another
organisation's document.

**It does not check who the caller is.** The brief excludes authentication, and the
development principal is `dev:no-auth` with `ActorType.SERVICE` -- so `require_human()`
would have made all three writers **unreachable from the page**, which is the
library-with-no-consumer failure this repository has now hit three times. The kill and
revive endpoints in `agents_control.py` are in the same position and do the same thing.

What that costs is worth stating plainly: **the `acknowledged_by` name is
self-asserted.** It records who says they read the document, and nothing here verifies it.
The rule that survives is the one that is not about identity -- an acknowledgement *must*
carry a name (`ck_document_distributions_an_acknowledgement_names_who`), so the column
cannot be a tick in a box, and the actor is written honestly as `dev:no-auth` rather than
invented as a person. Tập 3 §4.1 -- an agent is never Accountable -- is still enforced
where it belongs: an agent may *propose* a revision, and `approval_id` is what records that
a person signed it.

## Errors are 404 for another tenant's document, and 422 for a refusal

A document id from another tenant is a **404, not a 403**: saying "forbidden" confirms the
row exists. A refusal from the application layer -- no change note, an acknowledgement with
no name, a draft that cannot be circulated -- is a **422** carrying the sentence to show the
person, because `ck_document_distributions_an_acknowledgement_names_who` is not an answer
to "why did my button do nothing".

## Order matters, and it is declared, not discovered

`/documents/summary` is declared **above** `/documents/{document_id}`. Starlette matches in
registration order, so a summary route declared below would be captured by the `{id}` route
and answer with *"no document summary"*. Same reason `/approvals/stats` sits above
`/approvals/{id}` (F123) -- and the reason this comment exists is that it happened once.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text

from ai_orchestrator.api.deps import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ApiContext,
    get_context,
    require_body_tenant,
)
from ai_orchestrator.application.document_control import (
    acknowledge,
    distribute,
    document_detail,
    document_register,
    issue_version,
)
from ai_orchestrator.domain.document_code import DocumentCode

#: One aggregate for the whole organisation, so a tile and a list cannot disagree.
#:
#: Six numbers, one statement. The obvious alternative -- `total` from the register plus a
#: second call for the unread count -- is two moments in time, and a page that shows both
#: can show two answers.
_SUMMARY = """
SELECT
  (SELECT count(*) FROM documents
    WHERE organization_id = CAST(:o AS varchar(40)) AND is_deleted = false)
      AS documents,
  (SELECT count(*) FROM documents
    WHERE organization_id = CAST(:o AS varchar(40)) AND is_deleted = false
      AND code IS NOT NULL)
      AS numbered,
  (SELECT count(*) FROM document_versions
    WHERE organization_id = CAST(:o AS varchar(40)) AND status = 'approved')
      AS versions_in_force,
  (SELECT count(*) FROM document_distributions
    WHERE organization_id = CAST(:o AS varchar(40)) AND is_mandatory)
      AS required,
  (SELECT count(*) FROM document_distributions
    WHERE organization_id = CAST(:o AS varchar(40)) AND is_mandatory
      AND acknowledged_at IS NOT NULL)
      AS acknowledged,
  (SELECT count(*) FROM document_distributions
    WHERE organization_id = CAST(:o AS varchar(40)) AND is_mandatory
      AND distributed_at IS NOT NULL AND acknowledged_at IS NULL)
      AS outstanding
"""

#: The revision in force for a document, so `POST .../distributions` can be called without
#: the client having to know the version id. A client that had to look the version up first
#: would circulate whatever it found, and a stale lookup circulates the wrong revision --
#: which is the failure the version table exists to prevent.
_LIVE_VERSION_OF = """
SELECT id FROM document_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = :id
  AND status = 'approved' AND effective_to IS NULL
ORDER BY version_no DESC
LIMIT 1
"""


router = APIRouter(tags=["documents"])


class IssueVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A content hash, never the content. The document lives in the object store; this
    #: module records *which* bytes were in force and when, which is the question an audit
    #: asks. Uploading a file through a JSON body is a different endpoint and a different
    #: decision.
    content_hash: str = Field(min_length=8, max_length=64, pattern=r"^[0-9a-fA-F]{8,64}$")
    change_note: str = Field(min_length=3, max_length=2000)
    issued_on: dt.date
    effective_from: dt.date | None = None
    approved_by: str = Field(default="", max_length=128)
    #: `False` writes the revision in `draft`, which is the ordinary state of a revision
    #: nobody has signed. A draft that is in force is the thing this whole module exists to
    #: prevent.
    activate: bool = True
    organization_id: str | None = None
    source_actor: str = Field(default="", max_length=128)
    proposal_id: str | None = None
    approval_id: str | None = None

    @model_validator(mode="after")
    def _window_is_not_inverted(self) -> IssueVersionRequest:
        if self.effective_from is not None and self.effective_from < self.issued_on:
            raise ValueError(
                f"effective_from ({self.effective_from}) is before issued_on "
                f"({self.issued_on}); a revision cannot take effect before it was issued"
            )
        return self


class DistributeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The dossier's own role vocabulary -- the same words `sop_definitions.owner_role_key`
    #: uses -- so a document's owner and its audience are one concept and not two lists.
    audience_role_key: str = Field(min_length=2, max_length=128)
    document_version_id: str | None = None
    channel: str = Field(default="system", max_length=32)
    is_mandatory: bool = True
    note: str = Field(default="", max_length=2000)
    distributed_at: dt.datetime | None = None
    organization_id: str | None = None
    source_actor: str = Field(default="", max_length=128)
    proposal_id: str | None = None

    @model_validator(mode="after")
    def _a_time_is_supplied(self) -> DistributeRequest:
        if self.distributed_at is not None and self.distributed_at.tzinfo is None:
            raise ValueError(
                f"distributed_at is {self.distributed_at!r}, which has no timezone. A naive "
                "datetime is stored against the server's zone, so a row written from two "
                "offices can come out in the wrong order"
            )
        return self


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    distribution_id: str
    #: Required and non-empty, because `ck_document_distributions_an_acknowledgement_names_
    #: who` refuses an acknowledgement with nobody behind it. The database's rule arriving
    #: at the edge where it can be a 422 rather than a constraint name.
    acknowledged_by: str = Field(min_length=2, max_length=128)
    acknowledged_at: dt.datetime | None = None
    organization_id: str | None = None


@router.get("/documents/summary")
async def summary(ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """What a person is told before they open anything: how many, and how much is unread.

    One aggregate rather than the register's `total` plus a second call, so the number on a
    tile and the number on the list cannot come from two different moments.
    """
    row = (
        (
            await ctx.session.execute(
                text(_SUMMARY),
                {"o": ctx.organization_id},
            )
        )
        .mappings()
        .one()
    )
    return dict(row)


@router.get("/documents")
async def register(
    block: str | None = Query(default=None, max_length=8),
    department: str | None = Query(default=None, max_length=8),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """The controlled document register.

    `block` and `department` are parsed prefixes, validated against the same vocabulary the
    code uses, so a filter cannot quietly mean something different from the code it filters.
    A caller searching `ONX-BO-%` by hand would also match a block called `BOGUS`; that
    only shows up on the day somebody adds one.
    """
    if block is not None:
        DocumentCode.make(block, "XX", 1)  # validates the block vocabulary
    if department is not None and not (2 <= len(department) <= 3):
        raise ValueError(
            f"department {department!r} must be two or three characters, as in a document "
            "code's third segment"
        )
    return await document_register(
        ctx.session,
        organization_id=ctx.organization_id,
        block=block,
        department=department,
        q=q,
        limit=limit,
        offset=offset,
    )


@router.get("/documents/{document_id}")
async def one(document_id: str, ctx: ApiContext = Depends(get_context)) -> dict[str, Any]:
    """One document: its lineage, its distribution matrix, and who has not read it."""
    return await document_detail(
        ctx.session, organization_id=ctx.organization_id, document_id=document_id
    )


@router.post("/documents/{document_id}/versions")
async def issue(
    document_id: str,
    body: IssueVersionRequest,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Write the next revision, and close the one it replaces.

    An agent may reach this only with an `approval_id`, which is the approval that records
    that a person signed the proposal. That is the whole of Tập 3 §4.1 on this path, and it
    is a `CHECK` on the table as well as a check here.
    """
    require_body_tenant(body.model_dump(), ctx)
    result = await issue_version(
        ctx.session,
        organization_id=ctx.organization_id,
        document_id=document_id,
        content_hash=body.content_hash,
        change_note=body.change_note,
        issued_on=body.issued_on,
        effective_from=body.effective_from,
        approved_by=body.approved_by or str(ctx.actor.display_name or ctx.actor.id),
        source_actor=body.source_actor or str(ctx.actor.id),
        proposal_id=body.proposal_id,
        approval_id=body.approval_id,
        activate=body.activate,
    )
    return {"document_id": document_id, **result}


@router.post("/documents/{document_id}/distributions")
async def send(
    document_id: str,
    body: DistributeRequest,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Tell an audience a revision exists, and record that they were told.

    A re-send to the same target on the same channel **updates the existing obligation**
    rather than creating a second one, so retrying a failed distribution cannot inflate the
    compliance figure. That is the `ON CONFLICT` in the operation, and it is why a retry is
    safe to offer a person at all.
    """
    require_body_tenant(body.model_dump(), ctx)
    version_id = body.document_version_id
    if version_id is None:
        row = (
            await ctx.session.execute(
                text(_LIVE_VERSION_OF),
                {"o": ctx.organization_id, "id": document_id},
            )
        ).first()
        if row is None:
            from ai_orchestrator.domain.errors import NotFoundError

            raise NotFoundError(
                f"document {document_id} has no revision in force, so there is nothing to "
                "circulate. Issue one first"
            )
        version_id = str(row[0])
    result = await distribute(
        ctx.session,
        organization_id=ctx.organization_id,
        document_version_id=version_id,
        audience_role_key=body.audience_role_key,
        channel=body.channel,
        is_mandatory=body.is_mandatory,
        distributed_at=body.distributed_at or dt.datetime.now(tz=dt.UTC),
        note=body.note,
        source_actor=body.source_actor or str(ctx.actor.id),
        proposal_id=body.proposal_id,
    )
    return {"document_id": document_id, **result}


@router.post("/documents/{document_id}/acknowledge")
async def read(
    document_id: str,
    body: AcknowledgeRequest,
    ctx: ApiContext = Depends(get_context),
) -> dict[str, Any]:
    """Record that somebody has read it, by name.

    Idempotent in the direction that matters: acknowledging twice keeps the first
    acknowledgement rather than moving it, because "when did they actually read it" is the
    only reason the column has a timestamp at all. A second press keeping the *later* time
    would make the time-to-acknowledge figure measure button presses.
    """
    require_body_tenant(body.model_dump(), ctx)
    result = await acknowledge(
        ctx.session,
        organization_id=ctx.organization_id,
        distribution_id=body.distribution_id,
        acknowledged_by=body.acknowledged_by,
        acknowledged_at=body.acknowledged_at or dt.datetime.now(tz=dt.UTC),
    )
    return {"document_id": document_id, **result}
