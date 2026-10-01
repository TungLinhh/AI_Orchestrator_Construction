"""Read the corpus into the database, end to end, and report what it could not read.

`python -m scripts.ingest_construction --dry-run`

The pipeline, in the order the data depends on:

    project header  ->  projects            (ingest/project_reader  + project_operations)
    progress sheet  ->  wbs                (ingest/wbs_reader      + wbs_operations)
                     ->  progress_snapshots (ingest/progress_reader + progress_operations)

## Why this exists as a script and not as a test

The readers and the writers are both proved by tests. What is *not* proved by any test
is that the three fit together and produce a database somebody can use — and until
this ran, the construction side of the development schema had **zero rows**, so every
role-shaped read in `application/role_views.py` returned an empty list that was
indistinguishable from a broken one.

That is the same position the tender agent is in for a different reason, and the
script's real output is the numbers it prints at the end: sheets read, projects
written, nodes written, readings written, and **refused**.

## `--dry-run` is the default, and it is not a safety belt

A dry run reads everything and writes nothing, so a first run cannot damage a
database. But the interesting numbers are the same either way, because the refusals
come from the readers rather than from the writes. Running without `--dry-run` is the
only way to see whether the *writers* agree, and this script's job is to find that out.

## Idempotent, because a pipeline that has to be run once is a migration

Every write is keyed on a business code — the project's `code`, the version's
`(procedure_id, version_no)`, the reading's `(report_ref, section_label,
line_label)` — and the script **skips** rather than replaces. A progress report already
in the database is left alone, because `progress_operations.report_exists` exists for
precisely that question: replacing 110 rows is a different act from reading them, and
an operator should have to say so.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ai_orchestrator.application.progress_operations import (
    report_exists,
    write_report,
)
from ai_orchestrator.application.project_operations import (
    AddressResolution,
    observed_spellings,
    project_by_code,
    write_project,
)
from ai_orchestrator.application.wbs_operations import write_structure
from ai_orchestrator.ingest.progress_reader import read_progress
from ai_orchestrator.ingest.project_reader import (
    HeaderRead,
    read_corpus,
    read_project_header,
)
from ai_orchestrator.ingest.sheets import SheetKind, detect_sheet
from ai_orchestrator.ingest.wbs_reader import read_structure
from ai_orchestrator.persistence.session import Database

#: Where the reference corpus lives. **Overridable**, because the corpus is not in
#: this repository: it is a client's spreadsheets, and a hard-coded absolute path
#: means the script can only ever run on the machine it was written on.
#:
#: Without `AO_CORPUS_ROOT` the construction surfaces are empty and
#: `make seed-construction` says so. That is a better failure than a silent one,
#: and better still than shipping someone else's spreadsheets.
DEFAULT_CORPUS = Path(os.environ.get("AO_CORPUS_ROOT") or "corpus")

#: The progress sheets are found by their own content, not by filename, so a renamed
#: file is still read and a workbook that merely happens to be called "TĐ" is not.
_PROGRESS_SHEET = SheetKind.CONSTRUCTION_PROGRESS


class Report:
    """What the run did, and what it would not do. Printed, and returned."""

    def __init__(self) -> None:
        self.sheets_read = 0
        self.projects_found: dict[str, str] = {}
        self.projects_written = 0
        #: Codes already present, so a second run is a no-op rather than a crash.
        self.projects_skipped: list[str] = []
        self.nodes_written = 0
        self.nodes_refused: list[tuple[str, ...]] = []
        self.readings_written = 0
        #: Readings the reader produced and the writer refused. The number that says
        #: whether a file was understood, as against merely opened.
        self.readings_refused = 0
        self.reports_skipped: list[str] = []
        self.sheets_failed: list[str] = []
        #: Progress sheets whose project header could not be read, with the reasons.
        #: A sheet in this list is a project the corpus describes and the pipeline
        #: cannot place, which is a finding rather than a no-op.
        self.sheets_without_identity: list[tuple[str, tuple[tuple[str, str, str], ...]]] = []

    def as_dict(self) -> dict[str, object]:
        return {
            "sheets_read": self.sheets_read,
            "projects_found": len(self.projects_found),
            "projects_written": self.projects_written,
            "projects_skipped": self.projects_skipped,
            "nodes_written": self.nodes_written,
            "nodes_refused": len(self.nodes_refused),
            "readings_written": self.readings_written,
            "readings_refused": self.readings_refused,
            "reports_skipped": self.reports_skipped,
            "sheets_failed": self.sheets_failed,
            "sheets_without_identity": len(self.sheets_without_identity),
        }

    def render(self) -> str:
        d = self.as_dict()
        lines = [
            f"  sheets read                {d['sheets_read']}",
            f"  projects found             {d['projects_found']}",
            f"  projects written           {d['projects_written']}",
            f"  projects skipped (present) {len(self.projects_skipped)}",
            f"  wbs nodes written          {d['nodes_written']}",
            f"  wbs nodes refused          {d['nodes_refused']}",
            f"  progress readings written  {d['readings_written']}",
            f"  progress readings refused  {d['readings_refused']}",
            f"  reports skipped (present)  {len(self.reports_skipped)}",
            f"  sheets failed              {len(self.sheets_failed)}",
            f"  sheets w/o a project name  {len(self.sheets_without_identity)}",
        ]
        for name, reasons in self.sheets_without_identity[:8]:
            detail = "; ".join(f"{f}: {v!r}" for f, v, _ in reasons) or "no project name"
            lines.append(f"    no project {name}: {detail}")
        if len(self.sheets_without_identity) > 8:
            lines.append(f"    ... and {len(self.sheets_without_identity) - 8} more")
        for code, reasons in self.nodes_refused[:4]:
            lines.append(f"    refused {code}: {'; '.join(reasons)}")
        if len(self.nodes_refused) > 4:
            lines.append(
                f"    ... and {len(self.nodes_refused) - 4} more refusals of the same kinds"
            )
        for name in self.sheets_failed:
            lines.append(f"    failed  {name}")
        return "\n".join(lines)


def _rows_from(path: Path, sheet_name: str) -> list[tuple[object, ...]]:
    import openpyxl

    with warnings.catch_warnings():
        # openpyxl warns about headers/footers and conditional formatting it will
        # drop. Neither affects a header block or a progress table.
        warnings.simplefilter("ignore")
        book = openpyxl.load_workbook(path, data_only=True)
    try:
        sheet = book[sheet_name]
        return [tuple(r) for r in sheet.iter_rows(values_only=True)]
    finally:
        book.close()


async def _organisations(conn: AsyncConnection) -> list[str]:
    rows = (await conn.execute(text("SELECT id FROM organizations ORDER BY id"))).all()
    return [r[0] for r in rows]


@dataclass(frozen=True, slots=True)
class Survey:
    """Everything read from the corpus before any database work starts.

    Held in one object because the reading is **blocking** -- a filesystem walk and 59
    workbook parses -- and doing it inside the async section would park the event loop
    for the duration. The first version also called `read_corpus` three times, once for
    the survey and twice more for the headers, so the corpus was parsed three times to
    answer one set of questions.
    """

    headers: dict[str, HeaderRead]
    spellings: dict[str, tuple[str, ...]]
    sheets_with_identity: int
    books: tuple[Path, ...]


def survey_corpus(corpus: Path) -> Survey:
    """Read every workbook once and keep everything the writers need."""
    read_once = read_corpus(corpus)
    survey = read_once.survey()
    headers: dict[str, HeaderRead] = {}
    for read in read_once.reads:
        if read.accepted and read.header.project_name not in headers:
            headers[read.header.project_name] = read
    return Survey(
        headers=headers,
        spellings=observed_spellings(survey.findings),
        sheets_with_identity=survey.sheets_with_identity,
        books=tuple(sorted(corpus.rglob("*.xlsx"))),
    )


async def run(
    corpus: Path,
    *,
    dry_run: bool,
    report_ref: str,
    today: dt.datetime,
    org: str | None = None,
) -> Report:
    """Read the corpus and, unless `dry_run`, write it. Returns the report."""
    report = Report()
    # Every blocking read happens here, before the first `await`.
    survey = survey_corpus(corpus)
    seen_spellings = survey.spellings
    headers = survey.headers
    books = survey.books

    db = Database.from_settings()
    async with db.engine.connect() as probe:
        organisations = await _organisations(probe)
    if not organisations:
        report.sheets_failed.append("no organisations in the database; run `make seed` first")
        return report

    # **The organisation named, or the first one.** A pipeline that wrote the same
    # corpus into every tenant would be a very quiet way to fabricate a multi-tenant
    # demo, so the default is still "one tenant" rather than "all of them" -- but
    # "the first row of a table" is not the same as "the tenant you are looking at".
    #
    # Measured: the corpus landed in the oldest organisation on the machine, which
    # predates the three-tier seed, and the tenant the page opened showed an empty
    # Projects list and an empty Documents register. The page was not broken; it
    # was pointed at a tenant that had never been given any content.
    organization_id = org or organisations[0]

    if dry_run:
        report.projects_found = dict.fromkeys(headers)
        report.sheets_read = survey.sheets_with_identity
        return report

    # `tenant_session`, not a hand-rolled `set_config`. The binding is
    # transaction-local, so a `set_config` issued before a `commit()` is gone by the
    # time the next statement runs -- and the first version did exactly that, and every
    # write after the first commit died on
    # `InsufficientPrivilegeError: new row violates row-level security policy`. The
    # application's own helper re-establishes the binding per transaction, which is
    # the one thing that must not be reimplemented at a call site.
    report.projects_found = dict.fromkeys(headers)

    async with db.tenant_session(organization_id) as session:
        project_ids = await _write_projects(
            session, organization_id, headers, seen_spellings, today, report
        )
        for code, project_id in project_ids.items():
            await _write_progress(
                session,
                organization_id=organization_id,
                books=books,
                code=code,
                project_id=project_id,
                report_ref=f"{report_ref}:{code}",
                today=today,
                report=report,
            )
    return report
    return report


async def _write_projects(
    conn: AsyncConnection,
    organization_id: str,
    headers: dict[str, HeaderRead],
    spellings: dict[str, tuple[str, ...]],
    today: dt.datetime,
    report: Report,
) -> dict[str, str]:
    """One project per distinct name, with the address decision recorded.

    The address is settled with the **first spelling the corpus produced**, and the
    decision is attributed to the script with today's date. That is a real decision
    and it is a defensible one — first-seen is deterministic — but it is recorded as a
    decision rather than applied silently, so somebody can change it.
    """
    out: dict[str, str] = {}
    for index, (_name, read) in enumerate(sorted(headers.items()), start=1):
        alternatives = spellings.get("address", ())
        chosen = read.header.address or (alternatives[0] if alternatives else "")
        code = f"CORPUS-{index:02d}"
        # Skip rather than replace, as the module docstring promises. The first
        # version wrote unconditionally and the second run died on
        # `uq_projects_org_code` -- a pipeline that can only be run once is a
        # migration wearing a script's clothes.
        existing = await project_by_code(conn, organization_id=organization_id, code=code)
        if existing is not None:
            out[code] = str(existing["id"])
            report.projects_skipped.append(code)
            continue
        outcome = await write_project(
            conn,
            organization_id=organization_id,
            read=read,
            code=code,
            address=AddressResolution(
                chosen=chosen,
                seen=alternatives,
                decided_by="scripts/ingest_construction",
                decided_on=today,
            ),
            observed=spellings,
            written_on=today,
        )
        if outcome.written and outcome.project_id:
            out[code] = outcome.project_id
            report.projects_written += 1
        else:
            report.nodes_refused.append((code, outcome.refusals))
    return out


async def _write_progress(
    conn: AsyncConnection,
    *,
    organization_id: str,
    books: Sequence[Path],
    code: str,
    project_id: str,
    report_ref: str,
    today: dt.datetime,
    report: Report,
) -> None:
    """Find this project's progress sheets, read them, write the tree and the readings.

    The WBS is written **first** and the readings linked afterwards, because
    `wbs_operations` links them by `(report_ref, line_label)` — so the readings have to
    exist before the link can match. A project with a tree and no readings is a
    recoverable state; readings with nowhere to attach are not.
    """
    import openpyxl

    for path in books:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                book = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except Exception as exc:
            report.sheets_failed.append(f"{path.name}: {type(exc).__name__}")
            continue
        try:
            for sheet_name in book.sheetnames:
                rows = [
                    tuple(r)
                    for r in book[sheet_name].iter_rows(min_row=1, max_col=20, values_only=True)
                ]
                detection = detect_sheet(rows)
                if not any(s.kind is _PROGRESS_SHEET for s in detection.shapes):
                    continue
                report.sheets_read += 1
                shape = next(s for s in detection.shapes if s.kind is _PROGRESS_SHEET)
                header = read_project_header(rows[:14])
                if not header.accepted:
                    # Reported, not skipped in silence. The first version did
                    # `continue` here and the run printed "sheets read 48,
                    # readings written 0" with no hint that all 48 had been dropped
                    # — and the reason was a typo in the corpus, a capital `Ơ` where
                    # `ự` belongs on the project line of `TĐ BOH.xlsx`. A count that
                    # hides a refusal is the F-pattern: it looks like a result.
                    report.sheets_without_identity.append(
                        (f"{path.name}::{sheet_name}", header.refusals)
                    )
                    continue
                await _write_one_sheet(
                    conn,
                    organization_id=organization_id,
                    name=path.name,
                    sheet=sheet_name,
                    rows=rows,
                    shape=shape,
                    project_id=project_id,
                    report_ref=f"{report_ref}:{path.stem}",
                    today=today,
                    report=report,
                )
        finally:
            book.close()


async def _write_one_sheet(
    conn: AsyncConnection,
    *,
    organization_id: str,
    name: str,
    sheet: str,
    rows: list[tuple[object, ...]],
    shape,
    project_id: str,
    report_ref: str,
    today: dt.datetime,
    report: Report,
) -> None:
    if await report_exists(conn, organization_id=organization_id, report_ref=report_ref):
        report.reports_skipped.append(report_ref)
        return

    read = read_progress(rows, shape)
    # The whole `ProgressRead`, not just its readings: `write_report` is the layer that
    # splits what was read from what was skipped, and handing it a bare tuple would
    # discard that distinction at exactly the point it matters.
    outcome = await write_report(
        conn,
        organization_id=organization_id,
        read=read,
        report_ref=report_ref,
        observed_on=today.date(),
        project_id=project_id,
    )
    report.readings_written += outcome.written
    report.readings_refused += len(outcome.refused)

    structure = read_structure(rows, shape)
    wbs = await write_structure(
        conn,
        organization_id=organization_id,
        project_id=project_id,
        structure=structure,
        written_on=today,
        report_ref=report_ref,
    )
    report.nodes_written += len(wbs.written)
    for refused in wbs.refused:
        report.nodes_refused.append((refused.code, refused.refusals))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--org",
        help="organisation to ingest into; the first one when omitted",
    )
    parser.add_argument("--dry-run", action="store_true", help="read everything, write nothing")
    parser.add_argument("--report-ref", default="corpus", help="prefix for the progress report_ref")
    args = parser.parse_args(argv)

    today = dt.datetime.now(tz=dt.UTC)
    report = asyncio.run(
        run(
            args.corpus,
            dry_run=args.dry_run,
            report_ref=args.report_ref,
            today=today,
            org=args.org,
        )
    )
    print(("DRY RUN" if args.dry_run else "INGEST") + ":")
    print(report.render())
    # A report of zeros is indistinguishable from a corpus that was never pointed
    # at, so name the directory that was searched. It is the one line that turns
    # "nothing happened" into something the reader can act on.
    print(f"  corpus searched     {args.corpus} ({'found' if args.corpus.exists() else 'MISSING'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
