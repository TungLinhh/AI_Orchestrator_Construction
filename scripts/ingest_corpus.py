#!/usr/bin/env python
"""Report what ingesting the corpus would produce. Writes nothing.

The first pass over 221 workbooks must not write, and the reason is not caution for
its own sake: a first pass that writes whatever the detector finds fills the
material master with confident garbage, and that is not recoverable by deleting the
rows afterwards. The cost is not the rows — it is that nobody trusts the table
again, and every later import inherits that.

So this prints. The output is shaped around the questions a person needs answered
before trusting an import, per `ingest.reader.plan_ingest`:

    what kind of table is this, which columns were found, how many rows,
    how many are clean, what the unclean ones say, and five rows to eyeball.

**The summary is the point.** A total row count over 221 workbooks is not a finding;
"412 tables, 3,180 rows, 2,914 clean, and the 266 refusals are almost all one
column" is. The exit code is 0 whenever the corpus was read, whether or not the
result is good, because a bad result is information and a non-zero exit would make
this unusable in a pipeline.

    python scripts/ingest_corpus.py                       # every workbook found
    python scripts/ingest_corpus.py --root DIR            # somewhere else
    python scripts/ingest_corpus.py --kinds price_schedule
    python scripts/ingest_corpus.py --json                # machine-readable
    python scripts/ingest_corpus.py --limit 10            # a quick look
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_orchestrator.ingest.reader import plan_ingest
from ai_orchestrator.ingest.sheets import SheetKind, detect_sheet

#: Where the corpus lives on this machine. Overridable, because the corpus is not
#: part of this repository and a hard-coded absolute path in a script that a human
#: runs is a trap.
#: Read from `AO_CORPUS_ROOTS` (a `:`-separated list) when it is set, so a clone on
#: another machine points at its own corpus. The defaults are the paths this was
#: developed against and are almost certainly wrong for anyone else.
DEFAULT_ROOTS = tuple(
    part
    for part in os.environ.get(
        "AO_CORPUS_ROOTS",
        "/home/vutun/procurement/input:"
        "/home/vutun/pmo_project/reference_sheets:"
        "/home/vutun/pmo_project_procore/backend/uploads",
    ).split(":")
    if part
)

#: `openpyxl` warns about headers and footers it cannot parse, which is most of
#: them in this corpus and is never the thing being looked at.
warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

#: Rows read per sheet. A price schedule is tens of rows; the contract templates
#: have long prose sections above their tables. Deep enough for the tables, shallow
#: enough that opening 221 workbooks does not turn into a coffee break.
MAX_ROWS_PER_SHEET = 400


@dataclass(slots=True)
class Report:
    """What the whole pass found, accumulated.

    `refusal_columns` is a `Counter` rather than a per-file list because the useful
    question is "which column is broken across the corpus", and that is only
    answerable in aggregate. A per-file list of 266 refusals is a worse report than
    "quantity: 198, unit: 51, unit_rate: 17".
    """

    workbooks: int = 0
    unreadable: int = 0
    sheets: int = 0
    tables: int = 0
    rows: int = 0
    clean_rows: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    refusal_columns: Counter[str] = field(default_factory=Counter)
    disagreements: int = 0
    no_table_sheets: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workbooks_read": self.workbooks,
            "workbooks_unreadable": self.unreadable,
            "sheets": self.sheets,
            "tables": self.tables,
            "rows": self.rows,
            "clean_rows": self.clean_rows,
            "rows_with_refusals": self.rows - self.clean_rows,
            "by_kind": dict(self.by_kind.most_common()),
            "refusal_columns": dict(self.refusal_columns.most_common()),
            "amounts_disagreeing": self.disagreements,
            "sheets_with_no_table": self.no_table_sheets,
        }


def _workbooks(roots: list[str]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        base = Path(root)
        if base.is_dir():
            found.extend(sorted(base.rglob("*.xlsx")))
    return found


def scan(paths: list[Path], kinds: set[str] | None, limit: int | None) -> Report:
    """Read every workbook and accumulate. No database is touched."""
    import openpyxl

    report = Report()
    for path in paths[:limit] if limit else paths:
        try:
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except Exception:  # unreadable workbooks are expected: .xls, or protected
            report.unreadable += 1
            continue
        report.workbooks += 1
        try:
            for sheet in book.worksheets:
                report.sheets += 1
                rows = [
                    tuple(r)
                    for r in sheet.iter_rows(
                        max_row=MAX_ROWS_PER_SHEET, max_col=24, values_only=True
                    )
                ]
                detection = detect_sheet(list(rows))
                if not detection.shapes:
                    report.no_table_sheets += 1
                    continue
                for shape in detection.shapes:
                    if kinds and str(shape.kind) not in kinds:
                        continue
                    plan = plan_ingest(rows, shape)
                    report.tables += 1
                    report.rows += int(plan["rows"])
                    report.clean_rows += int(plan["clean_rows"])
                    report.by_kind[str(shape.kind)] += 1
                    report.disagreements += int(plan["amounts_disagreeing_with_qty_x_rate"])
                    for column, n in dict(plan["refusals_by_column"]).items():
                        report.refusal_columns[column] += int(n)
                    if len(report.examples) < 25:
                        report.examples.append(
                            {
                                "workbook": path.name,
                                "sheet": sheet.title,
                                **plan,
                            }
                        )
        finally:
            book.close()
    return report


def _print_human(report: Report, examples: bool) -> None:
    d = report.as_dict()
    print("Ingest dry run — nothing was written.\n")
    print(f"  workbooks read        {d['workbooks_read']}")
    if d["workbooks_unreadable"]:
        print(f"  workbooks unreadable  {d['workbooks_unreadable']}  (.xls, or protected)")
    print(f"  sheets                {d['sheets']}")
    print(f"  sheets with no table  {d['sheets_with_no_table']}")
    print(f"  tables found          {d['tables']}")
    print(f"  rows                  {d['rows']}")
    print(f"  clean rows            {d['clean_rows']}")
    print(f"  rows with refusals    {d['rows_with_refusals']}")
    print(f"  totals disagreeing    {d['amounts_disagreeing']}  (qty x rate != amount)")

    if report.by_kind:
        print("\n  by kind")
        for kind, n in report.by_kind.most_common():
            print(f"    {n:5d}  {kind}")

    if report.refusal_columns:
        print("\n  refusals by column  <- this is the list to act on")
        for column, n in report.refusal_columns.most_common():
            print(f"    {n:5d}  {column}")

    if examples:
        print("\n  sample tables")
        for ex in report.examples[:5]:
            print(f"\n    {ex['workbook']} :: {ex['sheet']}")
            print(f"      {ex['kind']} at row {ex['header_row']}, {ex['rows']} rows")
            print(f"      columns: {ex['columns']}")
            for sample in ex["sample"][:3]:
                flag = "" if not sample["refusals"] else f"  <- {sample['refusals']}"
                print(
                    f"        {sample['line_no']:>3}  {sample['name'][:40]:40} "
                    f"qty={sample['quantity']} {sample['unit'] or '?'} "
                    f"rate={sample['unit_rate']} amount={sample['amount']}{flag}"
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", help="corpus root; repeatable")
    parser.add_argument(
        "--kinds",
        help="comma-separated sheet kinds to include, e.g. price_schedule",
    )
    parser.add_argument("--limit", type=int, help="stop after N workbooks")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-examples", action="store_true", help="summary only")
    args = parser.parse_args(argv)

    roots = args.root or list(DEFAULT_ROOTS)
    paths = _workbooks(roots)
    if not paths:
        print(f"no .xlsx found under {roots}", file=sys.stderr)
        return 1

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()} if args.kinds else None
    if kinds:
        known = {str(k) for k in SheetKind}
        unknown = kinds - known
        if unknown:
            print(
                f"unknown kind(s) {sorted(unknown)}; known: {sorted(known)}",
                file=sys.stderr,
            )
            return 1

    report = scan(paths, kinds, args.limit)
    if args.json:
        print(json.dumps({**report.as_dict(), "examples": report.examples}, indent=2))
    else:
        _print_human(report, examples=not args.no_examples)
    # Always 0. A poor result is the output, not a failure of the tool, and a
    # non-zero exit here would make this unusable as the first step of a pipeline.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
