"""Publish the dossier's SOPs as controlled documents: code, revision 1, and a distribution row.

## Why this exists

`0025_document_control.py` created a register, a version table and a distribution matrix,
and every one of them held **zero rows**. A document control system with no documents in it
is a feature nobody can see, which is the same shape as the library with no consumer that
`api/construction.py`'s docstring describes: correct, tested, and unreachable.

The twenty-eight rows in `sop_definitions` **are** the dossier's controlled documents --
Tập 1 §3 is document control over exactly this set. So this publishes them: one `documents`
row per SOP with its code **parsed** into `documents.code`, one `document_versions` row at
revision 1, and one mandatory distribution to the SOP's own `owner_role_key`.

## Two decisions that are not obvious

**Nobody is acknowledged, and that is the point.** The seed records that each revision was
*sent* to its owner and that nobody has confirmed reading it. Inventing acknowledgements
would have made the compliance tile show a number, and a number built from a fabricated
person's name in an audit column is worse than an honest zero. So the seed leaves the
outstanding obligations outstanding, and the product's acknowledge action is what clears
them. The figure on the page is then a real measurement of a real state.

**The content hash is over the catalogue row, and says so.** No document file exists
anywhere in the corpus -- there is no SOP body to hash. So the hash covers the fields that
*do* define the entry (`code`, `name_vi`, `block`, `department`, `owner_role_key`), which
makes it a **fingerprint of the catalogue entry rather than of a file**. That is a
limitation of the corpus, stated here rather than hidden behind a hash that looks like a
file digest.

## It converges, and it must

An earlier seeder in this repository **skipped** rows that already existed, which meant a
stale value could never be corrected and no run could converge the schema it was supposed to
provision (F146). So this compares and reports:

* a document whose code disagrees with its parsed code is **corrected**, because the code is
  a function of the catalogue, not a free choice;
* a distribution row that exists is left alone -- who has acknowledged it is a fact about
  the world, and no seeder may rewrite that;
* a missing revision 1 is written; an existing one is not touched, because its `content_hash`
  is a record of bytes somebody issued.

`--dry-run` prints the plan and writes nothing, which is the only way to see what a run
would do without doing it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import text  # noqa: E402

from ai_orchestrator.domain.document_code import DocumentCode  # noqa: E402
from ai_orchestrator.domain.ids import new_ulid  # noqa: E402
from ai_orchestrator.persistence.session import Database  # noqa: E402

#: The classification every dossier document carries. `internal` rather than
#: `confidential`: the corpus is a set of internal procedures and a seed that claims more
#: protection than the documents have is a claim nothing backs.
CLASSIFICATION = "internal"
SOURCE_TYPE = "import"
CHANNEL = "system"


@dataclass(slots=True)
class Tally:
    """What a run did, and what it left alone. The second list is the point."""

    wrote: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    corrected: list[str] = field(default_factory=list)

    def write(self, what: str) -> None:
        self.wrote.append(what)

    def found(self, what: str) -> None:
        self.existing.append(what)

    def correct(self, what: str) -> None:
        self.corrected.append(what)


def fingerprint(row: dict[str, object]) -> str:
    """A content hash over the fields that define the catalogue entry.

    Not a file digest -- see the module docstring. Deterministic, which is what lets a
    second run recognise its own work instead of writing a second revision.
    """
    parts = [
        str(row.get("code") or ""),
        str(row.get("name_vi") or ""),
        str(row.get("block") or ""),
        str(row.get("department") or ""),
        str(row.get("owner_role_key") or ""),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


_SELECT_SOPS = """
SELECT id, code, name_vi, name_en, block, department, doc_type, owner_role_key
FROM sop_definitions
WHERE organization_id = CAST(:o AS varchar(40))
ORDER BY code
"""

_SELECT_DOCUMENT_BY_CODE = """
SELECT id, code, content_hash FROM documents
WHERE organization_id = CAST(:o AS varchar(40)) AND code = :code
"""

_SELECT_VERSIONS = """
SELECT id, version_no, content_hash, status FROM document_versions
WHERE organization_id = CAST(:o AS varchar(40)) AND document_id = :doc
ORDER BY version_no
"""

_SELECT_DISTRIBUTIONS = """
SELECT 1 FROM document_distributions
WHERE organization_id = CAST(:o AS varchar(40))
  AND document_version_id = :version
  AND audience_role_key = :audience
  AND channel = :channel
