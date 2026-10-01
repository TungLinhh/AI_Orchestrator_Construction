"""Document control, as operations: publish a document, version it, distribute it, acknowledge it.

## Why this module exists

`0025_document_control.py` gave the platform three tables and the product could not see any
of them. There was a register with a code nobody could enter, a version lineage with no
writer, and a distribution matrix with nothing to distribute. A person holding a controlled
document set has one question — *"does the site manager have revision 4?"* — and before
this module the answer required a SQL client.

So this is the path that makes the tables mean something, and it is a **writer**, not
another read. The rest of the construction API is deliberately read-only, and this module
is the deliberate exception: distributing a document and acknowledging one are *acts*, and an
act that only a script can perform is an act nobody performs.

## The four operations, and what each one is for

| operation | the act | what would be wrong without it |
|---|---|---|
| `document_register` | the list a person reads | a register nobody can read |
| `document_detail` | lineage, matrix, compliance | the audit question is unanswerable |
| `issue_version` | a revision goes into force | no history, so "the tenth" cannot be answered |
| `distribute` + `acknowledge` | who must hold it, and whether they have | a paper exercise |

## Three rules the operations hold that the schema only *could*

**Issuing a version closes the previous one.** `document_versions` carries
`effective_from` / `effective_to` and a `superseded` status, and nothing in the database
makes them agree. So `issue_version` reads the version currently in force and ends it in
the *same transaction* as it starts the new one. A version table where a superseded row
has no end date is a table that cannot answer "which revision was in force on the tenth" —
and the `CHECK` refuses a `superseded` row without an end date only if something sets one.

**A re-send updates the obligation.** `UNIQUE (organization_id, document_version_id,
audience_role_key, channel)` means one row per target, so a retry is an `ON CONFLICT ...
DO UPDATE`. Without it, retrying a failed distribution doubles a mandatory count, and a
compliance figure that goes up when you retry it is worse than no figure at all.

**An acknowledgement is refused in words, not in a constraint name.** The table refuses an
acknowledgement with nobody behind it, one dated before its distribution, and one that
arrives for a row never sent. All three are real states a person will produce by accident,
and a `CheckViolationError` naming `ck_document_distributions_...` is not an answer to
"why did my button do nothing". Each refusal here says what to do instead.

## The audience vocabulary is the dossier's, not this module's

`audience_role_key` holds a `sop_definitions.owner_role_key` — `chief_accountant`,
`project_manager`, and so on. Not a `roles.name`, and not a new vocabulary: the point of a
distribution matrix is that the SOP's owner and its audience are the same word, and two
lists for one concept is how a matrix ends up with three spellings of "site manager".

It is a `varchar` with no foreign key, deliberately. `roles` is keyed by `id` and named in
English, and a distribution list has to survive a role being renamed — a matrix that loses
its history when somebody fixes a job title is not an audit trail.

## Provenance on the two that change what a person must do

`issue_version` and `distribute` take `source` / `source_actor` / `proposal_id`, and an
`agent_proposal` version additionally requires `approval_id` — the same rule
`procedure_operations.propose_version` holds, and for the same reason. Tập 3 §4.1 says an
agent is never Accountable, and that is a constraint here rather than a review convention:
an agent may propose a revision, and a person signs it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from ai_orchestrator.application.ports import ReadConnection
from ai_orchestrator.domain.document_code import Block, DocumentCode
from ai_orchestrator.domain.errors import NotFoundError, ValidationError
from ai_orchestrator.domain.ids import new_ulid

#: The register. Left-joined to the version in force and to the distribution count, because
#: the two questions a reader asks of a register are *"which revision is live"* and *"has
#: everybody got it"* — and a register that answers neither is a list of filenames.
#:
#: `live_version_no` and `mandatory_total` / `mandatory_acknowledged` come from the same
#: grouped subquery so the three cannot disagree with each other on one row.
_REGISTER = """
SELECT d.id, d.code, d.title, d.classification, d.expires_on, d.retention_until,
       d.source_type, d.updated_at,
       v.version_no       AS live_version_no,
       v.effective_from   AS live_effective_from,
       m.mandatory_total, m.mandatory_acknowledged
