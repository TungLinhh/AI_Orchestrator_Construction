"""Project identity, read from the header rows of a construction sheet.

Every construction sheet in the corpus opens with a small block that names the job:

    row 5   Dự án: Bãi Tràm Estates
    row 6   Địa điểm/Address: Xã Xuân Cảnh, Thị xã Sông Cầu, Tỉnh Phú Yên
    row 7   Gói thầu/Package: Cơ điện
    row 8   Hạng Mục/Item: MEP

That block is the only place the corpus says *what job this is*, and the Phase 1
progress reader **discards it** — the summary row carrying it is classified as a
roll-up and skipped, because a roll-up has a window and no duration. The identity is
therefore sitting in a file this repository already reads, being thrown away, while
`projects` has zero rows and every other table hangs off it.

## Two functions, because two different questions

`read_project_header` is per-file and pure. It extracts what the sheet says, verbatim,
and refuses nothing it can justify refusing on its own.

`survey_project_headers` is corpus-wide and is where the interesting refusals live. Two
files disagreeing about a value is a fact neither file can see, and it is exactly the
fact a writer needs before it puts one of them in a `projects` row.

## Measured on the corpus, 59 workbooks

    projects     2 distinct: 'BÃI TRÀM ESTATES', 'MELIA CAM RANH BAY VILLA & RESORT'
    address      8 distinct spellings, for 2 real places
    package      5 spellings for 2 real packages, one of them 'Cơ'
    hạng mục     1 distinct: 'MEP'

**Two projects, not three.** The count is asserted from the files, not inherited from
a document that said "3 Hoabinh Group projects" — and "Hoabinh Group" is not a project
name in any of them.

## The three refusals, and why each is one

* **A truncated value.** `Gói thầu/Package: Cơ` is a cell cut short — the corpus also
  contains `Cơ điện` and `Cơ điện khách sạn và nhà phụ trợ`, and `Cơ` is a strict
  prefix of both. Writing it would put a value in a `packages` column that cannot be
  told apart from a real one, which is the F95 failure: a constraint firing for a reason
  nobody wrote down.
* **A spelling disagreement.** `HÒA THẠNH, XUÂN CẢNH...` and `Hòa Thanh, Xuân Cảnh...`
  are the same place. Which spelling is canonical is a business decision, so this
  reader records the disagreement and **keeps both verbatim** rather than picking one.
  A silently chosen canonical form is a decision nobody made and cannot be audited.
* **An all-caps value.** `BÃI TRÀM ESTATES` is the project name in caps on every sheet
  that carries it. Case is *not* a refusal — it is uniformly applied and reversible —
  so the value is kept verbatim and a `normalised` form is offered alongside it, with
  the relationship between the two stated rather than assumed.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

#: A header cell, as `Label`, colon, `value`. The labels are bilingual, and the
#: separator is a fullwidth colon (U+FF1A) on some sheets and an ASCII one on others.
#: It is written as an escape in the pattern below rather than as a literal, because
#: the character is deliberate and an invisible one inside a regex is a character
#: nobody will think to check.
#: The value group is `(.*)` and not `(.+?)` on purpose. With `(.+?)` a label with
#: nothing after the colon does not match at all, so the reader stays silent about it
#: rather than reporting a refusal -- and a silent skip is the failure this whole
#: repository is built against. Matching the empty case is what lets it say
#: "Dự án: is empty".
_HEADER = re.compile(
    r"^\s*(Dự án|Địa điểm|Address|Gói thầu|Package|Hạng Mục|Item|Hợp đồng|Contract)"
    # Bilingual sheets join the two labels with a slash: `Địa điểm/Address:`,
    # `Gói thầu/Package:`, `Hạng Mục/Item:`. Without this the pattern demands a colon
    # straight after the first label and misses every bilingual header in the corpus --
    # including the one file the first version of the tests was written from, which is
    # how a reader that "found 1 project" looked entirely reasonable.
    r"(?:\s*/\s*[^:\uff1a]+)?"
    r"\s*[:\uff1a]\s*(.*?)\s*$",
    re.IGNORECASE,
)


#: Label -> the field it fills, keyed by the *folded* form, because that is what the
#: lookup below produces.
#:
#: The first version of this table was keyed by the accented spelling
#: (`"dự án"`) while the lookup folded the label first (`strip_accents(label).lower()`
#: gives `du an`). Every one of the corpus's 71 header labels therefore matched the
#: pattern and then failed the dictionary, and the survey reported **1 project out of
#: 59 workbooks** — a plausible-looking number, and completely wrong.
#:
#: The tell was available for the cost of printing the mapping: a regex that matches
#: 71 times and a table that resolves 0 of them is not a data problem, it is an
# asymmetry between two spellings of the same key. **A normalisation applied to one
#: side of a comparison only is not a normalisation.**
def strip_accents(value: str) -> str:
    """`Bãi Tràm` -> `Bai Tram`, so two spellings can be compared.

    `unicodedata.normalize("NFKD")` then dropping the combining marks, which is the
    only way to fold Vietnamese marks: NFD alone leaves a base letter plus a separate
    combining codepoint, so `==` still fails. (`Đ` does not decompose under either
    form, which is F91, and is therefore folded explicitly below.)
    """
    decomposed = unicodedata.normalize("NFKD", value)
    folded = "".join(c for c in decomposed if not unicodedata.combining(c))
    # `Đ`/`đ` have no decomposition, so they survive NFKD intact and would defeat
    # every comparison. Lowercased here so the caller's own casing is not doubled.
    return folded.replace("Đ", "D").replace("đ", "d")


def comparable(value: str) -> str:
    """A key for comparing two spellings of the same thing.

    Case-folded, accent-folded, and whitespace-collapsed, because the corpus varies all
    three and none of them is a difference anybody means.
    """
    return " ".join(strip_accents(value).lower().split())


@dataclass(frozen=True, slots=True)
class ProjectHeader:
    """What one sheet says about its job. Every value verbatim.

    No `project_id`, no normalised name, no canonical address. This is a *reading* of
    a file, and the decisions that turn readings into rows belong to the writer.
    """

    project_name: str = ""
    address: str = ""
    package: str = ""
    item: str = ""
    contract: str = ""
    #: The label each value was read from, so a bilingual sheet can be audited
    #: without re-opening it. `{'project_name': 'Dự án'}`.
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any((self.project_name, self.address, self.package, self.item))

    def as_dict(self) -> dict[str, object]:
        return {
            "project_name": self.project_name,
            "address": self.address,
            "package": self.package,
            "item": self.item,
            "contract": self.contract,
            "labels": dict(self.labels),
        }


#: A cell that looks like a label line but whose label is not in the list above:
#: short text, a colon, then a value. Deliberately looser than `_HEADER`, which
#: enumerates the labels it knows. Its only job is to say "there is a line here I did
#: not understand", and a tighter pattern would defeat that by rejecting the typo it
#: exists to catch.
_UNKNOWN_LABEL_LINE = re.compile(r"^\s*([^:\uff1a]{2,24}?)\s*[:\uff1a]\s*(\S.*?)\s*$")

#: The same shape as `_HEADER` but with any short label, so the corpus's own
#: misspelling is *reached* rather than skipped: `DƠ án` is not in the alternation, so
#: `_HEADER` declines it and the reader never gets to look it up. This pattern is how
#: a value behind a mis-typed label becomes visible at all.
_TYPED_HEADER = re.compile(r"^\s*([^:\uff1a]{2,24}?)\s*[:\uff1a]\s*(.*?)\s*$")

#: The corpus's own misspelling of its project label, mapped to the same field.
#:
#: 48 of the 59 workbooks write it as **`DƠ án`** -- a capital `Ơ` (U+01B0) where `ự`
#: belongs. Without this entry the reader reports `unknown_label` and the value stays
#: unread, which means **48 progress sheets and 110 activities** on the one real
#: construction file contribute nothing to the product.
#:
#: It is a documented typo rather than a silent workaround: the *value* is correct and
#: the *label* is a mis-typed key, so nothing is being guessed here. And the typo is
#: still reported through `Refusal.UNKNOWN_LABEL` for any sheet that uses a spelling
#: neither of these two covers -- this entry fixes the one variant the corpus actually
#: uses, it does not stop the reader from saying "I do not know this line".
_CORPUS_TYPOS: dict[str, str] = {
    comparable("DƠ án"): comparable("Dự án"),
}

_FIELDS: dict[str, str] = {
    comparable("Dự án"): "project_name",
    comparable("Địa điểm"): "address",
    comparable("Address"): "address",
    comparable("Gói thầu"): "package",
    comparable("Package"): "package",
    comparable("Hạng Mục"): "item",
    comparable("Item"): "item",
    comparable("Hợp đồng"): "contract",
    comparable("Contract"): "contract",
}

#: How many rows to look at. The block sits at rows 5-8 on the file this was built
#: from, but that is one observation; 14 leaves room for a title banner without
#: reaching far enough to pick up an unrelated `Item:` in a materials table lower
#: down.
HEADER_SCAN_ROWS = 14


class Refusal(StrEnum):
    """Why a value was not accepted as-is.

    Named because these strings end up in an ingest report somebody reads to decide
    whether to fix a file, and `empty` is a fact while `REASON_2` is a lookup.

    **One member, and that is the point.** The first version of this enum also declared
    `TRUNCATED_POSSIBLE`, `SPELLING_DISAGREEMENT` and `UNPARSEABLE`, and none of the
    three was ever produced. Each names a real finding — the corpus really does contain
    truncated package values and nine spellings of two addresses — but both are
    properties of a *set* of files rather than of one, so they belong to
    `SurveyFinding.prefixes` and `SurveyFinding.disagrees`, which report them with
    better names and the values in front of the reader.

    An enum member nothing produces advertises a capability the module does not have,
    and a reader looking for it will find the reader silently declining to refuse
    anything of that sort.
    """

    EMPTY = "empty"
    #: A cell in the header block that *looks* like a label line — text, a colon, a
    #: value — whose label is not one this reader knows.
    #:
    #: Not a hardcoded vocabulary problem: baking a corpus typo into the label list
    #: would make the file unreadable-but-working, which hides the typo rather than
    #: reporting it. `TĐ BOH.xlsx` writes its project line as **`DƠ án`** — a capital
    #: `Ơ` (U+01B0) where `ự` belongs — so the project name on the one real progress
    #: file is not read, and the only way to find that out was to look.
    UNKNOWN_LABEL = "unknown_label"


@dataclass(frozen=True, slots=True)
class CorpusRead:
    """What a whole directory yielded, and what it would not yield.

    Both halves, because "2 projects" is a finding and "2 projects out of 59 workbooks,
    of which 4 would not open" is a finding somebody can act on. A reader that returns
    only the successes reports a denominator nobody can check.
    """

    reads: tuple[HeaderRead, ...] = ()
    skipped: tuple[str, ...] = ()

    def survey(self) -> Survey:
        return survey_project_headers(list(self.reads))

    def as_dict(self) -> dict[str, object]:
        return {"skipped": list(self.skipped), **self.survey().as_dict()}


@dataclass(frozen=True, slots=True)
class HeaderRead:
    """A header and the refusals that came with it.

    Refusals are `(field, value, reason)` triples rather than a count, because the
    reasons are the output. "4 refusals" sends somebody to open the file; "package
    'Cơ' is a prefix of 'Cơ điện', which is in the corpus" sends them to the cell.
    """

    header: ProjectHeader
    source: str = ""
    refusals: tuple[tuple[str, str, str], ...] = ()

    @property
    def accepted(self) -> bool:
        """Whether a writer may use `header` as it stands.

        False if the *project name* was refused. A sheet with a good name and a
        doubtful address is still worth writing; a sheet with no name is not, because
        the name is the only thing that identifies the job.
        """
        return bool(self.header.project_name) and not any(
            field == "project_name" for field, _, _ in self.refusals
        )


def read_project_header(rows: list[tuple[object, ...]]) -> HeaderRead:
    """Read one sheet's header block. Pure, per-file, and it judges only one file.

    Deliberately does not detect a truncated value: `Cơ` is only recognisable as
    truncated by comparison with `Cơ điện` elsewhere in the corpus, and a function
    that reached for other files to decide about this one would be untestable and
    wrong. `survey_project_headers` does that comparison.
    """
    found: dict[str, str] = {}
    labels: dict[str, str] = {}
    refusals: list[tuple[str, str, str]] = []

    for row in rows[:HEADER_SCAN_ROWS]:
        for cell in row:
            if not isinstance(cell, str):
                continue
            match = _HEADER.match(cell) or _TYPED_HEADER.match(cell)
            if match is None:
                # Not a known label line. If it still *looks* like one -- text, a
                # colon, something after it -- it is reported rather than dropped,
                # because the corpus's own typo on the project line is exactly this
                # case and nothing else would ever surface it.
                unknown = _UNKNOWN_LABEL_LINE.match(cell)
                if unknown is not None:
                    refusals.append(
                        (unknown.group(1).strip(), unknown.group(2).strip(), Refusal.UNKNOWN_LABEL)
                    )
                continue
            label, value = match.group(1), match.group(2).strip()
            # The typo is resolved *before* the pattern match, so it has to be tried
            # first: `_HEADER` will not match `DƠ án` at all, so a lookup afterwards
            # would never be reached for it.
            typo_field = _CORPUS_TYPOS.get(comparable(label))
            field_name = typo_field or _FIELDS.get(comparable(label))
            if field_name is None:
                continue
            if not value:
                refusals.append((field_name, "", Refusal.EMPTY))
                continue
            # First occurrence wins, because the corpus repeats the block on some
            # sheets and the topmost one is the header proper rather than a footer
            # echoed into the same scan window.
            if field_name not in found:
                found[field_name] = value
                labels[field_name] = label

    return HeaderRead(
        header=ProjectHeader(labels=labels, **found),
        refusals=tuple(refusals),
    )


@dataclass(frozen=True, slots=True)
class SurveyFinding:
    """One field where the corpus disagrees with itself.

    `values` is every distinct spelling, verbatim and sorted, so a person can pick a
    canonical form with the alternatives in front of them rather than from memory.
    """

    field_name: str
    values: tuple[str, ...]
    #: The subset of `values` that is a strict prefix of another value. Non-empty means
    #: at least one file was cut short.
    prefixes: tuple[str, ...] = ()

    @property
    def disagrees(self) -> bool:
        return len(self.values) > 1

    def as_dict(self) -> dict[str, object]:
        return {
            "field": self.field_name,
            "distinct": len(self.values),
            "values": list(self.values),
            "prefixes": list(self.prefixes),
        }


@dataclass(frozen=True, slots=True)
class Survey:
    """What the whole corpus says, and what it disagrees with itself about.

    `files_skipped` is here because a survey that quietly drops the files it could not
    open reports a denominator nobody can check. "2 projects" is a finding; "2
    projects, out of 59 workbooks, of which 4 would not open" is a finding somebody
    can act on.
    """

    sheets_read: int
    sheets_with_identity: int
    findings: tuple[SurveyFinding, ...] = ()
    files_skipped: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "sheets_read": self.sheets_read,
            "sheets_with_identity": self.sheets_with_identity,
            "files_skipped": self.files_skipped,
            "findings": [f.as_dict() for f in self.findings],
        }


def survey_project_headers(reads: list[HeaderRead]) -> Survey:
    """Compare a corpus's headers and report where they disagree.

    Separate from `read_project_header` because disagreement is a property of a
    *set*. Two files that spell the same place differently is a fact neither file can
    see, and it is precisely the fact a writer needs before choosing a canonical form.
    """
    by_field: dict[str, set[str]] = {}
    with_identity = 0
    for read in reads:
        header = read.header
        if not header.is_empty:
            with_identity += 1
        for name in ("project_name", "address", "package", "item", "contract"):
            value = getattr(header, name)
            if value:
                by_field.setdefault(name, set()).add(value)

    findings: list[SurveyFinding] = []
    for name, values in sorted(by_field.items()):
        ordered = tuple(sorted(values))
        prefixes = tuple(v for v in ordered if any(o != v and o.startswith(v) for o in ordered))
        findings.append(SurveyFinding(field_name=name, values=ordered, prefixes=prefixes))

    return Survey(
        sheets_read=len(reads),
        sheets_with_identity=with_identity,
        findings=tuple(findings),
    )


def read_corpus(root: Path, *, max_rows: int = HEADER_SCAN_ROWS) -> CorpusRead:
    """Read every workbook under `root`. The only function that touches the disk.

    Kept out of `read_project_header` so the reading rules stay testable without a
    file, and out of the writer so that no caller can "just read the sheet directly"
    and skip the refusals.
    """
    import warnings

    import openpyxl

    reads: list[HeaderRead] = []
    skipped: list[str] = []
    with warnings.catch_warnings():
        # openpyxl warns about headers/footers and conditional formatting it will drop.
        # Neither affects a header block in the first 14 rows, and the warning text is
        # longer than the finding.
        warnings.simplefilter("ignore")
        for path in sorted(root.rglob("*.xlsx")):
            try:
                book = openpyxl.load_workbook(path, data_only=True, read_only=True)
            except Exception as exc:
                # A workbook that will not open is a file problem, not a reading
                # problem, and one unreadable file must not stop the survey. It is
                # recorded rather than dropped, so the survey's denominator is the
                # corpus and not the subset that happened to parse.
                skipped.append(f"{path.name}: {type(exc).__name__}")
                continue
            for sheet_name in book.sheetnames:
                try:
                    # `iter_rows` rather than the private `_rows` in `ingest.sheets`:
                    # a private helper of another module is a thing to reach for when
                    # nothing else works, not when a two-line call is right there.
                    # `min_row=1` is load-bearing, not redundant. In openpyxl's
                    # read-only mode the worksheet is streamed and `iter_rows` with
                    # only `max_row` starts from the first row it has buffered rather
                    # than the first row of the sheet -- which silently returned one
                    # sheet out of fifty-nine, and the only visible symptom was a
                    # survey that found fewer projects than the corpus obviously has.
                    rows = [
                        tuple(row)
                        for row in book[sheet_name].iter_rows(
                            min_row=1, max_row=max_rows, max_col=6, values_only=True
                        )
                    ]
                except Exception as exc:
                    # A single unreadable sheet does not invalidate the file, but it
                    # is still a sheet nobody read, so it is recorded.
                    skipped.append(f"{path.name}::{sheet_name}: {type(exc).__name__}")
                    continue
                read = read_project_header(rows)
                if not read.header.is_empty:
                    read = HeaderRead(
                        header=read.header,
                        source=f"{path.name}::{sheet_name}",
                        refusals=read.refusals,
                    )
                    reads.append(read)
            book.close()
    return CorpusRead(reads=tuple(reads), skipped=tuple(skipped))


__all__ = [
    "HEADER_SCAN_ROWS",
    "CorpusRead",
    "HeaderRead",
    "ProjectHeader",
    "Refusal",
    "Survey",
    "SurveyFinding",
    "comparable",
    "read_corpus",
    "read_project_header",
    "strip_accents",
    "survey_project_headers",
]