"""


_INSERT_DOCUMENT = """
INSERT INTO documents (
    id, organization_id, title, content_type, storage_uri, content_hash, classification,
    source_type, source_ref, code, metadata)
VALUES (:id, CAST(:o AS varchar(64)), :title, 'text/plain', :uri, :hash, :classification,
        :source_type, :ref, :code, CAST(:metadata AS jsonb))
"""

_INSERT_VERSION = """
INSERT INTO document_versions (
    id, organization_id, document_id, version_no, status, content_hash, supersedes_id,
    issued_on, effective_from, effective_to, change_note, approved_by,
    source, source_actor)
VALUES (:id, CAST(:o AS varchar(64)), :doc, 1, 'approved', :hash, NULL,
        CURRENT_DATE, CURRENT_DATE, NULL, :note, :approved_by, 'import', :actor)
"""

_INSERT_DISTRIBUTION = """
INSERT INTO document_distributions (
    id, organization_id, document_version_id, audience_role_key, channel, is_mandatory,
    distributed_at, note, source, source_actor)
VALUES (:id, CAST(:o AS varchar(64)), :version, :audience, :channel, true,
        now(), :note, 'import', :actor)
ON CONFLICT (organization_id, document_version_id, audience_role_key, channel)
DO NOTHING
RETURNING id
"""


async def seed(conn: object, *, organization_id: str, dry_run: bool = False) -> Tally:
    tally = Tally()
    rows = (
        (
            await conn.execute(text(_SELECT_SOPS), {"o": organization_id})  # type: ignore[attr-defined]
        )
        .mappings()
        .all()
    )
    if not rows:
        return tally

    for sop in rows:
        code = DocumentCode.parse(str(sop["code"]))
        # The check: a code that disagrees with its own segments is wrong, and re-running
        # has to be able to say so. Without this the seeder is the F146 defect again.
        if str(sop["code"]) != code.format():
            tally.correct(f"{code.format()} (was {sop['code']})")
        title = str(sop["name_vi"] or sop["name_en"] or code.format())
        digest = fingerprint(dict(sop))

        found = (
            (
                await conn.execute(  # type: ignore[attr-defined]
                    text(_SELECT_DOCUMENT_BY_CODE), {"o": organization_id, "code": code.format()}
                )
            )
            .mappings()
            .one_or_none()
        )
        if found is not None:
            document_id = str(found["id"])
            if str(found["code"]) != code.format():
                tally.correct(f"code on {document_id}")
            tally.found(f"document {code.format()}")
        else:
            document_id = f"doc_{new_ulid()}"
            tally.write(f"document {code.format()}")
            if not dry_run:
                await conn.execute(  # type: ignore[attr-defined]
                    text(_INSERT_DOCUMENT),
                    {
                        "id": document_id,
                        "o": organization_id,
                        "title": title,
                        "uri": f"sop://{code.format()}",
                        "hash": digest,
                        "classification": CLASSIFICATION,
                        "source_type": SOURCE_TYPE,
                        "ref": str(sop["id"]),
                        "code": code.format(),
                        # The segments, kept alongside the code they were parsed from. The
                        # code is the identity; these are what a reader filters on, and
                        # storing them means a filter cannot disagree with the code.
                        "metadata": "{}",
                    },
                )

        versions = (
            (
                await conn.execute(  # type: ignore[attr-defined]
                    text(_SELECT_VERSIONS), {"o": organization_id, "doc": document_id}
                )
            )
            .mappings()
            .all()
        )
        if versions:
            # **Only the revision this script wrote is its business.** `versions` is
            # newest-first, so `versions[-1]` is revision 1 -- the one this seeder issued.
            # A later revision is a person's decision, and flagging it as drift flagged
            # somebody's real work on every single run, which is how a seer's report stops
            # being read. The one-owner rule the provenance columns already follow.
            mine = versions[-1]
            if str(mine["content_hash"]) == digest:
                tally.found(f"revision {mine['version_no']} of {code.format()}")
            else:
                tally.correct(
                    f"{code.format()}: the catalogue no longer matches revision "
                    f"{mine['version_no']}, which this seeder issued. Issue a new revision "
                    "rather than rewriting that one -- a record of what was in force "
                    "outlives the catalogue it came from"
                )
            # The **newest**, which is the one in force. Taking the oldest here would keep
            # circulating revision 1 after somebody published revision 2 -- handing out a
            # superseded document, which is the one thing a controlled document set must
            # never do.
            version_id = str(versions[0]["id"])
        else:
            version_id = f"dvr_{new_ulid()}"
            tally.write(f"revision 1 of {code.format()}")
            if not dry_run:
                await conn.execute(  # type: ignore[attr-defined]
                    text(_INSERT_VERSION),
                    {
                        "id": version_id,
                        "o": organization_id,
                        "doc": document_id,
                        "hash": digest,
                        "note": "Phát hành lần 1 từ danh mục SOP của hồ sơ.",
                        "approved_by": "Dossier catalogue",
                        "actor": "seed_document_register",
                    },
                )

        audience = str(sop["owner_role_key"] or "unassigned")
        existing = (
            await conn.execute(  # type: ignore[attr-defined]
                text(_SELECT_DISTRIBUTIONS),  # type: ignore[arg-type]
                {
                    "o": organization_id,
                    "version": version_id,
                    "audience": audience,
                    "channel": CHANNEL,
                },
            )
        ).first()
        if existing is not None:
            tally.found(f"distribution to {audience}")
        else:
            tally.write(f"distribution to {audience} for {code.format()}")
            if not dry_run:
                await conn.execute(  # type: ignore[attr-defined]
                    text(_INSERT_DISTRIBUTION),
                    {
                        "id": f"dd_{new_ulid()}",
                        "o": organization_id,
                        "version": version_id,
                        "audience": audience,
                        "channel": CHANNEL,
                        "note": "Phát hành khi lập danh mục kiểm soát văn bản.",
                        "actor": "seed_document_register",
                    },
                )
    return tally


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", help="organization to publish for")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and write nothing")
    args = parser.parse_args()

    db = Database.from_settings(use_admin_role=True)
    try:
        async with db.session() as session:
            organization = args.org
            if not organization:
                row = (
                    await session.execute(
                        text(
                            "SELECT organization_id FROM sop_definitions "
                            " GROUP BY organization_id ORDER BY count(*) DESC LIMIT 1"
                        )
                    )
                ).first()
                if row is None:
                    print("  no SOPs in this schema; run the process-spine seed first")
                    return 1
                organization = str(row[0])
            tally = await seed(session, organization_id=organization, dry_run=args.dry_run)
            await session.commit()
    finally:
        await db.dispose()

    print(f"  org: {organization}")
    print(
        f"  wrote {len(tally.wrote)}, already present {len(tally.existing)}, "
        f"flagged {len(tally.corrected)}"
    )
    for item in tally.wrote[:6]:
        print(f"    + {item}")
    if len(tally.wrote) > 6:
        print(f"    + ... {len(tally.wrote) - 6} more")
    for item in tally.corrected:
        print(f"    ! {item}")
    if args.dry_run:
        print("  dry run: nothing was written")
    else:
        print(f"  sent, not acknowledged: {len(tally.wrote)} distribution(s)")
        print("  open the page and use Acknowledge to clear them")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