FROM documents d
LEFT JOIN LATERAL (
    SELECT version_no, effective_from
    FROM document_versions
    WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = d.id
      AND status = 'approved'
      AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
    ORDER BY version_no DESC
    LIMIT 1
) v ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE is_mandatory) AS mandatory_total,
           count(*) FILTER (WHERE is_mandatory AND acknowledged_at IS NOT NULL)
               AS mandatory_acknowledged
    FROM document_distributions
    WHERE organization_id = CAST(:o AS varchar(40))
      AND document_version_id IN (
          SELECT id FROM document_versions
          WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = d.id)
) m ON true
WHERE d.organization_id = CAST(:o AS varchar(40)) AND d.is_deleted = false
  AND (CAST(:block AS text) IS NULL OR d.code LIKE 'ONX-' || :block || '-%')
  AND (CAST(:department AS text) IS NULL OR d.code LIKE 'ONX-%-' || :department || '-%')
  AND (CAST(:q AS text) IS NULL OR d.title ILIKE '%' || :q || '%' OR d.code ILIKE '%' || :q || '%')
ORDER BY d.code NULLS LAST, d.title
LIMIT :limit OFFSET :offset
"""

_REGISTER_COUNT = """
SELECT count(*) FROM documents d
WHERE d.organization_id = CAST(:o AS varchar(40)) AND d.is_deleted = false
  AND (CAST(:block AS text) IS NULL OR d.code LIKE 'ONX-' || :block || '-%')
  AND (CAST(:department AS text) IS NULL OR d.code LIKE 'ONX-%-' || :department || '-%')
  AND (CAST(:q AS text) IS NULL OR d.title ILIKE '%' || :q || '%' OR d.code ILIKE '%' || :q || '%')
"""

#: One document with its full lineage. The versions come back **newest first** because the
#: question is "what is live, and what came before it", and a reader who has to scroll to
#: the bottom to learn which revision is current has been asked the wrong question.
_DETAIL = """
SELECT id, title, code, classification, content_type, storage_uri, content_hash,
       source_type, source_ref, expires_on, retention_until, created_at, updated_at
FROM documents
WHERE organization_id = CAST(:o AS varchar(40)) AND id = :id AND is_deleted = false
"""

_VERSIONS = """
SELECT id, version_no, status, content_hash, issued_on, effective_from, effective_to,
       change_note, approved_by, supersedes_id
FROM document_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = :id
ORDER BY version_no DESC
"""

#: The matrix, joined to the version it distributes, so a reader sees *which* revision an
#: audience is behind rather than a list of obligations with no subject.
_DISTRIBUTIONS = """
SELECT dd.id, dd.document_version_id, dd.audience_role_key, dd.channel, dd.is_mandatory,
       dd.distributed_at, dd.acknowledged_at, dd.acknowledged_by, dd.note,
       dv.version_no
FROM document_distributions dd
JOIN document_versions dv
  ON dv.organization_id = dd.organization_id AND dv.id = dd.document_version_id
WHERE dd.organization_id = CAST(:o AS varchar(40)) AND dv.document_id = :id
ORDER BY dv.version_no DESC, dd.is_mandatory DESC, dd.audience_role_key
"""

#: The version in force, read *before* anything is written so the identifiers being
#: superseded are known rather than inferred. Same reason as `procedure_operations`:
#: a single `UPDATE ... WHERE status = 'approved'` cannot report what it touched without
#: `RETURNING`, and mixing `RETURNING` into a multi-statement transaction makes the failure
#: modes harder to read than three separate statements.
_VERSION_IN_FORCE = """
SELECT id, version_no, effective_from
FROM document_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = :id
  AND status = 'approved' AND effective_to IS NULL
ORDER BY version_no DESC
LIMIT 1
"""

_NEXT_VERSION_NO = """
SELECT COALESCE(max(version_no), 0) + 1 FROM document_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = :id
"""

_END_THE_LIVE_VERSION = """
UPDATE document_versions
SET status = 'superseded',
    effective_to = COALESCE(:on_date, CURRENT_DATE),
    updated_at = now()
