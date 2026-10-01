"""The document code: `ONX-[KHỐI]-[BỘ PHẬN]-[LOẠI]-[SỐ]`, parsed rather than concatenated.

## Why a value object and not a string

Every one of the dossier's twenty-eight codes has **five** hyphen-separated segments, and
the first two are already stored as columns on `sop_definitions` -- `block` and `department` --
alongside the flat `code`. So the information exists twice, and nothing checked that they
agree.

The failure this prevents is concrete. Code that is *built* by concatenation:

```python
code = f"ONX-{block}-{dept}-{kind}-{n:03d}"     # anywhere: a script, a seed, an API call
```

spreads the grammar. A `kind` that grows a character, a `dept` that arrives lowercase, a
department renamed to a four-letter code and a two-character one colliding -- each of those
is a *different* document with the same-looking prefix, and the only symptom is that a
`LIKE 'ONX-BO-%'` search returns the wrong thing. The rules end up in nine places and
agree in none of them.

So the grammar lives here, once, and **the only way to produce a code is
`DocumentCode.make(...)`.** Nothing in this repository concatenates one.

## The grammar, measured

Split on `-`, the twenty-eight seeded codes give:

| # | segment | values seen | notes |
|---|---|---|---|
| 1 | project | `ONX` | fixed; the dossier's own prefix, not a company name |
| 2 | khối (block) | `BO`, `FO`, `MO`, `PMO` | 4 of 4 seen |
| 3 | bộ phận (department) | 18 distinct | 2-3 letters |
| 4 | loại (kind) | `SOP` | one value in the seed, but the scheme is `loại` |
| 5 | số (number) | `001`…`010` | three digits, zero-padded |

The block and kind vocabularies are **closed** and are checked; the department is **open**,
because eighteen codes cannot establish a vocabulary and a closed list invented from them
would refuse the nineteenth department the day it arrives. An open segment with a length
rule is the honest shape: `two to three uppercase letters`, checked, not enumerated.

## Parsing is strict, and the message says which segment

`DocumentCode.parse` raises `ValueError` naming the **position** and the **value**, because
"invalid document code" on a code a person typed is a message that costs a support ticket.
Every message here is of the form ``document code segment 3 (bộ phận) is 'x'; expected two
or three uppercase letters``.

## What this deliberately does not do

* It does not validate that a department exists. That is a *seed* question -- the 28 codes
  name eighteen departments and `sop_definitions` carries the names -- and a code that
  references a department nobody has heard of is a data problem to find in a report, not a
  parse error.
* It does not normalise case silently. `onx-bo-hr-sop-004` is refused with a message that
  says the case, because a code that parses case-insensitively is a code whose identity
  depends on the database's collation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: The dossier's project prefix. Fixed rather than configurable: a code's first segment
#: says which scheme it belongs to, and a scheme that can be renamed by an operator is a
#: scheme whose codes stop matching.
PROJECT = "ONX"

#: The segment count, measured: all twenty-eight seeded codes split into exactly five.
SEGMENTS = 5

_SEGMENT_NAMES = ("project", "khối (block)", "bộ phận (department)", "loại (kind)", "số (number)")


class Block(StrEnum):
    """`khối` -- the four business blocks the dossier is organised into.

    Closed, because the dossier enumerates them: Back Office, Front Office, Middle Office,
    and PMO. A fifth block is a change to the organisation, not a new spelling.
    """

    BO = "BO"
    FO = "FO"
    MO = "MO"
    PMO = "PMO"

    @property
    def name_vi(self) -> str:
        return _BLOCK_NAMES[self]


_BLOCK_NAMES: dict[Block, str] = {
    Block.BO: "Back Office",
    Block.FO: "Front Office",
    Block.MO: "Middle Office",
    Block.PMO: "Project Management Office",
}


class DocumentKind(StrEnum):
    """`loại` -- what the document *is*.

    One value in the seed, and that is honest: the dossier's twenty-eight documents are all
    SOPs. The vocabulary is an enum because a `loại` is a closed list in the scheme -- a
    document that is not one of these is a document from another scheme, and the code should
    say so rather than accept a fourth letter.
    """

    SOP = "SOP"

    @property
    def name_vi(self) -> str:
        return "Quy trình vận hành"


_DEPARTMENT = re.compile(r"^[A-Z]{2,3}$")
_NUMBER = re.compile(r"^\d{3}$")


@dataclass(frozen=True, slots=True, order=True)
class DocumentCode:
    """One document's identity, in five segments.

    Frozen and ordered, so a code can be a sort key and a set member without being
    compared by object identity. Two codes for the same document compare equal, which is the
    property that makes `code in seen_codes` mean what it says.
    """

    block: Block
    department: str
    kind: DocumentKind
    number: int
    project: str = PROJECT

    def __post_init__(self) -> None:
        if not _DEPARTMENT.match(self.department):
            raise ValueError(
                f"department segment {self.department!r} is not two or three uppercase letters"
            )
        if not 1 <= self.number <= 999:
            raise ValueError(f"number {self.number} is outside 001..999")

    @classmethod
    def make(
        cls,
        block: Block | str,
        department: str,
        number: int,
        kind: DocumentKind | str = DocumentKind.SOP,
        *,
        project: str = PROJECT,
    ) -> DocumentCode:
        """Build a code from its parts. **The only supported way to make one.**

        `number` is third and required, `kind` fourth and defaulted, so the common call
        reads `make(Block.BO, "HR", 4)` and a caller who reaches for the default cannot
        leave `number` unset -- which a parameter order with `kind` third would allow.
        """
        return cls(
            # Uppercased **before** the enum lookup. `Block("bo")` raises, so normalising
            # after the conversion is a normalisation that does not happen -- and the
            # docstring's promise that `make` takes a sloppy block would have been a lie
            # only the tests could catch.
            block=Block(str(block).strip().upper()),
            department=department.strip().upper(),
            kind=DocumentKind(str(kind).strip().upper()),
            number=number,
            project=project.strip().upper(),
        )

    @classmethod
    def parse(cls, value: str) -> DocumentCode:
        """Parse `ONX-BO-HR-SOP-004`, naming the segment that is wrong when it is."""
        text_value = value.strip()
        parts = text_value.split("-")
        if len(parts) != SEGMENTS:
            # The full segment names, not their first words: `bộ phận (department)` is a
            # name with spaces in it, and splitting on a space printed `[bộ]` where the
            # segment is `bộ phận`. A message that misnames a segment is worse than one
            # that is terse, because the reader trusts it.
            shape = "-".join(f"[{name}]" for name in _SEGMENT_NAMES)
            raise ValueError(
                f"document code {text_value!r} has {len(parts)} segment(s); the scheme is {shape}"
            )
        project, block, department, kind, number = parts
        for index, (name, segment) in enumerate(zip(_SEGMENT_NAMES, parts, strict=True), start=1):
            if segment != segment.upper() or not segment:
                raise ValueError(
                    f"document code {text_value!r} segment {index} ({name}) is {segment!r}; "
                    "the scheme is uppercase throughout"
                )
        if project != PROJECT:
            raise ValueError(
                f"document code {text_value!r} starts {project!r}; this scheme's project "
                f"segment is {PROJECT!r}"
            )
        if block not in set(Block):
            raise ValueError(
                f"document code {text_value!r} segment 2 (khối) is {block!r}; expected one "
                f"of {', '.join(sorted(b.value for b in Block))}"
            )
        if kind not in set(DocumentKind):
            raise ValueError(
                f"document code {text_value!r} segment 4 (loại) is {kind!r}; expected one "
                f"of {', '.join(sorted(k.value for k in DocumentKind))}"
            )
        if not _DEPARTMENT.match(department):
            raise ValueError(
                f"document code {text_value!r} segment 3 (bộ phận) is {department!r}; "
                "expected two or three uppercase letters"
            )
        if not _NUMBER.match(number):
            raise ValueError(
                f"document code {text_value!r} segment 5 (số) is {number!r}; expected three "
                "digits, zero-padded"
            )
        return cls(
            block=Block(block),
            department=department,
            kind=DocumentKind(kind),
            number=int(number),
            project=project,
        )

    def format(self) -> str:
        """The canonical string. Three digits, always -- `7` and `007` are one document."""
        return (
            f"{self.project}-{self.block.value}-{self.department}"
            f"-{self.kind.value}-{self.number:03d}"
        )

    def __str__(self) -> str:
        return self.format()

    @property
    def prefix(self) -> str:
        """Everything but the number: the part a block owns.

        `ONX-BO-HR-SOP-` -- the handle for "the HR department's SOPs", which is what a
        distribution list, a review cycle and a numbering check all need.
        """
        return f"{self.project}-{self.block.value}-{self.department}-{self.kind.value}-"

    def is_in(self, block: Block | str | None = None, department: str | None = None) -> bool:
        """Whether this code belongs to a block and/or a department.

        The case-insensitive comparison on department is deliberate and is the **only** one
        in this module: a query filter is a convenience and refusing `hr` there would make
        every caller uppercase its own input, which spreads the rule again.
        """
        if block is not None and self.block != Block(str(block).strip().upper()):
            return False
        return not (department is not None and self.department != department.strip().upper())

    def next_number(self) -> DocumentCode:
        """The next free number in this series.

        Refuses at 999 rather than wrapping to a four-digit segment, because
        `ONX-BO-HR-SOP-1000` is a code this scheme cannot parse and a document nobody can
        find. A department that reaches a thousand SOPs has a numbering problem, not a
        code problem.
        """
        if self.number >= 999:
            raise ValueError(
                f"{self.format()} is the last number in the three-digit scheme; a thousand "
                "SOPs in one department is a numbering decision, not a code"
            )
        return DocumentCode(
            block=self.block,
            department=self.department,
            kind=self.kind,
            number=self.number + 1,
            project=self.project,
        )