WHERE organization_id = CAST(:o AS varchar(64))
  AND id = :version
  AND status = 'approved'
  AND effective_to IS NULL
"""

_INSERT_VERSION = """
INSERT INTO document_versions (
    id, organization_id, document_id, version_no, status, content_hash, supersedes_id,
    issued_on, effective_from, change_note, approved_by, approval_id,
    source, source_actor, proposal_id)
VALUES (:id, CAST(:o AS varchar(64)), :document, :no, :status, :hash, :supersedes,
        :issued, :effective, :note, :approved_by, :approval,
        :source, :actor, :proposal)
"""

_INSERT_DISTRIBUTION = """
INSERT INTO document_distributions (
    id, organization_id, document_version_id, audience_role_key, channel, is_mandatory,
    distributed_at, note, source, source_actor, proposal_id)
VALUES (:id, CAST(:o AS varchar(64)), :version, :audience, :channel, :mandatory,
        :sent, :note, :source, :actor, :proposal)
ON CONFLICT (organization_id, document_version_id, audience_role_key, channel)
DO UPDATE SET distributed_at = EXCLUDED.distributed_at,
              note = EXCLUDED.note,
              updated_at = now()
RETURNING id
"""

#: `RETURNING` names every column the function reads back. The first version returned
#: `distributed_at, acknowledged_by` and then asked for `row["acknowledged_at"]`, which
#: SQLAlchemy reports as `NoSuchColumnError: Could not locate column in row` -- a 500 on
#: the one endpoint whose whole job is to say whether somebody has already read a document.
#: Idempotence *is* the acknowledgement timestamp, so that column has to come back.
#: `AND acknowledged_at IS NULL` **is** the idempotence guard, and it has to be in the
#: `WHERE` rather than in a branch after the write.
#:
#: The first version updated unconditionally and then asked `if row["acknowledged_at"]` --
#: so the second press had already overwritten the first reader's name and timestamp before
#: anything could look at them, and the function cheerfully reported the *new* values as
#: `already_acknowledged: true`. The check was after the write, so it could only ever be
#: wrong. A guard that runs after the effect is not a guard.
_ACK_ONE = """
UPDATE document_distributions
SET acknowledged_at = :at, acknowledged_by = :by, updated_at = now()
WHERE organization_id = CAST(:o AS varchar(64)) AND id = :id
  AND acknowledged_at IS NULL
RETURNING distributed_at, acknowledged_at, acknowledged_by
"""

#: Read the row as it stands, for the report-only path. Same columns, no `SET`.
_ACK_READ = """
SELECT distributed_at, acknowledged_at, acknowledged_by
FROM document_distributions
WHERE organization_id = CAST(:o AS varchar(64)) AND id = :id
"""

_CHANNELS = ("system", "email", "print", "signage", "induction")


@dataclass(frozen=True, slots=True)
class Compliance:
    """How much of a document actually reached the people it is required to reach.

    `acknowledged` is the only figure that counts. A distribution that was *sent* is an
    intention, and the difference between the two is the whole reason this table exists —
    a controlled document set that has been emailed to everybody and read by nobody is the
    most common state of a controlled document set in construction, and it is invisible
    without a column somebody is required to fill in.
    """

    required: int
    acknowledged: int

    @property
    def outstanding(self) -> int:
        return self.required - self.acknowledged

    @property
    def percent(self) -> int:
        if not self.required:
            return 100
        return round(100 * self.acknowledged / self.required)

    def as_dict(self) -> dict[str, int]:
        return {
            "required": self.required,
            "acknowledged": self.acknowledged,
            "outstanding": self.outstanding,
            "percent": self.percent,
        }


def _parse_code(code: str | None) -> DocumentCode | None:
    """The register's code, parsed, or `None` when the document has no number.

    A `NULL` is a real state: `documents` also holds an uploaded attachment with no dossier
    number, and inventing one would be a lie in the identifier column. A **malformed**
    non-null code is a data fault and is allowed to raise -- the register is a read path
    and a code it cannot parse is a code a person is looking at and cannot understand.
    """
    return None if code is None else DocumentCode.parse(str(code))


def _register_row(row: Any) -> dict[str, Any]:
    code = _parse_code(row["code"])
    required = int(row["mandatory_total"] or 0)
    acknowledged = int(row["mandatory_acknowledged"] or 0)
    return {
        "id": row["id"],
        "code": row["code"],
        "block": code.block.value if code else None,
        "block_name": code.block.name_vi if code else None,
        "department": code.department if code else None,
        "title": row["title"],
        "classification": row["classification"],
        "expires_on": row["expires_on"],
        "retention_until": row["retention_until"],
        "live_version_no": row["live_version_no"],
        "live_effective_from": row["live_effective_from"],
        "compliance": Compliance(required, acknowledged).as_dict(),
    }


async def document_register(
    conn: ReadConnection,
    *,
    organization_id: str,
    block: str | None = None,
    department: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """The controlled document register, newest code first.

    `block` and `department` filter on the **parsed** prefix rather than a `LIKE` a caller
    composes, so the filter and the code cannot disagree about what a block is. A caller
    that searched `ONX-BO-%` would match a document in a block called `BOGUS` as well, and
    the difference only shows up on the day somebody adds one.

    Both are **upper-cased here**, at the one place the filter is built, so the operation
    behaves the same whether it is called by the API or by a script. Normalising in the
    route as well as here would be two places, and the first version had the route doing it
    -- so a direct call with `block="bo"` returned an empty list, which is a quiet wrong
    answer rather than an error. This matches `DocumentCode.is_in`, which is deliberately
    case-tolerant for exactly this reason.
    """
    block = block.strip().upper() if block is not None else None
    department = department.strip().upper() if department is not None else None
    total = int(
        (
            await conn.execute(
                text(_REGISTER_COUNT),
                {"o": organization_id, "block": block, "department": department, "q": q},
            )
        ).scalar_one()
    )
    rows = (
        (
            await conn.execute(
                text(_REGISTER),
                {
                    "o": organization_id,
                    "block": block,
                    "department": department,
                    "q": q,
                    "limit": limit,
                    "offset": offset,
                },
            )
        )
        .mappings()
        .all()
    )
    return {
        "items": [_register_row(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def document_detail(
    conn: ReadConnection, *, organization_id: str, document_id: str
) -> dict[str, Any]:
    """One document: its lineage, its distribution matrix, and its compliance.

    A 404 for another tenant's id, not a 403. Saying "forbidden" would confirm the row
    exists, and the whole reason the composite keys exist is that a reader must not be able
    to tell the difference.
    """
    head = (
        (await conn.execute(text(_DETAIL), {"o": organization_id, "id": document_id}))
        .mappings()
        .one_or_none()
    )
    if head is None:
        raise NotFoundError(f"no document {document_id}")

    versions = (
        (await conn.execute(text(_VERSIONS), {"o": organization_id, "id": document_id}))
        .mappings()
        .all()
    )
    distributions = (
        (await conn.execute(text(_DISTRIBUTIONS), {"o": organization_id, "id": document_id}))
        .mappings()
        .all()
    )

    code = _parse_code(head["code"])
    live = next((v for v in versions if v["status"] == "approved"), None)
    live_no = live["version_no"] if live else None
    # Compliance is measured against the revision **in force**, not against every
    # obligation the document has ever carried. A reader who acknowledged revision 1 and
    # has not seen revision 4 has not acknowledged the current document, and a figure that
    # credits them for the old one is the number an audit rejects.
    required = [d for d in distributions if d["is_mandatory"] and d["version_no"] == live_no]
    acknowledged = [d for d in required if d["acknowledged_at"] is not None]

    return {
        "id": head["id"],
        "code": head["code"],
        "block": code.block.value if code else None,
        "department": code.department if code else None,
        "title": head["title"],
        "classification": head["classification"],
        "content_type": head["content_type"],
        "storage_uri": head["storage_uri"],
        "content_hash": head["content_hash"],
        "source_type": head["source_type"],
        "expires_on": head["expires_on"],
        "retention_until": head["retention_until"],
        "created_at": head["created_at"],
        "updated_at": head["updated_at"],
        "live_version_no": live["version_no"] if live else None,
        "compliance": Compliance(len(required), len(acknowledged)).as_dict(),
        "versions": [
            {
                "id": v["id"],
                "version_no": v["version_no"],
                "status": v["status"],
                "content_hash": v["content_hash"],
                "issued_on": v["issued_on"],
                "effective_from": v["effective_from"],
                "effective_to": v["effective_to"],
                "change_note": v["change_note"],
                "approved_by": v["approved_by"],
                "supersedes_id": v["supersedes_id"],
            }
            for v in versions
        ],
        "distributions": [
            {
                "id": d["id"],
                "document_version_id": d["document_version_id"],
                "version_no": d["version_no"],
                "audience_role_key": d["audience_role_key"],
                "channel": d["channel"],
                "is_mandatory": d["is_mandatory"],
                "distributed_at": d["distributed_at"],
                "acknowledged_at": d["acknowledged_at"],
                "acknowledged_by": d["acknowledged_by"],
                "note": d["note"],
            }
            for d in distributions
        ],
    }


async def issue_version(
    conn: ReadConnection,
    *,
    organization_id: str,
    document_id: str,
    content_hash: str,
    change_note: str,
    issued_on: dt.date,
    effective_from: dt.date | None = None,
    approved_by: str = "",
    source: str = "human",
    source_actor: str = "",
    proposal_id: str | None = None,
    approval_id: str | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Write the next revision, and close the one it replaces.

    The number is `max + 1` rather than supplied, so two concurrent issuances cannot claim
    the same one; the `UNIQUE (organization_id, document_id, version_no)` is what actually
    enforces it and this is the friendly path.

    `activate = False` writes the version in `draft`, which is the ordinary state of a
    revision nobody has approved yet. An `approved` version that was never approved by
    anybody is exactly the thing `ck_document_versions_agent_needs_approval` exists to
    refuse for an agent, and this is the same rule for a person.
    """
    if not change_note.strip():
        raise ValidationError(
            "a revision with no change note is unreviewable: the next reader cannot tell "
            "what moved without re-reading both revisions side by side"
        )
    if source == "agent_proposal" and not approval_id:
        raise ValidationError(
            "an agent-proposed revision must cite the approval that authorised it. "
            "Tập 3 §4.1 -- an agent is never Accountable -- and the table refuses it too"
        )

    exists = (
        await conn.execute(
            text(
                "SELECT 1 FROM documents WHERE organization_id = CAST(:o AS varchar(64)) "
                " AND id = :id"
            ),
            {"o": organization_id, "id": document_id},
        )
    ).first()
    if exists is None:
        raise NotFoundError(f"no document {document_id}")

    next_no = int(
        (
            await conn.execute(text(_NEXT_VERSION_NO), {"o": organization_id, "id": document_id})
        ).scalar_one()
    )
    in_force = (
        (await conn.execute(text(_VERSION_IN_FORCE), {"o": organization_id, "id": document_id}))
        .mappings()
        .one_or_none()
    )

    # A revision cannot take effect **before** the one it replaces.
    #
    # Found by walking every write the page makes: issuing a revision dated earlier than the
    # current one produced a 500 --
    # `CheckViolationError ... violates check constraint
    # "ck_document_versions_the_window_is_not_inverted"` -- because ending the live version
    # at the new revision's date gives it an `effective_to` before its own `effective_from`.
    #
    # That is the operation's fault and not the constraint's: the constraint is telling the
    # truth about an impossible window, and the operation should never have asked for one. A
    # person would also have no idea what to do with a 500, so the refusal says what to do.
    effective = effective_from or issued_on
    if (
        in_force is not None
        and in_force["effective_from"] is not None
        and effective < in_force["effective_from"]
    ):
        raise ValidationError(
            f"this revision would take effect on {effective.isoformat()}, before the "
            f"revision it replaces, which is in force from "
            f"{in_force['effective_from'].isoformat()}. A revision that starts earlier "
            "than its predecessor leaves the document in force for no one in the gap. "
            "Pick a date on or after it, or use a backdated note in the change note."
        )

    # A revision that goes into force before the one it replaces would leave two
    # "approved" versions and make "which is in force" depend on which row a reader found
    # first. The order of the two writes is: read, end, insert -- inside one transaction, so
    # a failure at any point leaves the previous version in force.
    if activate and in_force is not None:
        await conn.execute(
            text(_END_THE_LIVE_VERSION),
            {
                "o": organization_id,
                "version": in_force["id"],
                "on_date": effective,
            },
        )

    version_id = f"dvr_{new_ulid()}"
    await conn.execute(
        text(_INSERT_VERSION),
        {
            "id": version_id,
            "o": organization_id,
            "document": document_id,
            "no": next_no,
            "status": "approved" if activate else "draft",
            "hash": content_hash,
            "supersedes": in_force["id"] if in_force is not None else None,
            "issued": issued_on,
            "effective": effective,
            "note": change_note.strip(),
            "approved_by": approved_by,
            "approval": approval_id,
            "source": source,
            "actor": source_actor,
            "proposal": proposal_id,
        },
    )
    return {
        "version_id": version_id,
        "version_no": next_no,
        "superseded": in_force["id"] if in_force is not None else None,
        "status": "approved" if activate else "draft",
    }


async def distribute(
    conn: ReadConnection,
    *,
    organization_id: str,
    document_version_id: str,
    audience_role_key: str,
    channel: str = "system",
    is_mandatory: bool = True,
    distributed_at: dt.datetime,
    note: str = "",
    source: str = "human",
    source_actor: str = "",
    proposal_id: str | None = None,
) -> dict[str, Any]:
    """Tell an audience a revision exists, and record that they were told.

    A re-send to the same target on the same channel is an **update of the same
    obligation**, not a second one. That is what the `ON CONFLICT` is for: a retried
    distribution that inserted a row would double a mandatory count, and a compliance figure
    that rises when you retry it is not a measurement of anything.
    """
    if channel not in _CHANNELS:
        raise ValidationError(f"{channel!r} is not a channel. One of: {', '.join(_CHANNELS)}.")
    if not audience_role_key.strip():
        raise ValidationError("a distribution with no audience is a note, not a distribution")
    if distributed_at.tzinfo is None:
        raise ValidationError("distributed_at must be timezone-aware; see `acknowledge`")

    version = (
        (
            await conn.execute(
                text(
                    "SELECT id, status FROM document_versions "
                    " WHERE organization_id = CAST(:o AS varchar(64)) AND id = :id"
                ),
                {"o": organization_id, "id": document_version_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if version is None:
        raise NotFoundError(f"no document version {document_version_id}")
    if version["status"] not in ("approved", "in_review"):
        raise ValidationError(
            f"a {version['status']} revision cannot be circulated. The point of a "
            "controlled document is that people know which revision they are holding, and "
            "handing out a draft defeats that"
        )

    row = await conn.execute(
        text(_INSERT_DISTRIBUTION),
        {
            "id": f"dd_{new_ulid()}",
            "o": organization_id,
            "version": document_version_id,
            "audience": audience_role_key.strip(),
            "channel": channel,
            "mandatory": is_mandatory,
            "sent": distributed_at,
            "note": note,
            "source": source,
            "actor": source_actor,
            "proposal": proposal_id,
        },
    )
    return {
        "distribution_id": str(row.scalar_one()),
        "document_version_id": document_version_id,
        "audience_role_key": audience_role_key.strip(),
        "channel": channel,
        "is_mandatory": is_mandatory,
    }


async def acknowledge(
    conn: ReadConnection,
    *,
    organization_id: str,
    distribution_id: str,
    acknowledged_by: str,
    acknowledged_at: dt.datetime,
) -> dict[str, Any]:
    """Record that somebody has read it. **By name**, never anonymously.

    The three refusals are the table's, restated in words. A person pressing a button needs
    to know which of three things to fix, and `ck_document_distributions_...` is not an
    answer to that:

    * nobody behind it -- supply a name;
    * dated before the distribution -- a clock or a timezone fault, and it would otherwise
      make a lag figure negative without anybody noticing;
    * the row was never sent -- there is nothing to acknowledge.
    """
    if not acknowledged_by.strip():
        raise ValidationError(
            "an acknowledgement needs a name. 'Acknowledged' with nobody behind it is a "
            "tick in a box, and a tick in a box is what this column exists to avoid"
        )
    if acknowledged_at.tzinfo is None:
        raise ValidationError(
            f"acknowledged_at must be timezone-aware; got {acknowledged_at!r}. A naive "
            "datetime compared against a timestamptz is resolved against the *server's* "
            "zone, which is how an acknowledgement ends up dated before the thing it "
            "acknowledges"
        )

    # **Read, validate, then write.** In that order, and the order is the whole design.
    #
    # The first version wrote first and checked afterwards, which failed twice for the same
    # reason. `ck_document_distributions_acknowledged_after_distribution` fired before the
    # Python comparison could, so a person whose clock was wrong got a constraint name
    # instead of the sentence that would have told them which way round the two timestamps
    # are. And the idempotence check was after the write too, so the second press overwrote
    # the first reader's name and then reported the overwrite as `already_acknowledged`.
    #
    # A check that runs after the effect is not a check. So every refusal is decided from
    # the row as it stands, and the write that follows carries its own guard for the case
    # that changes underneath us.
    row = (
        (await conn.execute(text(_ACK_READ), {"o": organization_id, "id": distribution_id}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError(f"no distribution {distribution_id} in this organisation")
    if row["distributed_at"] is None:
        raise ValidationError(
            "this distribution was never sent, so there is nothing to acknowledge. Send it "
            "first -- a record that somebody read a document nobody gave them is worse "
            "than no record"
        )
    if row["acknowledged_at"] is not None:
        return {
            "distribution_id": distribution_id,
            "already_acknowledged": True,
            "acknowledged_at": row["acknowledged_at"],
            "acknowledged_by": row["acknowledged_by"],
        }
    if acknowledged_at < row["distributed_at"]:
        raise ValidationError(
            f"the acknowledgement is dated {acknowledged_at.isoformat()}, before the "
            f"distribution it acknowledges ({row['distributed_at'].isoformat()}). That is a "
            "clock fault, and it would make the time-to-acknowledge figure negative"
        )

    written = (
        (
            await conn.execute(
                text(_ACK_ONE),
                {
                    "o": organization_id,
                    "id": distribution_id,
                    "at": acknowledged_at,
                    "by": acknowledged_by.strip(),
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if written is not None:
        return {
            "distribution_id": distribution_id,
            "already_acknowledged": False,
            "acknowledged_at": written["acknowledged_at"],
            "acknowledged_by": written["acknowledged_by"],
        }

    # The `IS NULL` guard found the row already acknowledged: somebody pressed between our
    # read and our write. Report *their* record, not ours, which is the same answer the
    # early-return above gives.
    after = (
        (await conn.execute(text(_ACK_READ), {"o": organization_id, "id": distribution_id}))
        .mappings()
        .one()
    )
    return {
        "distribution_id": distribution_id,
        "already_acknowledged": True,
        "acknowledged_at": after["acknowledged_at"],
        "acknowledged_by": after["acknowledged_by"],
    }


def summarise(distributions: Sequence[dict[str, Any]], version_no: int | None) -> dict[str, Any]:
    """Compliance for one revision, from distribution rows already loaded.

    Exposed so the register and the detail view cannot compute the same figure two ways --
    which is how a dashboard and its own detail page end up disagreeing about the number
    that matters.
    """
    required = [
        d
        for d in distributions
        if d["is_mandatory"] and (version_no is None or d.get("version_no") == version_no)
    ]
    return Compliance(
        len(required), sum(1 for d in required if d["acknowledged_at"] is not None)
    ).as_dict()


__all__ = [
    "Block",
    "Compliance",
    "acknowledge",
    "distribute",
    "document_detail",
    "document_register",
    "issue_version",
    "summarise",
]
